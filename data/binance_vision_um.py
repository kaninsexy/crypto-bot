"""Binance USDT-M (UM) perpetual public-archive data layer.

Free public futures data from https://data.binance.vision
(official repository: github.com/binance/binance-public-data, MIT
licence).  No API key, no subscription.  This is the substrate for the
2026-09 revival Track C work (docs/research_revival_2026-09.md §C.3):
cross-sectional perp research needs funding, open interest and
long/short ratios for the *whole* UM universe including delisted
symbols, which no live REST endpoint provides.

Cross-venue provenance: research data is Binance USDT-M perpetuals;
the execution venue remains OKX.  Any strategy validated on this
substrate carries the same cross-venue disclosure discipline as the
2026-06-11 BNB backfill (see docs/bot_status.md).

HOLDOUT ENFORCEMENT — read this before using the module
-------------------------------------------------------
This module is a RAW ARCHIVE LOADER.  It deliberately contains no code
path that reads ``backtest.holdout`` and no code path that bypasses the
``backtest/cache.py`` enforcement machinery — it simply does not go
through it.  Holdout enforcement is applied by CALLERS later (via a
``holdout_manifest.json`` entry for the perp substrate and reads routed
through ``backtest/holdout.py``), not here.  Until such a manifest entry
exists, use this module for data-layer development and feature unit
tests only, never for trials.  Every fetcher accepts ``until=`` so a
caller can cap the returned range at its dev cutoff.

Archive layout
--------------
- monthly klines
  data/futures/um/monthly/klines/{SYM}/{ITV}/{SYM}-{ITV}-{YYYY-MM}.zip
  12-column futures kline CSV (see ``_KLINE_COLUMNS``); some months
  ship a header row, some do not; ``open_time`` is milliseconds for
  every month checked so far but microsecond files exist elsewhere in
  the archive, so the unit is detected by magnitude.
- monthly funding
  data/futures/um/monthly/fundingRate/{SYM}/{SYM}-fundingRate-{YYYY-MM}.zip
  columns: calc_time (ms), funding_interval_hours, last_funding_rate.
- daily metrics (5-minute rows)
  data/futures/um/daily/metrics/{SYM}/{SYM}-metrics-{YYYY-MM-DD}.zip
  columns: create_time, symbol, sum_open_interest,
  sum_open_interest_value, count_toptrader_long_short_ratio,
  sum_toptrader_long_short_ratio, count_long_short_ratio,
  sum_taker_long_short_vol_ratio.
- The UM ``liquidationSnapshot`` prefix is EMPTY (verified 2026-09-02);
  it is intentionally not implemented.

Public API
----------
list_prefixes(prefix)                 -> list[str]
list_keys(prefix)                     -> list[str]
list_symbols()                        -> list[str]
universe_table(force=False)           -> DataFrame[symbol, first_month,
                                                   last_month, delisted]
fetch_klines(symbol, interval, start, end, until=None)  -> DataFrame
fetch_funding(symbol, start, end, until=None)           -> DataFrame
fetch_metrics(symbol, start_date, end_date, ...)        -> DataFrame
resample_metrics(df, freq)                              -> DataFrame
month_range(start, end)               -> list[str]
day_range(start, end)                 -> list[str]
"""

from __future__ import annotations

import io
import json
import re
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Optional

import pandas as pd
import requests
from loguru import logger

BASE = "https://data.binance.vision/data/futures/um"
S3_ENDPOINT = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
S3_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"

_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CACHE_DIR = _REPO_ROOT / "backtest" / "cache" / "binance_um"

KLINES_PREFIX = "data/futures/um/monthly/klines/"
FUNDING_PREFIX = "data/futures/um/monthly/fundingRate/"
METRICS_PREFIX = "data/futures/um/daily/metrics/"

SUPPORTED_INTERVALS = ("1h", "1d")

_HTTP_TIMEOUT = 60          # seconds per request
_RETRIES = 3                # attempts per URL on network/5xx failure
_RETRY_BACKOFF = 2.0        # seconds, doubled per attempt
_LIST_SLEEP = 0.05          # politeness sleep between listing calls
_LIST_PROGRESS_EVERY = 100  # symbols per progress log line

