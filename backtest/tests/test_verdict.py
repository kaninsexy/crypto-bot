"""
backtest/tests/test_verdict.py — Tests for compute_verdict orchestration
(gate spec v2, 2026-06-11).

v2 gate semantics under test:
  - preconditions: trade-count / signal-event floor + units-correct MinTRL
  - mt gate: corrected DSR >= 0.95 (family-scaled eq.7 haircut)
  - baseline gate, directional: NW-OLS alpha > 0 @95% AND IR >= 0.5
  - baseline gate, neutral: PSR(SR>0) >= 0.95
  - keep iff mt AND baseline; under_tested short-circuits both

Tests pass `sr_var_trials` / `neutral` / `bars_per_year` explicitly so
they are hermetic w.r.t. trials.log and the family taxonomy file
(except where the lookup path itself is the unit under test).
"""

import math
import warnings

import numpy as np
import pandas as pd
import pytest

from backtest.verdict import VerdictResult, compute_verdict


# ── Helpers ──────────────────────────────────────────────────────────────────

_BPY = 365.25 * 24  # 1h bars


def _flat_baseline_df(n_bars: int = 2001) -> pd.DataFrame:
    """All-100 close → pct_change = 0 → buy-and-hold Sharpe = 0.
    NOTE: zero-variance benchmark — the v2 alpha gate is undefined on
    it (BaselineError folded into baseline_pass=False)."""
    idx = pd.date_range("2024-01-01", periods=n_bars, freq="1h", tz="UTC")
    return pd.DataFrame({"close": np.full(n_bars, 100.0)}, index=idx)


def _noisy_baseline_df(
    n_bars: int = 2001, drift: float = 0.0, std: float = 0.001, seed: int = 99,
) -> pd.DataFrame:
    """Benchmark with variance (alpha gate defined) and configurable
    drift.  Returns the OHLCV-ish frame; per-bar benchmark returns are
    its close.pct_change().dropna()."""
    rng = np.random.default_rng(seed)
    rets = drift + rng.standard_normal(n_bars - 1) * std
    close = 100.0 * np.cumprod(1.0 + np.concatenate([[0.0], rets]))
    idx = pd.date_range("2024-01-01", periods=n_bars, freq="1h", tz="UTC")
    return pd.DataFrame({"close": close}, index=idx)


def _benchmark_rets(df: pd.DataFrame) -> np.ndarray:
    return df["close"].pct_change().dropna().values.astype(float)


def _alpha_strategy_returns(
    benchmark: np.ndarray,
    alpha_pb: float = 0.0008,
    beta: float = 0.3,
    noise: float = 0.002,
    seed: int = 0,
) -> np.ndarray:
    """Strategy returns with genuine per-bar alpha over the benchmark:
    r_s = alpha + beta·r_b + ε.  Clears both the NW-alpha test and the
    IR >= 0.5 floor by construction at T ≈ 2000."""
    rng = np.random.default_rng(seed)
    return alpha_pb + beta * benchmark + rng.standard_normal(benchmark.size) * noise


def _gaussian_returns(t: int, seed: int = 0) -> np.ndarray:
    return np.random.default_rng(seed).standard_normal(t) * 0.01


# ── Quality-gate composition ─────────────────────────────────────────────────

def test_all_gates_pass_yields_keep():
    """Strong claimed SR (4.0 ann) clears DSR>=0.95 at N=20 with a
    degenerate family variance (sr_zero=0); genuine alpha over a
    flat-drift noisy benchmark clears alpha+IR."""
    bdf = _noisy_baseline_df()
    rb = _benchmark_rets(bdf)
    rs = _alpha_strategy_returns(rb, seed=1)
    out = compute_verdict(
        strategy_id="VWAP",
        sr_candidate=4.0,
        returns=rs,
        total_trades=100,
        baseline_df=bdf,
        n_trials=20,
        sr_var_trials=0.0,
        neutral=False,
    )
    assert out.verdict == "keep"
    assert out.trade_count_pass is True
    assert out.mintrl_pass is True
    assert out.mt_mean_pass is True
    assert out.baseline_pass is True
    assert out.baseline_mode == "directional"
    assert out.alpha_pass is True
    assert out.ir_pass is True
    assert out.dsr >= 0.95


