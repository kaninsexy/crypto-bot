"""data/okx_funding.py — OKX USDT-M perpetual funding-rate history.

Phase 4.B data layer (Track B + Path 5 deep-history extension).

Two-tier ingestion strategy
───────────────────────────
The OKX V5 endpoint
`/api/v5/public/funding-rate-history` is server-capped to ~94 days
of recent history regardless of pagination.  For the FundingRateHarvest
dev window (2023-05-03 → 2025-09-22) we need ~33 months of depth.

Path 5 — hybrid live + archive ingestion:

    | window                       | source                        |
    |------------------------------|-------------------------------|
    | now − LIVE_API_DEPTH_DAYS    | OKX V5 funding-rate-history   |
    |   ... → now                  | (paginated; ccxt wrapper)     |
    | start → cutover              | OKX historical-data archive   |
    |                              | static.okx.com .zip per month |

Mark price (which is NOT included in either funding source) is then
joined onto the assembled funding DataFrame at the settlement
timestamps.  For windows that fit inside the live depth, the existing
ccxt `fetch_mark_ohlcv` path runs.  For deeper windows, mark price is
fetched via direct HTTP to
`/api/v5/market/history-mark-price-candles` (the deep history
endpoint, distinct from `mark-price-candles` which ccxt's
`fetch_mark_ohlcv` shortcut wraps and which is itself ~60-day capped).

Public API
──────────
    load_or_fetch_funding_history(symbol, months, ...) -> DataFrame
        Cache-wrapped accessor.  `symbol` is manifest notation
        ("BTC/USDT"); the module translates internally.

    fetch_funding_history(symbol, months) -> DataFrame
        Hybrid (live + archive) paginated fetch.  Returns the
        canonical `[funding_rate, mark_price]` shape regardless of
        whether the window crosses the live depth horizon.

    fetch_funding_archive_month(instid, year, month) -> DataFrame
        Single-month archive zip fetcher with per-month parquet
        cache.  Raises FileNotFoundError on HTTP 404.

    fetch_mark_price_history_deep(instid, start_ms, end_ms) -> DataFrame
        Direct-HTTP deep mark price fetcher (1H bars).  Returns
        `[open, close]` indexed by UTC timestamp.

    detect_funding_cadence(df) -> dict
        Inspect consecutive timestamps in a funding history DataFrame
        and report the dominant cadence (median seconds between
        settlements).

Returned DataFrame schema (canonical)
─────────────────────────────────────
    Index   : DatetimeIndex (UTC), one row per funding settlement.
    Columns :
        funding_rate     float — settled rate (sign convention:
                                  positive = longs pay shorts; short
                                  leg receives in normal markets).
        mark_price       float — mark price snapshot at the settlement
                                  timestamp; sourced from either the
                                  live `fetch_mark_ohlcv` path (recent
                                  windows) or the deep-history HTTP
                                  endpoint (windows beyond
                                  LIVE_API_DEPTH_DAYS).

Cache layout
────────────
    backtest/cache/perp_funding/{instId}_funding_{months}mo.parquet
        Top-level assembled+deduped DataFrame (live+archive merged).

    backtest/cache/perp_funding/archive/{instId}/{yyyy}-{mm}.parquet
        Per-month archive funding-rate cache.  Archive months are
        immutable once published, so no TTL is applied.

    backtest/cache/perp_funding/mark_price_archive/{instId}/{yyyy}-{mm}.parquet
        Per-month deep mark-price cache.  Past months immutable; the
        current month's slice is re-fetched (not cached) so partial
        fills update on each call.

Cadence note
────────────
OKX USDT-M majors settle every 8 hours (00:00, 08:00, 16:00 UTC).
The risk model in `research/funding-rate-risk-model.md` assumes 8h.
Per-pair overrides are a manifest-shape change (approval gate G3) and
out of this prompt's scope; the `detect_funding_cadence` helper is the
HALT-AND-CONSULT trigger.
"""