# ms epochs are ~1.6e12 in the 2020s; µs epochs ~1.6e15.
_US_THRESHOLD = 10 ** 14

_KLINE_COLUMNS = [
    "open_time", "open", "high", "low", "close", "volume",
    "close_time", "quote_volume", "count",
    "taker_buy_volume", "taker_buy_quote_volume", "ignore",
]
_KLINE_KEEP = [
    "open", "high", "low", "close", "volume",
    "quote_volume", "count", "taker_buy_volume", "taker_buy_quote_volume",
]

_FUNDING_COLUMNS = ["calc_time", "funding_interval_hours", "last_funding_rate"]
_FUNDING_KEEP = ["funding_interval_hours", "last_funding_rate"]

_METRICS_COLUMNS = [
    "create_time", "symbol", "sum_open_interest", "sum_open_interest_value",
    "count_toptrader_long_short_ratio", "sum_toptrader_long_short_ratio",
    "count_long_short_ratio", "sum_taker_long_short_vol_ratio",
]
_METRICS_KEEP = _METRICS_COLUMNS[2:]

_MONTH_RE = re.compile(r"-(\d{4}-\d{2})\.zip$")


class BinanceUMError(RuntimeError):
    """Download or parse failure that is NOT a plain 404."""


# ────────────────────────────────────────────────────────── helpers ──

def normalise_symbol(symbol: str) -> str:
    return symbol.upper().replace("/", "").replace("-", "").replace(":", "")


def month_range(start: str, end: str) -> list:
    """Inclusive list of 'YYYY-MM' strings."""
    cur = pd.Period(str(start)[:7], freq="M")
    last = pd.Period(str(end)[:7], freq="M")
    if cur > last:
        raise ValueError(f"start {start} > end {end}")
    out = []
    while cur <= last:
        out.append(str(cur))
        cur += 1
    return out


def day_range(start: str, end: str) -> list:
    """Inclusive list of 'YYYY-MM-DD' strings."""
    first = pd.Timestamp(str(start)).normalize()
    last = pd.Timestamp(str(end)).normalize()
    if first > last:
        raise ValueError(f"start {start} > end {end}")
    return [d.strftime("%Y-%m-%d") for d in pd.date_range(first, last, freq="D")]


def _current_month() -> pd.Period:
    return pd.Period(pd.Timestamp.now(tz="UTC").tz_localize(None), freq="M")


def _get(url: str, session: Optional[requests.Session] = None):
    """GET with retries. Returns the Response, or None on 404.

    Retries transient network errors and 5xx responses ``_RETRIES``
    times with exponential backoff; a 404 is an expected outcome
    (symbol not listed that month) and returns None immediately.
    """
    http = session if session is not None else requests
    delay = _RETRY_BACKOFF
    last_err = None
    for attempt in range(1, _RETRIES + 1):
        try:
            resp = http.get(url, timeout=_HTTP_TIMEOUT)
        except Exception as exc:  # network-level failure
            last_err = exc
            logger.warning(
                "binance_um: attempt {}/{} failed for {}: {}",
                attempt, _RETRIES, url, exc,
            )
        else:
            if resp.status_code == 404:
                return None
            if resp.status_code == 200:
                return resp
            last_err = BinanceUMError(f"HTTP {resp.status_code} for {url}")
            if resp.status_code < 500:
                raise last_err
            logger.warning(
                "binance_um: attempt {}/{} got HTTP {} for {}",
                attempt, _RETRIES, resp.status_code, url,
            )
        if attempt < _RETRIES:
            time.sleep(delay)
            delay *= 2
    raise BinanceUMError(f"failed after {_RETRIES} attempts: {url} ({last_err})")


def _download_zip(url: str, session: Optional[requests.Session] = None):
    """Download a zip and return the bytes of its single member.

    None on 404.  Monkeypatched wholesale by the offline test-suite.
    """
    resp = _get(url, session=session)
    if resp is None:
        return None
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        return zf.read(zf.namelist()[0])


