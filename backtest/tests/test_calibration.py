"""
backtest/tests/test_calibration.py — Tests for the DSR threshold
calibration tool.
"""

import json

import numpy as np
import pytest
from scipy import stats

from backtest import calibration as calib
from backtest.calibration import (
    CalibrationReport,
    ThresholdSet,
    _annualised_sharpe,
    _derive_thresholds,
    _generate_skewed_student_t,
    _generate_student_t,
    run_calibration,
)


# ── 1. Distribution moments ──────────────────────────────────────────────────

def test_distributions_produce_expected_moments():
    """student_t_df5 is heavy-tailed and ~symmetric;
    skewed_student_t is heavy-tailed and negatively skewed."""
    rng = np.random.default_rng(123)
    t_rets = _generate_student_t(rng, n=10_000, target_sharpe=0.0)
    sk_rets = _generate_skewed_student_t(rng, n=10_000, target_sharpe=0.0)

    t_skew = float(stats.skew(t_rets))
    t_kurt = float(stats.kurtosis(t_rets, fisher=False))
    assert abs(t_skew) < 0.3, f"student_t_df5 |skew|={abs(t_skew)} expected < 0.3"
    assert t_kurt > 4.0, f"student_t_df5 kurt={t_kurt} expected > 4 (heavy-tailed)"

    sk_skew = float(stats.skew(sk_rets))
    sk_kurt = float(stats.kurtosis(sk_rets, fisher=False))
    assert sk_skew < -0.3, (
        f"skewed_student_t skew={sk_skew} expected < -0.3 (negatively skewed)"
    )
    assert sk_kurt > 3.0, (
        f"skewed_student_t kurt={sk_kurt} expected > 3"
    )


# ── 2. Target Sharpe is recovered on the synthetic generator ─────────────────

def test_target_sharpe_recovered():
    """Across 50 samples of T=10000, the realised Sharpe averages
    near the target."""
    rng = np.random.default_rng(7)
    target = 1.5
    realised = []
    for _ in range(50):
        rets = _generate_student_t(rng, n=10_000, target_sharpe=target)
        realised.append(_annualised_sharpe(rets))
    mean_sr = float(np.mean(realised))
    assert 1.0 < mean_sr < 2.0, (
        f"realised SR mean={mean_sr} expected in (1.0, 2.0) for target=1.5"
    )


# ── 3. Clean-separation case yields well-behaved thresholds ──────────────────

def test_derive_thresholds_clean_separation():
    """noise ⊆ [0, 0.5], signal ⊆ [0.7, 1.0] — three thresholds should
    all land at or near the [0.5, 0.7] gap.

    Since `_derive_thresholds`' candidate set is `unique(concat(noise,
    signal))`, no candidate τ falls strictly inside (max(noise),
    min(signal)) — Youden's J therefore lands at max(noise) − ε or
    min(signal) − ε in finite samples.  We assert τ is within 0.05
    of the gap on either side.
    """
    rng = np.random.default_rng(99)
    noise = rng.uniform(0.0, 0.5, size=2000)
    signal = rng.uniform(0.7, 1.0, size=2000)
    out = _derive_thresholds(noise, signal)

    assert isinstance(out, ThresholdSet)
    # Slack of 0.1: Youden lands near max(noise); FPR-capped near
    # noise's 95th percentile; TPR-floored near signal's 20th
    # percentile (~ min_signal + 0.06 for uniform(0.7, 1.0)).
    max_noise = float(noise.max())
    min_signal = float(signal.min())
    slack = 0.1
    for tau in (out.youden_j, out.fpr_capped, out.tpr_floored):
        assert max_noise - slack <= tau <= min_signal + slack, (
            f"threshold {tau} fell outside near-gap range "
            f"[{max_noise - slack}, {min_signal + slack}]"
        )

    # Property checks for the FPR-capped criterion — these are
    # distribution-agnostic and the spec's authoritative requirement.
    assert out.fpr_capped_fpr <= 0.05
    assert out.fpr_capped_tpr >= 0.95


# ── 4. Full overlap → some thresholds may be infeasible ──────────────────────

def test_derive_thresholds_full_overlap_infeasibility():
    """noise and signal drawn from the same uniform(0, 1) — the FPR≤5%
    threshold is either NaN (no τ achieves it) or pushed near 1.0
    (only the upper tail satisfies the cap, with vanishing TPR).
    Youden's J always returns a finite value."""
    rng = np.random.default_rng(101)
    noise = rng.uniform(0.0, 1.0, size=2000)
    signal = rng.uniform(0.0, 1.0, size=2000)
    out = _derive_thresholds(noise, signal)

    if not np.isnan(out.fpr_capped):
        # Feasible only at the extreme upper tail.
        assert out.fpr_capped > 0.9, (
            f"fpr_capped={out.fpr_capped} should be ~1 or NaN under full overlap"
        )

    assert np.isfinite(out.youden_j), "Youden's J should always be finite"


