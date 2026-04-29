"""data/okx_perp.py — OKX USDT-M perpetual OHLCV ingestion.

Phase 4.B data layer (Track A).  Reuses the 24-hour-TTL parquet cache
pattern from `backtest/cache.py:load_or_download_ohlcv`, but routes
into a separate cache directory (`backtest/cache/perp/`) so perp data
never collides with the existing spot OHLCV layer at
`backtest/cache/ohlcv/`.

Public API
──────────
    load_or_fetch_perp_ohlcv(symbol, timeframe, months, ...) -> DataFrame
        High-level cache-wrapped accessor.  `symbol` is the manifest
        notation ("BTC/USDT"); the module translates internally to
        OKX's `instId` ("BTC-USDT-SWAP") and CCXT's unified swap
        symbol ("BTC/USDT:USDT").

    fetch_perp_ohlcv(symbol, timeframe, months) -> DataFrame
        Direct paginated fetch from OKX (no cache).  Same network path
        the cache wrapper uses on miss.

Symbol translation (data-layer boundary)
────────────────────────────────────────
    Manifest:  "BTC/USDT"        ← what holdout_manifest.json uses
    OKX instId: "BTC-USDT-SWAP"   ← OKX REST + cache-filename key
    CCXT swap: "BTC/USDT:USDT"    ← CCXT unified notation, fetch arg

Cache key shape
───────────────
    Logical: (exchange="okx", market="perp", symbol_manifest, timeframe, months)
    Physical: backtest/cache/perp/{instId}_{timeframe}_{months}mo.parquet

The "perp" subdirectory IS the market dimension.  Existing spot
readers (holdout.py, runner.py) glob `backtest/cache/ohlcv/`
exclusively — they will never see perp files, and vice versa.

Holdout enforcement
───────────────────
`backtest/cache.py:_earliest_holdout_start(symbol)` scans the
manifest for `symbol` membership.  Calling
`load_or_download_ohlcv(symbol="BTC-USDT-SWAP", ...)` returns None
from that lookup (manifest has "BTC/USDT", not the SWAP form), so
the holdout-bypass enforcement is a no-op for perp data.  This is
correct: perp data is a separate audit namespace from spot, and the
Phase 4.B harness will introduce its own perp manifest entries
under approval gate G3 (out of this prompt's scope).
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
from loguru import logger

try:
    import ccxt
    _CCXT_AVAILABLE = True
except ImportError:
    _CCXT_AVAILABLE = False

from backtest.cache import load_or_download_ohlcv


# ── Configuration ────────────────────────────────────────────────────────────

PERP_CACHE_DIR: Path = Path("backtest/cache/perp")
DEFAULT_TTL_HOURS: int = 24
DEFAULT_BATCH_SIZE: int = 300  # OKX max per /api/v5/market/history-candles
DEFAULT_REQUEST_DELAY_S: float = 0.10  # Polite delay between paginated requests


# ── Symbol translation ───────────────────────────────────────────────────────

def manifest_to_okx_instid(symbol_manifest: str) -> str:
    """`"BTC/USDT"` → `"BTC-USDT-SWAP"` (OKX REST `instId`).

    Raises:
        ValueError: input does not contain a single forward slash.
    """
    parts = symbol_manifest.split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(
            f"manifest symbol must be 'BASE/QUOTE'; got {symbol_manifest!r}"
        )
    base, quote = parts
    return f"{base}-{quote}-SWAP"


def okx_instid_to_ccxt_symbol(instid: str) -> str:
    """`"BTC-USDT-SWAP"` → `"BTC/USDT:USDT"` (CCXT unified swap symbol)."""
    parts = instid.split("-")
    if len(parts) != 3 or parts[2] != "SWAP":
        raise ValueError(
            f"OKX instId must be 'BASE-QUOTE-SWAP'; got {instid!r}"
        )
    base, quote, _ = parts
    # OKX USDT-M perps are settled in USDT; CCXT notation is BASE/QUOTE:SETTLE.
    return f"{base}/{quote}:{quote}"


def manifest_to_ccxt_swap_symbol(symbol_manifest: str) -> str:
    """`"BTC/USDT"` → `"BTC/USDT:USDT"`.  Combines the two translations."""
    return okx_instid_to_ccxt_symbol(manifest_to_okx_instid(symbol_manifest))


# ── Direct fetch (no cache) ──────────────────────────────────────────────────

def _make_swap_exchange():
    """Return a CCXT OKX client configured for SWAP markets, no auth."""
    if not _CCXT_AVAILABLE:
        raise RuntimeError(
            "CCXT not installed.  Run: pip install ccxt"
        )
    return ccxt.okx({
        "enableRateLimit": True,
        "options": {"defaultType": "swap"},
    })


def fetch_perp_ohlcv(
    symbol: str,
    timeframe: str = "1h",
    months: int = 12,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    request_delay_s: float = DEFAULT_REQUEST_DELAY_S,
) -> pd.DataFrame:
    """Paginated OHLCV fetch for one OKX USDT-M perp.

    Args:
        symbol:     Either manifest format ("BTC/USDT") or OKX instId
                    ("BTC-USDT-SWAP"); the function detects which by
                    presence of "/" and translates accordingly.
        timeframe:  CCXT-compatible timeframe ("1h", "4h", "1d", ...).
        months:     Approximate lookback in months (paged 30.44 days).
        batch_size: Per-request candle limit (OKX caps at 300).
        request_delay_s: Sleep between paginated requests.

    Returns:
        DataFrame with DatetimeIndex (UTC) and columns
        [open, high, low, close, volume].  Sorted ascending; the most
        recent partial candle is dropped.

    Raises:
        RuntimeError: CCXT is unavailable.
        ValueError:   `symbol` is malformed.
    """
    # Normalise symbol to CCXT swap form.
    if "/" in symbol:
        ccxt_symbol = manifest_to_ccxt_swap_symbol(symbol)
        instid = manifest_to_okx_instid(symbol)
    else:
        instid = symbol
        ccxt_symbol = okx_instid_to_ccxt_symbol(symbol)

    exchange = _make_swap_exchange()
    exchange.load_markets()
    if ccxt_symbol not in exchange.markets:
        raise ValueError(
            f"OKX swap market {ccxt_symbol!r} (from {symbol!r}) not in "
            f"exchange.markets — verify instId={instid!r} is listed."
        )

    since_dt = datetime.now(timezone.utc) - timedelta(days=int(months * 30.44))
    since_ms = int(since_dt.timestamp() * 1000)

    logger.info(
        f"[okx_perp] Downloading {months}mo of {timeframe} for {instid} "
        f"(CCXT={ccxt_symbol})"
    )

    all_rows: list[list] = []
    n_requests = 0
    while True:
        try:
            raw = exchange.fetch_ohlcv(
                ccxt_symbol,
                timeframe=timeframe,
                since=since_ms,
                limit=batch_size,
            )
        except (ccxt.NetworkError, ccxt.ExchangeError) as e:
            logger.error(f"[okx_perp] OHLCV fetch error: {e}")
            raise

        n_requests += 1
        if not raw:
            break

        all_rows.extend(raw)
        last_ts_ms = raw[-1][0]
        since_ms = last_ts_ms + 1
        if len(raw) < batch_size:
            break
        time.sleep(request_delay_s)

    if not all_rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    df = pd.DataFrame(
        all_rows,
        columns=["timestamp", "open", "high", "low", "close", "volume"],
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df.set_index("timestamp", inplace=True)
    df.index.name = "timestamp"
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = df[col].astype(float)

    # Drop the most-recent partial candle (still forming).
    if len(df) > 1:
        df = df.iloc[:-1]

    df = df[~df.index.duplicated(keep="last")].sort_index()
    logger.info(
        f"[okx_perp] Fetched {len(df)} candles for {instid} "
        f"({df.index[0]} → {df.index[-1]}) in {n_requests} requests"
    )
    return df


# ── Cache-wrapped fetch ──────────────────────────────────────────────────────

def load_or_fetch_perp_ohlcv(
    symbol: str,
    timeframe: str = "1h",
    months: int = 12,
    *,
    cache_dir: Path = PERP_CACHE_DIR,
    ttl_hours: int = DEFAULT_TTL_HOURS,
    until_ts: Optional[pd.Timestamp] = None,
) -> pd.DataFrame:
    """Cache-wrapped perp OHLCV accessor.

    The cache filename uses the OKX `instId` (e.g.
    `BTC-USDT-SWAP_1h_12mo.parquet`) so spot and perp can never share
    a key even if `cache_dir` is misconfigured.

    Args:
        symbol:    Manifest format ("BTC/USDT") or instId
                   ("BTC-USDT-SWAP").  Normalised to instId for cache
                   keying.
        timeframe: CCXT-compatible timeframe.
        months:    Approximate lookback in months.
        cache_dir: Override the default `backtest/cache/perp/` location.
        ttl_hours: Treat cache files younger than this as fresh.
        until_ts:  If given, clip returned rows to `index < until_ts`.

    Returns:
        DataFrame indexed by UTC timestamp with OHLCV columns.
    """
    instid = manifest_to_okx_instid(symbol) if "/" in symbol else symbol

    # `load_or_download_ohlcv` calls `download_fn(symbol, timeframe, months)`
    # — wire it to our perp fetcher with the same instId we used for the
    # cache key.  The wrapper takes care of TTL, parquet round-trip, and
    # the holdout enforcement (no-op for perp instIds, see module docstring).
    return load_or_download_ohlcv(
        symbol=instid,
        timeframe=timeframe,
        months=months,
        download_fn=fetch_perp_ohlcv,
        cache_dir=cache_dir,
        ttl_hours=ttl_hours,
        until_ts=until_ts,
    )


# ── CLI for one-off diagnostics ──────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 2:
        sym = sys.argv[1]
        tf = sys.argv[2] if len(sys.argv) > 2 else "1h"
        mo = int(sys.argv[3]) if len(sys.argv) > 3 else 1
        df = load_or_fetch_perp_ohlcv(sym, tf, mo)
        print(f"Loaded {len(df)} candles for {sym} {tf} ({mo}mo)")
        print(f"Range: {df.index[0]} → {df.index[-1]}")
        print(df.tail(3))
    else:
        print("Usage: python -m data.okx_perp BTC/USDT 1h 1")
