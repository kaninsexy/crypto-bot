"""
data/historical_fetcher.py — Historical OHLCV fetcher with local cache.

WHY THIS EXISTS
───────────────
The live data_fetcher.py fetches only the most recent N candles (enough
for indicator warm-up). For backtesting we need years of data. Fetching
years of hourly candles on every backtest run wastes time and hammers the
Binance API. This module solves both problems:

  1. Downloads full history in paginated chunks (Binance limit: 1000 bars/req)
  2. Stores the result as a compressed Parquet file in data/cache/
  3. On subsequent calls, loads the cache and fetches ONLY the new candles
     since the last cached timestamp — then appends and saves
  4. If the cache is less than CACHE_MAX_AGE_HOURS old, skips the API call entirely

USAGE
─────
  from data.historical_fetcher import load_or_fetch

  # 3 years of hourly BTC/USDT (first call: ~35 API requests, ~10s)
  df = load_or_fetch("BTC/USDT", "1h", years=3)

  # 3 years of SOL/USDT (separate cache file per symbol/timeframe)
  df = load_or_fetch("SOL/USDT", "1h", years=3)

  # Force refresh even if cache is fresh
  df = load_or_fetch("BTC/USDT", "1h", years=3, force_refresh=True)

CACHE LOCATION
──────────────
  data/cache/BTC_USDT_1h.parquet       (CCXT-style → filename-safe)
  data/cache/SOL_USDT_1h.parquet
  data/cache/ETH_USDT_1h.parquet
"""

from __future__ import annotations

import time
import math
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

try:
    import ccxt
    _CCXT_AVAILABLE = True
except ImportError:
    _CCXT_AVAILABLE = False

# ── Config ────────────────────────────────────────────────────────────────────

CACHE_DIR          = Path(__file__).parent / "cache"
CACHE_MAX_AGE_HOURS = 6        # Re-use cache if younger than this
BARS_PER_REQUEST   = 1000      # Binance max per OHLCV request
REQUEST_DELAY_S    = 0.25      # Polite delay between paginated requests
TIMEFRAME_HOURS    = {         # hours per candle — used to compute start timestamp
    "1m": 1/60, "5m": 5/60, "15m": 15/60, "30m": 30/60,
    "1h": 1,    "2h": 2,     "4h": 4,     "6h": 6,
    "8h": 8,    "12h": 12,   "1d": 24,    "3d": 72,   "1w": 168,
}


# ── Public API ────────────────────────────────────────────────────────────────

def load_or_fetch(
    symbol:        str,
    timeframe:     str = "1h",
    years:         float = 3.0,
    exchange_id:   str = "binance",
    force_refresh: bool = False,
    cache_dir:     Optional[Path] = None,
) -> pd.DataFrame:
    """
    Return a DataFrame of OHLCV candles for *symbol* going back *years* years.

    Strategy:
      1. If a fresh cache file exists (< CACHE_MAX_AGE_HOURS old) and force_refresh
         is False → load and return cache immediately.
      2. If a stale cache exists → load it, fetch only missing candles since last
         cached timestamp, append, save, return.
      3. If no cache → fetch everything from scratch, save, return.

    Args:
        symbol:        e.g. "BTC/USDT", "SOL/USDT"
        timeframe:     e.g. "1h", "4h", "1d"
        years:         How many years of history to target (default 3)
        exchange_id:   CCXT exchange ID (default "binance")
        force_refresh: Skip age check; always fetch latest candles.
        cache_dir:     Override default cache directory.

    Returns:
        DataFrame with DatetimeIndex and columns [open, high, low, close, volume].
        Sorted ascending by timestamp.

    Raises:
        RuntimeError: If CCXT is not installed and no cache file exists.
    """
    _cache_dir = cache_dir or CACHE_DIR
    _cache_dir.mkdir(parents=True, exist_ok=True)

    cache_path = _cache_dir / _cache_filename(symbol, timeframe)

    # ── 1. Fresh cache → return immediately ──────────────────────────────────
    if cache_path.exists() and not force_refresh:
        age_hours = _cache_age_hours(cache_path)
        if age_hours < CACHE_MAX_AGE_HOURS:
            print(f"[DataFetcher] Cache hit for {symbol} {timeframe} "
                  f"(age {age_hours:.1f}h < {CACHE_MAX_AGE_HOURS}h) — loading.")
            return _load_cache(cache_path)

    # ── Need to fetch ─────────────────────────────────────────────────────────
    if not _CCXT_AVAILABLE:
        if cache_path.exists():
            print(f"[DataFetcher] CCXT not installed — loading stale cache for {symbol}.")
            return _load_cache(cache_path)
        raise RuntimeError(
            "CCXT is not installed and no cache file exists. "
            "Run: pip install ccxt"
        )

    exchange = _make_exchange(exchange_id)

    # ── 2. Stale cache → incremental update ─────────────────────────────────
    if cache_path.exists():
        cached_df     = _load_cache(cache_path)
        last_ts       = cached_df.index[-1]
        since_ms      = int(last_ts.timestamp() * 1000) + 1   # +1ms to avoid duplicate
        print(f"[DataFetcher] Incremental update: {symbol} {timeframe} "
              f"since {last_ts.strftime('%Y-%m-%d %H:%M')} UTC …")
        new_df = _fetch_since(exchange, symbol, timeframe, since_ms)
        if not new_df.empty:
            combined = pd.concat([cached_df, new_df])
            combined = combined[~combined.index.duplicated(keep="last")]
            combined.sort_index(inplace=True)
            _save_cache(combined, cache_path)
            print(f"[DataFetcher] Appended {len(new_df)} new candles → "
                  f"{len(combined):,} total.")
            return combined
        else:
            print(f"[DataFetcher] No new candles since last cache.")
            return cached_df

    # ── 3. No cache → full download ──────────────────────────────────────────
    tf_hours  = TIMEFRAME_HOURS.get(timeframe, 1)
    n_candles = int(years * 365.25 * 24 / tf_hours)
    since_dt  = datetime.now(timezone.utc) - timedelta(hours=tf_hours * n_candles)
    since_ms  = int(since_dt.timestamp() * 1000)

    print(f"[DataFetcher] Full download: {symbol} {timeframe} "
          f"({years:.1f} years ≈ {n_candles:,} candles) …")
    df = _fetch_since(exchange, symbol, timeframe, since_ms)

    if df.empty:
        raise ValueError(f"No data returned for {symbol} {timeframe}")

    _save_cache(df, cache_path)
    print(f"[DataFetcher] Downloaded {len(df):,} candles for {symbol} {timeframe}. "
          f"Saved to {cache_path.name}")
    return df


