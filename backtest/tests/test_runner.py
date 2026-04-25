"""
backtest/tests/test_runner.py — Tests for the runner's mode flag.

Focus: the chunk-11 final_gate path on `run_all`.  The dev path is
unchanged from chunk 10; we only assert the default-preservation
property here (full dev-path coverage requires the OKX download
pipeline and is out of scope for unit tests).
"""

from __future__ import annotations

import inspect
import json
from dataclasses import dataclass

import numpy as np
import pandas as pd
import pytest

from backtest import runner
from backtest import holdout as _holdout
from backtest import trials as _trials
import backtest.engine as engine_mod
import backtest.logs as logs


# ── Fixture data ──────────────────────────────────────────────────────────────

HOLDOUT_START = "2025-09-12T14:12:00+00:00"

MANIFEST = {
    "VWAP": {
        "symbol": "ETH/USDT",
        "timeframe": "1h",
        "data_start": "2023-04-20T15:00:00+00:00",
        "data_end":   "2026-04-19T14:00:00+00:00",
        "dev_end":    HOLDOUT_START,
        "holdout_start": HOLDOUT_START,
    },
}


def _make_holdout_df(n_bars: int = 500) -> pd.DataFrame:
    """Synthetic holdout-window OHLCV (single-symbol, 1h cadence).
    Drift small enough not to overflow the engine's annualisation
    formula, large enough to produce non-trivial trades."""
    idx = pd.date_range(
        start=HOLDOUT_START, periods=n_bars, freq="1h", tz="UTC",
    )
    rng = np.random.default_rng(0)
    rets = rng.normal(0.0001, 0.005, n_bars)
    close = 100.0 * np.cumprod(1.0 + rets)
    return pd.DataFrame(
        {
            "open":   close * (1.0 - 0.0001),
            "high":   close * (1.0 + 0.0005),
            "low":    close * (1.0 - 0.0005),
            "close":  close,
            "volume": np.full(n_bars, 1000.0),
        },
        index=idx,
    )


def _full_cpcv_event(strategy_id: str = "VWAP") -> dict:
    """Schema-valid full_cpcv row to seed trials.log (so the runner
    can pull cpcv block + dsr_validation from it)."""
    return {
        "strategy_id": strategy_id,
        "variation_id": "v0",
        "trial_type": "full_cpcv",
        "params": {"lookback": 20},
        "hypothesis": "VWAP reverts within 1 day",
        "split_holdout_start": HOLDOUT_START,
        "symbols": ["ETH/USDT"],
        "n_trades": 50,
        "sharpe": 1.2,
        "cpcv": {
            "n_paths": 45,
            "n_blocks": 10,
            "k_held_out": 2,
            "purge_periods": 24,
            "embargo_periods": 6,
            "sharpe_distribution": {
                "mean": 1.0,
                "std": 0.3,
                "quantiles": {
                    "p05": 0.4, "p25": 0.7, "p50": 1.0,
                    "p75": 1.3, "p95": 1.7,
                },
            },
        },
        "dsr_validation": 0.85,
    }


