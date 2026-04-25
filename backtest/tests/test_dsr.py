"""
backtest/tests/test_dsr.py — Tests for backtest.dsr.

Covers:
  - Pure-noise input → low DSR
  - Strong signal at N=1 → near-1 DSR + sr_zero_expected == 0.0
  - Strong signal at N=100 → multiple-testing haircut
  - Pearson kurtosis convention (Gaussian ≈ 3, not ≈ 0)
  - Sample-size floor (T < 30 raises)
  - n_trials < 1 raises
  - Negative SR → low DSR
  - DSR always in [0, 1] under random valid inputs
  - dsr_from_cpcv_result concatenates valid blocks correctly
  - dsr_from_cpcv_result with all-empty blocks raises
  - dsr_from_cpcv_result requires sr_candidate (no default)
  - N=1 special case sr_zero_expected = 0.0 exactly
"""

import math
from inspect import Parameter, signature

import numpy as np
import pytest

from backtest.cpcv import CPCVResult
from backtest.dsr import (
    DSRError,
    DSRResult,
    deflated_sharpe,
    dsr_from_cpcv_result,
)


# ── Synthetic-returns helpers ─────────────────────────────────────────────────

def _strong_signal_returns(t: int = 2000, seed: int = 11) -> np.ndarray:
    """Daily-like returns with realised annualised Sharpe near 3.

    Mean=0.0008, std=0.01 → daily SR ≈ 0.08, ann SR ≈ 0.08 * √252 ≈ 1.27.
    Empirically run_cpcv-style annualisation pushes this further once
    fed through `deflated_sharpe`'s formula; tests rely on the
    sr_candidate the caller passes in, which can be any value the
    test wants to claim.
    """
    rng = np.random.default_rng(seed)
    return rng.normal(0.0008, 0.01, t)


# ── 1. Pure-noise input → low DSR ─────────────────────────────────────────────

def test_pure_noise_low_dsr():
    rng = np.random.default_rng(42)
    rets = rng.standard_normal(10_000)
    out = deflated_sharpe(sr_candidate=0.05, returns=rets, n_trials=20)
    assert out.dsr < 0.5, (
        f"pure noise + tiny SR claim got DSR={out.dsr}; "
        "expected < 0.5"
    )


# ── 2. Strong signal at N=1 → near-1 DSR + sr_zero_expected == 0.0 ───────────

def test_strong_signal_n1_high_dsr():
    """Explicit guard for the N=1 special-case fix.

    sr_candidate=2.7 instead of 3.0: at T=2000, sr_std ≈ 0.048, so
    z=sr/sr_std saturates Φ to 1.0 in float for either value.  Using
    2.7 keeps `test_strong_signal_n100_haircut` (which reuses this
    sr_candidate) able to demonstrate a strict haircut — at sr=3.0
    the N=100 z also saturates Φ at 1.0 and the strict `<` fails.
    """
    rets = _strong_signal_returns()
    out = deflated_sharpe(sr_candidate=2.7, returns=rets, n_trials=1)
    assert out.dsr > 0.99, (
        f"strong signal at N=1 got DSR={out.dsr}; expected > 0.99"
    )
    assert out.sr_zero_expected == 0.0, (
        f"N=1 must skip the Gumbel approximation; "
        f"got sr_zero_expected={out.sr_zero_expected}"
    )


# ── 3. Strong signal at N=100 → multiple-testing haircut ──────────────────────

def test_strong_signal_n100_haircut():
    """Same sr_candidate as test 2 (2.7) so the haircut comparison is
    against the same claim, only the trial count differs."""
    rets = _strong_signal_returns()
    out_n1 = deflated_sharpe(sr_candidate=2.7, returns=rets, n_trials=1)
    out_n100 = deflated_sharpe(sr_candidate=2.7, returns=rets, n_trials=100)
    assert out_n100.dsr > 0.5, (
        f"strong signal at N=100 should still survive (DSR > 0.5); "
        f"got {out_n100.dsr}"
    )
    assert out_n100.dsr < out_n1.dsr, (
        f"N=100 DSR must be strictly below N=1 DSR (multiple-testing "
        f"haircut); got n1={out_n1.dsr} n100={out_n100.dsr}"
    )