from __future__ import annotations

import io
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import requests
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

# ── Path 5 — archive + deep mark-price configuration ────────────────────────
ARCHIVE_BASE_URL: str = "https://static.okx.com/cdn/okex/traderecords"
ARCHIVE_FUNDING_PATH_TEMPLATE: str = (
    "swaprates/monthly/{yyyymm}/{instid}-fundingrates-{yyyy}-{mm}.zip"
)
ARCHIVE_CACHE_DIR: Path = FUNDING_CACHE_DIR / "archive"
MARK_PRICE_ARCHIVE_CACHE_DIR: Path = FUNDING_CACHE_DIR / "mark_price_archive"

# Live `fetch_funding_rate_history` returns ~94 days at the time of
# this writing; we use 90 for safety so the archive path always fully
# covers the fall-back window with a small overlap (resolved by
# last-wins dedup).
LIVE_API_DEPTH_DAYS: int = 90
MARK_PRICE_HISTORY_BATCH_SIZE: int = 100
MARK_PRICE_HISTORY_URL: str = (
    "https://www.okx.com/api/v5/market/history-mark-price-candles"
)
_HTTP_USER_AGENT: str = "Mozilla/5.0 (okx_funding/path5)"


# ── Direct fetch (no cache) ──────────────────────────────────────────────────

def _make_swap_exchange():
    if not _CCXT_AVAILABLE:
        raise RuntimeError("CCXT not installed.  Run: pip install ccxt")
    return ccxt.okx({
        "enableRateLimit": True,
        "options": {"defaultType": "swap"},
    })


def _detect_live_api_depth(exchange, ccxt_symbol: str) -> int:
    """Probe the live API to measure actual depth in days.
    One cheap call.  Returns observed depth in days, or
    LIVE_API_DEPTH_DAYS if the probe fails (conservative
    fallback)."""
    try:
        since_dt = datetime.now(timezone.utc) - timedelta(
            days=LIVE_API_DEPTH_DAYS,
        )
        since_ms = int(since_dt.timestamp() * 1000)
        batch = exchange.fetch_funding_rate_history(
            ccxt_symbol, since=since_ms, limit=100,
        )
        if not batch:
            # Fall back to a shorter probe — depth is < ceiling.
            since_dt = datetime.now(timezone.utc) - timedelta(days=30)
            since_ms = int(since_dt.timestamp() * 1000)
            batch = exchange.fetch_funding_rate_history(
                ccxt_symbol, since=since_ms, limit=100,
            )
            if not batch:
                logger.warning(
                    "[okx_funding] live-API depth probe returned "
                    "0 rows even at 30d; falling back to "
                    f"LIVE_API_DEPTH_DAYS={LIVE_API_DEPTH_DAYS}"
                )
                return LIVE_API_DEPTH_DAYS
        earliest_ms = min(int(r["timestamp"]) for r in batch)
        earliest_dt = datetime.fromtimestamp(
            earliest_ms / 1000, tz=timezone.utc,
        )
        observed = (datetime.now(timezone.utc) - earliest_dt).days
        # Conservative: subtract 1 day so the cutover never
        # lands exactly on the depth boundary.
        return max(1, observed - 1)
    except Exception as e:
        logger.warning(
            f"[okx_funding] live-API depth probe failed: {e}; "
            f"falling back to LIVE_API_DEPTH_DAYS="
            f"{LIVE_API_DEPTH_DAYS}"
        )
        return LIVE_API_DEPTH_DAYS