@pytest.fixture
def runner_paths(tmp_path, monkeypatch):
    """Wire holdout / trials / access-log paths to tmp files and seed
    the manifest.  Returns the patched paths so tests can read/write
    them."""
    manifest_path = tmp_path / "manifest.json"
    access_log_path = tmp_path / "holdout_access.log"
    trials_log_path = tmp_path / "trials.log"

    manifest_path.write_text(json.dumps(MANIFEST), encoding="utf-8")

    monkeypatch.setattr(_holdout, "_MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(_holdout, "_ACCESS_LOG_PATH", access_log_path)
    monkeypatch.setattr(_trials, "_TRIALS_LOG_PATH", trials_log_path)
    _holdout.load_manifest.cache_clear()

    # Seed a prior full_cpcv row so the runner has something to copy
    # forensic context from.
    _trials.record_trial(_full_cpcv_event("VWAP"))

    yield {
        "manifest": manifest_path,
        "access_log": access_log_path,
        "trials_log": trials_log_path,
    }
    _holdout.load_manifest.cache_clear()


# ── Mock BacktestResult for engine patching ─────────────────────────────────

@dataclass
class _MockMetrics:
    sharpe_ratio: float
    total_trades: int
    n_candles: int = 500
    total_return_pct: float = 0.0
    annualised_return_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    calmar_ratio: float = 0.0
    volatility_pct: float = 0.0
    win_rate_pct: float = 0.0
    profit_factor: float = 0.0
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    avg_trade_pct: float = 0.0
    best_trade_pct: float = 0.0
    worst_trade_pct: float = 0.0
    total_fees_usdt: float = 0.0
    start_date: str = "2025-09-12"
    end_date: str = "2025-12-12"
    symbol: str = "ETH/USDT"
    strategy_name: str = "VWAP"


def _mock_backtest_result(sharpe: float, trades: int, eq_index: pd.Index):
    """Build a BacktestResult-shaped object with controllable Sharpe
    and trade count.  equity_curve must produce non-degenerate per-bar
    returns once pct_change'd, so we synthesise a slightly drifted
    series."""
    n = len(eq_index)
    rng = np.random.default_rng(42)
    rets = rng.normal(0.0001, 0.005, n)
    eq = pd.Series(
        10_000.0 * np.cumprod(1.0 + rets), index=eq_index,
    )
    metrics = _MockMetrics(sharpe_ratio=sharpe, total_trades=trades)
    return engine_mod.BacktestResult(
        metrics=metrics, equity_curve=eq, trade_history=[],
        period_label="final_gate-VWAP",
    )


# ── Test 1: dev is the default ──────────────────────────────────────────────

def test_run_all_mode_default_is_dev():
    """Signature inspection — the default preserves all current
    behaviour without changing any caller."""
    sig = inspect.signature(runner.run_all)
    assert "mode" in sig.parameters
    assert sig.parameters["mode"].default == "dev"


# ── Test 2: final_gate calls load_holdout once with a valid caller ──────────

def test_final_gate_calls_load_holdout_with_valid_caller(
    runner_paths, monkeypatch,
):
    """`_run_strategy_final_gate` must call `holdout.load_holdout`
    exactly once with a caller string that matches the holdout regex
    grammar (`<phase>.<strategy_id>.<purpose>`)."""
    captured: list[dict] = []

    real_load_holdout = _holdout.load_holdout

    def spy(strategy_id, *, caller, reason):
        captured.append({"strategy_id": strategy_id, "caller": caller, "reason": reason})
        return _make_holdout_df()

    monkeypatch.setattr(_holdout, "load_holdout", spy)
    # Mock engine so the test doesn't actually run the strategy logic.
    monkeypatch.setattr(
        engine_mod.BacktestEngine, "run",
        lambda self, df, strategy, period_label, universe_dfs=None:
            _mock_backtest_result(sharpe=2.5, trades=60, eq_index=df.index),
    )

    runner._run_strategy_final_gate(
        strategy_id="VWAP", timeframe="1h", balance=10_000.0,
    )

    assert len(captured) == 1, (
        f"load_holdout should be called exactly once, got {len(captured)}"
    )
    call = captured[0]
    assert call["strategy_id"] == "VWAP"
    # Caller must match the regex grammar in holdout.py.
    import re
    assert re.match(
        r"^(phase3c|phase3d|phase4|phase5|manual)\.[A-Za-z][A-Za-z0-9_]*\."
        r"(final_dsr|regression_check|manual_inspection)$",
        call["caller"],
    ), f"caller {call['caller']!r} does not match holdout regex"
    # Specifically the chunk-11 form — phase3c, strategy_id, final_dsr.
    assert call["caller"] == "phase3c.VWAP.final_dsr"


# ── Test 3: final_gate writes exactly one trials.log row with v2 fields ─────

def test_final_gate_writes_one_v2_trial_row(runner_paths, monkeypatch):
    monkeypatch.setattr(
        _holdout, "load_holdout",
        lambda strategy_id, *, caller, reason: _make_holdout_df(),
    )
    monkeypatch.setattr(
        engine_mod.BacktestEngine, "run",
        lambda self, df, strategy, period_label, universe_dfs=None:
            _mock_backtest_result(sharpe=2.5, trades=60, eq_index=df.index),
    )

    runner._run_strategy_final_gate(
        strategy_id="VWAP", timeframe="1h", balance=10_000.0,
    )

    rows = list(logs.read_jsonl(runner_paths["trials_log"]))
    final_gate_rows = [r for r in rows if r["trial_type"] == "final_gate"]
    assert len(final_gate_rows) == 1, (
        f"expected exactly one final_gate row; got {len(final_gate_rows)}"
    )

    row = final_gate_rows[0]
    # All v2 required fields present.
    for k in (
        "verdict",
        "trade_count_pass",
        "mintrl_pass",
        "mt_mean_pass",
        "baseline_pass",
        "sr_zero_expected_at_eval",
        "mintrl_required_at_eval",
        "baseline_sharpe_at_eval",
        "total_trades",
    ):
        assert k in row, f"v2 final_gate missing required field {k!r}"
    # And the v1 fields the spec said to keep.
    for k in ("sharpe", "dsr_holdout", "cpcv", "dsr_validation"):
        assert k in row, f"v1 final_gate missing field {k!r}"
    # Verdict is one of the documented values.
    assert row["verdict"] in {"keep", "retire", "under_tested"}
    # Schema version is v2.
    assert row["schema_version"] == 2


# ── Test 4: final_gate raises on second invocation (existing guard) ─────────

def test_final_gate_raises_on_second_invocation(runner_paths, monkeypatch):
    """The single-access guard fires on the second call — either via
    `holdout.HoldoutAlreadyAccessed` (load_holdout's own guard, which
    fires first) or `trials.FinalGateAlreadyRecorded` (trials.py's
    guard).  Either is acceptable; both prevent a re-run within the
    same split epoch."""
    monkeypatch.setattr(
        _holdout, "load_holdout",
        # Use a real load_holdout via the patched paths so the access
        # log is consulted (this is the test's whole point).  But
        # `_holdout.load_holdout` is the real function — paths are
        # already monkey-patched in `runner_paths`.  Re-bind here is
        # just to undo any other test's spy.
        _holdout.load_holdout.__wrapped__ if hasattr(_holdout.load_holdout, "__wrapped__")
        else _holdout.load_holdout,
    )
    # Need real cache data for load_holdout to succeed.  Easier path:
    # spy on load_holdout but track call count, and raise on second.
    # The functional point is that the guard fires — we test it by
    # asserting an exception type after the second invocation, having
    # already consumed the first.

    # Strategy: mock load_holdout to write to the access log on first
    # call (mimicking real behaviour), then on the second call check
    # the access log and raise HoldoutAlreadyAccessed.  This is what
    # the real load_holdout does, but we don't need full OHLCV cache
    # plumbing in the test.
    call_count = [0]

    def fake_load_holdout(strategy_id, *, caller, reason):
        call_count[0] += 1
        if _holdout._has_prior_access(strategy_id):
            raise _holdout.HoldoutAlreadyAccessed(
                f"Holdout for {strategy_id!r} has already been accessed."
            )
        # Append access event mirroring the real flow.
        logs.append_jsonl(_holdout._ACCESS_LOG_PATH, {
            "ts": "2026-04-25T00:00:00+00:00",
            "strategy_id": strategy_id,
            "caller": caller,
            "reason": reason,
            "git_commit": "test",
            "n_rows": 500,
            "regenerated": False,
        })
        return _make_holdout_df()

    monkeypatch.setattr(_holdout, "load_holdout", fake_load_holdout)
    monkeypatch.setattr(
        engine_mod.BacktestEngine, "run",
        lambda self, df, strategy, period_label, universe_dfs=None:
            _mock_backtest_result(sharpe=2.5, trades=60, eq_index=df.index),
    )

    # First call succeeds.
    runner._run_strategy_final_gate(
        strategy_id="VWAP", timeframe="1h", balance=10_000.0,
    )

    # Second call must raise — either guard is acceptable.
    with pytest.raises(
        (_holdout.HoldoutAlreadyAccessed, _trials.FinalGateAlreadyRecorded)
    ):
        runner._run_strategy_final_gate(
            strategy_id="VWAP", timeframe="1h", balance=10_000.0,
        )


# ── Test 5: final_gate doesn't call any OKX API ─────────────────────────────

def test_final_gate_does_not_call_okx(runner_paths, monkeypatch):
    """No path through `_run_strategy_final_gate` may invoke ccxt.
    `download_history` is the runner's only OKX touch-point in dev
    mode; final_gate must bypass it entirely (data flows through
    `holdout.load_holdout` → `cache.py` → parquet only)."""
    download_called = [0]

    def explode(*args, **kwargs):
        download_called[0] += 1
        raise AssertionError(
            "OKX download_history was called from final_gate path — "
            "paper_mode/no-network guard violated"
        )

    monkeypatch.setattr(runner, "download_history", explode)
    # Also explode if anything tries to construct ccxt.okx.
    import ccxt
    monkeypatch.setattr(
        ccxt, "okx", lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError(
                "ccxt.okx instantiated from final_gate path"
            )
        ),
    )

    monkeypatch.setattr(
        _holdout, "load_holdout",
        lambda strategy_id, *, caller, reason: _make_holdout_df(),
    )
    monkeypatch.setattr(
        engine_mod.BacktestEngine, "run",
        lambda self, df, strategy, period_label, universe_dfs=None:
            _mock_backtest_result(sharpe=2.5, trades=60, eq_index=df.index),
    )

    runner._run_strategy_final_gate(
        strategy_id="VWAP", timeframe="1h", balance=10_000.0,
    )

    assert download_called[0] == 0, (
        "download_history was called — final_gate must not touch OKX"
    )