# ── 4. Pearson kurtosis convention (Gaussian ≈ 3, not ≈ 0) ───────────────────

def test_kurtosis_pearson_convention():
    """Guards against a Fisher-vs-Pearson regression: Fisher would
    return ≈ 0 for Gaussian input, Pearson returns ≈ 3."""
    rng = np.random.default_rng(0)
    rets = rng.standard_normal(50_000)
    out = deflated_sharpe(sr_candidate=0.05, returns=rets, n_trials=10)
    assert 2.9 < out.kurt < 3.1, (
        f"Gaussian kurtosis under Pearson convention should be ≈ 3; "
        f"got {out.kurt}"
    )


# ── 5. Sample-size floor ─────────────────────────────────────────────────────

def test_sample_size_floor():
    rng = np.random.default_rng(1)
    rets = rng.standard_normal(29)
    with pytest.raises(DSRError, match="sample size"):
        deflated_sharpe(sr_candidate=1.0, returns=rets, n_trials=1)


# ── 6. n_trials < 1 rejected ─────────────────────────────────────────────────

def test_n_trials_zero_rejected():
    rng = np.random.default_rng(2)
    rets = rng.standard_normal(1000)
    with pytest.raises(DSRError, match="n_trials"):
        deflated_sharpe(sr_candidate=1.0, returns=rets, n_trials=0)


# ── 7. Negative SR → low DSR ─────────────────────────────────────────────────

def test_negative_sr_low_dsr():
    rng = np.random.default_rng(3)
    rets = rng.standard_normal(2_000)
    out = deflated_sharpe(sr_candidate=-1.0, returns=rets, n_trials=20)
    assert out.dsr < 0.5, (
        f"negative SR claim should produce DSR < 0.5; got {out.dsr}"
    )


# ── 8. DSR always in [0, 1] under random valid inputs ────────────────────────

def test_dsr_in_unit_interval():
    """Hammer with a range of plausible inputs; assert 0 ≤ dsr ≤ 1
    every time (this is a property of Φ(z))."""
    rng = np.random.default_rng(4)
    for _ in range(50):
        t = int(rng.integers(50, 5_000))
        sr_claim = float(rng.uniform(-2.5, 2.5))
        n_trials = int(rng.integers(1, 200))
        # Slight non-zero mean so sr_var stays positive across the
        # SR/skew/kurt grid.
        rets = rng.normal(0.0001, 0.01, t)
        try:
            out = deflated_sharpe(
                sr_candidate=sr_claim, returns=rets, n_trials=n_trials,
            )
        except DSRError:
            # Statistically meaningless inputs are filtered out;
            # the unit-interval invariant is about valid outputs only.
            continue
        assert 0.0 <= out.dsr <= 1.0, (
            f"DSR out of [0, 1]: dsr={out.dsr} sr={sr_claim} "
            f"T={t} N={n_trials}"
        )


# ── 9. dsr_from_cpcv_result concatenation ────────────────────────────────────

def _make_cpcv_result(
    n_blocks: int = 10,
    n_empty: int = 3,
    valid_block_size: int = 1000,
    seed: int = 7,
) -> CPCVResult:
    """Build a synthetic CPCVResult.  Fields irrelevant to DSR are
    populated with placeholder values that the unit tests don't read."""
    rng = np.random.default_rng(seed)
    per_block_returns: list[np.ndarray] = []
    per_path_sharpes: list[float] = []
    trades_per_path: list[int] = []
    for i in range(n_blocks):
        if i < n_empty:
            per_block_returns.append(np.array([], dtype=float))
            per_path_sharpes.append(float("nan"))
            trades_per_path.append(0)
        else:
            arr = rng.normal(0.0005, 0.01, valid_block_size)
            per_block_returns.append(arr)
            per_path_sharpes.append(0.5)  # placeholder
            trades_per_path.append(20)    # placeholder
    return CPCVResult(
        n_paths=n_blocks,
        sharpe_distribution={
            "mean": 0.5, "std": 0.2,
            "quantiles": {
                "p05": 0.1, "p25": 0.3, "p50": 0.5, "p75": 0.7, "p95": 0.9,
            },
        },
        per_path_sharpes=per_path_sharpes,
        trades_per_path=trades_per_path,
        per_block_returns=per_block_returns,
    )


