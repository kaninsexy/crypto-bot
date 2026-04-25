"""
backtest/tests/test_verdict.py — Tests for compute_verdict orchestration.
"""

import math

import numpy as np
import pandas as pd
import pytest

from backtest.verdict import VerdictResult, compute_verdict


# ── Helpers ──────────────────────────────────────────────────────────────────

_BPY = 365.25 * 24


def _flat_baseline_df(n_bars: int = 200) -> pd.DataFrame:
    """All-100 close → pct_change = 0 → buy-and-hold Sharpe = 0."""
    idx = pd.date_range("2024-01-01", periods=n_bars, freq="1h", tz="UTC")
    return pd.DataFrame({"close": np.full(n_bars, 100.0)}, index=idx)


def _baseline_df_with_target_sharpe(
    target_sharpe: float,
    n_bars: int = 200,
    std: float = 0.001,
) -> pd.DataFrame:
    """Deterministic close path whose buy-and-hold Sharpe is roughly
    `target_sharpe` under the engine's compound-Sharpe formula.

    Returns alternate ±std around a fixed drift μ:
      sample mean(rets) = μ exactly
      sample std (rets) = std exactly
    so the engine's annualisation lands at the target with no
    sampling jitter.
    """
    drift = (
        math.log(1.0 + target_sharpe * std * math.sqrt(_BPY)) / _BPY
        + 0.5 * std * std
    )
    rets = np.array([
        drift + (std if i % 2 == 0 else -std)
        for i in range(n_bars)
    ])
    close = 100.0 * np.cumprod(1.0 + rets)
    idx = pd.date_range("2024-01-01", periods=n_bars, freq="1h", tz="UTC")
    return pd.DataFrame({"close": close}, index=idx)


def _gaussian_returns(t: int, seed: int = 0) -> np.ndarray:
    return np.random.default_rng(seed).standard_normal(t) * 0.01


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_all_gates_pass_yields_keep():
    """Strong SR (4.0) above sr_zero_expected at N=20 (~1.78) and above
    a flat (zero-SR) baseline; preconditions both pass."""
    out = compute_verdict(
        strategy_id="VWAP",
        sr_candidate=4.0,
        returns=_gaussian_returns(2000, seed=1),
        total_trades=100,
        baseline_df=_flat_baseline_df(),
        n_trials=20,
    )
    assert out.verdict == "keep"
    assert out.trade_count_pass is True
    assert out.mintrl_pass is True
    assert out.mt_mean_pass is True
    assert out.baseline_pass is True


def test_below_mt_mean_yields_retire():
    """SR=1.0 < sr_zero_expected (~1.78 at N=20) → mt_mean_pass=False
    → retire even though baseline is beaten."""
    out = compute_verdict(
        strategy_id="VWAP",
        sr_candidate=1.0,
        returns=_gaussian_returns(2000, seed=2),
        total_trades=100,
        baseline_df=_flat_baseline_df(),
        n_trials=20,
    )
    assert out.verdict == "retire"
    assert out.mt_mean_pass is False
    # Baseline is flat (0); sr=1.0 > 0, so baseline_pass is True.  The
    # retire is purely from the multi-test gate.
    assert out.baseline_pass is True


def test_above_mt_mean_below_baseline_yields_retire():
    """SR=2.5 clears mt_mean (sr_zero_expected ≈ 1.78 at N=20) but
    fails the buy-and-hold floor when baseline_sharpe > 2.5."""
    baseline_df = _baseline_df_with_target_sharpe(target_sharpe=4.0)
    out = compute_verdict(
        strategy_id="VWAP",
        sr_candidate=2.5,
        returns=_gaussian_returns(2000, seed=3),
        total_trades=100,
        baseline_df=baseline_df,
        n_trials=20,
    )
    assert out.verdict == "retire"
    assert out.mt_mean_pass is True
    assert out.baseline_pass is False
    # Sanity: realised baseline_sharpe came out > sr_candidate.
    assert out.baseline_sharpe_at_eval > out.sr_observed