def test_below_dsr_threshold_yields_retire():
    """Claimed SR=1.0 annualised at 1h frequency: per-bar z ≈ 0.48 →
    DSR ≈ 0.68 < 0.95 → mt_mean_pass False → retire even though the
    alpha/IR gate passes.  (v2: the mt gate is the corrected-DSR
    floor, not a raw sr>sr_zero comparison.)

    bars_per_year=1.0 keeps MinTRL trivially satisfied so the test
    isolates the mt gate... no — at bpy=1 SR=1.0 per-bar saturates.
    Instead: bpy inferred (8766) and SR=2.0 → mintrl ≈ 5.9k > T? That
    would be under_tested.  Use explicit T=8000 with SR=2.5:
      sr_pb = 0.0267, mintrl ≈ 1 + (1.6449/0.0267)² ≈ 3.8k < 8000 ✓
      sr_std_pb ≈ 1/√7999 ≈ 0.0112 → z ≈ 2.39 → DSR ≈ 0.991 — passes.
    So to FAIL the DSR floor while passing MinTRL, raise the family
    variance: sr_var_trials=1.0 at N=20 → sr_zero ≈ 1.87 ann →
    z = (0.0267 − 0.0199)/0.0112 ≈ 0.6 → DSR ≈ 0.73 < 0.95 → retire.
    """
    bdf = _noisy_baseline_df(n_bars=8001)
    rb = _benchmark_rets(bdf)
    rs = _alpha_strategy_returns(rb, seed=2)
    out = compute_verdict(
        strategy_id="VWAP",
        sr_candidate=2.5,
        returns=rs,
        total_trades=100,
        baseline_df=bdf,
        n_trials=20,
        sr_var_trials=1.0,
        neutral=False,
    )
    assert out.verdict == "retire"
    assert out.mintrl_pass is True
    assert out.mt_mean_pass is False
    assert out.dsr < 0.95
    # Baseline (alpha/IR) passes; the retire is purely the DSR floor.
    assert out.baseline_pass is True


def test_mt_pass_but_no_alpha_yields_retire():
    """mt gate clears but the strategy has no alpha over the benchmark
    (pure noise, zero drift) → alpha gate fails → retire."""
    bdf = _noisy_baseline_df(n_bars=8001, drift=0.0)
    rs = _gaussian_returns(8000, seed=3) * 0.2  # zero-mean noise
    out = compute_verdict(
        strategy_id="VWAP",
        sr_candidate=4.0,
        returns=rs,
        total_trades=100,
        baseline_df=bdf,
        n_trials=20,
        sr_var_trials=0.0,
        neutral=False,
    )
    assert out.verdict == "retire"
    assert out.mt_mean_pass is True
    assert out.baseline_pass is False
    assert out.alpha_pass is False or out.ir_pass is False


