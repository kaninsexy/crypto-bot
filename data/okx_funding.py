"""data/okx_funding.py — OKX USDT-M perpetual funding-rate history.

Phase 4.B data layer (Track B).  Fetches funding-rate history at the
settlement cadence (8h on USDT-M majors as of 2026-04-29), and joins
mark-price snapshots at each settlement timestamp so downstream
consumers receive a self-contained DataFrame.

Public API
──────────
    load_or_fetch_funding_history(symbol, months, ...) -> DataFrame
        Cache-wrapped accessor.  `symbol` is manifest notation
        ("BTC/USDT"); the module translates internally.

    fetch_funding_history(symbol, months) -> DataFrame
        Direct paginated fetch (no cache).

    detect_funding_cadence(df) -> dict
        Inspect consecutive timestamps in a funding history DataFrame
        and report the dominant cadence (median seconds between
        settlements).  Used to satisfy the prompt's HALT-AND-CONSULT
        gate: cadence ≠ 8h on any in-scope pair triggers a manifest-
        shape question that is out of this prompt's scope.

Returned DataFrame schema
─────────────────────────
    Index   : DatetimeIndex (UTC), one row per funding settlement.
    Columns :
        funding_rate     float — settled rate (sign convention:
                                  positive = longs pay shorts; short
                                  leg receives in normal markets).
        mark_price       float — mark price snapshot at the settlement
                                  timestamp, joined from OKX
                                  /api/v5/market/mark-price-candles
                                  (1h cadence, close-price selected
                                  at the funding timestamp boundary).

Cache layout
────────────
    backtest/cache/perp_funding/{instId}_funding_{months}mo.parquet

Cadence note
────────────
OKX USDT-M majors settle every 8 hours (00:00, 08:00, 16:00 UTC).
The risk model in `research/funding-rate-risk-model.md` assumes 8h.
Per-pair overrides are a manifest-shape change (approval gate G3) and
out of this prompt's scope; the `detect_funding_cadence` helper is the
HALT-AND-CONSULT trigger.
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

from data.okx_perp import (
    manifest_to_okx_instid,
    manifest_to_ccxt_swap_symbol,
    okx_instid_to_ccxt_symbol,
)


# ── Configuration ────────────────────────────────────────────────────────────

FUNDING_CACHE_DIR: Path = Path("backtest/cache/perp_funding")
DEFAULT_TTL_HOURS: int = 24
DEFAULT_BATCH_SIZE: int = 100  # OKX max per /api/v5/public/funding-rate-history
DEFAULT_REQUEST_DELAY_S: float = 0.10
EXPECTED_CADENCE_HOURS: int = 8
CADENCE_TOLERANCE_SECONDS: int = 60  # ±60s slop at settlement boundaries


# ── Direct fetch (no cache) ──────────────────────────────────────────────────

def _make_swap_exchange():
    if not _CCXT_AVAILABLE:
        raise RuntimeError("CCXT not installed.  Run: pip install ccxt")
    return ccxt.okx({
        "enableRateLimit": True,
        "options": {"defaultType": "swap"},
    })


def fetch_funding_history(
    symbol: str,
    months: int = 12,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    request_delay_s: float = DEFAULT_REQUEST_DELAY_S,
) -> pd.DataFrame:
    """Paginated funding-rate-history fetch for one OKX USDT-M perp.

    The CCXT response carries the `fundingRate` and timestamp.  Mark
    prices are NOT included in the funding-rate-history payload, so
    we issue a parallel `fetch_mark_ohlcv(..., '1h')` call covering
    the same window and snap-merge the close price at each
    settlement timestamp.

    Args:
        symbol:     Manifest format ("BTC/USDT") or instId
                    ("BTC-USDT-SWAP").
        months:     Approximate lookback (paged 30.44 days/month).
        batch_size: Per-request row limit (OKX caps at 100 for the
                    funding history endpoint).
        request_delay_s: Sleep between paginated requests.

    Returns:
        DataFrame indexed by UTC timestamp at funding settlements,
        columns [funding_rate, mark_price].

    Raises:
        RuntimeError: CCXT unavailable.
        ValueError:   `symbol` malformed or market not on OKX.
    """
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
        f"[okx_funding] Downloading {months}mo of funding history for {instid} "
        f"(CCXT={ccxt_symbol})"
    )

    # ── 1. Funding-rate history (paginated) ──────────────────────────────────
    rows: list[dict] = []
    cursor = since_ms
    n_funding_requests = 0
    while True:
        try:
            batch = exchange.fetch_funding_rate_history(
                ccxt_symbol, since=cursor, limit=batch_size,
            )
        except (ccxt.NetworkError, ccxt.ExchangeError) as e:
            logger.error(f"[okx_funding] funding fetch error: {e}")
            raise

        n_funding_requests += 1
        if not batch:
            break

        rows.extend(batch)
        last_ts_ms = batch[-1]["timestamp"]
        # Advance past the last seen timestamp to avoid re-fetching it.
        new_cursor = last_ts_ms + 1
        if new_cursor == cursor:  # API gave no forward progress — bail out
            break
        cursor = new_cursor
        if len(batch) < batch_size:
            break
        time.sleep(request_delay_s)

    if not rows:
        return pd.DataFrame(columns=["funding_rate", "mark_price"])

    funding_df = pd.DataFrame([
        {
            "timestamp": r["timestamp"],
            "funding_rate": float(r["fundingRate"]),
        }
        for r in rows
    ])
    funding_df["timestamp"] = pd.to_datetime(
        funding_df["timestamp"], unit="ms", utc=True,
    )
    funding_df = funding_df.set_index("timestamp").sort_index()
    funding_df = funding_df[~funding_df.index.duplicated(keep="last")]

    # ── 2. Mark price at 1h cadence covering the same window ────────────────
    # OKX /api/v5/market/mark-price-candles supports 1h granularity.  We
    # fetch from a few candles before the earliest funding settlement to
    # ensure we have a mark close available for any boundary.
    mark_since_ms = int(funding_df.index[0].timestamp() * 1000) - 3600 * 1000
    mark_rows: list[list] = []
    cursor = mark_since_ms
    n_mark_requests = 0
    mark_batch_size = 300
    while True:
        try:
            batch = exchange.fetch_mark_ohlcv(
                ccxt_symbol, "1h", since=cursor, limit=mark_batch_size,
            )
        except (ccxt.NetworkError, ccxt.ExchangeError) as e:
            logger.error(f"[okx_funding] mark OHLCV fetch error: {e}")
            raise

        n_mark_requests += 1
        if not batch:
            break

        mark_rows.extend(batch)
        last_ts_ms = batch[-1][0]
        new_cursor = last_ts_ms + 1
        if new_cursor == cursor:
            break
        cursor = new_cursor
        if len(batch) < mark_batch_size:
            break
        time.sleep(request_delay_s)

    mark_df = pd.DataFrame(
        mark_rows,
        columns=["timestamp", "open", "high", "low", "close", "volume"],
    )
    if not mark_df.empty:
        mark_df["timestamp"] = pd.to_datetime(
            mark_df["timestamp"], unit="ms", utc=True,
        )
        mark_df = mark_df.set_index("timestamp").sort_index()
        mark_df = mark_df[~mark_df.index.duplicated(keep="last")]

    # ── 3. Snap-merge mark close onto funding timestamps ────────────────────
    # Funding settles on the hour boundary (00:00, 08:00, 16:00 UTC).  The
    # 1h mark candle indexed at that timestamp covers [t, t+1h); its CLOSE
    # is the mark price one hour after settlement, which is wrong for our
    # purposes.  We want the mark price AT the settlement, which is
    # equivalent to the OPEN of the 1h candle at that timestamp (which
    # equals the close of the previous candle).
    #
    # OKX mark candles are timestamped at the start of the bar, so:
    #   funding_ts = 16:00 → mark candle indexed 16:00 has open == mark
    #                         price at 16:00 settlement instant.
    # Use `open` not `close`.
    if not mark_df.empty:
        funding_df = funding_df.join(
            mark_df[["open"]].rename(columns={"open": "mark_price"}),
            how="left",
        )
        # Forward-fill any settlement that landed on a missing 1h bar
        # (rare gap on OKX mark feed); back-fill the very first row if
        # the mark series starts after the earliest funding row (we
        # added a 1h cushion before so this should be rare).
        funding_df["mark_price"] = (
            funding_df["mark_price"].ffill().bfill()
        )
    else:
        funding_df["mark_price"] = float("nan")

    logger.info(
        f"[okx_funding] {instid}: {len(funding_df)} funding rows, "
        f"{len(mark_df)} 1h mark candles (mark requests={n_mark_requests}, "
        f"funding requests={n_funding_requests})"
    )
    return funding_df


# ── Cadence detector ─────────────────────────────────────────────────────────

def detect_funding_cadence(df: pd.DataFrame) -> dict:
    """Inspect consecutive index gaps and report the dominant cadence.

    Args:
        df: Funding history DataFrame with DatetimeIndex.

    Returns:
        dict with:
            median_seconds : float — median gap between settlements.
            min_seconds    : float — smallest gap observed.
            max_seconds    : float — largest gap observed.
            n_gaps         : int   — number of consecutive-gap samples.
            is_8h          : bool  — True iff median is within
                                       CADENCE_TOLERANCE_SECONDS of 8h
                                       (28800s).
    """
    if len(df) < 2:
        return {
            "median_seconds": 0.0, "min_seconds": 0.0, "max_seconds": 0.0,
            "n_gaps": 0, "is_8h": False,
        }
    gaps_s = df.index.to_series().diff().dt.total_seconds().dropna().values
    median_s = float(pd.Series(gaps_s).median())
    expected_s = EXPECTED_CADENCE_HOURS * 3600
    return {
        "median_seconds": median_s,
        "min_seconds": float(min(gaps_s)),
        "max_seconds": float(max(gaps_s)),
        "n_gaps": int(len(gaps_s)),
        "is_8h": abs(median_s - expected_s) <= CADENCE_TOLERANCE_SECONDS,
    }


# ── Cache-wrapped fetch ──────────────────────────────────────────────────────

def _funding_cache_path(instid: str, months: int, cache_dir: Path) -> Path:
    return cache_dir / f"{instid}_funding_{months}mo.parquet"


def load_or_fetch_funding_history(
    symbol: str,
    months: int = 12,
    *,
    cache_dir: Path = FUNDING_CACHE_DIR,
    ttl_hours: int = DEFAULT_TTL_HOURS,
    until_ts: Optional[pd.Timestamp] = None,
) -> pd.DataFrame:
    """Cache-wrapped funding-rate-history accessor.

    The cache layout mirrors `data.okx_perp` but in a separate dir
    (`backtest/cache/perp_funding/`).  We do NOT route through
    `backtest.cache.load_or_download_ohlcv` because the shape is
    different (settlement timestamps, not OHLCV bars).

    Args:
        symbol:    Manifest format ("BTC/USDT") or instId
                   ("BTC-USDT-SWAP").
        months:    Approximate lookback in months.
        cache_dir: Override the default `backtest/cache/perp_funding/`.
        ttl_hours: Treat cache files younger than this as fresh.
        until_ts:  If given, clip rows to `index < until_ts`.

    Returns:
        DataFrame indexed by UTC timestamp at funding settlements,
        columns [funding_rate, mark_price].
    """
    instid = manifest_to_okx_instid(symbol) if "/" in symbol else symbol
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = _funding_cache_path(instid, months, cache_dir)

    # Cache freshness.
    use_cache = False
    if cache_path.exists():
        age_s = time.time() - cache_path.stat().st_mtime
        if age_s < ttl_hours * 3600:
            use_cache = True

    if use_cache:
        df = pd.read_parquet(cache_path)
        if not isinstance(df.index, pd.DatetimeIndex):
            df.index = pd.to_datetime(df.index, utc=True)
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        logger.info(
            f"[okx_funding] cache hit {cache_path.name} "
            f"(age {age_s/3600:.1f}h, rows {len(df)})"
        )
    else:
        df = fetch_funding_history(instid, months=months)
        try:
            df.to_parquet(cache_path)
            logger.info(
                f"[okx_funding] cached {cache_path.name} ({len(df)} rows)"
            )
        except Exception as e:
            logger.warning(f"[okx_funding] cache write failed: {e}")

    if until_ts is not None:
        df = df[df.index < until_ts]
    return df


# ── CLI for one-off diagnostics ──────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 2:
        sym = sys.argv[1]
        mo = int(sys.argv[2]) if len(sys.argv) > 2 else 1
        df = load_or_fetch_funding_history(sym, mo)
        print(f"Loaded {len(df)} funding rows for {sym} ({mo}mo)")
        if len(df) >= 2:
            print(f"Range: {df.index[0]} → {df.index[-1]}")
            cadence = detect_funding_cadence(df)
            print(f"Cadence: {cadence}")
            print(df.tail(3))
    else:
        print("Usage: python -m data.okx_funding BTC/USDT 1")
