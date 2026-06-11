"""
backtest/tests/test_cpcv_event_mode.py — Event-based block sizing
(gate spec v2, 2026-06-11, work-order item 5).

block_mode="event" sizes blocks by equal signal-event count with a
minimum of 5 blocks; "calendar" (default) is byte-for-byte the
pre-v2 behaviour.
"""

import numpy as np
import pandas as pd
import pytest

import backtest.holdout as holdout
from backtest.cpcv import run_cpcv
from backtest.cpcv_common import (
    CPCVConfig,
    CPCVError,
    _MIN_EVENTS_PER_BLOCK,
    _split_blocks_event,
)
from backtest.tests.test_cpcv import (
    _PeriodicStrategy,
    _make_ohlcv,
    _single_symbol_manifest,
)


# ── Splitter unit tests ──────────────────────────────────────────────────────

def test_equal_event_counts_per_block():
    df = _make_ohlcv(1000)
    # 40 events spread over the window → 8 blocks of 5 at max_blocks=8.
    events = np.linspace(10, 980, 40, dtype=int)
    blocks = _split_blocks_event(df, events, max_blocks=8)
    assert len(blocks) == 8
    # Coverage: every row in exactly one block, order preserved.
    total_rows = sum(len(b) for b in blocks)
    assert total_rows == len(df)
    # Equal event count per block (5 each; last absorbs remainder=0).
    for b in blocks:
        start = df.index.get_loc(b.index[0])
        end = start + len(b)
        n_ev = int(((events >= start) & (events < end)).sum())
        assert n_ev == 5, f"block [{start}:{end}) holds {n_ev} events"


def test_block_count_downshifts_to_event_budget():
    """27 events at max_blocks=10 → 27//5 = 5 blocks (the minimum)."""
    df = _make_ohlcv(1000)
    events = np.linspace(5, 990, 27, dtype=int)
    blocks = _split_blocks_event(df, events, max_blocks=10)
    assert len(blocks) == 5


def test_below_25_events_raises():
    """5 blocks × 5 events = 25 is the hard floor (audit §3: the
    ExchangeListingDrift case — 21 recorded trades — stays untestable
    even event-based)."""
    df = _make_ohlcv(1000)
    events = np.linspace(5, 990, 21, dtype=int)
    with pytest.raises(CPCVError, match="event-based blocking needs"):
        _split_blocks_event(df, events, max_blocks=10)


def test_clustered_events_collapse_raises():
    """Multiple events on the SAME row (duplicate positions) make two
    block boundaries coincide → CPCVError rather than zero-width
    blocks.  (Adjacent-but-distinct rows still yield strictly
    increasing cuts and are fine.)"""
    df = _make_ohlcv(1000)
    events = np.full(30, 500)  # 30 events, all on one row
    with pytest.raises(CPCVError, match="too clustered"):
        _split_blocks_event(df, events, max_blocks=6)


def test_out_of_range_positions_rejected():
    df = _make_ohlcv(100)
    with pytest.raises(CPCVError, match="out of range"):
        _split_blocks_event(df, np.array([5, 200]), max_blocks=10)


# ── Config validation ────────────────────────────────────────────────────────

def test_config_default_is_calendar():
    cfg = CPCVConfig()
    assert cfg.block_mode == "calendar"
    cfg.validate()  # default config still validates — no caller change


def test_config_rejects_unknown_block_mode():
    with pytest.raises(ValueError, match="block_mode"):
        CPCVConfig(block_mode="weekly").validate()


def test_config_event_mode_requires_locator():
    with pytest.raises(ValueError, match="locate_signal_events"):
        CPCVConfig(block_mode="event").validate()


# ── run_cpcv integration ─────────────────────────────────────────────────────

@pytest.fixture
def patch_holdout_for_cpcv(monkeypatch):
    def _wire(manifest: dict, dev_df: pd.DataFrame):
        monkeypatch.setattr(holdout, "load_manifest", lambda: manifest)
        monkeypatch.setattr(holdout, "load_dev", lambda sid: dev_df)
    return _wire


def test_run_cpcv_event_mode_end_to_end(patch_holdout_for_cpcv):
    """Event mode end-to-end: locator returns the strategy's trade
    cadence positions; run_cpcv produces one Sharpe per event-sized
    block and n_paths reflects the realized (possibly downshifted)
    block count."""
    dev_df = _make_ohlcv(2000)
    patch_holdout_for_cpcv(_single_symbol_manifest(dev_df), dev_df)

    def locator(df: pd.DataFrame):
        # _PeriodicStrategy(trade_period=30) trades roughly every 30
        # bars after warmup; model that cadence as the event grid.
        return np.arange(60, len(df) - 10, 30, dtype=int)

    config = CPCVConfig(
        n_blocks=6, k_held_out=2,
        block_mode="event", locate_signal_events=locator,
    )
    factory = lambda: _PeriodicStrategy(
        symbol="BTC/USDT", trade_period=30, hold_candles=5,
    )
    result = run_cpcv("TestStrat", {}, config, factory)
    assert result.n_paths == 6
    assert len(result.per_path_sharpes) == 6
    assert len(result.per_block_returns) == 6


def test_run_cpcv_event_mode_rejects_multi_symbol(patch_holdout_for_cpcv):
    dev_df = _make_ohlcv(1000)
    dev_df = dev_df.copy()
    dev_df["symbol"] = "BTC/USDT"
    manifest = {
        "TestStrat": {
            "symbols": ["BTC/USDT", "ETH/USDT"],
            "timeframe": "1h",
            "data_start": str(dev_df.index[0]),
            "data_end": str(dev_df.index[-1]),
            "dev_end": str(dev_df.index[-1]),
            "holdout_start": str(dev_df.index[-1]),
        }
    }
    patch_holdout_for_cpcv(manifest, dev_df)
    config = CPCVConfig(
        block_mode="event",
        locate_signal_events=lambda df: np.arange(0, 900, 30),
    )
    factory = lambda: _PeriodicStrategy(symbol="BTC/USDT")
    with pytest.raises(CPCVError, match="single-symbol only"):
        run_cpcv("TestStrat", {}, config, factory)


def test_run_cpcv_calendar_mode_unchanged(patch_holdout_for_cpcv):
    """Regression guard: a default-config run is identical in shape to
    the pre-v2 behaviour (n_paths == n_blocks, equal-row blocks)."""
    dev_df = _make_ohlcv(1000)
    patch_holdout_for_cpcv(_single_symbol_manifest(dev_df), dev_df)
    config = CPCVConfig(n_blocks=4, k_held_out=2)
    factory = lambda: _PeriodicStrategy(
        symbol="BTC/USDT", trade_period=30, hold_candles=5,
    )
    result = run_cpcv("TestStrat", {}, config, factory)
    assert result.n_paths == 4
    assert len(result.per_path_sharpes) == 4
