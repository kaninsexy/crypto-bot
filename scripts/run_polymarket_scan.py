"""scripts/run_polymarket_scan.py -- Polymarket scanner CLI.

Implements the scanner agent's logic (.claude/agents/scanner.md
steps 2-5) as a runnable Python script. Returns the top-N candidate
markets ranked by a composite score (liquidity-weighted), filtered
by liquidity / time-to-resolution / spread per the agent spec.

Output: JSON list to stdout (machine-readable for the orchestrator)
plus a human-readable summary to stderr.

Read-only: hits Gamma /markets via data/polymarket.py. No order
placement. No CLOB POST. The Phase 5 no-execute boundary applies.

Usage
-----
    python scripts/run_polymarket_scan.py
        # default: top 10 candidates, 200-market scan, default filters

    python scripts/run_polymarket_scan.py --top-n 5
        # only return top 5

    python scripts/run_polymarket_scan.py --min-liquidity 10000
        # raise liquidity floor to $10k

    python scripts/run_polymarket_scan.py --force-refresh
        # bypass cache; live Gamma fetch

    python scripts/run_polymarket_scan.py --output candidates.json
        # write JSON to file instead of stdout
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data.polymarket import (
    compute_time_to_resolution_hours,
    filter_scanner_candidates,
    load_or_fetch_markets,
)


def _normalise(series: pd.Series) -> pd.Series:
    """Min-max normalise to [0, 1]; constant series -> 0.5 (neutral)."""
    s = series.astype(float).fillna(0.0)
    smin = float(s.min())
    smax = float(s.max())
    if smax - smin <= 0:
        return pd.Series([0.5] * len(s), index=s.index)
    return (s - smin) / (smax - smin)


def score_candidates(
    df: pd.DataFrame,
    *,
    w_liquidity: float = 0.5,
    w_volume_24h: float = 0.3,
    w_inverse_spread: float = 0.2,
) -> pd.DataFrame:
    """Compute the scanner agent's composite score (step 4).

        score = w_liq * norm(liquidity)
              + w_vol * norm(volume_24h)
              + w_inv_spread * norm(1 / spread)

    All three components are min-max normalised across the candidate
    set before weighting so units don't dominate. Returns the input
    DataFrame with `_score` and component-wise normalised columns
    appended.
    """
    if df.empty:
        return df.copy()

    work = df.copy()
    for col in ("liquidityNum", "volume24hr", "spread"):
        if col in work.columns:
            work[col] = pd.to_numeric(work[col], errors="coerce")

    # 1 / spread: spread is a price unit. Use price-units inverse,
    # then normalise. Avoid div-by-zero via small epsilon.
    eps = 1e-6
    work["_inv_spread"] = 1.0 / (work["spread"].fillna(1.0) + eps)

    n_liq = _normalise(work["liquidityNum"])
    n_vol = _normalise(work["volume24hr"])
    n_inv_spread = _normalise(work["_inv_spread"])

    work["_score"] = (
        w_liquidity * n_liq
        + w_volume_24h * n_vol
        + w_inverse_spread * n_inv_spread
    )
    work["_norm_liquidity"] = n_liq
    work["_norm_volume_24h"] = n_vol
    work["_norm_inv_spread"] = n_inv_spread
    return work.drop(columns=["_inv_spread"])


def candidate_to_scanner_dict(row: pd.Series) -> dict:
    """Project a market row to the scanner agent's output schema.

    Mirrors `.claude/agents/scanner.md` step 5 exactly.
    """
    spread = float(row.get("spread") or 0.0)
    yes_price = float(row.get("bestAsk") or 0.0)  # buy-yes cost
    no_price = max(0.0, 1.0 - float(row.get("bestBid") or 0.0))  # buy-no cost
    return {
        "market_id": str(row.name),  # row.name == DataFrame index value
        "question": str(row.get("question") or "")[:200],
        "slug": str(row.get("slug") or ""),
        "yes_price": yes_price,
        "no_price": no_price,
        "best_bid": float(row.get("bestBid") or 0.0),
        "best_ask": float(row.get("bestAsk") or 0.0),
        "liquidity_usd": float(row.get("liquidityNum") or 0.0),
        "volume_24h_usd": float(row.get("volume24hr") or 0.0),
        "spread_pp": spread * 100.0,
        "time_to_resolution_h": float(row.get("_ttr_hours") or 0.0),
        "end_date_iso": str(row.get("endDateIso") or ""),
        "outcomes": str(row.get("outcomes") or ""),
        "clob_token_ids": str(row.get("clobTokenIds") or ""),
        "score": float(row.get("_score") or 0.0),
    }


def run_scan(
    *,
    top_n: int = 10,
    market_pool_size: int = 200,
    min_liquidity_usd: float = 5_000.0,
    min_time_to_resolution_h: float = 24.0,
    max_spread_pp: float = 5.0,
    min_yes_price: float = 0.0,
    max_yes_price: float = 1.0,
    force_refresh: bool = False,
) -> list[dict]:
    """End-to-end scan: fetch -> filter -> score -> top-N projection.

    Returns a list of `top_n` candidate dicts in the scanner agent's
    output schema (see `candidate_to_scanner_dict`). Empty list if no
    market clears the filters.
    """
    # 1. Fetch markets pool.
    df = load_or_fetch_markets(
        active=True, closed=False,
        limit=market_pool_size,
        force_refresh=force_refresh,
    )

    # 2. Filter per scanner agent step 3.
    candidates = filter_scanner_candidates(
        df,
        min_liquidity_usd=min_liquidity_usd,
        min_time_to_resolution_h=min_time_to_resolution_h,
        max_spread_pp=max_spread_pp,
        min_yes_price=min_yes_price,
        max_yes_price=max_yes_price,
    )
    if candidates.empty:
        return []

    # Compute time-to-resolution again (filter dropped the helper col)
    # so the candidate dict can include it.
    candidates = candidates.copy()
    candidates["_ttr_hours"] = compute_time_to_resolution_hours(candidates)

    # 3. Score per scanner agent step 4.
    scored = score_candidates(candidates)

    # 4. Top-N.
    top = scored.nlargest(top_n, "_score")
    return [candidate_to_scanner_dict(row) for _, row in top.iterrows()]


# -- CLI ---------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Polymarket scanner: top-N markets by liquidity-"
                    "weighted score. Read-only.",
    )
    parser.add_argument("--top-n", type=int, default=10)
    parser.add_argument("--market-pool-size", type=int, default=200)
    parser.add_argument("--min-liquidity", type=float, default=5_000.0)
    parser.add_argument(
        "--min-time-to-resolution-h", type=float, default=24.0,
    )
    parser.add_argument("--max-spread-pp", type=float, default=5.0)
    parser.add_argument(
        "--min-yes-price", type=float, default=0.0,
        help="Min bestAsk (YES price) to keep. Tighten to ~0.20 to "
             "exclude long-odds markets where research can't meaningfully "
             "shift implied probability past the 2pp edge threshold.",
    )
    parser.add_argument(
        "--max-yes-price", type=float, default=1.0,
        help="Max bestAsk (YES price) to keep. Tighten to ~0.80 to "
             "exclude near-certain markets symmetric to min-yes-price.",
    )
    parser.add_argument(
        "--force-refresh", action="store_true",
        help="Bypass cache; live Gamma fetch.",
    )
    parser.add_argument(
        "--output", type=str, default="-",
        help="Output JSON file path; '-' (default) -> stdout.",
    )
    args = parser.parse_args()

    candidates = run_scan(
        top_n=args.top_n,
        market_pool_size=args.market_pool_size,
        min_liquidity_usd=args.min_liquidity,
        min_time_to_resolution_h=args.min_time_to_resolution_h,
        max_spread_pp=args.max_spread_pp,
        min_yes_price=args.min_yes_price,
        max_yes_price=args.max_yes_price,
        force_refresh=args.force_refresh,
    )

    payload = json.dumps(candidates, indent=2, ensure_ascii=False)

    if args.output == "-":
        print(payload)
    else:
        Path(args.output).write_text(payload, encoding="utf-8")
        print(f"wrote {len(candidates)} candidates to {args.output}",
              file=sys.stderr)

    # Human-readable summary to stderr (out-of-band so JSON-on-stdout
    # consumers see only the JSON).
    print(file=sys.stderr)
    print(
        f"[scan] {len(candidates)} candidate(s); "
        f"market_pool_size={args.market_pool_size}; "
        f"filters: min_liq=${args.min_liquidity:.0f}, "
        f"min_ttr={args.min_time_to_resolution_h}h, "
        f"max_spread={args.max_spread_pp}pp",
        file=sys.stderr,
    )
    for i, c in enumerate(candidates):
        print(
            f"  #{i+1} {c['market_id']:<8} "
            f"yes={c['yes_price']:.3f} no={c['no_price']:.3f} "
            f"liq=${c['liquidity_usd']:>9,.0f} "
            f"vol24={c['volume_24h_usd']:>9,.0f} "
            f"spd={c['spread_pp']:.2f}pp "
            f"ttr={c['time_to_resolution_h']:.0f}h "
            f"score={c['score']:.3f}",
            file=sys.stderr,
        )
        print(f"      Q: {c['question']}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