def test_low_trade_count_yields_under_tested():
    """total_trades < 30 → trade_count_pass False → under_tested,
    regardless of how strong the SR is."""
    out = compute_verdict(
        strategy_id="VWAP",
        sr_candidate=4.0,
        returns=_gaussian_returns(2000, seed=4),
        total_trades=10,
        baseline_df=_flat_baseline_df(),
        n_trials=20,
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
    """Weak SR (0.05) at modest T (500) → MinTRL ≈ 1083 > T → under-
    tested even with adequate trade count."""
    out = compute_verdict(
        strategy_id="VWAP",
        sr_candidate=0.05,
        returns=_gaussian_returns(500, seed=5),
        total_trades=100,
        baseline_df=_flat_baseline_df(),
        n_trials=20,
    )
    assert out.verdict == "under_tested"
    assert out.trade_count_pass is True
    assert out.mintrl_pass is False
    assert out.mt_mean_pass is None
    assert out.baseline_pass is None


def test_both_preconditions_fail_under_tested():
    """Both trade-count AND MinTRL fail; quality fields stay None."""
    out = compute_verdict(
        strategy_id="VWAP",
        sr_candidate=0.05,
        returns=_gaussian_returns(200, seed=6),
        total_trades=5,
        baseline_df=_flat_baseline_df(),
        n_trials=20,
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
        baseline_df=_flat_baseline_df(),
        n_trials=20,
    )
    assert out.verdict == "under_tested"
    assert out.mintrl_pass is False
    assert math.isnan(out.mintrl_required_at_eval)
    assert out.mt_mean_pass is None
    assert out.baseline_pass is None


def test_n1_special_case_keep():
    """At n_trials=1 the Gumbel haircut is skipped: sr_zero_expected =
    0.0.  So a small positive SR that beats the (flat) baseline is a
    keep, not a retire — the multi-test gate trivially passes."""
    out = compute_verdict(
        strategy_id="VWAP",
        sr_candidate=0.5,
        returns=_gaussian_returns(2000, seed=8),
        total_trades=100,
        baseline_df=_flat_baseline_df(),
        n_trials=1,
    )
    assert out.verdict == "keep"
    assert out.sr_zero_expected_at_eval == 0.0
    assert out.mt_mean_pass is True
    assert out.baseline_pass is True


def test_all_fields_populated_when_keep():
    """Every numeric field is a number (not NaN) when the verdict is
    a quality outcome (keep/retire)."""
    out = compute_verdict(
        strategy_id="VWAP",
        sr_candidate=4.0,
        returns=_gaussian_returns(2000, seed=9),
        total_trades=100,
        baseline_df=_flat_baseline_df(),
        n_trials=20,
    )
    assert isinstance(out, VerdictResult)
    # Numeric fields all finite.
    for field_name in (
        "sr_observed",
        "sr_zero_expected_at_eval",
        "mintrl_required_at_eval",
        "baseline_sharpe_at_eval",
        "dsr",
    ):
        v = getattr(out, field_name)
        assert math.isfinite(v), (
            f"{field_name} should be finite for a keep verdict; got {v}"
        )
    # Margins populated for keep/retire branches.
    assert out.sr_margin_vs_mt_mean is not None
    assert out.sr_margin_vs_baseline is not None
    # Echoed metadata.
    assert out.total_trades == 100
    assert out.t_observed == 2000
    assert out.n_trials == 20


def test_forensic_margins_match_components():
    """sr_margin_vs_mt_mean = sr_observed − sr_zero_expected_at_eval;
    sr_margin_vs_baseline = sr_observed − baseline_sharpe_at_eval."""
    out = compute_verdict(
        strategy_id="VWAP",
        sr_candidate=4.0,
        returns=_gaussian_returns(2000, seed=10),
        total_trades=100,
        baseline_df=_baseline_df_with_target_sharpe(target_sharpe=1.0),
        n_trials=20,
    )
    assert out.sr_margin_vs_mt_mean == pytest.approx(
        out.sr_observed - out.sr_zero_expected_at_eval, abs=1e-12
    )
    assert out.sr_margin_vs_baseline == pytest.approx(
        out.sr_observed - out.baseline_sharpe_at_eval, abs=1e-12
    )