def get_cache_info() -> list[dict]:
    """Return info about all cached files (symbol, timeframe, rows, last timestamp)."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    info = []
    for f in sorted(CACHE_DIR.glob("*.parquet")):
        try:
            df = _load_cache(f)
            info.append({
                "file":       f.name,
                "rows":       len(df),
                "start":      str(df.index[0])[:10],
                "end":        str(df.index[-1])[:10],
                "age_hours":  round(_cache_age_hours(f), 1),
            })
        except Exception as e:
            info.append({"file": f.name, "error": str(e)})
    return info


def clear_cache(symbol: str = None, timeframe: str = None):
    """Delete cache files. Pass symbol+timeframe to clear one, or neither to clear all."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if symbol and timeframe:
        path = CACHE_DIR / _cache_filename(symbol, timeframe)
        if path.exists():
            path.unlink()
            print(f"[DataFetcher] Cleared cache: {path.name}")
    else:
        for f in CACHE_DIR.glob("*.parquet"):
            f.unlink()
        print("[DataFetcher] All cache files cleared.")


# ── Internal helpers ──────────────────────────────────────────────────────────

def _cache_filename(symbol: str, timeframe: str) -> str:
    """BTC/USDT + 1h  →  BTC_USDT_1h.parquet"""
    safe = symbol.replace("/", "_").replace(":", "_")
    return f"{safe}_{timeframe}.parquet"


def _cache_age_hours(path: Path) -> float:
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return (datetime.now(timezone.utc) - mtime).total_seconds() / 3600


def _load_cache(path: Path) -> pd.DataFrame:
    df = pd.read_parquet(path)
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index, utc=True)
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    return df.sort_index()


def _save_cache(df: pd.DataFrame, path: Path):
    df.to_parquet(path, compression="snappy")


def _make_exchange(exchange_id: str):
    """Create a public (no-auth) CCXT exchange instance."""
    exchange_cls = getattr(ccxt, exchange_id)
    exchange = exchange_cls({
        "enableRateLimit": True,
        "options": {"defaultType": "spot"},
    })
    # Load markets so symbol validation works
    exchange.load_markets()
    return exchange


def _fetch_since(
    exchange,
    symbol: str,
    timeframe: str,
    since_ms: int,
) -> pd.DataFrame:
    """
    Paginate through all candles from since_ms to now.
    Binance returns max 1000 bars per call so we loop with the last timestamp.
    """
    all_rows = []
    current_since = since_ms
    n_requests = 0

    while True:
        try:
            raw = exchange.fetch_ohlcv(
                symbol,
                timeframe=timeframe,
                since=current_since,
                limit=BARS_PER_REQUEST,
            )
        except Exception as e:
            print(f"[DataFetcher] API error (req {n_requests}): {e} — stopping pagination.")
            break

        n_requests += 1

        if not raw:
            break   # No more data

        all_rows.extend(raw)

        # Advance cursor: last returned timestamp + 1ms
        last_ts = raw[-1][0]
        current_since = last_ts + 1

        # If we got fewer bars than the max, we've reached "now"
        if len(raw) < BARS_PER_REQUEST:
            break

        # Print progress every 10 requests
        if n_requests % 10 == 0:
            last_dt = datetime.fromtimestamp(last_ts / 1000, tz=timezone.utc)
            print(f"  … {len(all_rows):,} candles fetched up to {last_dt.strftime('%Y-%m-%d')}")

        time.sleep(REQUEST_DELAY_S)

    if not all_rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    df = pd.DataFrame(all_rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df.set_index("timestamp", inplace=True)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)

    # Drop the last (possibly incomplete) candle — it's still forming live
    if len(df) > 1:
        df = df.iloc[:-1]

    return df.sort_index()


# ── CLI convenience ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3:
        sym = sys.argv[1]
        tf  = sys.argv[2]
        yrs = float(sys.argv[3]) if len(sys.argv) > 3 else 3.0
        df  = load_or_fetch(sym, tf, years=yrs)
        print(f"\nLoaded {len(df):,} candles for {sym} {tf}")
        print(f"Range: {df.index[0]} → {df.index[-1]}")
        print(df.tail(3))
    else:
        print("Usage: python -m data.historical_fetcher BTC/USDT 1h 3")
        info = get_cache_info()
        if info:
            print("\nCached files:")
            for i in info:
                print(f"  {i}")
