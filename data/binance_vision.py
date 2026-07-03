"""Binance Vision bulk historical data ingestion (Phase 4.E substrate).

Free public market data from https://data.binance.vision (official
repository: github.com/binance/binance-public-data, MIT licence).
No API key, no subscription. Used as the research substrate for the
Phase 4.E microstructure batch because Binance spot klines carry the
taker buy/sell aggressor split ("taker buy base asset volume") that
OKX candles lack.

Cross-venue provenance: research data is Binance spot; the execution
venue remains OKX. Any strategy validated on this substrate carries
the same cross-venue disclosure discipline as the 2026-06-11 BNB
backfill (see docs/bot_status.md).

Layout notes
------------
- Monthly zip per (symbol, interval, month):
  https://data.binance.vision/data/spot/monthly/klines/{SYM}/{ITV}/{SYM}-{ITV}-{YYYY-MM}.zip
- Each zip holds one CSV, no header row, 12 columns (see _COLUMNS).
- SPOT timestamps are MILLISECONDS before 2025-01 and MICROSECONDS
  from 2025-01 onwards (per the official README). Normalised here.
- Each zip has a sibling .CHECKSUM file (sha256) — verified on
  download unless verify_checksum=False.
- Parsed months are cached as parquet under
  backtest/cache/binance_vision/{SYM}/{ITV}/{YYYY-MM}.parquet.
  A 404 (symbol not listed yet that month) is cached as an empty
  parquet so we don't re-hit the server.

Public API
----------
load_klines(symbol, start_month, end_month, interval="1m")
    -> pd.DataFrame indexed by UTC open time with columns:
       open, high, low, close, volume, quote_volume, n_trades,
       taker_buy_base, taker_buy_quote
month_range(start_month, end_month) -> list[str]  ("YYYY-MM")

This module is INGESTION ONLY. It never touches
backtest/holdout_manifest.json and performs no dev/holdout
enforcement — enforcement happens when a manifest entry for the
microstructure substrate is created (Phase 4.E sequencing step 3)
and reads go through backtest/holdout.py like every other substrate.
Until then, use for data-layer development and feature unit tests
only, not for trials.
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path

import pandas as pd
import requests

BASE_URL = "https://data.binance.vision/data/spot/monthly/klines"

_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CACHE_DIR = _REPO_ROOT / "backtest" / "cache" / "binance_vision"

_COLUMNS = [
    "open_time", "open", "high", "low", "close", "volume",
    "close_time", "quote_volume", "n_trades",
    "taker_buy_base", "taker_buy_quote", "ignore",
]
_KEEP = [
    "open", "high", "low", "close", "volume",
    "quote_volume", "n_trades", "taker_buy_base", "taker_buy_quote",
]

# Timestamps: ms epochs are ~1.6e12 in the 2020s; µs epochs ~1.6e15.
_US_THRESHOLD = 10 ** 14

_HTTP_TIMEOUT = 60  # seconds per request


class BinanceVisionError(RuntimeError):
    """Download or integrity failure that is NOT a plain 404."""


def month_range(start_month: str, end_month: str) -> list[str]:
    """Inclusive list of 'YYYY-MM' strings from start to end."""
    out = []
    cur = pd.Period(start_month, freq="M")
    end = pd.Period(end_month, freq="M")
    if cur > end:
        raise ValueError(f"start_month {start_month} > end_month {end_month}")
    while cur <= end:
        out.append(str(cur))
        cur += 1
    return out


def _normalise_epoch(series: pd.Series) -> pd.Series:
    """Return epoch-milliseconds regardless of ms/µs source unit."""
    s = series.astype("int64")
    return s.where(s < _US_THRESHOLD, s // 1000)


def _parse_csv(raw: bytes) -> pd.DataFrame:
    df = pd.read_csv(io.BytesIO(raw), header=None, names=_COLUMNS)
    # Some files ship a header row; drop it if the first cell isn't numeric.
    if len(df):
        try:
            float(df.iloc[0]["open_time"])
        except (TypeError, ValueError):
            df = df.iloc[1:].reset_index(drop=True)
    ts = pd.to_datetime(_normalise_epoch(df["open_time"]), unit="ms", utc=True)
    out = df[_KEEP].apply(pd.to_numeric, errors="raise")
    out.index = pd.DatetimeIndex(ts, name="ts")
    return out


def _checksum_ok(payload: bytes, checksum_text: str) -> bool:
    expected = checksum_text.strip().split()[0].lower()
    return hashlib.sha256(payload).hexdigest() == expected


def download_month(
    symbol: str,
    month: str,
    interval: str = "1m",
    verify_checksum: bool = True,
    session: requests.Session | None = None,
) -> pd.DataFrame | None:
    """Download+parse one monthly zip. None on 404 (not listed yet)."""
    sym = symbol.upper().replace("/", "").replace("-", "")
    url = f"{BASE_URL}/{sym}/{interval}/{sym}-{interval}-{month}.zip"
    http = session or requests
    resp = http.get(url, timeout=_HTTP_TIMEOUT)
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        raise BinanceVisionError(f"HTTP {resp.status_code} for {url}")
    payload = resp.content
    if verify_checksum:
        c = http.get(url + ".CHECKSUM", timeout=_HTTP_TIMEOUT)
        if c.status_code == 200 and not _checksum_ok(payload, c.text):
            raise BinanceVisionError(f"sha256 mismatch for {url}")
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        raw = zf.read(zf.namelist()[0])
    return _parse_csv(raw)


def load_klines(
    symbol: str,
    start_month: str,
    end_month: str,
    interval: str = "1m",
    cache_dir: Path | str = DEFAULT_CACHE_DIR,
    verify_checksum: bool = True,
) -> pd.DataFrame:
    """Load klines across months, downloading + caching as needed.

    Returns a UTC-indexed DataFrame (may span months where the symbol
    was not yet listed — those months simply contribute no rows).
    Raises BinanceVisionError if NO month in the range has data.
    """
    sym = symbol.upper().replace("/", "").replace("-", "")
    cache_root = Path(cache_dir) / sym / interval
    cache_root.mkdir(parents=True, exist_ok=True)

    frames: list[pd.DataFrame] = []
    session = requests.Session()
    for month in month_range(start_month, end_month):
        pq = cache_root / f"{month}.parquet"
        if pq.exists():
            df = pd.read_parquet(pq)
        else:
            df = download_month(
                sym, month, interval,
                verify_checksum=verify_checksum, session=session,
            )
            if df is None:
                df = pd.DataFrame(columns=_KEEP)  # negative-cache a 404
            df.to_parquet(pq)
        if len(df):
            frames.append(df)

    if not frames:
        raise BinanceVisionError(
            f"no data for {sym} {interval} in {start_month}..{end_month}"
        )
    out = pd.concat(frames).sort_index()
    out = out[~out.index.duplicated(keep="first")]
    return out
