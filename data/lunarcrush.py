"""data/lunarcrush.py -- LunarCrush time-series fetcher with parquet cache.

Provides ``load_or_fetch_galaxy_score(symbol, months)`` returning a
DataFrame with columns ``[galaxy_score, alt_rank, price]`` indexed by
UTC timestamp. Used by the SocialSentimentMomentum trial script.

LunarCrush v4 public API:
    GET https://lunarcrush.com/api4/public/coins/{coin}/time-series/v2
    params:  bucket=day, interval={months}Month
    headers: Authorization: Bearer {LUNARCRUSH_API_KEY}

Cache pattern mirrors backtest/cache.py: 24 hour TTL, parquet on disk
under backtest/cache/lunarcrush/. Corrupted cache files trigger a
re-fetch with a warning.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional

import pandas as pd
import requests
from loguru import logger


LUNARCRUSH_API_BASE = "https://lunarcrush.com/api4/public/coins"
DEFAULT_CACHE_DIR = Path("backtest/cache/lunarcrush")
DEFAULT_TTL_HOURS = 24


# Manifest-symbol -> LunarCrush coin slug.  Extend additively as new
# strategies bring new symbols into scope.
_SYMBOL_TO_COIN: dict[str, str] = {
    "BTC/USDT": "bitcoin",
    "ETH/USDT": "ethereum",
}


class LunarCrushError(RuntimeError):
    """LunarCrush API or cache read/write failure."""


def _coin_slug(symbol: str) -> str:
    """Translate a manifest-format symbol (BASE/QUOTE) to the LunarCrush
    coin slug. Raises LunarCrushError if the symbol is not in the
    locked allow-list (additive extensions only)."""
    if symbol not in _SYMBOL_TO_COIN:
        raise LunarCrushError(
            f"No LunarCrush slug mapping for symbol {symbol!r}. "
            f"Known mappings: {sorted(_SYMBOL_TO_COIN.keys())}"
        )
    return _SYMBOL_TO_COIN[symbol]


def _cache_path(
    symbol: str, months: int, cache_dir: Path,
) -> Path:
    fname = f"{symbol.replace('/', '-')}_{months}mo.parquet"
    return cache_dir / fname


def _read_cache(path: Path) -> Optional[pd.DataFrame]:
    if not path.exists():
        return None
    try:
        return pd.read_parquet(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"[lunarcrush] failed to read cache {path}: {exc}; will re-fetch"
        )
        return None


def _write_cache(path: Path, df: pd.DataFrame) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"[lunarcrush] failed to write cache {path}: {exc}"
        )


def _is_fresh(path: Path, ttl_hours: int) -> bool:
    if not path.exists():
        return False
    age_seconds = time.time() - path.stat().st_mtime
    return age_seconds < ttl_hours * 3600


def _fetch_remote(coin: str, months: int, api_key: str) -> pd.DataFrame:
    """Pull the time-series payload from LunarCrush and return a
    DataFrame indexed by UTC timestamp with columns galaxy_score,
    alt_rank, price (NaN-tolerant)."""
    url = f"{LUNARCRUSH_API_BASE}/{coin}/time-series/v2"
    params = {"bucket": "day", "interval": f"{int(months)}Month"}
    headers = {"Authorization": f"Bearer {api_key}"}

    logger.info(
        f"[lunarcrush] GET {url} params={params}"
    )
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=30)
    except requests.RequestException as exc:
        raise LunarCrushError(
            f"LunarCrush request failed for coin={coin}: {exc}"
        ) from exc
    if resp.status_code != 200:
        raise LunarCrushError(
            f"LunarCrush returned HTTP {resp.status_code} for coin={coin}: "
            f"{resp.text[:200]}"
        )

    try:
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise LunarCrushError(
            f"LunarCrush JSON parse error for coin={coin}: {exc}"
        ) from exc

    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or len(rows) == 0:
        raise LunarCrushError(
            f"LunarCrush payload missing 'data' array or empty for coin={coin}"
        )

    df = pd.DataFrame(rows)
    if "time" not in df.columns:
        raise LunarCrushError(
            f"LunarCrush rows missing 'time' field for coin={coin}; "
            f"available={list(df.columns)[:10]}"
        )
    df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.set_index("timestamp").sort_index()

    keep_cols: list[str] = []
    for col in ("galaxy_score", "alt_rank", "close", "price"):
        if col in df.columns:
            keep_cols.append(col)
    out = df[keep_cols].copy()
    if "close" in out.columns and "price" not in out.columns:
        out = out.rename(columns={"close": "price"})
    if "galaxy_score" not in out.columns:
        out["galaxy_score"] = float("nan")
    if "alt_rank" not in out.columns:
        out["alt_rank"] = float("nan")
    if "price" not in out.columns:
        out["price"] = float("nan")

    return out[["galaxy_score", "alt_rank", "price"]].astype(float)


def load_or_fetch_galaxy_score(
    symbol: str,
    months: int,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    ttl_hours: int = DEFAULT_TTL_HOURS,
) -> pd.DataFrame:
    """Return cached or freshly fetched LunarCrush time-series data.

    Args:
        symbol: Manifest-format pair (e.g. "BTC/USDT"). Translated to
                a LunarCrush coin slug via _SYMBOL_TO_COIN.
        months: Months of history to fetch.
        cache_dir: Parquet cache directory.
        ttl_hours: Cache freshness window in hours.

    Returns:
        DataFrame indexed by UTC timestamp with columns
        [galaxy_score, alt_rank, price].

    Raises:
        LunarCrushError: API or cache failure.
    """
    coin = _coin_slug(symbol)
    path = _cache_path(symbol, months, cache_dir)

    if _is_fresh(path, ttl_hours):
        cached = _read_cache(path)
        if cached is not None and len(cached) > 0:
            logger.info(
                f"[lunarcrush] cache hit {path} rows={len(cached)}"
            )
            return cached

    api_key = os.environ.get("LUNARCRUSH_API_KEY", "").strip()
    if api_key == "":
        # Cache miss with no key: surface clearly. The trial script
        # treats this as actionable failure (exit 1) rather than a
        # silent fallback.
        raise LunarCrushError(
            "LUNARCRUSH_API_KEY environment variable is unset; cannot "
            "fetch sentiment time-series."
        )

    df = _fetch_remote(coin, months, api_key)
    _write_cache(path, df)
    logger.info(
        f"[lunarcrush] fetched {coin} months={months} rows={len(df)} "
        f"-> {path}"
    )
    return df