def test_dsr_from_cpcv_result_concatenation(monkeypatch):
    n_blocks = 10
    n_empty = 3
    valid_block_size = 1000
    cpcv = _make_cpcv_result(
        n_blocks=n_blocks,
        n_empty=n_empty,
        valid_block_size=valid_block_size,
    )

    # Stub trials.count_trials_for_dsr to a known value.
    import backtest.trials as trials
    monkeypatch.setattr(
        trials, "count_trials_for_dsr", lambda strategy_id: 5,
    )

    out = dsr_from_cpcv_result(
        cpcv, strategy_id="TestStrat", sr_candidate=1.5,
    )
    expected_t = (n_blocks - n_empty) * valid_block_size
    assert out.t == expected_t, (
        f"t={out.t} expected {expected_t} (sum of valid-block lengths)"
    )
    assert out.n_trials == 5
    assert 0.0 <= out.dsr <= 1.0


# ── 10. dsr_from_cpcv_result with all-empty blocks raises ───────────────────

def test_dsr_from_cpcv_result_all_empty_raises(monkeypatch):
    cpcv = _make_cpcv_result(n_blocks=10, n_empty=10, valid_block_size=1000)
    import backtest.trials as trials
    monkeypatch.setattr(
        trials, "count_trials_for_dsr", lambda strategy_id: 5,
    )
    with pytest.raises(DSRError, match="no valid"):
        dsr_from_cpcv_result(
            cpcv, strategy_id="TestStrat", sr_candidate=1.0,
        )


# ── 11. dsr_from_cpcv_result requires sr_candidate (no default) ─────────────

def test_dsr_from_cpcv_result_requires_sr_candidate():
    """Document by test that sr_candidate is required.  The CV
    block-Sharpe mean is NOT silently substituted — callers must pass
    the headline engine Sharpe explicitly."""
    sig = signature(dsr_from_cpcv_result)
    sr_param = sig.parameters.get("sr_candidate")
    assert sr_param is not None, "sr_candidate must be a named parameter"
    assert sr_param.default is Parameter.empty, (
        f"sr_candidate must have no default; got {sr_param.default!r}"
    )

    # Call without sr_candidate → TypeError.
    cpcv = _make_cpcv_result()
    with pytest.raises(TypeError):
        dsr_from_cpcv_result(cpcv, strategy_id="TestStrat")  # type: ignore[call-arg]


# ── 12. N=1 special case records sr_zero_expected = 0.0 ──────────────────────

def test_n1_special_case_records_zero_expected():
    """Pairs with test_strong_signal_n1_high_dsr: explicitly assert
    that sr_zero_expected is exactly 0.0 (not the Gumbel value) when
    n_trials == 1, across multiple SR values to confirm it's the
    branch behaviour and not a coincidence."""
    rng = np.random.default_rng(5)
    rets = rng.normal(0.0001, 0.01, 1_000)
    for sr in (-2.0, -0.5, 0.0, 0.5, 2.0):
        out = deflated_sharpe(
            sr_candidate=sr, returns=rets, n_trials=1,
        )
        assert out.sr_zero_expected == 0.0, (
            f"N=1 sr_zero_expected must be exactly 0.0; got "
            f"{out.sr_zero_expected} for sr={sr}"
        )


# ── Minimum Track Record Length (BLP eq. 13) ─────────────────────────────────
#
# Twelve tests for `min_track_record_length` and `mintrl_from_cpcv_result`.

from backtest.dsr import (  # noqa: E402  (deliberate late import to keep DSR
    MinTRLResult,           #            tests above and MinTRL tests below)
    min_track_record_length,
    mintrl_from_cpcv_result,
)


