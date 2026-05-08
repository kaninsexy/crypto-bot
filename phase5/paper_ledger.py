"""phase5/paper_ledger.py -- shadow-ledger paper-trading layer for Polymarket.

Polymarket has NO testnet / sandbox / paper-money mode (it runs on
Polygon mainnet; the only "test" path is to skip placing the order
and simulate the result yourself). This module is the simulation
yourself: a recommendation is "paper-traded" by appending an entry
to a ledger; resolution is detected by polling Gamma's /markets
endpoint; realized P&L is computed at resolution time.

Used to validate calibration over many resolved markets BEFORE
deploying real capital. The shadow-ledger approach is the
prediction-market analogue of the bot's existing paper-mode for
crypto trading -- no real money at risk, but identical decision
inputs and full P&L tracking.

Public API
----------
    record_recommendation(market_id, action, size_usd, entry_price,
                          p_research, market_snapshot, ledger_path=None)
        -> dict (the appended ledger entry)

    update_resolutions(ledger_path=None) -> dict[str, int]
        Polls Gamma for each open entry; updates status + realized
        P&L for newly-resolved markets. Returns counts dict.

    load_ledger(ledger_path=None) -> list[dict]
        Read all entries (open + resolved + voided).

    compute_calibration_stats(ledger_path=None) -> dict
        Brier score, hit rate, P&L summary over resolved entries.

P&L convention
--------------
For BUY_YES at entry_price p_y, size_usd s:
    if resolves YES: realized = s * (1.0 / p_y - 1.0)    (gain)
    if resolves NO:  realized = -s                       (loss)

For BUY_NO at entry_price p_n, size_usd s:
    if resolves NO:  realized = s * (1.0 / p_n - 1.0)    (gain)
    if resolves YES: realized = -s                       (loss)

For 50/50-resolved (Polymarket "voided" / equal-payout markets):
    realized = s * (0.5 / entry_price - 1.0)
    (which is positive if entry_price < 0.5 and negative otherwise)

Resolution detection
--------------------
Gamma API /markets/{id} returns:
  - `closed`: true once UMA / oracle has finalized
  - `outcomePrices`: JSON-string list, ["1.0", "0.0"] for YES,
    ["0.0", "1.0"] for NO, ["0.5", "0.5"] for 50/50 timeout cases.

Edge cases the updater handles:
  - Market voided by UMA (rare): entry remains "open" pending manual
    resolution; surfaced via a `voided_markets` count in the updater
    return value.
  - Gamma API transient failure: skip that entry this round; retry
    on next update_resolutions() call.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data.polymarket import fetch_market_by_id, walk_asks_for_buy


DEFAULT_LEDGER_PATH: Path = (
    ROOT / "backtest" / "cache" / "polymarket" / "paper_ledger.jsonl"
)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ledger(ledger_path: Optional[Path]) -> Path:
    return ledger_path if ledger_path is not None else DEFAULT_LEDGER_PATH


def load_ledger(ledger_path: Optional[Path] = None) -> list[dict]:
    """Read all ledger entries; returns [] if file is absent."""
    p = _ledger(ledger_path)
    if not p.exists():
        return []
    out: list[dict] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _save_ledger(entries: list[dict], ledger_path: Optional[Path] = None) -> None:
    """Atomic write of full ledger (tmp + os.replace)."""
    p = _ledger(ledger_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, p)


def record_recommendation(
    *,
    market_id: str,
    action: str,
    size_usd: float,
    entry_price: float,
    p_research: float,
    market_snapshot: dict,
    edge: Optional[float] = None,
    rationale: Optional[str] = None,
    research_confidence: Optional[str] = None,
    ledger_path: Optional[Path] = None,
    walk_orderbook: bool = True,
    clob_token_ids: Optional[list] = None,
) -> dict:
    """Append one paper-traded recommendation to the ledger.

    Args:
        market_id:        Polymarket market id (string).
        action:           "BUY_YES" | "BUY_NO".
        size_usd:         USD notional the sizer recommended.
        entry_price:      The price you "paid" -- yes_price for BUY_YES,
                          no_price for BUY_NO.
        p_research:       Researcher's probability estimate (in [0,1]).
        market_snapshot:  Full candidate dict from the scanner (used
                          to capture question, end_date, liquidity at
                          recommendation time -- frozen for audit).
        edge:             Optional sizer-computed edge. If None, derived
                          from p_research and entry_price.
        rationale:        Optional one-sentence sizer/researcher rationale.
        research_confidence:  Optional "low"/"medium"/"high".

    Returns:
        The appended ledger entry dict.
    """
    if action not in ("BUY_YES", "BUY_NO"):
        raise ValueError(
            f"action must be BUY_YES or BUY_NO; got {action!r}. "
            "(HOLD recommendations are not paper-traded.)"
        )
    if size_usd <= 0:
        raise ValueError(f"size_usd must be positive; got {size_usd!r}")
    if not (0.0 < entry_price < 1.0):
        raise ValueError(
            f"entry_price must be in (0, 1); got {entry_price!r}"
        )

    # Orderbook walk: take the top-of-book entry_price as the QUOTED
    # price, but compute the realistic book-walked average fill price
    # for the recommended size. The book-walk overrides entry_price
    # for P&L purposes -- a $500 order doesn't all fill at the top
    # ask in thin markets, and we want paper P&L to reflect what a
    # real order would actually pay. The original quoted entry_price
    # is preserved on the entry as `quoted_entry_price` for audit.
    quoted_entry_price = float(entry_price)
    book_walk: Optional[dict] = None
    fill_price = float(entry_price)
    if walk_orderbook and clob_token_ids:
        try:
            # action=BUY_YES -> walk YES asks (clobTokenIds[0])
            # action=BUY_NO  -> walk NO  asks (clobTokenIds[1])
            token_idx = 0 if action == "BUY_YES" else 1
            if token_idx < len(clob_token_ids):
                walk = walk_asks_for_buy(
                    clob_token_ids[token_idx], size_usd,
                )
                book_walk = walk
                if walk.get("fully_filled") and walk.get("avg_price"):
                    fill_price = float(walk["avg_price"])
        except Exception as exc:  # noqa: BLE001
            book_walk = {"error": f"{exc.__class__.__name__}: {exc}"}

    if edge is None:
        # Edge is computed against the QUOTED top-of-book price (the
        # researcher's frame of reference). Fill slippage is captured
        # separately as the difference between fill_price and
        # quoted_entry_price.
        if action == "BUY_YES":
            edge = p_research - quoted_entry_price
        else:
            edge = (1.0 - p_research) - quoted_entry_price
    slippage_pp = (fill_price - quoted_entry_price) * 100.0

    entry = {
        "ts_recorded": _utcnow(),
        "market_id": str(market_id),
        "market_question": str(market_snapshot.get("question", ""))[:200],
        "market_end_date_iso": str(market_snapshot.get("end_date_iso", "")),
        "market_liquidity_usd_at_record": float(
            market_snapshot.get("liquidity_usd", 0.0)
        ),
        "action": action,
        "size_usd": float(size_usd),
        "quoted_entry_price": quoted_entry_price,
        "entry_price": fill_price,            # book-walked realistic fill
        "slippage_pp_vs_top": slippage_pp,
        "book_walk": book_walk,
        "p_research": float(p_research),
        "edge": float(edge),
        "rationale": rationale,
        "research_confidence": research_confidence,
        # Resolution fields (filled by update_resolutions):
        "status": "open",
        "resolution_ts": None,
        "outcome_yes_price": None,  # Final outcomePrices[0]
        "outcome_no_price": None,   # Final outcomePrices[1]
        "realized_pnl_usd": None,
        "realized_outcome": None,   # 1.0 if YES, 0.0 if NO, 0.5 if 50/50
    }

    entries = load_ledger(ledger_path)
    entries.append(entry)
    _save_ledger(entries, ledger_path)
    return entry


def _compute_pnl(entry: dict, outcome: float) -> float:
    """Compute realized P&L for one paper-traded entry given the
    market's terminal outcome.

    `outcome` semantics:
      1.0  -> YES resolved (YES holders paid $1, NO holders paid $0)
      0.0  -> NO resolved (NO holders paid $1, YES holders paid $0)
      0.5  -> 50/50 timeout / void (both sides paid $0.50)
    """
    action = entry["action"]
    size = float(entry["size_usd"])
    price = float(entry["entry_price"])

    if action == "BUY_YES":
        # Pays out (1.0 / price) per dollar staked, times the YES
        # outcome share. Profit = size * (payoff_per_dollar - 1).
        payoff_per_dollar = outcome / price
        return size * (payoff_per_dollar - 1.0)
    elif action == "BUY_NO":
        # NO pays out (1.0 / price) per dollar staked, times the NO
        # outcome share = (1 - outcome) for binary, or 0.5 for 50/50.
        no_share = (1.0 - outcome) if outcome != 0.5 else 0.5
        payoff_per_dollar = no_share / price
        return size * (payoff_per_dollar - 1.0)
    else:
        raise ValueError(f"unknown action {action!r}")


def _parse_outcome_prices(outcome_prices_raw) -> Optional[tuple[float, float]]:
    """Gamma stores outcomePrices as a JSON-string list. Parse to tuple."""
    if outcome_prices_raw is None:
        return None
    if isinstance(outcome_prices_raw, list):
        prices_list = outcome_prices_raw
    else:
        try:
            prices_list = json.loads(outcome_prices_raw)
        except (json.JSONDecodeError, TypeError):
            return None
    if not isinstance(prices_list, list) or len(prices_list) < 2:
        return None
    try:
        yes_p = float(prices_list[0])
        no_p = float(prices_list[1])
    except (ValueError, TypeError):
        return None
    return (yes_p, no_p)


def update_resolutions(ledger_path: Optional[Path] = None) -> dict[str, int]:
    """Poll Gamma for each open entry; mark resolved + compute P&L.

    Returns counts dict:
      {"open": <still-open after pass>,
       "newly_resolved_yes": ..., "newly_resolved_no": ...,
       "newly_resolved_5050": ..., "fetch_errors": ...}
    """
    entries = load_ledger(ledger_path)
    counts = {
        "open_before": sum(1 for e in entries if e.get("status") == "open"),
        "open_after": 0,
        "newly_resolved_yes": 0,
        "newly_resolved_no": 0,
        "newly_resolved_5050": 0,
        "fetch_errors": 0,
        "voided_pending": 0,
    }
    if counts["open_before"] == 0:
        counts["open_after"] = 0
        return counts

    for entry in entries:
        if entry.get("status") != "open":
            continue
        try:
            market = fetch_market_by_id(entry["market_id"])
        except Exception as exc:  # noqa: BLE001
            counts["fetch_errors"] += 1
            continue

        closed = bool(market.get("closed"))
        if not closed:
            counts["open_after"] += 1
            continue

        prices = _parse_outcome_prices(market.get("outcomePrices"))
        if prices is None:
            counts["voided_pending"] += 1
            counts["open_after"] += 1
            continue

        yes_p, no_p = prices
        # Determine outcome semantics. Polymarket settlements use
        # 1.0/0.0 for clean resolutions and 0.5/0.5 for timeouts.
        if yes_p >= 0.99 and no_p <= 0.01:
            outcome = 1.0
            counts["newly_resolved_yes"] += 1
        elif no_p >= 0.99 and yes_p <= 0.01:
            outcome = 0.0
            counts["newly_resolved_no"] += 1
        elif abs(yes_p - 0.5) < 0.05 and abs(no_p - 0.5) < 0.05:
            outcome = 0.5
            counts["newly_resolved_5050"] += 1
        else:
            # Non-standard settlement (rare); record raw values and
            # leave status open for human review.
            counts["voided_pending"] += 1
            counts["open_after"] += 1
            continue

        entry["status"] = (
            "resolved_yes" if outcome == 1.0
            else "resolved_no" if outcome == 0.0
            else "resolved_5050"
        )
        entry["resolution_ts"] = _utcnow()
        entry["outcome_yes_price"] = yes_p
        entry["outcome_no_price"] = no_p
        entry["realized_outcome"] = outcome
        entry["realized_pnl_usd"] = _compute_pnl(entry, outcome)

    _save_ledger(entries, ledger_path)
    return counts


def compute_calibration_stats(ledger_path: Optional[Path] = None) -> dict:
    """Brier score, hit rate, P&L summary over resolved entries.

    Brier: mean((p_research - realized_outcome)^2) over all resolved
    binary entries. Lower is better; 0.25 is the chance-rate baseline
    (always predicting 50% on a binary outcome).

    Hit rate: fraction of actionable recommendations whose direction
    was correct (BUY_YES + outcome=YES, BUY_NO + outcome=NO). 50/50
    resolutions are excluded from hit rate (the bet half-paid out).
    """
    entries = load_ledger(ledger_path)
    resolved_binary = [
        e for e in entries
        if e.get("status") in ("resolved_yes", "resolved_no")
        and e.get("realized_outcome") is not None
        and e.get("p_research") is not None
    ]
    resolved_5050 = [
        e for e in entries if e.get("status") == "resolved_5050"
    ]
    open_count = sum(1 for e in entries if e.get("status") == "open")

    if resolved_binary:
        brier_components = [
            (float(e["p_research"]) - float(e["realized_outcome"])) ** 2
            for e in resolved_binary
        ]
        brier = sum(brier_components) / len(brier_components)
    else:
        brier = None

    if resolved_binary:
        hits = sum(
            1 for e in resolved_binary
            if (e["action"] == "BUY_YES" and e["realized_outcome"] == 1.0)
            or (e["action"] == "BUY_NO" and e["realized_outcome"] == 0.0)
        )
        hit_rate = hits / len(resolved_binary)
    else:
        hits = 0
        hit_rate = None

    total_pnl = sum(
        float(e.get("realized_pnl_usd") or 0.0)
        for e in entries
        if e.get("status") in ("resolved_yes", "resolved_no", "resolved_5050")
    )
    total_staked_resolved = sum(
        float(e.get("size_usd") or 0.0)
        for e in entries
        if e.get("status") in ("resolved_yes", "resolved_no", "resolved_5050")
    )
    roi = (total_pnl / total_staked_resolved) if total_staked_resolved else None

    return {
        "n_total": len(entries),
        "n_open": open_count,
        "n_resolved_binary": len(resolved_binary),
        "n_resolved_5050": len(resolved_5050),
        "brier_score": brier,
        "hits": hits,
        "hit_rate": hit_rate,
        "total_pnl_usd": total_pnl,
        "total_staked_resolved_usd": total_staked_resolved,
        "roi_on_resolved": roi,
    }


# -- CLI ---------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Polymarket paper-ledger CLI: record / update / stats.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub_update = sub.add_parser(
        "update", help="Poll Gamma for resolutions; update ledger."
    )

    sub_stats = sub.add_parser(
        "stats", help="Print calibration + P&L stats over the ledger."
    )

    sub_show = sub.add_parser(
        "show", help="List all ledger entries."
    )

    sub_record = sub.add_parser(
        "record",
        help="Record one recommendation manually (advanced; usually "
             "fed by phase5/recommend.py).",
    )
    sub_record.add_argument("--market-id", required=True)
    sub_record.add_argument("--action", required=True,
                            choices=("BUY_YES", "BUY_NO"))
    sub_record.add_argument("--size-usd", type=float, required=True)
    sub_record.add_argument("--entry-price", type=float, required=True)
    sub_record.add_argument("--p-research", type=float, required=True)
    sub_record.add_argument("--question", default="")
    sub_record.add_argument("--end-date-iso", default="")
    sub_record.add_argument("--liquidity-usd", type=float, default=0.0)
    sub_record.add_argument("--rationale", default=None)
    sub_record.add_argument("--research-confidence", default=None)

    args = parser.parse_args()

    if args.cmd == "update":
        counts = update_resolutions()
        print(f"Resolution update:")
        for k, v in counts.items():
            print(f"  {k}: {v}")
    elif args.cmd == "stats":
        stats = compute_calibration_stats()
        print(f"Paper-ledger stats:")
        for k, v in stats.items():
            if v is None:
                print(f"  {k}: -")
            elif isinstance(v, float):
                print(f"  {k}: {v:.4f}")
            else:
                print(f"  {k}: {v}")
    elif args.cmd == "show":
        entries = load_ledger()
        print(f"Ledger ({len(entries)} entries):")
        for e in entries:
            ts = (e.get("ts_recorded") or "")[:19]
            mid = e.get("market_id", "?")
            act = e.get("action", "?")
            size = e.get("size_usd", 0.0)
            edge = e.get("edge", 0.0)
            stat = e.get("status", "?")
            pnl = e.get("realized_pnl_usd")
            pnl_str = f"${pnl:+.2f}" if pnl is not None else "-"
            print(f"  {ts}  {mid:<8} {act:<8} ${size:>7.2f} edge={edge:+.4f}  "
                  f"status={stat:<14} pnl={pnl_str}")
            print(f"             {e.get('market_question','')[:90]}")
    elif args.cmd == "record":
        entry = record_recommendation(
            market_id=args.market_id,
            action=args.action,
            size_usd=args.size_usd,
            entry_price=args.entry_price,
            p_research=args.p_research,
            market_snapshot={
                "question": args.question,
                "end_date_iso": args.end_date_iso,
                "liquidity_usd": args.liquidity_usd,
            },
            rationale=args.rationale,
            research_confidence=args.research_confidence,
        )
        print(f"Recorded {args.action} {args.market_id} ${args.size_usd:.2f}")
