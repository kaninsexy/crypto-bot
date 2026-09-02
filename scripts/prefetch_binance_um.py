#!/usr/bin/env python3
"""Prefetch Binance USDT-M perp archives into backtest/cache/binance_um/.

Warms the parquet cache used by the perp-structural research substrate
(docs/research_revival_2026-09.md §C.3) and prints a coverage table so
the operator can see, per symbol, what actually landed on disk.

This script is a DATA LOADER ONLY.  It writes no manifest entry, runs
no trial, and applies no holdout enforcement — see the module docstring
of data/binance_vision_um.py.  Use --until to cap the fetched range.

Examples
--------
    # named symbols, 1d klines + funding + 3 days of metrics
    python3 scripts/prefetch_binance_um.py \\
        --symbols BTCUSDT,LUNAUSDT --start 2021-01 --until 2022-12-31 \\
        --intervals 1d --funding --metrics-max-days 3

    # every currently-listed symbol, daily bars only
    python3 scripts/prefetch_binance_um.py --all --intervals 1d \\
        --no-funding --no-metrics

    # top 50 by trailing 30-day quote volume (heavy: scans all live symbols)
    python3 scripts/prefetch_binance_um.py --top 50 --intervals 1h,1d
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd
from loguru import logger

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data.binance_vision_um import (  # noqa: E402
    DEFAULT_CACHE_DIR,
    SUPPORTED_INTERVALS,
    fetch_funding,
    fetch_klines,
    fetch_metrics,
    normalise_symbol,
    universe_table,
)

DEFAULT_START = "2020-01"
DEFAULT_UNTIL = "2026-08-31"


def _parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Prefetch Binance USDT-M perp archives (klines/funding/metrics).",
    )
    sel = p.add_mutually_exclusive_group(required=True)
    sel.add_argument("--symbols", help="comma-separated symbol list, e.g. BTCUSDT,ETHUSDT")
    sel.add_argument("--top", type=int, help="top N non-delisted symbols by trailing 30d quote volume")
    sel.add_argument("--all", action="store_true", help="every non-delisted symbol")
    p.add_argument("--start", default=DEFAULT_START, help="first month YYYY-MM (default 2020-01)")
    p.add_argument("--until", default=DEFAULT_UNTIL, help="exclusive UTC cutoff YYYY-MM-DD (default 2026-08-31)")
    p.add_argument("--intervals", default="1h,1d", help="comma list from " + ",".join(SUPPORTED_INTERVALS))
    p.add_argument("--funding", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--metrics", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--metrics-max-days", type=int, default=30,
                   help="cap on NEW metrics days downloaded per symbol (default 30)")
    p.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR))
    p.add_argument("--force", action="store_true", help="ignore caches and redownload")
    return p.parse_args(argv)


def _end_month(until: str) -> str:
    return str(pd.Period(pd.Timestamp(until), freq="M"))


def _resolve_symbols(args) -> list:
    if args.symbols:
        return [normalise_symbol(s) for s in args.symbols.split(",") if s.strip()]

    uni = universe_table(force=args.force, cache_dir=args.cache_dir)
    live = uni.loc[~uni["delisted"].astype(bool), "symbol"].tolist()
    if args.all:
        return live

    logger.info("binance_um: ranking {} live symbols by trailing 30d quote volume", len(live))
    end_m = _end_month(args.until)
    start_m = str(pd.Period(end_m, freq="M") - 2)
    scores = {}
    for i, sym in enumerate(live, start=1):
        try:
            df = fetch_klines(sym, "1d", start_m, end_m, until=args.until,
                              cache_dir=args.cache_dir)
        except Exception as exc:
            logger.warning("binance_um: volume scan failed for {}: {}", sym, exc)
            continue
        if len(df) == 0:
            continue
        scores[sym] = float(df["quote_volume"].tail(30).sum())
        if i % 100 == 0:
            logger.info("binance_um: volume scan {}/{}", i, len(live))
    ranked = sorted(scores, key=scores.get, reverse=True)
    return ranked[: args.top]


def _coverage_row(sym, args, intervals) -> dict:
    row = {"symbol": sym}
    end_m = _end_month(args.until)

    for itv in intervals:
        df = fetch_klines(sym, itv, args.start, end_m, until=args.until,
                          cache_dir=args.cache_dir, force=args.force)
        row[f"{itv}_first"] = str(df.index[0].date()) if len(df) > 0 else "-"
        row[f"{itv}_last"] = str(df.index[-1].date()) if len(df) > 0 else "-"
        row[f"n_{itv}"] = len(df)

    if args.funding:
        fdf = fetch_funding(sym, args.start, end_m, until=args.until,
                            cache_dir=args.cache_dir, force=args.force)
        row["n_funding"] = len(fdf)
        row["funding_first"] = str(fdf.index[0].date()) if len(fdf) > 0 else "-"
        row["funding_last"] = str(fdf.index[-1].date()) if len(fdf) > 0 else "-"

    if args.metrics:
        start_day = str(pd.Period(args.start[:7], freq="M").start_time.date())
        mdf = fetch_metrics(sym, start_day, args.until, until=args.until,
                            max_days=args.metrics_max_days,
                            cache_dir=args.cache_dir, force=args.force)
        row["n_metrics_5m"] = len(mdf)
        row["metrics_first"] = str(mdf.index[0].date()) if len(mdf) > 0 else "-"
        row["metrics_last"] = str(mdf.index[-1].date()) if len(mdf) > 0 else "-"

    return row


def main(argv=None) -> int:
    args = _parse_args(argv)
    intervals = [s.strip() for s in args.intervals.split(",") if s.strip()]
    for itv in intervals:
        if itv not in SUPPORTED_INTERVALS:
            logger.error("unsupported interval {!r} (allowed: {})", itv, SUPPORTED_INTERVALS)
            return 2

    symbols = _resolve_symbols(args)
    if len(symbols) == 0:
        logger.error("no symbols selected")
        return 1
    logger.info("binance_um: prefetching {} symbol(s) {}..{} intervals={}",
                len(symbols), args.start, args.until, intervals)

    rows = [_coverage_row(sym, args, intervals) for sym in symbols]
    table = pd.DataFrame(rows)

    print()
    print("=== Binance UM coverage (cache: %s) ===" % args.cache_dir)
    print(table.to_string(index=False))
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
