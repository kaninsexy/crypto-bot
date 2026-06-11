"""
backtest/tests/test_per_bar_store.py — Per-bar return persistence
(gate spec v2, 2026-06-11, work-order item 4).
"""

import json

import numpy as np
import pandas as pd
import pytest

import backtest.holdout as holdout
import backtest.per_bar_store as store
import backtest.trials as trials


def test_round_trip_with_index_and_benchmark(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_PER_BAR_DIR", tmp_path / "pbr")
    idx = pd.date_range("2024-01-01", periods=100, freq="1h", tz="UTC")
    rs = np.random.default_rng(0).normal(0, 0.01, 100)
    rb = np.random.default_rng(1).normal(0, 0.01, 100)
    path = store.persist_per_bar_returns(
        "abc123", rs, benchmark_returns=rb, index=idx,
        meta={"strategy_id": "X"},
    )
    assert path is not None and path.exists()
    df = store.load_per_bar_returns("abc123")
    np.testing.assert_array_almost_equal(df["strategy_return"].values, rs)
    np.testing.assert_array_almost_equal(df["benchmark_return"].values, rb)
    assert (df.index == idx).all()


def test_benchmark_longer_is_tail_aligned(tmp_path, monkeypatch):
    """Engine warmup trims the strategy series; the benchmark keeps
    full length.  Persistence must tail-align (keep the LAST n bars)."""
    monkeypatch.setattr(store, "_PER_BAR_DIR", tmp_path / "pbr")
    rs = np.arange(50, dtype=float)
    rb = np.arange(80, dtype=float)
    store.persist_per_bar_returns("t1", rs, benchmark_returns=rb)
    df = store.load_per_bar_returns("t1")
    np.testing.assert_array_equal(df["benchmark_return"].values, rb[-50:])


def test_benchmark_shorter_is_nan_padded(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_PER_BAR_DIR", tmp_path / "pbr")
    rs = np.arange(50, dtype=float)
    rb = np.arange(30, dtype=float)
    store.persist_per_bar_returns("t2", rs, benchmark_returns=rb)
    df = store.load_per_bar_returns("t2")
    assert df["benchmark_return"].isna().sum() == 20
    np.testing.assert_array_equal(df["benchmark_return"].values[-30:], rb)


def test_empty_strategy_series_warns_and_skips(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_PER_BAR_DIR", tmp_path / "pbr")
    with pytest.warns(UserWarning, match="empty"):
        out = store.persist_per_bar_returns("t3", np.array([]))
    assert out is None


def test_missing_trial_raises_with_pre_v2_hint(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_PER_BAR_DIR", tmp_path / "pbr")
    with pytest.raises(FileNotFoundError, match="predate"):
        store.load_per_bar_returns("nonexistent")


def test_record_trial_persists_per_bar_series(tmp_path, monkeypatch):
    """End-to-end: record_trial with per_bar kwargs writes
    <trial_id>.parquet keyed by the writer-assigned trial_id, after a
    successful append.  The trials.log row itself is unchanged in
    shape (no new schema fields)."""
    log_path = tmp_path / "trials.log"
    log_path.touch()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({
        "TestStrat": {
            "timeframe": "1h",
            "data_start": "2023-01-01T00:00:00+00:00",
            "data_end": "2026-01-01T00:00:00+00:00",
            "dev_end": "2025-06-01T00:00:00+00:00",
            "holdout_start": "2025-06-01T00:00:00+00:00",
            "symbol": "BTC/USDT",
        },
    }), encoding="utf-8")
    monkeypatch.setattr(trials, "_TRIALS_LOG_PATH", log_path)
    monkeypatch.setattr(holdout, "_MANIFEST_PATH", manifest_path)
    holdout.load_manifest.cache_clear()
    monkeypatch.setattr(store, "_PER_BAR_DIR", tmp_path / "pbr")

    event = {
        "strategy_id": "TestStrat",
        "variation_id": "v1",
        "trial_type": "smoke",
        "params": {"a": 1},
        "hypothesis": "per-bar persistence smoke",
        "split_holdout_start": "2025-06-01T00:00:00+00:00",
        "symbols": ["BTC/USDT"],
        "n_trades": 10,
        "sharpe": 0.5,
    }
    rs = np.random.default_rng(2).normal(0, 0.01, 64)
    rb = np.random.default_rng(3).normal(0, 0.01, 64)
    trials.record_trial(event, per_bar_returns=rs, per_bar_benchmark=rb)

    tid = event["trial_id"]
    df = store.load_per_bar_returns(tid)
    assert len(df) == 64
    assert set(df.columns) == {"strategy_return", "benchmark_return"}
    # Row written, schema unchanged (no per-bar fields leak into it).
    row = json.loads(log_path.read_text().strip())
    assert row["trial_id"] == tid
    assert "per_bar_returns" not in row
    holdout.load_manifest.cache_clear()


def test_record_trial_without_series_writes_nothing(tmp_path, monkeypatch):
    log_path = tmp_path / "trials.log"
    log_path.touch()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({
        "TestStrat": {
            "timeframe": "1h",
            "data_start": "2023-01-01T00:00:00+00:00",
            "data_end": "2026-01-01T00:00:00+00:00",
            "dev_end": "2025-06-01T00:00:00+00:00",
            "holdout_start": "2025-06-01T00:00:00+00:00",
            "symbol": "BTC/USDT",
        },
    }), encoding="utf-8")
    monkeypatch.setattr(trials, "_TRIALS_LOG_PATH", log_path)
    monkeypatch.setattr(holdout, "_MANIFEST_PATH", manifest_path)
    holdout.load_manifest.cache_clear()
    pbr_dir = tmp_path / "pbr"
    monkeypatch.setattr(store, "_PER_BAR_DIR", pbr_dir)

    event = {
        "strategy_id": "TestStrat",
        "variation_id": "v1",
        "trial_type": "smoke",
        "params": {},
        "hypothesis": "legacy call",
        "split_holdout_start": "2025-06-01T00:00:00+00:00",
        "symbols": ["BTC/USDT"],
        "n_trades": 10,
        "sharpe": 0.5,
    }
    trials.record_trial(event)
    assert not pbr_dir.exists() or not list(pbr_dir.iterdir())
    holdout.load_manifest.cache_clear()