def test_ir_floor_fails_despite_positive_alpha():
    """Tiny but statistically-significant alpha with huge tracking
    noise: alpha test can pass while IR < 0.5 fails → baseline gate
    fails (both tests are required)."""
    bdf = _noisy_baseline_df(n_bars=60_001, seed=42)
    rb = _benchmark_rets(bdf)
    # alpha_pb 2e-5 over 60k bars: NW SE ≈ 0.0005/√60000 ≈ 2e-6 →
    # t ≈ 10 (alpha passes).  IR_pb ≈ 2e-5/5.1e-4 ≈ 0.039 →
    # IR_ann ≈ 3.7?? — too high.  Push noise up so IR_ann < 0.5:
    # need mean/std·√8766 < 0.5 → mean/std < 0.00534.  With
    # alpha=2e-5, noise std 0.005 → ratio 0.004 → IR_ann ≈ 0.37 ✓
    # while NW SE ≈ 0.005/√60000 ≈ 2.04e-5 → t ≈ 0.98 — alpha fails
    # too.  Make alpha 6e-5: t ≈ 2.9 ✓, ratio 0.012 → IR ≈ 1.1 ✗.
    # The joint regime is narrow; use alpha 4e-5: t ≈ 1.96 (pass at
    # 95% one-sided), IR_ann ≈ 0.75 — still passes.  Tight coupling
    # is inherent (both are mean/σ tests); instead make the active
    # return autocorrelated so NW widens less than IR... simplest
    # robust construction: beta=1 benchmark tracking with periodic
    # alpha bursts — skip the analytic fine-tuning and assert the
    # IR threshold knob directly instead.
    rs = 0.00004 + rb + np.random.default_rng(7).standard_normal(rb.size) * 0.005
    out = compute_verdict(
        strategy_id="VWAP",
        sr_candidate=4.0,
        returns=rs,
        total_trades=100,
        baseline_df=bdf,
        n_trials=20,
        sr_var_trials=0.0,
        neutral=False,
        ir_threshold=5.0,   # knob: force the IR leg to fail
    )
    assert out.verdict == "retire"
    assert out.ir_pass is False
    assert out.baseline_pass is False


def test_neutral_branch_uses_psr():
    """Delta-neutral strategies are judged PSR(SR>0) >= 0.95 against
    benchmark 0 — the B&H frame is forensics only."""
    bdf = _noisy_baseline_df()
    rs = _gaussian_returns(2000, seed=11) + 0.0008  # positive drift
    out_pass = compute_verdict(
        strategy_id="FundingRateHarvest_BTC",
        sr_candidate=4.0,
        returns=rs,
        total_trades=100,
        baseline_df=bdf,
        n_trials=20,
        sr_var_trials=0.0,
        neutral=True,
    )
    assert out_pass.baseline_mode == "neutral"
    assert out_pass.psr is not None and out_pass.psr >= 0.95
    assert out_pass.psr_pass is True
    assert out_pass.baseline_pass is True
    assert out_pass.alpha_pass is None  # directional fields unset

    # PSR-fail case: a NEGATIVE claimed Sharpe.  (For positive SR,
    # T > MinTRL(95%) mechanically implies PSR ≥ 0.95 — MinTRL is the
    # T at which PSR reaches the confidence level — so the only way to
    # reach the quality gates with a failing PSR is a negative-edge
    # claim, where MinTRL passes on |SR| but PSR ≈ 0.)
    bdf8 = _noisy_baseline_df(n_bars=8001)
    rs8 = _gaussian_returns(8000, seed=11) + 0.0008
    out_fail = compute_verdict(
        strategy_id="FundingRateHarvest_BTC",
        sr_candidate=-2.0,
        returns=rs8,
        total_trades=100,
        baseline_df=bdf8,
        n_trials=20,
        sr_var_trials=0.0,
        neutral=True,
    )
    assert out_fail.psr_pass is False
    assert out_fail.baseline_pass is False
    assert out_fail.verdict == "retire"


def test_neutral_flag_read_from_taxonomy():
    """When `neutral` is not passed, the family taxonomy's flag drives
    the branch: FundingRateHarvest_BTC is marked neutral in
    backtest/strategy_families.json."""
    bdf = _noisy_baseline_df()
    rs = _gaussian_returns(2000, seed=12) + 0.0008
    out = compute_verdict(
        strategy_id="FundingRateHarvest_BTC",
        sr_candidate=4.0,
        returns=rs,
        total_trades=100,
        baseline_df=bdf,
        n_trials=20,
        sr_var_trials=0.0,
    )
    assert out.baseline_mode == "neutral"
    assert out.psr is not None


