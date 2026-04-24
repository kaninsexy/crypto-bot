"""
backtest/tests/test_cache_enforcement.py — Tests for holdout enforcement in cache.py.

Verifies:
  1. Direct call returning holdout rows raises HoldoutBypass.
  2. _holdout_bypass_ctx=True suppresses the enforcement check.
  3. until_ts clips the result to [start, until_ts) before enforcement.
  4. Clipped result identical to manual slice of the full DataFrame.
  5. get_symbol_dev_cutoff returns the correct timestamp from the manifest.
  6. No manifest → no enforcement (HoldoutBypass never raised).
"""

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import backtest.cache as cache
from backtest.cache import (
    EnforcementManifestMalformed,
    EnforcementManifestMissing,
    HoldoutBypass,
    _holdout_bypass_ctx,
    _load_enforcement_manifest,
    get_symbol_dev_cutoff,
    load_or_download_ohlcv,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

HOLDOUT_START = pd.Timestamp("2024-07-01T00:00:00", tz="UTC")

MANIFEST_SINGLE = {
    "TestStrat": {
        "timeframe": "1h",
        "data_start": "2023-01-01T00:00:00+00:00",
        "data_end":   "2025-01-01T00:00:00+00:00",
        "dev_end":    HOLDOUT_START.isoformat(),
        "holdout_start": HOLDOUT_START.isoformat(),
        "symbol": "BTC/USDT",
    }
}

MANIFEST_MULTI = {
    "DualMom": {
        "timeframe": "1h",
        "data_start": "2023-01-01T00:00:00+00:00",
        "data_end":   "2025-01-01T00:00:00+00:00",
        "dev_end":    HOLDOUT_START.isoformat(),
        "holdout_start": HOLDOUT_START.isoformat(),
        "symbols": ["BTC/USDT", "ETH/USDT"],
    }
}


def make_ohlcv(start: str, end: str, freq: str = "1h") -> pd.DataFrame:
    idx = pd.date_range(start, end, freq=freq, tz="UTC", inclusive="left")
    idx.name = "timestamp"
    n = len(idx)
    return pd.DataFrame(
        {"open": np.ones(n), "high": np.ones(n),
         "low": np.ones(n), "close": np.ones(n), "volume": np.ones(n)},
        index=idx,
    )


def constant_download_fn(df: pd.DataFrame):
    """Return a download_fn that always returns df."""
    def _fn(symbol, timeframe, months):
        return df
    return _fn


# ── Autouse fixture: redirect manifest path and clear caches ──────────────────

@pytest.fixture(autouse=True)
def patch_cache(tmp_path, monkeypatch):
    manifest_path = tmp_path / "holdout_manifest.json"
    monkeypatch.setattr(cache, "_ENFORCEMENT_MANIFEST_PATH", manifest_path)
    _load_enforcement_manifest.cache_clear()
    yield
    _load_enforcement_manifest.cache_clear()


# ── Test 1: direct call with holdout rows raises HoldoutBypass ─────────────────

def test_direct_call_with_holdout_rows_raises(tmp_path):
    """load_or_download_ohlcv raises HoldoutBypass when result includes holdout rows."""
    manifest_path = cache._ENFORCEMENT_MANIFEST_PATH
    manifest_path.write_text(json.dumps(MANIFEST_SINGLE), encoding="utf-8")
    _load_enforcement_manifest.cache_clear()

    df = make_ohlcv("2023-01-01", "2025-01-01")
    assert df.index.max() >= HOLDOUT_START

    with pytest.raises(HoldoutBypass):
        load_or_download_ohlcv(
            symbol="BTC/USDT",
            timeframe="1h",
            months=36,
            download_fn=constant_download_fn(df),
            cache_dir=tmp_path / "cache",
        )


# ── Test 2: bypass context suppresses enforcement ──────────────────────────────

def test_bypass_context_suppresses_enforcement(tmp_path):
    """With _holdout_bypass_ctx=True, holdout rows pass through without error."""
    manifest_path = cache._ENFORCEMENT_MANIFEST_PATH
    manifest_path.write_text(json.dumps(MANIFEST_SINGLE), encoding="utf-8")
    _load_enforcement_manifest.cache_clear()

    df = make_ohlcv("2023-01-01", "2025-01-01")

    token = _holdout_bypass_ctx.set(True)
    try:
        result = load_or_download_ohlcv(
            symbol="BTC/USDT",
            timeframe="1h",
            months=36,
            download_fn=constant_download_fn(df),
            cache_dir=tmp_path / "cache",
        )
        assert len(result) == len(df)
    finally:
        _holdout_bypass_ctx.reset(token)


# ── Test 3: until_ts clips result before enforcement ──────────────────────────

def test_until_ts_clips_before_holdout_start(tmp_path):
    """Passing until_ts=HOLDOUT_START clips result and prevents HoldoutBypass."""
    manifest_path = cache._ENFORCEMENT_MANIFEST_PATH
    manifest_path.write_text(json.dumps(MANIFEST_SINGLE), encoding="utf-8")
    _load_enforcement_manifest.cache_clear()

    df = make_ohlcv("2023-01-01", "2025-01-01")
    expected = df[df.index < HOLDOUT_START]

    result = load_or_download_ohlcv(
        symbol="BTC/USDT",
        timeframe="1h",
        months=36,
        download_fn=constant_download_fn(df),
        cache_dir=tmp_path / "cache",
        until_ts=HOLDOUT_START,
    )

    assert len(result) == len(expected)
    assert result.index.max() < HOLDOUT_START


# ── Test 4: clipped result is identical to manual slice ───────────────────────

def test_clipped_result_matches_manual_slice(tmp_path):
    """DataFrame returned with until_ts is identical to df[df.index < until_ts]."""
    manifest_path = cache._ENFORCEMENT_MANIFEST_PATH
    manifest_path.write_text(json.dumps(MANIFEST_SINGLE), encoding="utf-8")
    _load_enforcement_manifest.cache_clear()

    df = make_ohlcv("2023-01-01", "2025-01-01")
    expected = df[df.index < HOLDOUT_START]

    result = load_or_download_ohlcv(
        symbol="BTC/USDT",
        timeframe="1h",
        months=36,
        download_fn=constant_download_fn(df),
        cache_dir=tmp_path / "cache",
        until_ts=HOLDOUT_START,
    )

    pd.testing.assert_frame_equal(result, expected)


# ── Test 5: get_symbol_dev_cutoff returns correct timestamp ───────────────────

def test_get_symbol_dev_cutoff_single_strategy():
    """get_symbol_dev_cutoff returns holdout_start for a single-strategy symbol."""
    manifest_path = cache._ENFORCEMENT_MANIFEST_PATH
    manifest_path.write_text(json.dumps(MANIFEST_SINGLE), encoding="utf-8")
    _load_enforcement_manifest.cache_clear()

    cutoff = get_symbol_dev_cutoff("BTC/USDT")
    assert cutoff == HOLDOUT_START


def test_get_symbol_dev_cutoff_multi_symbol_strategy():
    """get_symbol_dev_cutoff works for symbols in a multi-symbol strategy."""
    manifest_path = cache._ENFORCEMENT_MANIFEST_PATH
    manifest_path.write_text(json.dumps(MANIFEST_MULTI), encoding="utf-8")
    _load_enforcement_manifest.cache_clear()

    assert get_symbol_dev_cutoff("BTC/USDT") == HOLDOUT_START
    assert get_symbol_dev_cutoff("ETH/USDT") == HOLDOUT_START


def test_get_symbol_dev_cutoff_unknown_symbol_returns_none():
    """get_symbol_dev_cutoff returns None for a symbol not in the manifest."""
    manifest_path = cache._ENFORCEMENT_MANIFEST_PATH
    manifest_path.write_text(json.dumps(MANIFEST_SINGLE), encoding="utf-8")
    _load_enforcement_manifest.cache_clear()

    assert get_symbol_dev_cutoff("SOL/USDT") is None


def test_get_symbol_dev_cutoff_takes_minimum_across_strategies():
    """When multiple strategies use the same symbol, returns the earliest holdout_start."""
    earlier = pd.Timestamp("2024-01-01T00:00:00", tz="UTC")
    later   = pd.Timestamp("2024-07-01T00:00:00", tz="UTC")
    manifest = {
        "StratA": {
            "timeframe": "1h", "data_start": "2023-01-01T00:00:00+00:00",
            "data_end": "2025-01-01T00:00:00+00:00",
            "dev_end": earlier.isoformat(), "holdout_start": earlier.isoformat(),
            "symbol": "BTC/USDT",
        },
        "StratB": {
            "timeframe": "1h", "data_start": "2023-01-01T00:00:00+00:00",
            "data_end": "2025-01-01T00:00:00+00:00",
            "dev_end": later.isoformat(), "holdout_start": later.isoformat(),
            "symbol": "BTC/USDT",
        },
    }
    manifest_path = cache._ENFORCEMENT_MANIFEST_PATH
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    _load_enforcement_manifest.cache_clear()

    assert get_symbol_dev_cutoff("BTC/USDT") == earlier


# ── Test 6: no manifest → raises EnforcementManifestMissing ──────────────────

def test_no_manifest_raises_enforcement_missing(tmp_path):
    """When the manifest file is absent, load_or_download_ohlcv raises EnforcementManifestMissing."""
    assert not cache._ENFORCEMENT_MANIFEST_PATH.exists()

    df = make_ohlcv("2023-01-01", "2025-01-01")

    with pytest.raises(EnforcementManifestMissing):
        load_or_download_ohlcv(
            symbol="BTC/USDT",
            timeframe="1h",
            months=36,
            download_fn=constant_download_fn(df),
            cache_dir=tmp_path / "cache",
        )


def test_malformed_manifest_raises_enforcement_malformed(tmp_path):
    """When the manifest contains invalid JSON, raises EnforcementManifestMalformed."""
    cache._ENFORCEMENT_MANIFEST_PATH.write_text("{not valid json", encoding="utf-8")
    _load_enforcement_manifest.cache_clear()

    df = make_ohlcv("2023-01-01", "2025-01-01")

    with pytest.raises(EnforcementManifestMalformed):
        load_or_download_ohlcv(
            symbol="BTC/USDT",
            timeframe="1h",
            months=36,
            download_fn=constant_download_fn(df),
            cache_dir=tmp_path / "cache",
        )


# ── Test 7: enforcement fires on cached read too ──────────────────────────────

def test_enforcement_fires_on_cache_hit(tmp_path):
    """HoldoutBypass is raised even when the data comes from a cached parquet."""
    manifest_path = cache._ENFORCEMENT_MANIFEST_PATH
    manifest_path.write_text(json.dumps(MANIFEST_SINGLE), encoding="utf-8")
    _load_enforcement_manifest.cache_clear()

    df = make_ohlcv("2023-01-01", "2025-01-01")
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()

    # Write the parquet directly so it's already there as a "cached" file.
    cache_file = cache_dir / "BTC-USDT_1h_36mo.parquet"
    df.to_parquet(cache_file)

    with pytest.raises(HoldoutBypass):
        load_or_download_ohlcv(
            symbol="BTC/USDT",
            timeframe="1h",
            months=36,
            download_fn=constant_download_fn(df),
            cache_dir=cache_dir,
            ttl_hours=24 * 365,  # prevent TTL expiry
        )


# ── Test 8: regression — until_ts is a pure filter, no data drift ─────────────

def test_until_ts_is_pure_filter_no_drift(tmp_path):
    """until_ts path produces the same DataFrame as full load manually sliced.

    Proves the until_ts clipping is behaviour-preserving: same index, same
    values, tolerance 1e-9.  Guards against any accidental data drift introduced
    by the refactor.
    """
    manifest_path = cache._ENFORCEMENT_MANIFEST_PATH
    manifest_path.write_text(json.dumps(MANIFEST_SINGLE), encoding="utf-8")
    _load_enforcement_manifest.cache_clear()

    df_full = make_ohlcv("2023-01-01", "2025-01-01")
    cutoff = HOLDOUT_START

    # Load with until_ts — should clip to dev window without raising.
    result_clipped = load_or_download_ohlcv(
        symbol="BTC/USDT",
        timeframe="1h",
        months=36,
        download_fn=constant_download_fn(df_full),
        cache_dir=tmp_path / "cache_clipped",
        until_ts=cutoff,
    )

    # Load the full DataFrame in bypass context (authorised), then slice manually.
    token = _holdout_bypass_ctx.set(True)
    try:
        result_full = load_or_download_ohlcv(
            symbol="BTC/USDT",
            timeframe="1h",
            months=36,
            download_fn=constant_download_fn(df_full),
            cache_dir=tmp_path / "cache_full",
        )
    finally:
        _holdout_bypass_ctx.reset(token)

    expected = result_full[result_full.index < cutoff]

    pd.testing.assert_frame_equal(
        result_clipped.reset_index(),
        expected.reset_index(),
        check_exact=False,
        atol=1e-9,
    )