def _strip_header(df: pd.DataFrame, names: list) -> pd.DataFrame:
    """Drop a leading header row if the archive member shipped one."""
    if len(df) == 0:
        return df
    first = str(df.iloc[0][names[0]]).strip()
    if first == names[0]:
        return df.iloc[1:].reset_index(drop=True)
    return df


def _normalise_epoch(series: pd.Series) -> pd.Series:
    """Return epoch-milliseconds regardless of ms/µs source unit."""
    s = pd.to_numeric(series, errors="raise").astype("int64")
    return s.where(s < _US_THRESHOLD, s // 1000)


def _apply_until(df: pd.DataFrame, until) -> pd.DataFrame:
    """Drop rows at or after ``until`` (UTC)."""
    if until is None or len(df) == 0:
        return df
    ts = pd.Timestamp(until)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return df.loc[df.index < ts]


def _merge_cache(existing: Optional[pd.DataFrame], new: pd.DataFrame) -> pd.DataFrame:
    """Idempotent concat: sort by index, keep first of duplicates."""
    frames = [f for f in (existing, new) if f is not None and len(f) > 0]
    if len(frames) == 0:
        return new
    out = pd.concat(frames).sort_index()
    return out[~out.index.duplicated(keep="first")]


def _read_parquet_if_exists(path: Path):
    if not path.exists():
        return None
    try:
        return pd.read_parquet(path)
    except Exception as exc:  # corrupt cache -> redownload
        logger.warning("binance_um: unreadable cache {} ({}); ignoring", path, exc)
        return None


def _load_missing(path: Path) -> set:
    if not path.exists():
        return set()
    try:
        return set(json.loads(path.read_text()))
    except Exception:
        return set()


def _save_missing(path: Path, missing: set) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(missing)))


# ───────────────────────────────────────────────────────── listing ──

def _list_page(prefix: str, marker: str = "", session=None):
    """One S3 listing page -> (common_prefixes, keys, truncated, next_marker)."""
    url = (
        f"{S3_ENDPOINT}?delimiter=/&prefix={prefix}&max-keys=1000"
        + (f"&marker={marker}" if marker else "")
    )
    resp = _get(url, session=session)
    if resp is None:
        return [], [], False, ""
    root = ET.fromstring(resp.content)
    prefixes = [
        child.text
        for node in root.iter(f"{S3_NS}CommonPrefixes")
        for child in node.iter(f"{S3_NS}Prefix")
        if child.text
    ]
    keys = [
        child.text
        for node in root.iter(f"{S3_NS}Contents")
        for child in node.iter(f"{S3_NS}Key")
        if child.text
    ]
    trunc_el = root.find(f"{S3_NS}IsTruncated")
    truncated = trunc_el is not None and str(trunc_el.text).lower() == "true"
    nm_el = root.find(f"{S3_NS}NextMarker")
    next_marker = nm_el.text if (nm_el is not None and nm_el.text) else ""
    if truncated and not next_marker:
        # Fall back to the last key/prefix seen, per the S3 GET Bucket contract.
        candidates = keys + prefixes
        next_marker = candidates[-1] if candidates else ""
    return prefixes, keys, truncated, next_marker


def list_prefixes(prefix: str, session=None) -> list:
    """All CommonPrefixes directly under ``prefix`` (following pagination)."""
    out, marker = [], ""
    while True:
        prefixes, keys, truncated, marker = _list_page(prefix, marker, session=session)
        out.extend(prefixes)
        if not truncated or not marker:
            break
    return out


def list_keys(prefix: str, session=None) -> list:
    """All object Keys directly under ``prefix`` (following pagination)."""
    out, marker = [], ""
    while True:
        prefixes, keys, truncated, marker = _list_page(prefix, marker, session=session)
        out.extend(keys)
        if not truncated or not marker:
            break
    return out


def list_symbols(session=None) -> list:
    """Every UM symbol that has monthly klines in the archive."""
    prefixes = list_prefixes(KLINES_PREFIX, session=session)
    return sorted(p.rstrip("/").rsplit("/", 1)[-1] for p in prefixes)