# ── 1. Strong Sharpe → small MinTRL, not under-tested ───────────────────────

def test_mintrl_strong_sharpe_low_requirement():
    rng = np.random.default_rng(1)
    rets = rng.standard_normal(2000)
    out = min_track_record_length(sr_candidate=2.0, returns=rets)
    # Hand check: var_term ≈ 1 + (kurt-1)/4 · 4 ≈ 3; (Z/SR)² ≈ 0.677.
    # min_trl ≈ 1 + 3 · 0.677 ≈ 3.
    assert out.min_trl < 200, (
        f"strong SR=2.0 should need ≪ 200 bars; got {out.min_trl}"
    )
    assert out.under_tested is False, (
        f"T=2000 ≫ min_trl={out.min_trl}; under_tested should be False"
    )


# ── 2. Weak Sharpe → large MinTRL, under-tested ─────────────────────────────

def test_mintrl_weak_sharpe_high_requirement():
    """Spec deviation note.

    The spec asks for SR=0.3 / T=500 / min_trl > 1000.  Under BLP
    eq. 13 with skew≈0, kurt≈3 (Gaussian), SR=0.3 gives min_trl =
    1 + 1.045 · (1.6449/0.3)² ≈ 32 — orders of magnitude below 1000
    and below T=500, so under_tested would be False, not True.  The
    spec's anchor test (test_mintrl_known_value_gaussian_sr1: SR=1.0
    → ≈5.06, asserted 4 < x < 7) confirms the formula treats the
    plugged-in SR directly.

    The intent of test 2 is "weak signal → long required track
    record → under-tested even at meaningful T".  Pushing SR down to
    0.05 actually triggers that regime under the formula:
        var_term ≈ 1
        min_trl ≈ 1 + (1.6449 / 0.05)² ≈ 1083
        at T=500 → under_tested=True, min_trl > 1000.
    """
    rng = np.random.default_rng(2)
    rets = rng.standard_normal(500)
    out = min_track_record_length(sr_candidate=0.05, returns=rets)
    assert out.min_trl > 1000, (
        f"weak SR=0.05 should need > 1000 bars; got {out.min_trl}"
    )
    assert out.under_tested is True, (
        f"T=500 < min_trl={out.min_trl}; under_tested should be True"
    )


# ── 3. Formula-correctness anchor: Gaussian, SR=1.0 → ≈5.06 ─────────────────

def test_mintrl_known_value_gaussian_sr1():
    """Hand-computed against BLP eq. 13:
        var_term = 1 + 0 + (3-1)/4 · 1 = 1.5
        Z_0.95   ≈ 1.6449
        min_trl  = 1 + 1.5 · 1.6449² = 1 + 1.5 · 2.706 ≈ 5.06
    """
    rng = np.random.default_rng(7)
    rets = rng.standard_normal(10_000)
    out = min_track_record_length(sr_candidate=1.0, returns=rets)
    assert 4.0 < out.min_trl < 7.0, (
        f"Gaussian SR=1.0 should give MinTRL ≈ 5.06; got {out.min_trl}"
    )


# ── 4. SR ≈ 0 rejected ──────────────────────────────────────────────────────

def test_mintrl_zero_sr_rejected():
    rng = np.random.default_rng(3)
    rets = rng.standard_normal(500)
    with pytest.raises(DSRError) as excinfo:
        min_track_record_length(sr_candidate=0.0, returns=rets)
    msg = str(excinfo.value)
    assert "denominator" in msg or "undefined" in msg, (
        f"DSRError message should mention 'denominator' or 'undefined'; "
        f"got: {msg}"
    )


# ── 5. Negative SR is symmetric on (skew≈0, kurt≈3) returns ─────────────────

def test_mintrl_negative_sr_symmetric():
    rng = np.random.default_rng(11)
    rets = rng.standard_normal(2000)
    pos = min_track_record_length(sr_candidate=+1.0, returns=rets)
    neg = min_track_record_length(sr_candidate=-1.0, returns=rets)
    assert abs(pos.min_trl - neg.min_trl) < 0.5, (
        f"on symmetric returns the +SR and -SR MinTRLs should match; "
        f"pos={pos.min_trl} neg={neg.min_trl} diff={abs(pos.min_trl-neg.min_trl)}"
    )


