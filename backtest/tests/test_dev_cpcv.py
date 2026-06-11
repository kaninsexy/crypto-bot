"""
backtest/tests/test_dev_cpcv.py — Tests for the runner's dev_cpcv mode.

Focus: orchestration over the sacred surfaces (cpcv.run_cpcv,
deflated_sharpe, min_track_record_length, buy_and_hold_sharpe,
compute_verdict, trials.record_trial).  Heavy steps (engine, CPCV,
holdout) are stubbed; the tests verify the contract dev_cpcv promises
to its callers and to final_gate's downstream guard.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import numpy as np
import pandas as pd
import pytest

from backtest import runner
from backtest import holdout as _holdout
from backtest import trials as _trials
from backtest import cpcv as _cpcv
import backtest.engine as engine_mod
import backtest.dsr as dsr_mod
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


def _make_dev_df(n_bars: int = 1000) -> pd.DataFrame:
    """Synthetic dev-window OHLCV for VWAP (single-symbol).
    Slight upward drift so the engine produces a non-zero Sharpe and
    pct_change returns aren't all zero."""
    idx = pd.date_range(
        start="2024-01-01", periods=n_bars, freq="1h", tz="UTC",
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


@dataclass
class _MockMetrics:
    sharpe_ratio: float
    total_trades: int
    n_candles: int = 1000
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
    start_date: str = "2024-01-01"
    end_date: str = "2024-04-01"
    symbol: str = "ETH/USDT"
    strategy_name: str = "VWAP"


def _mock_backtest_result(
    sharpe: float, trades: int, eq_index: pd.Index,
) -> engine_mod.BacktestResult:
    n = len(eq_index)
    rng = np.random.default_rng(42)
    rets = rng.normal(0.0001, 0.005, n)
    eq = pd.Series(
        10_000.0 * np.cumprod(1.0 + rets), index=eq_index,
    )
    metrics = _MockMetrics(sharpe_ratio=sharpe, total_trades=trades)
    return engine_mod.BacktestResult(
        metrics=metrics, equity_curve=eq, trade_history=[],
        period_label="dev_cpcv-VWAP",
    )


def _mock_cpcv_result(n_blocks: int = 10) -> _cpcv.CPCVResult:
    """Build a CPCVResult with non-empty per_block_returns so DSR's
    concat path works.  The runner's dev_cpcv doesn't itself read
    per_block_returns (it uses the standalone engine.run for that),
    but downstream `compute_verdict` does.  Match shape, not values."""
    rng = np.random.default_rng(1)
    per_block_returns = [
        rng.normal(0.0001, 0.005, 100) for _ in range(n_blocks)
    ]
    per_path_sharpes = [0.5] * n_blocks
    trades_per_path = [10] * n_blocks
    return _cpcv.CPCVResult(
        n_paths=n_blocks,
        sharpe_distribution={
            "mean": 0.5,
            "std": 0.3,
            "quantiles": {
                "p05": 0.0, "p25": 0.2, "p50": 0.5,
                "p75": 0.8, "p95": 1.0,
            },
        },
        per_path_sharpes=per_path_sharpes,
        trades_per_path=trades_per_path,
        per_block_returns=per_block_returns,
    )


@pytest.fixture
def dev_cpcv_paths(tmp_path, monkeypatch):
    """Wire holdout / trials paths to tmp files and seed the manifest.
    No prior trials.log content — dev_cpcv is what writes the first
    full_cpcv row."""
    manifest_path = tmp_path / "manifest.json"
    access_log_path = tmp_path / "holdout_access.log"
    trials_log_path = tmp_path / "trials.log"

    manifest_path.write_text(json.dumps(MANIFEST), encoding="utf-8")

    monkeypatch.setattr(_holdout, "_MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(_holdout, "_ACCESS_LOG_PATH", access_log_path)
    monkeypatch.setattr(_trials, "_TRIALS_LOG_PATH", trials_log_path)
    _holdout.load_manifest.cache_clear()

    yield {
        "manifest": manifest_path,
        "access_log": access_log_path,
        "trials_log": trials_log_path,
    }
    _holdout.load_manifest.cache_clear()


@pytest.fixture
def dev_cpcv_stubs(monkeypatch, dev_cpcv_paths):
    """Stub out heavy dependencies of `_run_strategy_dev_cpcv`:
    `cpcv.run_cpcv` (returns canned CPCVResult), `holdout.load_dev`
    (returns synthetic OHLCV), and `BacktestEngine.run` (returns
    canned BacktestResult).  Tests can override per-call behaviour
    via further monkeypatch."""
    dev_df = _make_dev_df()

    monkeypatch.setattr(
        runner, "run_cpcv",
        lambda strategy_id, params, config, strategy_factory:
            _mock_cpcv_result(),
    )
    monkeypatch.setattr(
        _holdout, "load_dev",
        lambda strategy_id: dev_df,
    )
    monkeypatch.setattr(
        engine_mod.BacktestEngine, "run",
        lambda self, df, strategy, period_label, universe_dfs=None:
            _mock_backtest_result(
                sharpe=2.5, trades=80, eq_index=df.index,
            ),
    )
    return dev_cpcv_paths


# ── 1. RESCUE_TRIAL_BUDGET passthrough to deflated_sharpe ────────────────────

def test_rescue_trial_budget_passed_to_deflated_sharpe(
    dev_cpcv_stubs, monkeypatch,
):
    """The dev_cpcv path must call `deflated_sharpe` with
    n_trials=RESCUE_TRIAL_BUDGET (=20), bypassing
    `count_trials_for_dsr`."""
    seen_n_trials: list[int] = []

    real_deflated_sharpe = dsr_mod.deflated_sharpe

    def spy(sr_candidate, returns, n_trials, **kw):
        seen_n_trials.append(n_trials)
        return real_deflated_sharpe(
            sr_candidate=sr_candidate,
            returns=returns,
            n_trials=n_trials,
            **kw,
        )

    monkeypatch.setattr(dsr_mod, "deflated_sharpe", spy)
    # The runner imports deflated_sharpe at module-load time, so the
    # name in `runner` must also be patched for the spy to fire on
    # the standalone call.  compute_verdict imports from dsr_mod
    # internally so its call is naturally captured by the spy above.
    monkeypatch.setattr(runner, "deflated_sharpe", spy)

    runner._run_strategy_dev_cpcv(
        strategy_id="VWAP", timeframe="1h", balance=10_000.0,
    )

    assert len(seen_n_trials) >= 1, (
        "expected at least one deflated_sharpe call, got 0"
    )
    # Every call must use the policy budget (20).  Both the runner's
    # standalone call and the verdict layer's internal call should
    # see the same number — that's the whole point of bypassing
    # count_trials_for_dsr.
    for n in seen_n_trials:
        assert n == 20, (
            f"deflated_sharpe called with n_trials={n}; expected 20 "
            "(RESCUE_TRIAL_BUDGET)"
        )


# ── 2. Trial row carries n_trials=20 explicitly ──────────────────────────────

def test_trial_row_has_explicit_n_trials_20(dev_cpcv_stubs):
    """The trials.log row must record n_trials=20 as an explicit
    field, so downstream readers don't have to recompute or guess."""
    runner._run_strategy_dev_cpcv(
        strategy_id="VWAP", timeframe="1h", balance=10_000.0,
    )

    rows = list(logs.read_jsonl(dev_cpcv_stubs["trials_log"]))
    full_cpcv_rows = [r for r in rows if r["trial_type"] == "full_cpcv"]
    assert len(full_cpcv_rows) == 1
    assert full_cpcv_rows[0]["n_trials"] == 20


# ── 3. Trial row matches v1 schema (no verdict field) ───────────────────────

def test_trial_row_matches_v1_schema_no_verdict(dev_cpcv_stubs):
    """The full_cpcv row dev_cpcv writes uses the v1 schema — verdict
    info lives in the returned DevCpcvResult, not in the trial row.
    This protects v1 trials.log readers (final_gate's guard) from
    schema drift, and respects 'v1 schema is sacred' for full_cpcv."""
    runner._run_strategy_dev_cpcv(
        strategy_id="VWAP", timeframe="1h", balance=10_000.0,
    )
    rows = list(logs.read_jsonl(dev_cpcv_stubs["trials_log"]))
    row = rows[0]
    # No verdict field on the row.
    assert "verdict" not in row, (
        "full_cpcv row must not carry a 'verdict' field — that is a "
        "v2 final_gate field, not v1 full_cpcv"
    )
    # No v2 final_gate component bools either.
    for k in (
        "trade_count_pass", "mintrl_pass",
        "mt_mean_pass", "baseline_pass",
        "sr_zero_expected_at_eval", "mintrl_required_at_eval",
        "baseline_sharpe_at_eval",
    ):
        assert k not in row, (
            f"full_cpcv row must not carry v2 field {k!r}"
        )
    # But v1 required fields ARE present.
    for k in (
        "strategy_id", "trial_type", "params", "hypothesis",
        "split_holdout_start", "symbols", "n_trades", "sharpe",
        "cpcv", "dsr_validation",
    ):
        assert k in row, f"v1 row missing required field {k!r}"


# ── 4. DevCpcvResult contains verdict ───────────────────────────────────────

def test_result_object_contains_verdict(dev_cpcv_stubs):
    """The returned DevCpcvResult exposes the VerdictResult so the
    caller can log it / branch on it without re-reading trials.log."""
    res = runner._run_strategy_dev_cpcv(
        strategy_id="VWAP", timeframe="1h", balance=10_000.0,
    )
    assert isinstance(res, runner.DevCpcvResult)
    from backtest.verdict import VerdictResult
    assert isinstance(res.verdict, VerdictResult)
    assert res.verdict.verdict in {"keep", "retire", "under_tested"}
    assert res.strategy_id == "VWAP"
    # Numeric fields populated.
    import math
    for field_name in (
        "observed_sharpe",
        "sr_zero_expected",
        "dsr_pvalue",
        "mintrl",
        "baseline_sharpe",
    ):
        v = getattr(res, field_name)
        assert math.isfinite(v), (
            f"{field_name} should be finite, got {v}"
        )


# ── 5. Atomic-write: failure mid-flow leaves trials.log unchanged ───────────

def test_atomic_write_failure_leaves_no_partial_row(
    dev_cpcv_stubs, monkeypatch,
):
    """If `compute_verdict` (or any pre-record_trial step) raises,
    no row may be written to trials.log.  record_trial is the
    last side-effect; everything before it must complete first."""
    # Force compute_verdict to raise after CPCV / engine / DSR / MinTRL
    # have all completed but before record_trial fires.
    def boom(*args, **kwargs):
        raise RuntimeError("simulated mid-flow failure")
    monkeypatch.setattr(runner, "compute_verdict", boom)

    with pytest.raises(RuntimeError, match="simulated mid-flow failure"):
        runner._run_strategy_dev_cpcv(
            strategy_id="VWAP", timeframe="1h", balance=10_000.0,
        )

    # trials.log must be empty (or absent) — no partial row.
    trials_log = dev_cpcv_stubs["trials_log"]
    if trials_log.exists():
        rows = list(logs.read_jsonl(trials_log))
        assert rows == [], (
            f"expected trials.log empty after mid-flow failure; "
            f"got {rows}"
        )


# ── 6. Integration: row final_gate's guard accepts ───────────────────────────

def test_dev_cpcv_row_satisfies_final_gate_guard(dev_cpcv_stubs):
    """The row dev_cpcv writes must be the same shape final_gate's
    `_latest_full_cpcv_event` reads.  This is the cross-mode contract
    — dev_cpcv produces the prerequisite for final_gate."""
    runner._run_strategy_dev_cpcv(
        strategy_id="VWAP", timeframe="1h", balance=10_000.0,
    )

    prior = runner._latest_full_cpcv_event("VWAP")
    assert prior is not None, (
        "_latest_full_cpcv_event returned None despite dev_cpcv "
        "having written a row"
    )
    # Final_gate accesses these fields when building its row.
    assert "cpcv" in prior
    assert "dsr_validation" in prior
    assert prior.get("variation_id") is not None
    assert prior.get("hypothesis") is not None
    assert prior.get("split_holdout_start") is not None
    assert prior.get("symbols") is not None
    # cpcv block has the keys the schema validator + final_gate care
    # about.
    cpcv_block = prior["cpcv"]
    for k in (
        "n_paths", "n_blocks", "k_held_out",
        "purge_periods", "embargo_periods",
        "sharpe_distribution",
    ):
        assert k in cpcv_block, f"cpcv block missing {k!r}"
    sd = cpcv_block["sharpe_distribution"]
    assert set(sd.keys()) == {"mean", "std", "quantiles"}
    assert set(sd["quantiles"].keys()) == {
        "p05", "p25", "p50", "p75", "p95",
    }


# ── 7. run_all dispatch ──────────────────────────────────────────────────────

def test_run_all_dev_cpcv_dispatches(dev_cpcv_paths, monkeypatch):
    """run_all(mode='dev_cpcv') calls _run_all_dev_cpcv."""
    monkeypatch.setattr(
        runner, "_run_all_dev_cpcv",
        lambda timeframe, balance: {"_dispatched": True},
    )
    out = runner.run_all(mode="dev_cpcv")
    assert out == {"_dispatched": True}


# ── 8. Determinism: two runs with same fixtures produce equal rows ──────────

def test_dev_cpcv_determinism_same_inputs(dev_cpcv_stubs):
    """Two runs against the same stubbed surfaces produce structurally
    identical trial rows (modulo the writer-filled ts / trial_id /
    git_commit / params_hash, which are nondeterministic by design)."""
    res1 = runner._run_strategy_dev_cpcv(
        strategy_id="VWAP", timeframe="1h", balance=10_000.0,
    )

    # Wipe trials.log between runs so the second call writes fresh.
    dev_cpcv_stubs["trials_log"].write_text("", encoding="utf-8")

    res2 = runner._run_strategy_dev_cpcv(
        strategy_id="VWAP", timeframe="1h", balance=10_000.0,
    )

    # Verdict must match.
    assert res1.verdict.verdict == res2.verdict.verdict
    # Numeric outputs must match exactly.
    assert res1.observed_sharpe == res2.observed_sharpe
    assert res1.sr_zero_expected == res2.sr_zero_expected
    assert res1.dsr_pvalue == res2.dsr_pvalue
    assert res1.mintrl == res2.mintrl
    assert res1.baseline_sharpe == res2.baseline_sharpe

    # Row content match (excluding writer-filled fields).
    skip = {"ts", "trial_id", "git_commit", "params_hash", "schema_version"}
    row1 = {k: v for k, v in res1.trial_row.items() if k not in skip}
    row2 = {k: v for k, v in res2.trial_row.items() if k not in skip}
    assert row1 == row2


# ── 9. dev_cpcv does not touch holdout ──────────────────────────────────────

def test_dev_cpcv_does_not_call_load_holdout(dev_cpcv_stubs, monkeypatch):
    """dev_cpcv uses the dev window only (`load_dev`).  Any call to
    `load_holdout` from this path is a bug — that would consume the
    audited single-access slot before final_gate."""
    holdout_calls = [0]

    def explode(*args, **kwargs):
        holdout_calls[0] += 1
        raise AssertionError(
            "load_holdout was called from dev_cpcv path — holdout "
            "access from rescue iteration is forbidden"
        )

    monkeypatch.setattr(_holdout, "load_holdout", explode)

    runner._run_strategy_dev_cpcv(
        strategy_id="VWAP", timeframe="1h", balance=10_000.0,
    )

    assert holdout_calls[0] == 0


# ── Bug 1: cpcv block metadata reflects CPCVConfig ──────────────────────────

def test_cpcv_block_metadata_reflects_config_defaults(dev_cpcv_paths):
    """The cpcv block's n_blocks / k_held_out / purge_periods /
    embargo_periods come from the supplied CPCVConfig — not hardcoded
    or derived from CPCVResult.n_paths."""
    cfg = _cpcv.CPCVConfig()  # defaults: n_blocks=10, k=2, purge=0, embargo=0
    cpcv_result = _mock_cpcv_result(n_blocks=cfg.n_blocks)
    headline = _mock_backtest_result(
        sharpe=2.5, trades=80,
        eq_index=pd.date_range("2024-01-01", periods=10, freq="1h", tz="UTC"),
    )

    row = runner._build_full_cpcv_row(
        strategy_id="VWAP",
        primary_symbol="ETH/USDT",
        cpcv_result=cpcv_result,
        cpcv_config=cfg,
        headline_result=headline,
        dsr_validation_value=0.85,
        n_trials=20,
    )
    block = row["cpcv"]
    assert block["n_blocks"] == cfg.n_blocks == 10
    assert block["k_held_out"] == cfg.k_held_out == 2
    assert block["purge_periods"] == cfg.purge_periods == 0
    assert block["embargo_periods"] == cfg.embargo_periods == 0
    # n_paths still tracks the realised count from the result.
    assert block["n_paths"] == cpcv_result.n_paths


def test_cpcv_block_metadata_reflects_config_overrides(dev_cpcv_paths):
    """Same mapping, custom CPCVConfig values — no field comes from
    a hardcoded fallback."""
    cfg = _cpcv.CPCVConfig(
        n_blocks=8, k_held_out=3, purge_periods=24, embargo_periods=6,
    )
    cpcv_result = _mock_cpcv_result(n_blocks=8)
    headline = _mock_backtest_result(
        sharpe=2.5, trades=80,
        eq_index=pd.date_range("2024-01-01", periods=10, freq="1h", tz="UTC"),
    )

    row = runner._build_full_cpcv_row(
        strategy_id="VWAP",
        primary_symbol="ETH/USDT",
        cpcv_result=cpcv_result,
        cpcv_config=cfg,
        headline_result=headline,
        dsr_validation_value=0.85,
        n_trials=20,
    )
    block = row["cpcv"]
    assert block["n_blocks"] == 8
    assert block["k_held_out"] == 3
    assert block["purge_periods"] == 24
    assert block["embargo_periods"] == 6


# ── Bug 2: DSR / MinTRL / verdict receive the per-block-concat series ──────

def _expected_concat_returns(cpcv_result: _cpcv.CPCVResult) -> np.ndarray:
    """Reconstruct the concat the runner is supposed to feed
    downstream — same logic as `_concat_per_block_returns` but
    computed independently in the test for verification."""
    valid = [r for r in cpcv_result.per_block_returns if len(r) > 0]
    return np.concatenate(valid).astype(float)


def test_deflated_sharpe_receives_per_block_concat(
    dev_cpcv_paths, monkeypatch,
):
    """`deflated_sharpe` must receive np.concatenate(non-empty
    per_block_returns), NOT engine equity_curve.pct_change().
    This is the dsr_validation contract from dsr.py's docstring."""
    # Inject a CPCVResult with mixed empty / non-empty blocks so we
    # can verify both that the concat happens AND that empty arrays
    # are skipped.
    rng = np.random.default_rng(123)
    per_block = [
        rng.normal(0.0001, 0.005, 100),  # block 0 — non-empty
        np.array([], dtype=float),       # block 1 — empty (skipped)
        rng.normal(0.0001, 0.005, 80),   # block 2 — non-empty
        np.array([], dtype=float),       # block 3 — empty (skipped)
        rng.normal(0.0001, 0.005, 120),  # block 4 — non-empty
    ]
    cpcv_result = _cpcv.CPCVResult(
        n_paths=5,
        sharpe_distribution={
            "mean": 0.5, "std": 0.3,
            "quantiles": {
                "p05": 0.0, "p25": 0.2, "p50": 0.5,
                "p75": 0.8, "p95": 1.0,
            },
        },
        per_path_sharpes=[0.5, float("nan"), 0.5, float("nan"), 0.5],
        trades_per_path=[10, 0, 8, 0, 12],
        per_block_returns=per_block,
    )
    expected_concat = _expected_concat_returns(cpcv_result)
    assert expected_concat.size == 100 + 80 + 120  # empties skipped

    monkeypatch.setattr(
        runner, "run_cpcv",
        lambda strategy_id, params, config, strategy_factory: cpcv_result,
    )
    monkeypatch.setattr(
        _holdout, "load_dev",
        lambda strategy_id: _make_dev_df(),
    )
    monkeypatch.setattr(
        engine_mod.BacktestEngine, "run",
        lambda self, df, strategy, period_label, universe_dfs=None:
            _mock_backtest_result(
                sharpe=2.5, trades=80, eq_index=df.index,
            ),
    )

    seen_returns: list[np.ndarray] = []
    real_deflated_sharpe = dsr_mod.deflated_sharpe

    def spy(sr_candidate, returns, n_trials, **kw):
        seen_returns.append(np.asarray(returns, dtype=float).copy())
        return real_deflated_sharpe(
            sr_candidate=sr_candidate, returns=returns, n_trials=n_trials,
            **kw,
        )

    monkeypatch.setattr(dsr_mod, "deflated_sharpe", spy)
    monkeypatch.setattr(runner, "deflated_sharpe", spy)

    runner._run_strategy_dev_cpcv(
        strategy_id="VWAP", timeframe="1h", balance=10_000.0,
    )

    assert len(seen_returns) >= 1
    for got in seen_returns:
        # Length must match the concat — proves empties were skipped
        # and engine returns weren't substituted.
        assert got.size == expected_concat.size, (
            f"deflated_sharpe got returns of size {got.size}; "
            f"expected per-block concat size {expected_concat.size}"
        )
        # Element-wise match clinches the contract.
        np.testing.assert_array_equal(got, expected_concat)


def test_min_track_record_length_receives_per_block_concat(
    dev_cpcv_stubs, monkeypatch,
):
    """min_track_record_length must receive the same per-block concat
    as deflated_sharpe — its docstring says so explicitly under
    'dsr_validation context'."""
    seen: list[np.ndarray] = []
    real_mintrl = dsr_mod.min_track_record_length

    def spy(sr_candidate, returns, confidence=0.95, **kw):
        seen.append(np.asarray(returns, dtype=float).copy())
        return real_mintrl(
            sr_candidate=sr_candidate,
            returns=returns,
            confidence=confidence,
            **kw,
        )

    monkeypatch.setattr(dsr_mod, "min_track_record_length", spy)
    monkeypatch.setattr(runner, "min_track_record_length", spy)

    # Stubbed cpcv_result from dev_cpcv_stubs (10 blocks × 100 bars).
    runner._run_strategy_dev_cpcv(
        strategy_id="VWAP", timeframe="1h", balance=10_000.0,
    )

    assert len(seen) >= 1
    # 10 non-empty blocks × 100 bars each = 1000 elements.
    for got in seen:
        assert got.size == 10 * 100, (
            f"min_track_record_length got returns of size {got.size}; "
            f"expected 1000 (10 blocks × 100 bars)"
        )


def test_compute_verdict_receives_per_block_concat(
    dev_cpcv_stubs, monkeypatch,
):
    """compute_verdict's `returns` plumbs through to
    deflated_sharpe + min_track_record_length, so dev_cpcv must pass
    it the per-block concat — same series, not engine returns."""
    seen_returns: list[np.ndarray] = []
    real_compute_verdict = runner.compute_verdict

    def spy(**kwargs):
        seen_returns.append(
            np.asarray(kwargs["returns"], dtype=float).copy()
        )
        return real_compute_verdict(**kwargs)

    monkeypatch.setattr(runner, "compute_verdict", spy)

    runner._run_strategy_dev_cpcv(
        strategy_id="VWAP", timeframe="1h", balance=10_000.0,
    )

    assert len(seen_returns) == 1
    # Stubbed cpcv has 10 blocks × 100 bars, all non-empty.
    assert seen_returns[0].size == 10 * 100, (
        f"compute_verdict got returns of size {seen_returns[0].size}; "
        f"expected the per-block concat (1000)"
    )


def test_concat_per_block_returns_skips_empty_arrays():
    """Direct unit test of the helper — empty arrays out, others kept
    in original order, dtype float."""
    rng = np.random.default_rng(0)
    per_block = [
        rng.normal(0, 1, 50),
        np.array([], dtype=float),
        rng.normal(0, 1, 30),
        np.array([], dtype=float),
        rng.normal(0, 1, 20),
    ]
    cpcv_result = _cpcv.CPCVResult(
        n_paths=5,
        sharpe_distribution={
            "mean": 0.0, "std": 1.0,
            "quantiles": {
                "p05": 0.0, "p25": 0.0, "p50": 0.0,
                "p75": 0.0, "p95": 0.0,
            },
        },
        per_path_sharpes=[0.0] * 5,
        trades_per_path=[10] * 5,
        per_block_returns=per_block,
    )
    out = runner._concat_per_block_returns(cpcv_result)
    assert out.dtype == np.float64
    assert out.size == 50 + 30 + 20
    # Order preserved.
    np.testing.assert_array_equal(out[:50], per_block[0])
    np.testing.assert_array_equal(out[50:80], per_block[2])
    np.testing.assert_array_equal(out[80:100], per_block[4])


def test_concat_per_block_returns_all_empty_raises_clear_error():
    """All blocks empty → RuntimeError with a dev_cpcv-flavoured
    message (not a numpy 'need at least one array' leak)."""
    cpcv_result = _cpcv.CPCVResult(
        n_paths=3,
        sharpe_distribution={
            "mean": 0.0, "std": 0.0,
            "quantiles": {
                "p05": 0.0, "p25": 0.0, "p50": 0.0,
                "p75": 0.0, "p95": 0.0,
            },
        },
        per_path_sharpes=[float("nan")] * 3,
        trades_per_path=[0, 0, 0],
        per_block_returns=[
            np.array([], dtype=float),
            np.array([], dtype=float),
            np.array([], dtype=float),
        ],
    )
    with pytest.raises(RuntimeError, match="non-empty per_block_returns"):
        runner._concat_per_block_returns(cpcv_result)
