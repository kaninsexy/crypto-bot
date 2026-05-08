"""data/google_trends.py -- Google Trends search-volume fetcher with cache.

Provides ``load_or_fetch_trends(keyword, months)`` returning a daily
DataFrame indexed by UTC timestamp with column ``search_volume`` in
[0, 100]. Used by the ContrarianSearchVolume trial script.

Implementation uses pytrends. Weekly Google Trends data is resampled
to daily via forward-fill. Cache pattern mirrors backtest/cache.py:
24 hour TTL, parquet on disk under backtest/cache/google_trends/.
Corrupted cache files trigger a re-fetch with a warning. 429
responses retry with exponential backoff (max 3 retries).
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import pandas as pd
from loguru import logger


DEFAULT_CACHE_DIR = Path("backtest/cache/google_trends")
DEFAULT_TTL_HOURS = 24
MAX_RETRIES = 3
RETRY_BASE_SLEEP_SEC = 5.0


# Manifest-symbol -> Google Trends keyword. Additive extensions only.
_SYMBOL_TO_KEYWORD: dict[str, str] = {
    "BTC/USDT": "bitcoin",
    "ETH/USDT": "ethereum",
}


class GoogleTrendsError(RuntimeError):
    """Google Trends API or cache failure."""


def keyword_for(symbol: str) -> str:
    """Translate a manifest-format symbol to a Google Trends keyword."""
    if symbol not in _SYMBOL_TO_KEYWORD:
        raise GoogleTrendsError(
            f"No Google Trends keyword mapping for symbol {symbol!r}. "
            f"Known: {sorted(_SYMBOL_TO_KEYWORD.keys())}"
        )
    return _SYMBOL_TO_KEYWORD[symbol]


def _cache_path(keyword: str, months: int, cache_dir: Path) -> Path:
    safe = keyword.replace(" ", "_").replace("/", "-")
    return cache_dir / f"{safe}_{months}mo.parquet"


def _read_cache(path: Path) -> Optional[pd.DataFrame]:
    if not path.exists():
        return None
    try:
        return pd.read_parquet(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"[gtrends] failed to read cache {path}: {exc}; will re-fetch"
        )
        return None


def _write_cache(path: Path, df: pd.DataFrame) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"[gtrends] failed to write cache {path}: {exc}"
        )


def _is_fresh(path: Path, ttl_hours: int) -> bool:
    if not path.exists():
        return False
    age_seconds = time.time() - path.stat().st_mtime
    return age_seconds < ttl_hours * 3600


def _ensure_pytrends_installed() -> None:
    """Install pytrends into the active environment if it is missing.

    Trial scripts run in the same venv as the backtest harness, so a
    one-shot pip install is acceptable per the prompt's contract.
    """
    try:
        import pytrends  # noqa: F401
        return
    except ImportError:
        pass
    logger.info("[gtrends] pytrends not installed; running pip install ...")
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "pytrends"],
        )
    except subprocess.CalledProcessError as exc:
        raise GoogleTrendsError(
            f"Failed to install pytrends: {exc}"
        ) from exc


def _trends_timeframe_for_months(months: int) -> str:
    """Pick the smallest pytrends ``timeframe`` string covering N months."""
    if months <= 12:
        return "today 12-m"
    if months <= 60:
        return "today 5-y"
    return "all"


def _fetch_remote(keyword: str, months: int) -> pd.DataFrame:
    """Fetch weekly Google Trends data and resample to daily via ffill.

    Retries on 429 / TooManyRequestsError with exponential backoff
    (RETRY_BASE_SLEEP_SEC * 2 ** attempt).
    """
    _ensure_pytrends_installed()
    from pytrends.request import TrendReq
    try:
        from pytrends.exceptions import TooManyRequestsError  # type: ignore
    except Exception:  # noqa: BLE001
        TooManyRequestsError = Exception  # type: ignore[assignment]

    timeframe = _trends_timeframe_for_months(months)
    last_exc: Optional[BaseException] = None
    for attempt in range(MAX_RETRIES):
        try:
            client = TrendReq(hl="en-US", tz=0)
            client.build_payload(
                kw_list=[keyword],
                cat=0,
                timeframe=timeframe,
                geo="",
                gprop="",
            )
            weekly = client.interest_over_time()
            if weekly is None or len(weekly) == 0:
                raise GoogleTrendsError(
                    f"Google Trends returned empty for keyword={keyword!r}"
                )
            if "isPartial" in weekly.columns:
                weekly = weekly.drop(columns=["isPartial"])
            if keyword not in weekly.columns:
                raise GoogleTrendsError(
                    f"Trends payload missing column {keyword!r}; "
                    f"have {list(weekly.columns)}"
                )
            series = weekly[keyword].astype(float).rename("search_volume")
            # Convert to UTC-aware daily index via forward-fill.
            if series.index.tz is None:
                series.index = series.index.tz_localize("UTC")
            else:
                series.index = series.index.tz_convert("UTC")
            daily_idx = pd.date_range(
                start=series.index.min().normalize(),
                end=series.index.max().normalize(),
                freq="1D",
                tz="UTC",
            )
            daily = series.reindex(daily_idx, method="ffill")
            df = daily.to_frame(name="search_volume")
            df.index.name = "timestamp"
            return df
        except TooManyRequestsError as exc:
            last_exc = exc
            sleep_for = RETRY_BASE_SLEEP_SEC * (2 ** attempt)
            logger.warning(
                f"[gtrends] 429 on attempt {attempt + 1}/{MAX_RETRIES}; "
                f"sleeping {sleep_for:.1f}s"
            )
            time.sleep(sleep_for)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            break
    raise GoogleTrendsError(
        f"Google Trends fetch failed after {MAX_RETRIES} attempts: "
        f"{type(last_exc).__name__}: {last_exc}"
    )


def load_or_fetch_trends(
    keyword: str,
    months: int,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    ttl_hours: int = DEFAULT_TTL_HOURS,
    required_end_date: object = None,
) -> pd.DataFrame:
    """Return cached or freshly fetched Google Trends daily series.

    Args:
        keyword: Trend keyword (e.g. "bitcoin"). Use ``keyword_for(symbol)``
                 to translate a manifest-format symbol to its keyword.
        months:  Months of history to cover.
        cache_dir: Parquet cache directory.
        ttl_hours: Cache freshness window in hours.
        required_end_date:
            Optional caller-supplied end-date the cache MUST cover
            (typically `holdout_manifest[<sid>]["data_end"]` for
            holdout evaluations). When set, the cache is treated as
            fresh whenever the cached data extends to or past this
            date, regardless of mtime age. Bypasses the mtime TTL
            for the holdout path so a 24h-stale parquet whose data
            already covers the fixed holdout window is NOT re-fetched
            (the re-fetch trips Trends 429 with no benefit). When
            None (live-trading default), behaviour is unchanged --
            mtime TTL governs freshness.

    Returns:
        DataFrame indexed by UTC daily timestamp with column
        ``search_volume`` (0-100 scale).

    Raises:
        GoogleTrendsError: API or cache failure after retries.
    """
    path = _cache_path(keyword, months, cache_dir)

    # Coverage-first cache check: when the caller has named an exact
    # end-date the cached data must reach (e.g. holdout data_end), and
    # the on-disk parquet's last index satisfies it, treat as fresh
    # regardless of mtime. Live-trading callers pass None and fall
    # through to the mtime TTL branch.
    if required_end_date is not None:
        cached = _read_cache(path)
        if cached is not None and len(cached) > 0:
            try:
                last_ts = pd.Timestamp(cached.index.max())
                req_ts = pd.Timestamp(required_end_date)
                if last_ts.tzinfo is None:
                    last_ts = last_ts.tz_localize("UTC")
                if req_ts.tzinfo is None:
                    req_ts = req_ts.tz_localize("UTC")
                if last_ts >= req_ts:
                    logger.info(
                        f"[gtrends] coverage-cache hit {path} "
                        f"rows={len(cached)} "
                        f"(last={last_ts}, required>={req_ts})"
                    )
                    return cached
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    f"[gtrends] coverage check failed on {path} "
                    f"({exc.__class__.__name__}: {exc}); falling back to TTL"
                )

    if _is_fresh(path, ttl_hours):
        cached = _read_cache(path)
        if cached is not None and len(cached) > 0:
            logger.info(
                f"[gtrends] cache hit {path} rows={len(cached)}"
            )
            return cached

    df = _fetch_remote(keyword, months)
    _write_cache(path, df)
    logger.info(
        f"[gtrends] fetched keyword={keyword!r} months={months} "
        f"rows={len(df)} -> {path}"
    )
    return df