# ──────────────────────────────────────────────────────── universe ──

def _months_from_keys(keys) -> list:
    months = []
    for key in keys:
        m = _MONTH_RE.search(key)
        if m is not None:
            months.append(m.group(1))
    return sorted(set(months))


def universe_table(
    force: bool = False,
    cache_dir=DEFAULT_CACHE_DIR,
    symbols=None,
    session=None,
) -> pd.DataFrame:
    """Per-symbol listing/delisting table built from the 1d kline keys.

    Columns: symbol, first_month, last_month, delisted.  ``delisted`` is
    True when ``last_month`` is older than the previous calendar month
    (i.e. the symbol stopped producing monthly archives).

    ~990 listing calls on a cold run; cached to
    ``backtest/cache/binance_um/universe.parquet``.
    """
    cache_path = Path(cache_dir) / "universe.parquet"
    if not force:
        cached = _read_parquet_if_exists(cache_path)
        if cached is not None:
            return cached

    syms = list(symbols) if symbols is not None else list_symbols(session=session)
    logger.info("binance_um: building universe table for {} symbols", len(syms))

    rows = []
    for i, sym in enumerate(syms, start=1):
        keys = list_keys(f"{KLINES_PREFIX}{sym}/1d/", session=session)
        months = _months_from_keys(keys)
        if len(months) == 0:
            logger.warning("binance_um: no 1d kline months for {}", sym)
            continue
        rows.append({
            "symbol": sym,
            "first_month": months[0],
            "last_month": months[-1],
        })
        if i % _LIST_PROGRESS_EVERY == 0:
            logger.info("binance_um: universe scan {}/{}", i, len(syms))
        time.sleep(_LIST_SLEEP)

    df = pd.DataFrame(rows, columns=["symbol", "first_month", "last_month"])
    cutoff = _current_month() - 1
    if len(df) > 0:
        df["delisted"] = df["last_month"].map(lambda m: pd.Period(m, freq="M") < cutoff)
    else:
        df["delisted"] = pd.Series(dtype=bool)
    df = df.sort_values("symbol").reset_index(drop=True)

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(cache_path)
    logger.info(
        "binance_um: universe table {} symbols ({} delisted) -> {}",
        len(df), int(df["delisted"].sum()) if len(df) > 0 else 0, cache_path,
    )
    return df


# ────────────────────────────────────────────────────────── klines ──

def parse_klines_csv(raw: bytes) -> pd.DataFrame:
    """Parse one futures kline CSV member (header optional, ms or µs)."""
    df = pd.read_csv(io.BytesIO(raw), header=None, names=_KLINE_COLUMNS)
    df = _strip_header(df, _KLINE_COLUMNS)
    if len(df) == 0:
        return pd.DataFrame(
            columns=_KLINE_KEEP,
            index=pd.DatetimeIndex([], tz="UTC", name="ts"),
        ).astype("float64")
    ts = pd.to_datetime(_normalise_epoch(df["open_time"]), unit="ms", utc=True)
    out = df[_KLINE_KEEP].apply(pd.to_numeric, errors="raise").astype("float64")
    out.index = pd.DatetimeIndex(ts, name="ts")
    out = out.sort_index()
    return out[~out.index.duplicated(keep="first")]