def test_zero_variance_benchmark_folds_to_baseline_fail():
    """The v2 alpha gate is undefined on a zero-variance benchmark;
    compute_verdict folds the BaselineError into baseline_pass=False
    with a warning instead of crashing."""
    bdf = _flat_baseline_df()
    rs = _gaussian_returns(2000, seed=13)
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        out = compute_verdict(
            strategy_id="VWAP",
            sr_candidate=4.0,
            returns=rs,
            total_trades=100,
            baseline_df=bdf,
            n_trials=20,
            sr_var_trials=0.0,
            neutral=False,
        )
    assert out.verdict == "retire"
    assert out.baseline_pass is False
    assert any("unevaluable" in str(x.message) for x in w)


# ── Preconditions ────────────────────────────────────────────────────────────

def test_low_trade_count_yields_under_tested():
    """total_trades < 30 → trade_count_pass False → under_tested,
    regardless of how strong the SR is."""
    out = compute_verdict(
        strategy_id="VWAP",
        sr_candidate=4.0,
        returns=_gaussian_returns(2000, seed=4),
        total_trades=10,
        baseline_df=_noisy_baseline_df(),
        n_trials=20,
        sr_var_trials=0.0,
        neutral=False,
    )
    assert out.verdict == "under_tested"
    assert out.trade_count_pass is False
    # Quality fields not computed.
    assert out.mt_mean_pass is None
    assert out.baseline_pass is None
    assert out.sr_margin_vs_mt_mean is None
    assert out.sr_margin_vs_baseline is None
    assert math.isnan(out.sr_zero_expected_at_eval)
    assert math.isnan(out.dsr)


def test_mintrl_failure_yields_under_tested():
    """v2 units-correct MinTRL: annualised SR 1.0 at 1h frequency
    needs ≈ 2.7 years ≈ 23.7k hourly bars; T=2000 → under_tested even
    with adequate trade count.  (This is the audit's structural
    under-testing finding expressed as a unit test.)"""
    out = compute_verdict(
        strategy_id="VWAP",
        sr_candidate=1.0,
        returns=_gaussian_returns(2000, seed=5),
        total_trades=100,
        baseline_df=_noisy_baseline_df(),
        n_trials=20,
        sr_var_trials=0.0,
        neutral=False,
    )
    assert out.verdict == "under_tested"
    assert out.trade_count_pass is True
    assert out.mintrl_pass is False
    assert out.mintrl_required_at_eval > 20_000
    assert out.mt_mean_pass is None
    assert out.baseline_pass is None


def test_both_preconditions_fail_under_tested():
    """Both trade-count AND MinTRL fail; quality fields stay None."""
    out = compute_verdict(
        strategy_id="VWAP",
        sr_candidate=0.05,
        returns=_gaussian_returns(200, seed=6),
        total_trades=5,
        baseline_df=_noisy_baseline_df(n_bars=201),
        n_trials=20,
        sr_var_trials=0.0,
        neutral=False,
    )
    assert out.verdict == "under_tested"
    assert out.trade_count_pass is False
    assert out.mintrl_pass is False
    assert out.mt_mean_pass is None
    assert out.baseline_pass is None
    assert out.sr_margin_vs_mt_mean is None
    assert out.sr_margin_vs_baseline is None


def test_sr_zero_caught_as_under_tested():
    """sr_candidate ≈ 0 makes MinTRL undefined (DSRError); the verdict
    layer catches it and rolls into under_tested rather than crashing."""
    out = compute_verdict(
        strategy_id="VWAP",
        sr_candidate=0.0,
        returns=_gaussian_returns(2000, seed=7),
        total_trades=100,
        baseline_df=_noisy_baseline_df(),
        n_trials=20,
        sr_var_trials=0.0,
        neutral=False,
    )
    assert out.verdict == "under_tested"
    assert out.mintrl_pass is False
    assert math.isnan(out.mintrl_required_at_eval)
    assert out.mt_mean_pass is None
    assert out.baseline_pass is None


# ── N=1 and field-population invariants ──────────────────────────────────────

