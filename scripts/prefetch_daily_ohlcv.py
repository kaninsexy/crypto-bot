"""
scripts/prefetch_daily_ohlcv.py — populate backtest/cache/ohlcv/ with 1d
candles for the Phase 4.A TrendFollowing_multi basket.

The smoke script (`scripts/phase_4a_trendfollowing_smoke.py`) and any
future cpcv_multi run reads daily OHLCV from
`backtest/cache/ohlcv/{SYMBOL}_1d_{N}mo.parquet` via
`holdout.load_dev → backtest.cache.load_or_download_ohlcv`.  That helper
calls `download_fn(symbol, timeframe, months) -> pd.DataFrame` on a cache
miss; the manifest lookup is signed up for the spot OHLCV download path,
which lives in `backtest.runner.download_history` (the same function the
production runner already uses for spot symbols — `data/historical_fetcher.
load_or_fetch` uses a `years: float` argument and does NOT match the
`download_fn` contract, so it is not used here).

This script reads the symbols list from the manifest entry at runtime so
the basket can grow or shrink without touching code.  It does not append
to `trials.log`, edit any sacred-harness file, or call the holdout
window — every cache write is gated by `until_ts=get_symbol_dev_cutoff(symbol)` so the
`HoldoutBypass` enforcement in `backtest.cache._apply_until_and_enforce`
catches any accidental holdout-window leak.
"""

from __future__ import annotations

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


STRATEGY_ID = "TrendFollowing_multi"
TIMEFRAME = "1d"
CACHE_DIR = ROOT / "backtest" / "cache" / "ohlcv"


def main() -> int:
    manifest = load_manifest()
    if STRATEGY_ID not in manifest:
        print(f"FAIL: '{STRATEGY_ID}' not in manifest.", file=sys.stderr)
        return 1
    entry = manifest[STRATEGY_ID]
    symbols = list(entry["symbols"])
    data_start = pd.Timestamp(entry["data_start"])
    holdout_start = pd.Timestamp(entry["holdout_start"])

    # months-back math: same shape as scripts/phase_4b_full_cpcv_v1.py
    # (chat 2026-05-02 substrate-coverage fix) — the OKX exchange
    # archive walks back from "now", not from data_start, so the
    # months parameter must cover (now - data_start) in calendar
    # days, not the dev_span.
    now_utc = pd.Timestamp.now(tz="UTC")
    months_back_days = (now_utc - data_start).days
    months_needed = int(math.ceil(months_back_days / 30.44)) + 1

    print(
        f"[prefetch] strategy={STRATEGY_ID} timeframe={TIMEFRAME} "
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
        # strategy's holdout_start.  Required because shared symbols
        # (e.g. BTC/USDT is used by TrendFollowing_multi AND DCA at
        # different holdout_starts) must clip to the earliest cutoff
        # to avoid HoldoutBypass when later cache reads target the
        # earlier-cutoff strategy.  None ⇒ symbol is not in any
        # manifest entry, in which case load_or_download_ohlcv
        # applies no clipping (acceptable: no holdout to enforce).
        until_ts = get_symbol_dev_cutoff(sym)
        try:
            df = load_or_download_ohlcv(
                symbol=sym,
                timeframe=TIMEFRAME,
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
        # earliest cached bar is later than data_start.  Some basket
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
        print(f"[prefetch] DONE WITH ERRORS — {len(failures)}/{len(symbols)} failed:")
        for f in failures:
            print(f"  {f}")
        return 1

    print("Done. All symbols cached.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