def fetch_klines(
    symbol: str,
    interval: str,
    start,
    end,
    until=None,
    cache_dir=DEFAULT_CACHE_DIR,
    force: bool = False,
    session=None,
) -> pd.DataFrame:
    """Monthly UM klines for ``symbol`` over [start, end] (YYYY-MM inclusive).

    Missing months (404) are skipped with a warning, not an error.
    Cached to ``klines/{SYM}_{ITV}.parquet``; re-runs merge new months
    idempotently (dedupe on index).  Returns an empty frame if the
    symbol has no data in the window.
    """
    if interval not in SUPPORTED_INTERVALS:
        raise ValueError(
            f"interval {interval!r} not in {SUPPORTED_INTERVALS}"
        )
    sym = normalise_symbol(symbol)
    months = month_range(start, end)

    cache_path = Path(cache_dir) / "klines" / f"{sym}_{interval}.parquet"
    miss_path = cache_path.with_suffix(".missing.json")
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    cached = None if force else _read_parquet_if_exists(cache_path)
    missing = set() if force else _load_missing(miss_path)
    have = set()
    if cached is not None and len(cached) > 0:
        have = set(cached.index.strftime("%Y-%m"))

    new_frames = []
    for month in months:
        if month in have or month in missing:
            continue
        url = f"{BASE}/monthly/klines/{sym}/{interval}/{sym}-{interval}-{month}.zip"
        raw = _download_zip(url, session=session)
        if raw is None:
            logger.warning("binance_um: no kline archive {} {} {}", sym, interval, month)
            missing.add(month)
            continue
        new_frames.append(parse_klines_csv(raw))

    if len(new_frames) > 0:
        merged = _merge_cache(cached, pd.concat(new_frames).sort_index())
        merged.to_parquet(cache_path)
        cached = merged
    _save_missing(miss_path, missing)

    if cached is None or len(cached) == 0:
        return pd.DataFrame(
            columns=_KLINE_KEEP,
            index=pd.DatetimeIndex([], tz="UTC", name="ts"),
        ).astype("float64")

    lo = pd.Period(months[0], freq="M").start_time.tz_localize("UTC")
    hi = (pd.Period(months[-1], freq="M") + 1).start_time.tz_localize("UTC")
    out = cached.loc[(cached.index >= lo) & (cached.index < hi)]
    return _apply_until(out, until)


# ───────────────────────────────────────────────────────── funding ──

def parse_funding_csv(raw: bytes) -> pd.DataFrame:
    """Parse one fundingRate CSV member -> UTC-indexed settlement frame."""
    df = pd.read_csv(io.BytesIO(raw), header=None, names=_FUNDING_COLUMNS)
    df = _strip_header(df, _FUNDING_COLUMNS)
    if len(df) == 0:
        return pd.DataFrame(
            columns=_FUNDING_KEEP,
            index=pd.DatetimeIndex([], tz="UTC", name="ts"),
        ).astype("float64")
    ts = pd.to_datetime(_normalise_epoch(df["calc_time"]), unit="ms", utc=True)
    out = df[_FUNDING_KEEP].apply(pd.to_numeric, errors="raise").astype("float64")
    out.index = pd.DatetimeIndex(ts, name="ts")
    out = out.sort_index()
    return out[~out.index.duplicated(keep="first")]


def fetch_funding(
    symbol: str,
    start,
    end,
    until=None,
    cache_dir=DEFAULT_CACHE_DIR,
    force: bool = False,
    session=None,
) -> pd.DataFrame:
    """Monthly UM funding-rate history over [start, end] (YYYY-MM inclusive)."""
    sym = normalise_symbol(symbol)
    months = month_range(start, end)

    cache_path = Path(cache_dir) / "funding" / f"{sym}.parquet"
    miss_path = cache_path.with_suffix(".missing.json")
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    cached = None if force else _read_parquet_if_exists(cache_path)
    missing = set() if force else _load_missing(miss_path)
    have = set()
    if cached is not None and len(cached) > 0:
        have = set(cached.index.strftime("%Y-%m"))

    new_frames = []
    for month in months:
        if month in have or month in missing:
            continue
        url = f"{BASE}/monthly/fundingRate/{sym}/{sym}-fundingRate-{month}.zip"
        raw = _download_zip(url, session=session)
        if raw is None:
            logger.warning("binance_um: no funding archive {} {}", sym, month)
            missing.add(month)
            continue
        new_frames.append(parse_funding_csv(raw))

    if len(new_frames) > 0:
        merged = _merge_cache(cached, pd.concat(new_frames).sort_index())
        merged.to_parquet(cache_path)
        cached = merged
    _save_missing(miss_path, missing)

    if cached is None or len(cached) == 0:
        return pd.DataFrame(
            columns=_FUNDING_KEEP,
            index=pd.DatetimeIndex([], tz="UTC", name="ts"),
        ).astype("float64")

    lo = pd.Period(months[0], freq="M").start_time.tz_localize("UTC")
    hi = (pd.Period(months[-1], freq="M") + 1).start_time.tz_localize("UTC")
    out = cached.loc[(cached.index >= lo) & (cached.index < hi)]
    return _apply_until(out, until)


