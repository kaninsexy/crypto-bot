"""data/fear_greed.py -- Crypto Fear & Greed Index fetcher with parquet cache.

Substitute for data/lunarcrush.py after sq-002 hit HTTP 402 (LunarCrush
Individual subscription required, $72/month). The Crypto Fear & Greed
Index from alternative.me is free, requires no API key, and provides
daily data from 2018-02-01 onward -- a longer history than LunarCrush
exposes on the public tier.

Public API:
    GET https://api.alternative.me/fng/?limit=0&format=json
    No headers required.

Returns a DataFrame with columns:
    fear_greed_value (int, 0-100; 0 = Extreme Fear, 100 = Extreme Greed)
    fear_greed_label (str, e.g. "Fear", "Neutral", "Greed", "Extreme Greed")
indexed by UTC-aware daily timestamp, sorted ascending.

Cache pattern mirrors data/lunarcrush.py: 24h TTL, parquet on disk
under backtest/cache/fear_greed_raw.parquet. Corrupted cache files
trigger a re-fetch with a warning.

Output is ASCII-only (Windows cp1252 compatible).

Citation: alternative.me Crypto Fear & Greed Index documentation,
https://alternative.me/crypto/fear-and-greed-index/
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

import pandas as pd
import requests
from loguru import logger


FEAR_GREED_API_URL = "https://api.alternative.me/fng/"
DEFAULT_CACHE_PATH = Path("backtest/cache/fear_greed_raw.parquet")
DEFAULT_TTL_HOURS = 24
DEFAULT_TIMEOUT_S = 30


class FearGreedError(RuntimeError):
    """alternative.me Fear & Greed API or cache read/write failure."""


def _read_cache(path: Path) -> Optional[pd.DataFrame]:
    if not path.exists():
        return None
    try:
        return pd.read_parquet(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"[fear_greed] failed to read cache {path}: {exc}; will re-fetch"
        )
        return None


def _write_cache(path: Path, df: pd.DataFrame) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"[fear_greed] failed to write cache {path}: {exc}"
        )


def _is_fresh(path: Path, ttl_hours: int) -> bool:
    if not path.exists():
        return False
    age_seconds = time.time() - path.stat().st_mtime
    return age_seconds < ttl_hours * 3600


def _fetch_remote() -> pd.DataFrame:
    """Pull the full Fear & Greed Index time series and return a
    DataFrame indexed by UTC-aware daily timestamp with columns
    fear_greed_value (int) and fear_greed_label (str), sorted ascending.

    `limit=0` means "all history" per the alternative.me API spec.
    """
    params = {"limit": "0", "format": "json"}
    logger.info(f"[fear_greed] GET {FEAR_GREED_API_URL} params={params}")
    try:
        resp = requests.get(
            FEAR_GREED_API_URL, params=params, timeout=DEFAULT_TIMEOUT_S,
        )
    except requests.RequestException as exc:
        raise FearGreedError(
            f"alternative.me request failed: {exc}"
        ) from exc
    if resp.status_code != 200:
        raise FearGreedError(
            f"alternative.me returned HTTP {resp.status_code}: "
            f"{resp.text[:200]}"
        )

    try:
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise FearGreedError(
            f"alternative.me JSON parse error: {exc}"
        ) from exc

    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or len(rows) == 0:
        raise FearGreedError(
            "alternative.me payload missing 'data' array or empty"
        )

    df = pd.DataFrame(rows)
    required = {"timestamp", "value", "value_classification"}
    missing = required - set(df.columns)
    if missing:
        raise FearGreedError(
            f"alternative.me rows missing required fields {sorted(missing)}; "
            f"available={list(df.columns)[:10]}"
        )

    # The API ships timestamp as a Unix-epoch string. Coerce to UTC-aware
    # daily timestamps and rename to the canonical column shape.
    df["timestamp"] = pd.to_datetime(
        pd.to_numeric(df["timestamp"], errors="coerce"),
        unit="s", utc=True,
    )
    df["fear_greed_value"] = pd.to_numeric(
        df["value"], errors="coerce",
    ).astype("Int64")
    df["fear_greed_label"] = df["value_classification"].astype(str)

    out = (
        df[["timestamp", "fear_greed_value", "fear_greed_label"]]
        .dropna(subset=["timestamp", "fear_greed_value"])
        .set_index("timestamp")
        .sort_index()
    )
    out["fear_greed_value"] = out["fear_greed_value"].astype("int64")
    return out


def fetch_fear_greed(
    cache_path: Path = DEFAULT_CACHE_PATH,
    ttl_hours: int = DEFAULT_TTL_HOURS,
) -> pd.DataFrame:
    """Return cached or freshly fetched Fear & Greed Index time series.

    Args:
        cache_path: Parquet cache file path. Default
                    backtest/cache/fear_greed_raw.parquet.
        ttl_hours:  Cache freshness window in hours. Refresh older.

    Returns:
        DataFrame indexed by UTC-aware daily timestamp with columns
        [fear_greed_value (int 0-100), fear_greed_label (str)],
        sorted ascending.

    Raises:
        FearGreedError: HTTP, JSON, or cache failure.
    """
    if _is_fresh(cache_path, ttl_hours):
        cached = _read_cache(cache_path)
        if cached is not None and len(cached) > 0:
            logger.info(
                f"[fear_greed] cache hit {cache_path} rows={len(cached)}"
            )
            return cached

    df = _fetch_remote()
    _write_cache(cache_path, df)
    logger.info(
        f"[fear_greed] fetched rows={len(df)} -> {cache_path}"
    )
    return df