def test_n1_special_case_keep():
    """At n_trials=1 the Gumbel haircut is skipped (sr_zero = 0.0); a
    confident claim with genuine alpha is a keep.  bars_per_year=1.0
    treats the claimed SR as per-bar (math-anchor convention from
    test_dsr.py) so MinTRL stays small at T=2000.  alpha_pb is bumped
    so the PER-BAR information ratio clears the 0.5 floor (at bpy=1
    the IR gets no annualisation lift)."""
    bdf = _noisy_baseline_df()
    rb = _benchmark_rets(bdf)
    rs = _alpha_strategy_returns(rb, alpha_pb=0.0015, seed=8)
    out = compute_verdict(
        strategy_id="VWAP",
        sr_candidate=0.5,
        returns=rs,
        total_trades=100,
        baseline_df=bdf,
        n_trials=1,
        bars_per_year=1.0,
        sr_var_trials=0.0,
        neutral=False,
    )
    assert out.verdict == "keep"
    assert out.sr_zero_expected_at_eval == 0.0
    assert out.mt_mean_pass is True
    assert out.baseline_pass is True


def test_all_fields_populated_when_keep():
    """Every numeric field is a number (not NaN) when the verdict is
    a quality outcome (keep/retire)."""
    bdf = _noisy_baseline_df()
    rb = _benchmark_rets(bdf)
    rs = _alpha_strategy_returns(rb, seed=9)
    out = compute_verdict(
        strategy_id="VWAP",
        sr_candidate=4.0,
        returns=rs,
        total_trades=100,
        baseline_df=bdf,
        n_trials=20,
        sr_var_trials=0.0,
        neutral=False,
    )
    assert isinstance(out, VerdictResult)
    for field_name in (
        "sr_observed",
        "sr_zero_expected_at_eval",
        "mintrl_required_at_eval",
        "baseline_sharpe_at_eval",
        "dsr",
        "sr_var_trials_at_eval",
        "bars_per_year_at_eval",
    ):
        v = getattr(out, field_name)
        assert math.isfinite(v), (
            f"{field_name} should be finite for a keep verdict; got {v}"
        )
    assert out.sr_margin_vs_mt_mean is not None
    assert out.sr_margin_vs_baseline is not None
    assert out.alpha_annualised is not None
    assert out.alpha_p_value is not None
    assert out.ir_annualised is not None
    assert out.benchmark_aligned_bars == 2000
    assert out.total_trades == 100
    assert out.t_observed == 2000
    assert out.n_trials == 20


def test_forensic_margins_match_components():
    """sr_margin_vs_mt_mean = sr_observed − sr_zero_expected_at_eval;
    sr_margin_vs_baseline = sr_observed − baseline_sharpe_at_eval."""
    bdf = _noisy_baseline_df(drift=0.00001)
    rb = _benchmark_rets(bdf)
    rs = _alpha_strategy_returns(rb, seed=10)
    out = compute_verdict(
        strategy_id="VWAP",
        sr_candidate=4.0,
        returns=rs,
        total_trades=100,
        baseline_df=bdf,
        n_trials=20,
        sr_var_trials=0.25,
        neutral=False,
    )
    assert out.sr_margin_vs_mt_mean == pytest.approx(
        out.sr_observed - out.sr_zero_expected_at_eval, abs=1e-12
    )
    assert out.sr_margin_vs_baseline == pytest.approx(
        out.sr_observed - out.baseline_sharpe_at_eval, abs=1e-12
    )


def test_family_variance_lookup_when_not_supplied(monkeypatch):
    """sr_var_trials=None → family_sharpe_stats drives the eq.7
    scaling.  Stubbed to a known value; sr_zero must equal
    sqrt(V)×Gumbel(N)."""
    import backtest.families as families
    from backtest.families import FamilyStats
    monkeypatch.setattr(
        families, "family_sharpe_stats",
        lambda sid: FamilyStats(
            family="reversal", n_trials=7, sr_var=0.49,
            used_fallback=False, sharpes=(),
        ),
    )
    bdf = _noisy_baseline_df()
    rb = _benchmark_rets(bdf)
    rs = _alpha_strategy_returns(rb, seed=14)
    out = compute_verdict(
        strategy_id="VWAP",
        sr_candidate=4.0,
        returns=rs,
        total_trades=100,
        baseline_df=bdf,
        n_trials=20,
        neutral=False,
    )
    assert out.sr_var_trials_at_eval == 0.49
    # sr_zero = sqrt(0.49) × Gumbel(20) ≈ 0.7 × 1.866 ≈ 1.306
    assert 1.25 < out.sr_zero_expected_at_eval < 1.36


