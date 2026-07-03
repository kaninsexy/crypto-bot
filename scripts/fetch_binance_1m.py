#!/usr/bin/env python3
"""Bulk-fetch Binance Vision 1m klines for the Phase 4.E basket.

Free data, no API key. Roughly 2.4 MB/month zipped for BTC, less for
alts; the default basket+range is ~500 MB of downloads cached to
backtest/cache/binance_vision/ as parquet.

Usage:
    python scripts/fetch_binance_1m.py                      # default basket
    python scripts/fetch_binance_1m.py --symbols BTCUSDT    # one symbol
    python scripts/fetch_binance_1m.py --start 2021-01 --end 2026-06
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data.binance_vision import BinanceVisionError, load_klines  # noqa: E402

# Default basket per redesign proposal §5: majors with deep history,
# BNB excluded for v1 (avoids the cross-venue seam inside the seam).
DEFAULT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT"]
DEFAULT_START = "2021-01"
DEFAULT_END = "2026-06"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    ap.add_argument("--start", default=DEFAULT_START, help="YYYY-MM")
    ap.add_argument("--end", default=DEFAULT_END, help="YYYY-MM")
    args = ap.parse_args()

    failures = 0
    for sym in args.symbols:
        t0 = time.time()
        try:
            df = load_klines(sym, args.start, args.end)
        except BinanceVisionError as exc:
            print(f"[FAIL] {sym}: {exc}")
            failures += 1
            continue
        print(
            f"[OK]   {sym}: {len(df):,} bars "
            f"{df.index[0]} -> {df.index[-1]} "
            f"({time.time() - t0:.0f}s)"
        )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