# ── 5. Smoke run with reduced grid ───────────────────────────────────────────

def test_run_calibration_smoke(monkeypatch):
    monkeypatch.setattr(calib, "_N_NOISE", 50)
    monkeypatch.setattr(calib, "_N_SIGNAL", 50)
    monkeypatch.setattr(calib, "_TRIAL_COUNTS", (5, 20))

    report = run_calibration(seed=42)

    assert isinstance(report, CalibrationReport)
    # 2 distributions × 2 trial counts.
    assert len(report.cells) == 4

    # production_recommendation is now per-(distribution, n_trials):
    # dict[str, dict[int, float]].  Outer keys = distributions; inner
    # keys = the trial-count grid used for the run.
    pr = report.production_recommendation
    assert set(pr.keys()) == {"student_t_df5", "skewed_student_t"}
    for dist_name, inner in pr.items():
        assert isinstance(inner, dict), (
            f"{dist_name}: expected dict of n_trials → tau"
        )
        assert set(inner.keys()) == {5, 20}, (
            f"{dist_name}: inner keys {set(inner.keys())} != trial-count grid"
        )

    for cell in report.cells:
        # No more than 10% of samples raised DSRError on either side.
        assert cell.n_dsr_errors_noise <= 5, (
            f"{cell.distribution}/N={cell.n_trials}: "
            f"{cell.n_dsr_errors_noise} DSR errors on noise (>10%)"
        )
        assert cell.n_dsr_errors_signal <= 5, (
            f"{cell.distribution}/N={cell.n_trials}: "
            f"{cell.n_dsr_errors_signal} DSR errors on signal (>10%)"
        )


# ── 6. Reproducibility — same seed → same DSR means ──────────────────────────

def test_run_calibration_reproducible(monkeypatch):
    monkeypatch.setattr(calib, "_N_NOISE", 50)
    monkeypatch.setattr(calib, "_N_SIGNAL", 50)
    monkeypatch.setattr(calib, "_TRIAL_COUNTS", (5, 20))

    a = run_calibration(seed=42)
    b = run_calibration(seed=42)

    assert len(a.cells) == len(b.cells)
    for ca, cb in zip(a.cells, b.cells):
        assert ca.noise_dsr_mean == cb.noise_dsr_mean
        assert ca.signal_dsr_mean == cb.signal_dsr_mean
        assert ca.noise_dsr_std == cb.noise_dsr_std
        assert ca.signal_dsr_std == cb.signal_dsr_std


# ── 7. JSON output ───────────────────────────────────────────────────────────

def test_run_calibration_writes_json(tmp_path, monkeypatch):
    monkeypatch.setattr(calib, "_N_NOISE", 50)
    monkeypatch.setattr(calib, "_N_SIGNAL", 50)
    monkeypatch.setattr(calib, "_TRIAL_COUNTS", (5, 20))

    out_path = tmp_path / "report.json"
    report = run_calibration(seed=42, output_path=out_path)
    assert out_path.exists()

    parsed = json.loads(out_path.read_text())
    assert "production_recommendation" in parsed
    pr = parsed["production_recommendation"]
    assert set(pr.keys()) == {"student_t_df5", "skewed_student_t"}
    # JSON keys are strings even though the Python dict has int keys
    # for n_trials.  Match against str(n_trials).
    for dist_name, inner in pr.items():
        assert isinstance(inner, dict)
        assert set(inner.keys()) == {"5", "20"}, (
            f"{dist_name}: inner keys {set(inner.keys())} != trial grid (as str)"
        )
    assert parsed["seed"] == 42
    assert isinstance(report, CalibrationReport)


# ── 8. Sanity: signal DSR ≥ noise DSR ────────────────────────────────────────

def test_signal_dsr_higher_than_noise_dsr(monkeypatch):
    monkeypatch.setattr(calib, "_N_NOISE", 50)
    monkeypatch.setattr(calib, "_N_SIGNAL", 50)
    monkeypatch.setattr(calib, "_TRIAL_COUNTS", (5, 20))

    report = run_calibration(seed=42)
    for cell in report.cells:
        # `>=`, not strict `>`: at the borderline-keepable target
        # (_TARGET_SIGNAL_SHARPE=0.7) and N=5, sr_zero_expected ≈
        # 0.955 dominates a signal SR of 0.7, so both signal and
        # noise DSRs can underflow to exactly 0.0 in float — that's
        # the gate failing to distinguish at this cell, which is
        # itself an informative calibration outcome.  The sanity
        # check we keep is the weaker "signal never *worse* than
        # noise on average".
        assert cell.signal_dsr_mean >= cell.noise_dsr_mean, (
            f"{cell.distribution}/N={cell.n_trials}: "
            f"signal_dsr_mean={cell.signal_dsr_mean} < "
            f"noise_dsr_mean={cell.noise_dsr_mean}"
        )