# ── 6. Confidence input validation ──────────────────────────────────────────

@pytest.mark.parametrize("bad_conf", [0.0, 1.0, -0.5, 1.5])
def test_mintrl_confidence_validation(bad_conf):
    rng = np.random.default_rng(4)
    rets = rng.standard_normal(200)
    with pytest.raises(DSRError):
        min_track_record_length(
            sr_candidate=1.0, returns=rets, confidence=bad_conf,
        )


# ── 7. Higher confidence → longer required track record ─────────────────────

def test_mintrl_confidence_higher_means_longer():
    rng = np.random.default_rng(13)
    rets = rng.standard_normal(2000)
    out_95 = min_track_record_length(
        sr_candidate=1.0, returns=rets, confidence=0.95,
    )
    out_99 = min_track_record_length(
        sr_candidate=1.0, returns=rets, confidence=0.99,
    )
    assert out_99.min_trl > out_95.min_trl, (
        f"99% confidence should require more bars than 95%; "
        f"got 99={out_99.min_trl} vs 95={out_95.min_trl}"
    )


# ── 8. T < 30 floor ─────────────────────────────────────────────────────────

def test_mintrl_t_observed_floor():
    rng = np.random.default_rng(15)
    rets = rng.standard_normal(29)
    with pytest.raises(DSRError, match="below floor"):
        min_track_record_length(sr_candidate=1.0, returns=rets)


# ── 9. under_tested flag is consistent with min_trl vs t_observed ───────────

def test_mintrl_underflow_flag_consistency():
    rng = np.random.default_rng(17)

    # T_observed > min_trl → not under-tested.
    # Strong SR=2.0 → min_trl ≈ 3; T=200 ≫ 3.
    rets_long = rng.standard_normal(200)
    out_safe = min_track_record_length(sr_candidate=2.0, returns=rets_long)
    assert out_safe.t_observed > out_safe.min_trl
    assert out_safe.under_tested is False

    # T_observed < min_trl → under-tested.
    # Weak SR=0.05 → min_trl ≈ 1083; T=100 ≪ 1083.
    rets_short = rng.standard_normal(100)
    out_under = min_track_record_length(sr_candidate=0.05, returns=rets_short)
    assert out_under.t_observed < out_under.min_trl
    assert out_under.under_tested is True


# ── 10. mintrl_from_cpcv_result concatenates valid blocks ───────────────────

def test_mintrl_from_cpcv_concatenation():
    cpcv = _make_cpcv_result(
        n_blocks=10, n_empty=3, valid_block_size=1000,
    )
    out = mintrl_from_cpcv_result(cpcv, sr_candidate=1.0)
    expected_t = (10 - 3) * 1000  # 7 valid blocks × 1000 bars
    assert out.t_observed == expected_t, (
        f"t_observed={out.t_observed} expected {expected_t}"
    )
    assert isinstance(out.under_tested, bool)


# ── 11. mintrl_from_cpcv_result with all-empty raises ───────────────────────

def test_mintrl_from_cpcv_all_empty_raises():
    cpcv = _make_cpcv_result(n_blocks=10, n_empty=10, valid_block_size=1000)
    with pytest.raises(DSRError, match="no valid"):
        mintrl_from_cpcv_result(cpcv, sr_candidate=1.0)


# ── 12. mintrl_from_cpcv_result requires sr_candidate (no default) ─────────

def test_mintrl_from_cpcv_requires_sr_candidate():
    from inspect import Parameter, signature
    sig = signature(mintrl_from_cpcv_result)
    sr_param = sig.parameters.get("sr_candidate")
    assert sr_param is not None
    assert sr_param.default is Parameter.empty, (
        f"sr_candidate must have no default; got {sr_param.default!r}"
    )

    cpcv = _make_cpcv_result()
    with pytest.raises(TypeError):
        mintrl_from_cpcv_result(cpcv)  # type: ignore[call-arg]