# ── Test 6: run_all(mode="final_gate") dispatches to the helper ─────────────

def test_run_all_dispatches_to_final_gate(runner_paths, monkeypatch):
    """When mode='final_gate', run_all returns dict keys with
    holdout/verdict shape rather than is/oos shape, confirming the
    dispatch."""
    # Stub out everything heavy so this doesn't actually run any
    # strategies — just verify dispatch.
    monkeypatch.setattr(
        runner, "_run_all_final_gate",
        lambda timeframe, balance: {"_dispatched": True},
    )

    result = runner.run_all(mode="final_gate")
    assert result == {"_dispatched": True}


# ── Test 7: prior full_cpcv requirement raises if absent ────────────────────

def test_final_gate_requires_prior_full_cpcv(runner_paths, monkeypatch):
    """A strategy with no prior full_cpcv row in trials.log can't be
    final-gated — that means it was never validated on dev.  The
    runner raises a clear error rather than writing a final_gate row
    with placeholder cpcv data."""
    # Wipe the trials log so no prior full_cpcv exists.
    runner_paths["trials_log"].write_text("", encoding="utf-8")

    monkeypatch.setattr(
        _holdout, "load_holdout",
        lambda strategy_id, *, caller, reason: _make_holdout_df(),
    )

    with pytest.raises(RuntimeError, match="no prior full_cpcv"):
        runner._run_strategy_final_gate(
            strategy_id="VWAP", timeframe="1h", balance=10_000.0,
        )