def fetch_funding_history(
    symbol: str,
    months: int = 12,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    request_delay_s: float = DEFAULT_REQUEST_DELAY_S,
) -> pd.DataFrame:
    """Hybrid funding-rate-history fetch (live + archive) for one OKX
    USDT-M perp.

    For windows that fit inside `LIVE_API_DEPTH_DAYS`, the existing
    paginated CCXT path runs unchanged.  For deeper windows the
    function splits the request:

      [start_ms, cutover_ms)  →  static.okx.com archive (per-month .zip)
      [cutover_ms, now]       →  live V5 funding-rate-history

    Mark price (not present in either source) is then joined onto the
    assembled funding DataFrame.  When the funding series starts
    inside the live window, the existing `fetch_mark_ohlcv` join
    runs.  When the funding series starts deeper, mark price is
    fetched via direct HTTP to
    `/api/v5/market/history-mark-price-candles` (deep-history
    endpoint, distinct from the recent-only ccxt shortcut).

    Args:
        symbol:     Manifest format ("BTC/USDT") or instId
                    ("BTC-USDT-SWAP").
        months:     Approximate lookback (paged 30.44 days/month).
        batch_size: Per-request row limit on the live funding-rate
                    endpoint (OKX caps at 100).
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

    now_dt = datetime.now(timezone.utc)
    since_dt = now_dt - timedelta(days=int(months * 30.44))
    since_ms = int(since_dt.timestamp() * 1000)
    observed_depth_days = _detect_live_api_depth(exchange, ccxt_symbol)
    effective_depth_days = min(LIVE_API_DEPTH_DAYS, observed_depth_days)
    cutover_dt = now_dt - timedelta(days=effective_depth_days)
    cutover_ms = int(cutover_dt.timestamp() * 1000)
    if effective_depth_days < LIVE_API_DEPTH_DAYS:
        logger.info(
            f"[okx_funding] live-API depth measured "
            f"{observed_depth_days}d (< ceiling "
            f"{LIVE_API_DEPTH_DAYS}d); cutover auto-shifted."
        )

    logger.info(
        f"[okx_funding] Downloading {months}mo of funding history for {instid} "
        f"(CCXT={ccxt_symbol}); since={since_dt.isoformat()}, "
        f"cutover={cutover_dt.isoformat()} (effective_depth={effective_depth_days}d)"
    )

    # ── 1. Funding rate: live + (optional) archive ──────────────────────────
    # Dispatch decision is a function of the REQUEST WINDOW
    # (since_ms vs cutover_ms), not of the data that comes back.
    # Anchoring the path choice on the requested window keeps the
    # behaviour deterministic and preserves backwards compatibility
    # with mocked tests that stuff arbitrary historical timestamps
    # into the exchange-mock's return value.
    needs_archive = since_ms < cutover_ms
    if not needs_archive:
        funding_df = _fetch_funding_live(
            exchange=exchange,
            ccxt_symbol=ccxt_symbol,
            since_ms=since_ms,
            batch_size=batch_size,
            request_delay_s=request_delay_s,
        )
    else:
        archive_df = _fetch_funding_archive_window(
            instid=instid,
            since_ms=since_ms,
            until_ms=cutover_ms,
        )
        live_df = _fetch_funding_live(
            exchange=exchange,
            ccxt_symbol=ccxt_symbol,
            since_ms=cutover_ms,
            batch_size=batch_size,
            request_delay_s=request_delay_s,
        )
        funding_df = _concat_dedup_last_wins([archive_df, live_df])

    if funding_df.empty:
        return pd.DataFrame(columns=["funding_rate", "mark_price"])

    # Underlap detection: if there's a gap > 24h in the live segment,
    # OKX may have silently shrunk the funding-rate-history depth
    # below LIVE_API_DEPTH_DAYS.  Warn loudly so the next trial can
    # decide whether to lower the constant.
    cutover_ts = pd.Timestamp(cutover_ms, unit="ms", tz="UTC")
    live_segment = funding_df[funding_df.index >= cutover_ts]
    if len(live_segment) >= 2:
        gaps_h = live_segment.index.to_series().diff().dt.total_seconds() / 3600
        max_gap_h = float(gaps_h.max())
        if max_gap_h > 24:
            logger.warning(
                f"[okx_funding] Live segment gap {max_gap_h:.1f}h "
                f"detected for {instid}.  Possible OKX live-API depth "
                f"shrink below LIVE_API_DEPTH_DAYS={LIVE_API_DEPTH_DAYS}. "
                f"Lower the constant or investigate before next trial."
            )

    # ── 2. Mark price: live (existing) or deep-history (Path 5) ─────────────
    # Same window-driven dispatch.  Live-only window → ccxt path
    # (mockable).  Archive-touching window → deep-history HTTP path.
    earliest_funding_ms = int(funding_df.index[0].timestamp() * 1000)
    if not needs_archive:
        mark_df = _fetch_mark_live(
            exchange=exchange,
            ccxt_symbol=ccxt_symbol,
            since_ms=earliest_funding_ms - 3_600_000,
            request_delay_s=request_delay_s,
        )
    else:
        latest_funding_ms = int(funding_df.index[-1].timestamp() * 1000)
        mark_df = fetch_mark_price_history_deep(
            instid=instid,
            start_ms=earliest_funding_ms - 3_600_000,
            end_ms=latest_funding_ms + 3_600_000,
            request_delay_s=request_delay_s,
        )

    # ── 3. Snap-merge mark OPEN onto funding timestamps ─────────────────────
    # Funding settles on the hour boundary (00:00 / 08:00 / 16:00 UTC).
    # OKX mark candles are timestamped at the start of the bar, so the
    # candle indexed at the funding timestamp has `open` == mark price
    # at the settlement instant (= close of the previous candle).
    if mark_df is not None and not mark_df.empty:
        funding_df = funding_df.join(
            mark_df[["open"]].rename(columns={"open": "mark_price"}),
            how="left",
        )
        # Forward/back-fill any settlement that landed on a missing
        # 1h bar (rare).
        funding_df["mark_price"] = funding_df["mark_price"].ffill().bfill()
    else:
        funding_df["mark_price"] = float("nan")

    logger.info(
        f"[okx_funding] {instid}: {len(funding_df)} funding rows, "
        f"mark_rows={0 if mark_df is None else len(mark_df)}"
    )
    return funding_df


# ── Live funding fetch (paginated, ccxt) ─────────────────────────────────────

def _fetch_funding_live(
    *,
    exchange,
    ccxt_symbol: str,
    since_ms: int,
    batch_size: int,
    request_delay_s: float,
) -> pd.DataFrame:
    """Paginated forward-walk fetch via CCXT `fetch_funding_rate_history`.

    Behaviour preserved exactly from the pre-Path-5 implementation
    (so the existing mocked test_paginated_funding_history etc.
    continue to pass).  Returns DataFrame indexed by UTC timestamp
    with a single `funding_rate` column.
    """
    rows: list[dict] = []
    cursor = since_ms
    while True:
        try:
            batch = exchange.fetch_funding_rate_history(
                ccxt_symbol, since=cursor, limit=batch_size,
            )
        except (ccxt.NetworkError, ccxt.ExchangeError) as e:
            logger.error(f"[okx_funding] funding fetch error: {e}")
            raise

        if not batch:
            break

        rows.extend(batch)
        last_ts_ms = batch[-1]["timestamp"]
        new_cursor = last_ts_ms + 1
        if new_cursor == cursor:
            break
        cursor = new_cursor
        if len(batch) < batch_size:
            break
        time.sleep(request_delay_s)

    if not rows:
        return pd.DataFrame(
            columns=["funding_rate"],
            index=pd.DatetimeIndex([], tz="UTC", name="timestamp"),
        )

    df = pd.DataFrame([
        {"timestamp": r["timestamp"], "funding_rate": float(r["fundingRate"])}
        for r in rows
    ])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.set_index("timestamp").sort_index()
    df = df[~df.index.duplicated(keep="last")]
    return df


# ── Live mark fetch (paginated, ccxt) ────────────────────────────────────────

def _fetch_mark_live(
    *,
    exchange,
    ccxt_symbol: str,
    since_ms: int,
    request_delay_s: float,
    batch_size: int = 300,
) -> pd.DataFrame:
    """Paginated forward-walk fetch via CCXT `fetch_mark_ohlcv` 1h.

    Behaviour preserved exactly from the pre-Path-5 implementation.
    Returns DataFrame indexed by UTC timestamp with OHLCV columns.
    """
    mark_rows: list[list] = []
    cursor = since_ms
    while True:
        try:
            batch = exchange.fetch_mark_ohlcv(
                ccxt_symbol, "1h", since=cursor, limit=batch_size,
            )
        except (ccxt.NetworkError, ccxt.ExchangeError) as e:
            logger.error(f"[okx_funding] mark OHLCV fetch error: {e}")
            raise
        if not batch:
            break
        mark_rows.extend(batch)
        last_ts_ms = batch[-1][0]
        new_cursor = last_ts_ms + 1
        if new_cursor == cursor:
            break
        cursor = new_cursor
        if len(batch) < batch_size:
            break
        time.sleep(request_delay_s)

    if not mark_rows:
        return pd.DataFrame(
            columns=["open", "high", "low", "close", "volume"],
            index=pd.DatetimeIndex([], tz="UTC", name="timestamp"),
        )
    mark_df = pd.DataFrame(
        mark_rows,
        columns=["timestamp", "open", "high", "low", "close", "volume"],
    )
    mark_df["timestamp"] = pd.to_datetime(
        mark_df["timestamp"], unit="ms", utc=True,
    )
    mark_df = mark_df.set_index("timestamp").sort_index()
    mark_df = mark_df[~mark_df.index.duplicated(keep="last")]
    return mark_df


# ── Path 5 — archive (per-month .zip) funding fetch ──────────────────────────

def _enumerate_archive_months(
    since_ms: int, until_ms: int,
) -> list[tuple[int, int]]:
    """Return (year, month) tuples covering all months touched by
    [since_ms, until_ms].  Boundary months are included on both ends.
    """
    start = datetime.fromtimestamp(since_ms / 1000, tz=timezone.utc).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0,
    )
    end_dt = datetime.fromtimestamp(until_ms / 1000, tz=timezone.utc)
    out: list[tuple[int, int]] = []
    cur = start
    while cur <= end_dt:
        out.append((cur.year, cur.month))
        # Advance 32 days then snap to the first of the next month.
        cur = (cur + timedelta(days=32)).replace(day=1)
    return out


def fetch_funding_archive_month(
    instid: str, year: int, month: int,
) -> pd.DataFrame:
    """Fetch one monthly funding-rate archive zip from OKX's static
    historical-data CDN.

    Args:
        instid:  OKX `instId` (e.g. "BTC-USDT-SWAP").  Must be the
                 instId form, NOT the manifest "BASE/QUOTE" form.
        year:    Calendar year (e.g. 2024).
        month:   Calendar month, 1-indexed.

    Returns:
        DataFrame indexed by UTC timestamp at each funding
        settlement, single `funding_rate` column (float).  Rows are
        filtered to the requested instid (defensively; the file
        should already be filtered).

    Raises:
        FileNotFoundError: HTTP 404 — month not yet published, or
                           older than the archive's earliest month
                           (March 2022 for BTC-USDT-SWAP per probe
                           2026-05-02).
        RuntimeError:      Other HTTP errors.
        ValueError:        Zip is empty or contains no CSV.

    Caching:
        ARCHIVE_CACHE_DIR / instid / "{yyyy}-{mm}.parquet" — written
        on first fetch; archive months are immutable after
        publication so subsequent calls short-circuit on the parquet.
    """
    cache_path = ARCHIVE_CACHE_DIR / instid / f"{year:04d}-{month:02d}.parquet"
    if cache_path.exists():
        df = pd.read_parquet(cache_path)
        if not isinstance(df.index, pd.DatetimeIndex):
            df.index = pd.to_datetime(df.index, utc=True)
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        return df

    yyyymm = f"{year:04d}{month:02d}"
    yyyy = f"{year:04d}"
    mm = f"{month:02d}"
    url = (
        ARCHIVE_BASE_URL + "/"
        + ARCHIVE_FUNDING_PATH_TEMPLATE.format(
            yyyymm=yyyymm, instid=instid, yyyy=yyyy, mm=mm,
        )
    )
    logger.info(
        f"[okx_funding] archive GET {url}"
    )
    r = requests.get(url, timeout=30, headers={"User-Agent": _HTTP_USER_AGENT})
    if r.status_code == 404:
        raise FileNotFoundError(
            f"OKX funding archive 404 for {instid} {year:04d}-{month:02d} "
            f"({url}).  Either the month is before the archive's earliest "
            "publication (Mar 2022 for BTC-USDT-SWAP) or the current "
            "month is not yet finalized."
        )
    if r.status_code != 200:
        raise RuntimeError(
            f"Archive fetch failed: status={r.status_code} url={url} "
            f"body[:200]={r.text[:200]!r}"
        )

    zf = zipfile.ZipFile(io.BytesIO(r.content))
    csv_name = next((n for n in zf.namelist() if n.endswith(".csv")), None)
    if csv_name is None:
        raise ValueError(
            f"Archive zip contains no CSV: members={zf.namelist()} url={url}"
        )
    with zf.open(csv_name) as fh:
        raw = pd.read_csv(fh)

    expected_cols = {"instrument_name", "funding_rate", "funding_time"}
    missing = expected_cols - set(raw.columns)
    if missing:
        raise ValueError(
            f"Archive CSV missing columns {missing}; got {list(raw.columns)} "
            f"url={url}"
        )

    raw = raw[raw["instrument_name"] == instid].copy()
    df = pd.DataFrame(
        {"funding_rate": raw["funding_rate"].astype(float).values},
        index=pd.to_datetime(raw["funding_time"].astype("int64"),
                             unit="ms", utc=True),
    )
    df.index.name = "timestamp"
    df = df.sort_index()
    df = df[~df.index.duplicated(keep="last")]

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(cache_path)
    return df


def _fetch_funding_archive_window(
    *, instid: str, since_ms: int, until_ms: int,
) -> pd.DataFrame:
    """Fetch all archive months covering [since_ms, until_ms] and
    concatenate.  Missing months (404) are skipped with a warning."""
    frames: list[pd.DataFrame] = []
    for year, month in _enumerate_archive_months(since_ms, until_ms):
        try:
            month_df = fetch_funding_archive_month(instid, year, month)
        except FileNotFoundError as e:
            logger.warning(
                f"[okx_funding] archive miss {instid} {year:04d}-{month:02d}: {e}"
            )
            continue
        frames.append(month_df)

    if not frames:
        return pd.DataFrame(
            columns=["funding_rate"],
            index=pd.DatetimeIndex([], tz="UTC", name="timestamp"),
        )

    combined = pd.concat(frames).sort_index()
    combined = combined[~combined.index.duplicated(keep="last")]
    # Trim to the requested window.  Inclusive start, exclusive end
    # so the live segment owns the cutover boundary timestamp.
    start_ts = pd.Timestamp(since_ms, unit="ms", tz="UTC")
    end_ts = pd.Timestamp(until_ms, unit="ms", tz="UTC")
    combined = combined[(combined.index >= start_ts) & (combined.index < end_ts)]
    return combined


# ── Path 5 — deep mark-price fetch via direct HTTP ───────────────────────────

def fetch_mark_price_history_deep(
    instid: str,
    start_ms: int,
    end_ms: int,
    *,
    request_delay_s: float = DEFAULT_REQUEST_DELAY_S,
) -> pd.DataFrame:
    """Fetch 1H mark-price candles via direct HTTP to OKX's
    deep-history endpoint, walking backward from `end_ms`.

    Why direct HTTP instead of ccxt: ccxt's `fetch_mark_ohlcv`
    routes to `/api/v5/market/mark-price-candles` (recent-only,
    server-capped at ~60 days).  The
    `/api/v5/market/history-mark-price-candles` endpoint serves
    deeper history (verified back to 2022-12-28 for BTC-USDT-SWAP
    on 2026-05-02).

    Args:
        instid:           OKX `instId` (e.g. "BTC-USDT-SWAP").
        start_ms:         Lower bound of the requested window (UTC ms).
        end_ms:           Upper bound of the requested window (UTC ms).
        request_delay_s:  Sleep between requests (rate-limit cushion).

    Returns:
        DataFrame indexed by UTC timestamp, columns [open, close].
        `open` is the mark price at the bar-start timestamp (matches
        the snap-merge contract used by `fetch_funding_history`);
        `close` is preserved alongside for forensic parity.

    Caching:
        Per-month parquet under
        `MARK_PRICE_ARCHIVE_CACHE_DIR / instid / "{yyyy}-{mm}.parquet"`.
        Past complete months are persisted; the current (incomplete)
        month is re-fetched on each call so partial fills update.
    """
    if start_ms >= end_ms:
        return pd.DataFrame(
            columns=["open", "close"],
            index=pd.DatetimeIndex([], tz="UTC", name="timestamp"),
        )

    months = _enumerate_archive_months(start_ms, end_ms)
    cache_dir = MARK_PRICE_ARCHIVE_CACHE_DIR / instid

    now_dt = datetime.now(timezone.utc)
    cur_year, cur_month = now_dt.year, now_dt.month

    cached_frames: list[pd.DataFrame] = []
    needed_months: list[tuple[int, int]] = []
    for (year, month) in months:
        is_current = (year == cur_year and month == cur_month)
        cache_path = cache_dir / f"{year:04d}-{month:02d}.parquet"
        # Past months: use cache if present.  Current month: always
        # re-fetch (don't read the cache, don't write it either).
        if not is_current and cache_path.exists():
            df = pd.read_parquet(cache_path)
            if df.index.tz is None:
                df.index = df.index.tz_localize("UTC")
            cached_frames.append(df)
        else:
            needed_months.append((year, month))

    fetched_df = pd.DataFrame(
        columns=["open", "close"],
        index=pd.DatetimeIndex([], tz="UTC", name="timestamp"),
    )
    if needed_months:
        # Determine the actual fetch range: from the start of the
        # earliest needed month up to end_ms.  This single backward
        # walk amortises HTTP latency over all needed months.
        ny, nm = needed_months[0]
        earliest_needed_dt = datetime(ny, nm, 1, tzinfo=timezone.utc)
        earliest_needed_ms = int(earliest_needed_dt.timestamp() * 1000)
        fetched_df = _http_walk_history_mark_price(
            instid=instid,
            start_ms=max(earliest_needed_ms, start_ms),
            end_ms=end_ms,
            request_delay_s=request_delay_s,
        )

        # Split into per-month slices and persist past months.
        for (year, month) in needed_months:
            month_start = datetime(year, month, 1, tzinfo=timezone.utc)
            if month == 12:
                month_end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
            else:
                month_end = datetime(year, month + 1, 1, tzinfo=timezone.utc)
            mask = (
                (fetched_df.index >= month_start)
                & (fetched_df.index < month_end)
            )
            slice_df = fetched_df.loc[mask]
            if slice_df.empty:
                continue
            cached_frames.append(slice_df)
            if not (year == cur_year and month == cur_month):
                cache_dir.mkdir(parents=True, exist_ok=True)
                slice_df.to_parquet(
                    cache_dir / f"{year:04d}-{month:02d}.parquet"
                )

    if not cached_frames:
        return pd.DataFrame(
            columns=["open", "close"],
            index=pd.DatetimeIndex([], tz="UTC", name="timestamp"),
        )

    combined = pd.concat(cached_frames).sort_index()
    combined = combined[~combined.index.duplicated(keep="last")]
    start_ts = pd.Timestamp(start_ms, unit="ms", tz="UTC")
    end_ts = pd.Timestamp(end_ms, unit="ms", tz="UTC")
    combined = combined[(combined.index >= start_ts) & (combined.index <= end_ts)]
    return combined


def _http_walk_history_mark_price(
    *,
    instid: str,
    start_ms: int,
    end_ms: int,
    request_delay_s: float,
) -> pd.DataFrame:
    """Backward HTTP walk against /api/v5/market/history-mark-price-candles.

    OKX's `after` semantics: returns rows STRICTLY OLDER than the
    supplied timestamp (so to inclusively include the last bar
    before `end_ms`, we pass `after=end_ms + 1ms` on the first call,
    then advance with `after=oldest_in_batch` each iteration).

    Returns DataFrame indexed by UTC timestamp with [open, close]
    float columns.  Includes rows with `index >= start_ts` only.
    """
    rows: list[list] = []
    cursor_ms = end_ms + 1
    headers = {"User-Agent": _HTTP_USER_AGENT}
    iters = 0
    while iters < 1000:
        params = {
            "instId": instid,
            "bar": "1H",
            "after": str(cursor_ms),
            "limit": str(MARK_PRICE_HISTORY_BATCH_SIZE),
        }
        try:
            r = requests.get(
                MARK_PRICE_HISTORY_URL, params=params, timeout=15, headers=headers,
            )
        except requests.RequestException as e:
            logger.error(f"[okx_funding] mark-history HTTP error: {e}")
            raise
        iters += 1
        body = r.json()
        if str(body.get("code", "")) != "0":
            raise RuntimeError(
                f"OKX mark-history error code={body.get('code')} "
                f"msg={body.get('msg')!r} url={r.url}"
            )
        data = body.get("data") or []
        if not data:
            break
        rows.extend(data)
        timestamps = [int(d[0]) for d in data]
        oldest_in_batch = min(timestamps)
        if oldest_in_batch < start_ms:
            break
        if oldest_in_batch >= cursor_ms:
            # Cursor stuck — bail to avoid infinite loop.
            break
        cursor_ms = oldest_in_batch
        time.sleep(request_delay_s)

    if not rows:
        return pd.DataFrame(
            columns=["open", "close"],
            index=pd.DatetimeIndex([], tz="UTC", name="timestamp"),
        )

    # OKX history-mark-price-candles row shape:
    # [ts, open, high, low, close, confirm]
    df = pd.DataFrame(
        rows, columns=["timestamp", "open", "high", "low", "close", "confirm"],
    )
    df["timestamp"] = pd.to_datetime(
        df["timestamp"].astype("int64"), unit="ms", utc=True,
    )
    df = df.set_index("timestamp").sort_index()
    df = df[~df.index.duplicated(keep="last")]
    df["open"] = df["open"].astype(float)
    df["close"] = df["close"].astype(float)

    # Filter to [start_ms, end_ms] inclusive.
    start_ts = pd.Timestamp(start_ms, unit="ms", tz="UTC")
    end_ts = pd.Timestamp(end_ms, unit="ms", tz="UTC")
    df = df[(df.index >= start_ts) & (df.index <= end_ts)]
    return df[["open", "close"]]


# ── Concat + dedupe helper ───────────────────────────────────────────────────

def _concat_dedup_last_wins(frames: list[pd.DataFrame]) -> pd.DataFrame:
    """Concatenate `frames` in order and dedupe the index keeping the
    LAST occurrence — so for overlap regions the live-side (later in
    `frames`) values win over archive-side values."""
    non_empty = [f for f in frames if f is not None and not f.empty]
    if not non_empty:
        return pd.DataFrame(
            columns=["funding_rate"],
            index=pd.DatetimeIndex([], tz="UTC", name="timestamp"),
        )
    combined = pd.concat(non_empty)
    combined = combined.sort_index(kind="stable")
    combined = combined[~combined.index.duplicated(keep="last")]
    return combined


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