# ───────────────────────────────────────────────────────── metrics ──

def parse_metrics_csv(raw: bytes) -> pd.DataFrame:
    """Parse one daily metrics CSV member -> UTC-indexed 5-minute frame."""
    df = pd.read_csv(io.BytesIO(raw), header=None, names=_METRICS_COLUMNS)
    df = _strip_header(df, _METRICS_COLUMNS)
    if len(df) == 0:
        return pd.DataFrame(
            columns=_METRICS_KEEP,
            index=pd.DatetimeIndex([], tz="UTC", name="ts"),
        ).astype("float64")
    ts = pd.to_datetime(df["create_time"], utc=True)
    out = df[_METRICS_KEEP].apply(pd.to_numeric, errors="coerce").astype("float64")
    out.index = pd.DatetimeIndex(ts, name="ts")
    out = out.sort_index()
    return out[~out.index.duplicated(keep="first")]


def resample_metrics(df: pd.DataFrame, freq: str = "1h") -> pd.DataFrame:
    """Downsample the raw 5-minute metrics to 1h or 1d (last value per bin)."""
    if len(df) == 0:
        return df
    return df.resample(freq).last().dropna(how="all")


def fetch_metrics(
    symbol: str,
    start_date,
    end_date,
    until=None,
    max_days: int = 400,
    cache_dir=DEFAULT_CACHE_DIR,
    force: bool = False,
    session=None,
) -> pd.DataFrame:
    """Daily UM metrics archives (5-minute OI / long-short / taker ratios).

    One zip per DAY, so ``max_days`` caps how many *new* days a single
    call will download (days already cached do not count against it).
    Raw 5-minute rows are cached to ``metrics_5m/{SYM}.parquet``; use
    ``resample_metrics`` for 1h/1d series.
    """
    sym = normalise_symbol(symbol)
    days = day_range(start_date, end_date)

    cache_path = Path(cache_dir) / "metrics_5m" / f"{sym}.parquet"
    miss_path = cache_path.with_suffix(".missing.json")
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    cached = None if force else _read_parquet_if_exists(cache_path)
    missing = set() if force else _load_missing(miss_path)
    have = set()
    if cached is not None and len(cached) > 0:
        have = set(cached.index.strftime("%Y-%m-%d"))

    todo = [d for d in days if d not in have and d not in missing]
    if len(todo) > max_days:
        logger.warning(
            "binance_um: {} metrics days requested for {}, capped at max_days={}",
            len(todo), sym, max_days,
        )
        todo = todo[:max_days]

    new_frames = []
    for i, day in enumerate(todo, start=1):
        url = f"{BASE}/daily/metrics/{sym}/{sym}-metrics-{day}.zip"
        raw = _download_zip(url, session=session)
        if raw is None:
            logger.warning("binance_um: no metrics archive {} {}", sym, day)
            missing.add(day)
            continue
        new_frames.append(parse_metrics_csv(raw))
        if i % _LIST_PROGRESS_EVERY == 0:
            logger.info("binance_um: metrics {} {}/{} days", sym, i, len(todo))

    if len(new_frames) > 0:
        merged = _merge_cache(cached, pd.concat(new_frames).sort_index())
        merged.to_parquet(cache_path)
        cached = merged
    _save_missing(miss_path, missing)

    if cached is None or len(cached) == 0:
        return pd.DataFrame(
            columns=_METRICS_KEEP,
            index=pd.DatetimeIndex([], tz="UTC", name="ts"),
        ).astype("float64")

    lo = pd.Timestamp(days[0]).tz_localize("UTC")
    hi = pd.Timestamp(days[-1]).tz_localize("UTC") + pd.Timedelta(days=1)
    out = cached.loc[(cached.index >= lo) & (cached.index < hi)]
    return _apply_until(out, until)