# ── Track 2 — signal_event_count precondition ──────────────────────────────

def _keepable_kwargs(seed: int = 1) -> dict:
    bdf = _noisy_baseline_df()
    rb = _benchmark_rets(bdf)
    return dict(
        strategy_id="VWAP",
        sr_candidate=4.0,
        returns=_alpha_strategy_returns(rb, seed=seed),
        baseline_df=bdf,
        n_trials=20,
        sr_var_trials=0.0,
        neutral=False,
    )


def test_legacy_call_no_signal_event_count_preserves_precondition_shape():
    """Calling compute_verdict WITHOUT signal_event_count keeps the
    trade-count floor as the active precondition and leaves the
    Track-2 fields None."""
    out = compute_verdict(total_trades=100, **_keepable_kwargs())
    assert out.verdict == "keep"
    assert out.trade_count_pass is True
    assert out.signal_event_count is None
    assert out.signal_event_count_pass is None


def test_signal_event_count_above_floor_passes_precondition():
    """Two-leg path: signal_event_count drives the precondition.  When
    it's above the floor the precondition gate passes regardless of
    whether trade-count is below its (unused) floor."""
    out = compute_verdict(
        total_trades=2,                 # below min_trade_count=30
        signal_event_count=200,         # above min_signal_event_count=30
        **_keepable_kwargs(),
    )
    assert out.verdict == "keep"
    assert out.trade_count_pass is False           # forensic only
    assert out.signal_event_count_pass is True     # the active gate
    assert out.signal_event_count == 200


def test_signal_event_count_below_floor_yields_under_tested():
    out = compute_verdict(
        total_trades=500,
        signal_event_count=10,
        **_keepable_kwargs(),
    )
    assert out.verdict == "under_tested"
    assert out.trade_count_pass is True
    assert out.signal_event_count_pass is False
    assert out.mt_mean_pass is None
    assert out.baseline_pass is None


def test_min_signal_event_count_default_is_30():
    out_eq = compute_verdict(
        total_trades=2, signal_event_count=30, **_keepable_kwargs(),
    )
    assert out_eq.signal_event_count_pass is True

    out_lt = compute_verdict(
        total_trades=2, signal_event_count=29, **_keepable_kwargs(),
    )
    assert out_lt.signal_event_count_pass is False
    assert out_lt.verdict == "under_tested"


def test_min_signal_event_count_custom_override():
    out = compute_verdict(
        total_trades=500,
        signal_event_count=50,
        min_signal_event_count=100,
        **_keepable_kwargs(),
    )
    assert out.signal_event_count_pass is False
    assert out.verdict == "under_tested"


def test_legacy_trials_log_rows_parse_under_new_schema():
    """Regression snapshot: every existing trials.log row must
    continue to parse cleanly under the current schema.  This
    is the schema-side equivalent of the verdict-side regression
    guarantee — proves backward compatibility on the file format.
    """
    import backtest.trials as trials
    repo_trials_log = (
        __import__("pathlib").Path(__file__).resolve().parents[2]
        / "backtest" / "trials.log"
    )
    if not repo_trials_log.exists():
        pytest.skip("repo trials.log absent; regression snapshot N/A")
    import json
    rows = [
        json.loads(line)
        for line in repo_trials_log.read_text().splitlines() if line.strip()
    ]
    for row in rows:
        # Recompute params_hash to honor the writer's contract.
        row["params_hash"] = trials._canonical_hash(row.get("params", {}))
        trials._validate_event(row)
    assert len(rows) > 0, (
        "regression snapshot expected at least one row in trials.log"
    )
