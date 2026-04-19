"""
backtest/cache.py — On-disk OHLCV cache for backtests.

WHY
───
The Phase C runner now downloads OHLCV for an entire 5-symbol universe every
run.  Pulling 36 months of 1h data for 5 symbols from OKX takes several
minutes and hammers the public endpoint.  Re-running a backtest while
iterating on strategy code should not re-download the same historical data.

This module provides a single helper — `load_or_download_ohlcv` — that
persists each download to a parquet file and only re-downloads when the
cache is stale (default 24 h TTL) or when the caller explicitly forces a
refresh via the `BACKTEST_REFRESH_CACHE=1` environment variable.

DESIGN NOTES
────────────
• Cache files are plain parquet (columnar, compressed, round-trip safe for
  OHLCV DataFrames with a UTC DatetimeIndex).
• Filename convention encodes symbol / timeframe / months so that different
  parameter combos never collide:
      {symbol_with_slash_replaced_by_dash}_{timeframe}_{months}mo.parquet
  e.g.  BTC-USDT_1h_36mo.parquet
• Corrupted parquet files are handled gracefully: we log a warning and
  redownload rather than crashing the entire backtest.
• No network calls live here — the caller passes in `download_fn`, which
  keeps this module trivial to unit-test and reusable for any data source
  (OKX, Binance, a stub fixture, etc.).
• All numeric / DataFrame checks use explicit `is None` / `== 0` — the
  project style guide forbids truthy-falsy shortcuts on numeric values.
"""

import os
import time
from pathlib import Path
from typing import Callable

import pandas as pd
from loguru import logger


def load_or_download_ohlcv(
    symbol: str,
    timeframe: str,
    months: int,
    download_fn: Callable[[str, str, int], pd.DataFrame],
    cache_dir: Path = Path("backtest/cache/ohlcv"),
    ttl_hours: int = 24,
) -> pd.DataFrame:
    """
    Load an OHLCV DataFrame from the parquet cache or download fresh.

    Args:
        symbol:       Trading pair (e.g. "BTC/USDT"). The "/" is replaced with
                      "-" in the filename so it is filesystem-safe.
        timeframe:    Candle timeframe string (e.g. "1h", "4h", "1d").
        months:       How many months of history to fetch.
        download_fn:  Callable that returns a fresh OHLCV DataFrame.
                      Signature: (symbol, timeframe, months) -> pd.DataFrame.
                      Called on cache miss, corrupted cache, or forced refresh.
        cache_dir:    Directory where cached parquet files are written. Created
                      if it does not exist.
        ttl_hours:    How long a cache entry is considered fresh. Older files
                      trigger a redownload.

    Returns:
        OHLCV DataFrame (same structure `download_fn` returns).

    Env vars:
        BACKTEST_REFRESH_CACHE=1  → bypass the cache read path entirely;
                                    always redownload and overwrite the file.
    """

    # Ensure the cache directory exists; no-op if already present.
    cache_dir.mkdir(parents=True, exist_ok=True)

    filename = f"{symbol.replace('/', '-')}_{timeframe}_{months}mo.parquet"
    cache_path = cache_dir / filename

    force_refresh = os.getenv("BACKTEST_REFRESH_CACHE") == "1"

    # ── Cache read path ──────────────────────────────────────────────────────
    # Three conditions must hold to take the cache-hit branch:
    #   1. User hasn't forced a refresh.
    #   2. The parquet file actually exists on disk.
    #   3. The file's mtime is within the TTL window.
    if not force_refresh and cache_path.exists():
        age_seconds = time.time() - cache_path.stat().st_mtime
        if age_seconds < ttl_hours * 3600:
            try:
                df = pd.read_parquet(cache_path)
                logger.info(
                    f"[Cache] Loaded {symbol} {timeframe} {months}mo from "
                    f"{cache_path.name} "
                    f"(age: {age_seconds / 3600:.1f}h, rows: {len(df)})"
                )
                return df
            except Exception as e:
                # Corrupted parquet — fall through to redownload below.
                logger.warning(
                    f"[Cache] Failed to read {cache_path.name}: {e}. "
                    f"Will redownload."
                )

    # ── Cache miss / stale / forced refresh / corrupted ──────────────────────
    if force_refresh:
        logger.info(
            f"[Cache] BACKTEST_REFRESH_CACHE=1 — forcing fresh download "
            f"for {symbol} {timeframe} {months}mo"
        )
    else:
        logger.info(
            f"[Cache] Miss for {symbol} {timeframe} {months}mo — downloading"
        )

    df = download_fn(symbol, timeframe, months)

    # Save to cache for next run. If the save itself fails (disk full, perms,
    # etc.) we log but still return the downloaded DataFrame — the backtest
    # should not be held hostage to a caching problem.
    try:
        df.to_parquet(cache_path)
        logger.info(
            f"[Cache] Saved {symbol} {timeframe} {months}mo to "
            f"{cache_path.name} ({len(df)} rows)"
        )
    except Exception as e:
        logger.warning(f"[Cache] Failed to write {cache_path.name}: {e}")

    return df
