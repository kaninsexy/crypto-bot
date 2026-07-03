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

HOLDOUT ENFORCEMENT
───────────────────
`load_or_download_ohlcv` refuses to return rows inside any strategy's
holdout window unless the call originates from within `holdout.load_holdout`
(authorised via `_holdout_bypass_ctx`).  Pass `until_ts` to explicitly cap
the returned range to the dev window for a given symbol.

Callers that need the dev-only slice should pass:
    until_ts=get_symbol_dev_cutoff(symbol)
"""

import contextvars
import functools
import json
import os
import time
from pathlib import Path
from typing import Callable

import pandas as pd
from loguru import logger


# ── Holdout enforcement ───────────────────────────────────────────────────────

class HoldoutBypass(RuntimeError):
    """Raised when load_or_download_ohlcv would return holdout-window rows
    outside of an authorised holdout-read context.

    Use `until_ts=get_symbol_dev_cutoff(symbol)` to restrict to the dev
    window, or call from within `holdout.load_holdout` which sets the
    bypass context automatically.
    """


class EnforcementManifestMissing(RuntimeError):
    """Raised when the holdout manifest file is absent at enforcement time.

    Run `python -m backtest.generate_holdout_manifest init` first.
    """


class EnforcementManifestMalformed(RuntimeError):
    """Raised when the holdout manifest exists but cannot be parsed."""


# Set to True by holdout.load_holdout during its _build_df call so the
# enforcement check is skipped for that single authorised access.
_holdout_bypass_ctx: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "_holdout_bypass_ctx", default=False
)

# Independent manifest path — NOT imported from holdout.py to avoid circular
# imports.  Override in tests via monkeypatch.setattr.
_ENFORCEMENT_MANIFEST_PATH: Path = Path("backtest/holdout_manifest.json")


@functools.lru_cache(maxsize=1)
def _load_enforcement_manifest() -> dict:
    """Load holdout manifest for enforcement checks only.

    Raises EnforcementManifestMissing if the file does not exist.
    Raises EnforcementManifestMalformed if the file cannot be parsed.
    """
    p = _ENFORCEMENT_MANIFEST_PATH
    if not p.exists():
        raise EnforcementManifestMissing(
            f"Holdout manifest not found at {p}. "
            "Run `python -m backtest.generate_holdout_manifest init` first."
        )
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        raise EnforcementManifestMalformed(
            f"Could not parse holdout manifest at {p}: {exc}"
        ) from exc


def _earliest_holdout_start(symbol: str) -> "pd.Timestamp | None":
    """Return the earliest holdout_start across all strategies using symbol.

    Returns None if the manifest is absent or no strategy uses symbol.
    """
    manifest = _load_enforcement_manifest()
    timestamps: list[pd.Timestamp] = []
    for entry in manifest.values():
        if "symbols" in entry:
            syms = entry["symbols"]
        elif "symbol" in entry:
            syms = [entry["symbol"]]
        else:
            continue
        if symbol in syms and "holdout_start" in entry:
            timestamps.append(pd.Timestamp(entry["holdout_start"]))
    if not timestamps:
        return None
    return min(timestamps)


def get_symbol_dev_cutoff(symbol: str) -> "pd.Timestamp | None":
    """Return the dev-window cutoff (earliest holdout_start) for symbol.

    Pass the returned value as `until_ts` to `load_or_download_ohlcv` to
    restrict the returned DataFrame to the dev window only.

    Returns None if the manifest has no entry for this symbol — callers
    should treat None as "no restriction applies."
    """
    return _earliest_holdout_start(symbol)


# ── Main cache function ───────────────────────────────────────────────────────

def load_or_download_ohlcv(
    symbol: str,
    timeframe: str,
    months: int,
    download_fn: Callable[[str, str, int], pd.DataFrame],
    cache_dir: Path = Path("backtest/cache/ohlcv"),
    ttl_hours: int = 24,
    until_ts: "pd.Timestamp | None" = None,
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
        until_ts:     If given, the returned DataFrame is clipped to rows
                      with index < until_ts before the holdout-enforcement
                      check.  Pass get_symbol_dev_cutoff(symbol) to restrict
                      to the dev window.

    Returns:
        OHLCV DataFrame (same structure `download_fn` returns), optionally
        clipped to [start, until_ts).

    Raises:
        HoldoutBypass: if the returned rows include data at or after the
                       symbol's earliest holdout_start and the call is not
                       from within an authorised holdout-read context.

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
                return _apply_until_and_enforce(df, symbol, until_ts)
            except HoldoutBypass:
                raise
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

    return _apply_until_and_enforce(df, symbol, until_ts)


def _apply_until_and_enforce(
    df: pd.DataFrame,
    symbol: str,
    until_ts: "pd.Timestamp | None",
) -> pd.DataFrame:
    """Clip df to [start, until_ts) then enforce the holdout boundary."""
    if until_ts is not None:
        df = df[df.index < until_ts]

    if _holdout_bypass_ctx.get():
        return df

    earliest_hs = _earliest_holdout_start(symbol)
    if earliest_hs is not None and not df.empty:
        if df.index.max() >= earliest_hs:
            raise HoldoutBypass(
                f"load_or_download_ohlcv for {symbol!r} returned rows at or "
                f"after holdout_start={earliest_hs.isoformat()}. "
                "Pass until_ts=get_symbol_dev_cutoff(symbol) to restrict to "
                "the dev window, or call from within holdout.load_holdout."
            )

    return df


# ── Binance Vision 1m substrate (Phase 4.E microstructure) ────────────────────
#
# The Phase 4.E microstructure family reads a genuinely different substrate:
# free Binance Vision 1m klines (with the taker buy/sell split) rather than
# the OKX OHLCV cache above.  Its manifest entries carry the Binance
# concatenated ticker form ("BTCUSDT") as their `symbol`, which — because
# every OKX entry uses the "BASE/QUOTE" form — is the unambiguous dispatch
# signal used by holdout._load_substrate_df.  The engine iterates at the
# strategy's signal timeframe; the volume-profile features are computed from
# the 1m data (see data/microstructure_features.build_signal_frame) so the
# locked "1m profile" gate is honoured while the engine stays at signal
# cadence.
#
# The built signal frame is expensive (a rolling daily volume profile over
# the whole history), so it is parquet-cached under
# backtest/cache/binance_vision/_features/ and rebuilt only when the
# underlying 1m month files are newer than the cached frame.  This path does
# NOT go through load_or_download_ohlcv: it performs no network I/O (reads
# only already-downloaded 1m parquet) and is reached solely via
# holdout.load_dev / holdout.load_holdout, which own the single-access and
# audit-log guarantees.

_BINANCE_VISION_CACHE_DIR: Path = Path("backtest/cache/binance_vision")


def _bv_symbol(symbol: str) -> str:
    """Manifest symbol → Binance Vision cache/ticker form (BTCUSDT)."""
    return symbol.upper().replace("/", "").replace("-", "")


def _bv_cached_month_range(sym: str, interval: str = "1m") -> "tuple[str, str]":
    """Min/max cached month ('YYYY-MM') for a Binance Vision symbol.

    Reads the on-disk 1m cache only — never the network — so callers get
    exactly the locally available range.  Raises FileNotFoundError when the
    symbol has no cached months (the substrate must be fetched first via
    scripts/fetch_binance_1m.py).
    """
    d = _BINANCE_VISION_CACHE_DIR / sym / interval
    months = sorted(p.stem for p in d.glob("*.parquet"))
    if not months:
        raise FileNotFoundError(
            f"No Binance Vision 1m cache for {sym} in {d}. "
            "Run scripts/fetch_binance_1m.py first."
        )
    return months[0], months[-1]


def _bv_newest_month_mtime(sym: str, interval: str = "1m") -> float:
    d = _BINANCE_VISION_CACHE_DIR / sym / interval
    return max((p.stat().st_mtime for p in d.glob("*.parquet")), default=0.0)


def load_binance_vision_signal_frame(
    symbol: str,
    timeframe: str,
    cache_dir: Path = _BINANCE_VISION_CACHE_DIR,
) -> pd.DataFrame:
    """Return the enriched signal-timeframe frame for a Binance Vision symbol.

    Resamples the cached 1m klines to `timeframe` and attaches the cheap
    per-bar order-flow delta columns (delta/delta_ratio/cum_delta) via
    data.microstructure_features.build_signal_frame.  This is the substrate
    the engine iterates; the volume-profile features are built separately by
    the trial script (build_profile_features) and injected into the strategy
    via its constructor, so they are NOT on this frame.

    The full-history frame is parquet-cached under
    `{cache_dir}/_features/{SYM}_{timeframe}_sig.parquet` and reused unless a
    1m month file is newer than the cached frame.  Returns the FULL cached
    range; callers (holdout._build_df) filter to the dev/holdout window.
    """
    # Local imports keep the data package off cache.py's import-time graph
    # (mirrors holdout._load_perp_df's local import of data.okx_perp).
    from data.binance_vision import load_klines
    from data.microstructure_features import build_signal_frame

    sym = _bv_symbol(symbol)
    feat_dir = cache_dir / "_features"
    feat_dir.mkdir(parents=True, exist_ok=True)
    feat_path = feat_dir / f"{sym}_{timeframe}_sig.parquet"

    if feat_path.exists():
        if feat_path.stat().st_mtime >= _bv_newest_month_mtime(sym):
            try:
                return pd.read_parquet(feat_path)
            except Exception as e:  # corrupted cache → rebuild
                logger.warning(
                    f"[BV] Failed to read feature cache {feat_path.name}: "
                    f"{e}. Rebuilding."
                )

    start_month, end_month = _bv_cached_month_range(sym)
    klines_1m = load_klines(sym, start_month, end_month, interval="1m")
    frame = build_signal_frame(klines_1m, timeframe)
    try:
        frame.to_parquet(feat_path)
        logger.info(
            f"[BV] Built + cached signal frame {sym} {timeframe} "
            f"({len(frame)} bars) -> {feat_path.name}"
        )
    except Exception as e:
        logger.warning(f"[BV] Failed to write feature cache {feat_path.name}: {e}")
    return frame
