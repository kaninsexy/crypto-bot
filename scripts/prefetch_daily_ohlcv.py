"""
scripts/prefetch_daily_ohlcv.py — populate backtest/cache/ohlcv/ with
spot OHLCV candles for any strategy + timeframe combination.

Originally written for the Phase 4.A TrendFollowing_multi 1d basket
(hence the filename); now generalised so the same script populates
every strategy's cache without per-strategy clones.

CLI:
  python scripts/prefetch_daily_ohlcv.py
      [--strategy STRATEGY_ID]
      [--timeframe TIMEFRAME]
      [--symbols SYMBOL1,SYMBOL2,...]
      [--months N]

  Default behaviour (no flags) is unchanged: prefetch
  TrendFollowing_multi at 1d using the manifest's symbols list and
  the months-back-to-data-start calculation.

Reads the symbols list from the manifest entry at runtime so the
basket can grow or shrink without touching code. Does not append to
trials.log, edit any sacred-harness file, or call the holdout
window — every cache write is gated by
`until_ts=get_symbol_dev_cutoff(symbol)` so the `HoldoutBypass`
enforcement in `backtest.cache._apply_until_and_enforce` catches any
accidental holdout-window leak.

Smoke (`scripts/phase_4a_trendfollowing_smoke.py`) and any
cpcv_multi run reads cached OHLCV from
`backtest/cache/ohlcv/{SYMBOL}_{TIMEFRAME}_{N}mo.parquet` via
`holdout.load_dev → backtest.cache.load_or_download_ohlcv`.
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest.cache import get_symbol_dev_cutoff, load_or_download_ohlcv
from backtest.holdout import load_manifest
from backtest.runner import download_history


DEFAULT_STRATEGY_ID = "TrendFollowing_multi"
DEFAULT_TIMEFRAME = "1d"
CACHE_DIR = ROOT / "backtest" / "cache" / "ohlcv"


def _months_needed_from_data_start(data_start: pd.Timestamp) -> int:
    """months-back math: same shape as scripts/phase_4b_full_cpcv_v1.py
    (chat 2026-05-02 substrate-coverage fix) — the OKX exchange
    archive walks back from "now", not from data_start, so the
    months parameter must cover (now - data_start) in calendar
    days, not the dev_span.
    """
    now_utc = pd.Timestamp.now(tz="UTC")
    months_back_days = (now_utc - data_start).days
    return int(math.ceil(months_back_days / 30.44)) + 1


def _resolve_entry_symbols(entry: dict) -> list[str]:
    """Pull the list of spot symbols out of a manifest entry.

    Manifest entries carry exactly one of {symbol, symbols, legs}.
    For 'legs' (Phase 4.B perp+spot), only the spot leg is returned —
    perp prefetch goes through a different download path.
    """
    if "symbols" in entry:
        return list(entry["symbols"])
    if "symbol" in entry:
        return [entry["symbol"]]
    if "legs" in entry and isinstance(entry["legs"], dict):
        spot = entry["legs"].get("spot")
        return [spot] if spot else []
    return []


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Prefetch spot OHLCV for any strategy + timeframe combination."
        ),
    )
    parser.add_argument(
        "--strategy",
        default=DEFAULT_STRATEGY_ID,
        help=(
            "strategy_id whose manifest entry is the source for "
            "symbols + timeframe + data_start. "
            f"Default: {DEFAULT_STRATEGY_ID}."
        ),
    )
    parser.add_argument(
        "--timeframe",
        default=None,
        help=(
            "Override the manifest entry's timeframe (e.g. '1h', '4h', "
            "'1d'). When absent, uses the manifest value (or the "
            f"hardcoded default '{DEFAULT_TIMEFRAME}' for back-compat "
            "with TrendFollowing_multi)."
        ),
    )
    parser.add_argument(
        "--symbols",
        default=None,
        help=(
            "Comma-separated symbol list, e.g. 'BTC/USDT,ETH/USDT'. "
            "When absent, uses the manifest entry's symbols / symbol / "
            "legs.spot."
        ),
    )
    parser.add_argument(
        "--months",
        type=int,
        default=None,
        help=(
            "Override the months argument passed to the cache loader. "
            "When absent, derived from the manifest entry's data_start "
            "(months-back-to-now math)."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()

    manifest = load_manifest()
    if args.strategy not in manifest:
        print(
            f"FAIL: '{args.strategy}' not in manifest.",
            file=sys.stderr,
        )
        return 1
    entry = manifest[args.strategy]

    if args.symbols is not None:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
        if not symbols:
            print(
                "FAIL: --symbols was provided but parsed to an empty list.",
                file=sys.stderr,
            )
            return 1
    else:
        symbols = _resolve_entry_symbols(entry)
        if not symbols:
            print(
                f"FAIL: manifest entry '{args.strategy}' has no symbol / "
                f"symbols / legs.spot field, and no --symbols override.",
                file=sys.stderr,
            )
            return 1

    timeframe = args.timeframe or entry.get("timeframe", DEFAULT_TIMEFRAME)

    data_start = pd.Timestamp(entry["data_start"])
    months_needed = (
        int(args.months)
        if args.months is not None
        else _months_needed_from_data_start(data_start)
    )

    print(
        f"[prefetch] strategy={args.strategy} timeframe={timeframe} "
        f"symbols={len(symbols)} months_needed={months_needed} "
        f"(data_start={data_start.isoformat()})"
    )
    print(f"[prefetch] cache_dir={CACHE_DIR}")
    print()

    failures: list[str] = []
    for sym in symbols:
        print(f"[prefetch] {sym} …")
        # Per-symbol dev cutoff: returns the EARLIEST holdout_start
        # across every manifest entry that uses `sym`, not just this
        # strategy's holdout_start. Required because shared symbols
        # (e.g. BTC/USDT is used by TrendFollowing_multi AND DCA at
        # different holdout_starts) must clip to the earliest cutoff
        # to avoid HoldoutBypass when later cache reads target the
        # earlier-cutoff strategy. None ⇒ symbol is not in any
        # manifest entry, in which case load_or_download_ohlcv
        # applies no clipping (acceptable: no holdout to enforce).
        until_ts = get_symbol_dev_cutoff(sym)
        try:
            df = load_or_download_ohlcv(
                symbol=sym,
                timeframe=timeframe,
                months=months_needed,
                download_fn=download_history,
                cache_dir=CACHE_DIR,
                until_ts=until_ts,
            )
        except Exception as exc:
            failures.append(f"{sym}: {exc.__class__.__name__}: {exc}")
            print(f"  FAIL: {exc.__class__.__name__}: {exc}")
            continue

        if df.empty:
            failures.append(f"{sym}: empty DataFrame returned")
            print(f"  FAIL: empty DataFrame returned for {sym}")
            continue

        first_ts = df.index.min()
        last_ts = df.index.max()
        print(
            f"  rows={len(df):,}  range={first_ts.date()} → {last_ts.date()}"
        )

        # Substrate-coverage check: warn (not abort) when a symbol's
        # earliest cached bar is later than data_start. Some basket
        # members may have shorter listed history than others (e.g.
        # newer USDT pairs) — the smoke step decides whether the
        # shortfall is acceptable; this script just surfaces it.
        if first_ts > data_start:
            shortfall_days = (first_ts - data_start).days
            print(
                f"  WARNING: earliest bar {first_ts.date()} is "
                f"{shortfall_days}d after data_start {data_start.date()} — "
                f"{sym} has shorter listed history than the manifest "
                f"window. Continuing."
            )

    print()
    if failures:
        print(
            f"[prefetch] DONE WITH ERRORS — {len(failures)}/{len(symbols)} "
            f"failed:"
        )
        for f in failures:
            print(f"  {f}")
        return 1

    print("Done. All symbols cached.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
