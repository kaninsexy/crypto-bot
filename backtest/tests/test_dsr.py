"""
backtest/tests/test_dsr.py — Tests for backtest.dsr (gate spec v2).

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
  - GATE SPEC v2 (2026-06-11):
      * bars_per_year is required (keyword-only)
      * per-bar conversion round-trips (annualised+bpy ≡ per-bar+1.0)
      * sr_zero_expected = sqrt(V[{SR_n}]) × Gumbel(N) (eq.7 scaling)
      * a true-zero-edge synthetic strategy at a realistic bar
        frequency must NOT clear DSR ≥ 0.95 (calibration sanity)
      * MinTRL at annualised SR 1.0 / 1d bars ≈ 989 bars (2.71y)

Unit convention in the legacy tests: the v1 numeric anchors treated
`sr_candidate` as directly comparable to the per-bar return moments.
Under v2 that is the `bars_per_year=1.0` special case (annualised ==
per-bar), so the legacy tests pass `bars_per_year=1.0` explicitly and
their anchors are unchanged.  Realistic-frequency behaviour is covered
by the v2 section.
"""

import math
from inspect import Parameter, signature

import numpy as np
import pytest
from scipy import stats as _sps

from backtest.cpcv import CPCVResult
from backtest.dsr import (
    DSRError,
    DSRResult,
    bars_per_year_for_timeframe,
    bars_per_year_from_candle_hours,
    deflated_sharpe,
    dsr_from_cpcv_result,
)
from backtest.families import FamilyStats


# ── Synthetic-returns helpers ─────────────────────────────────────────────────

def _strong_signal_returns(t: int = 2000, seed: int = 11) -> np.ndarray:
    """Daily-like returns with realised annualised Sharpe near 3.

    Mean=0.0008, std=0.01 → daily SR ≈ 0.08, ann SR ≈ 0.08 * √252 ≈ 1.27.
    Tests rely on the sr_candidate the caller passes in, which can be
    any value the test wants to claim.
    """
    rng = np.random.default_rng(seed)
    return rng.normal(0.0008, 0.01, t)


def _fake_family_stats(n_trials: int = 4, sr_var: float = 0.25) -> FamilyStats:
    return FamilyStats(
        family="reversal",
        n_trials=n_trials,
        sr_var=sr_var,
        used_fallback=False,
        sharpes=(0.1, -0.2, 0.5, 0.3),
    )


# ── 1. Pure-noise input → low DSR ─────────────────────────────────────────────

def test_pure_noise_low_dsr():
    rng = np.random.default_rng(42)
    rets = rng.standard_normal(10_000)
    out = deflated_sharpe(
        sr_candidate=0.05, returns=rets, n_trials=20, bars_per_year=1.0,
    )
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
    out = deflated_sharpe(
        sr_candidate=2.7, returns=rets, n_trials=1, bars_per_year=1.0,
    )
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
    out_n1 = deflated_sharpe(
        sr_candidate=2.7, returns=rets, n_trials=1, bars_per_year=1.0,
    )
    out_n100 = deflated_sharpe(
        sr_candidate=2.7, returns=rets, n_trials=100, bars_per_year=1.0,
    )
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
    out = deflated_sharpe(
        sr_candidate=0.05, returns=rets, n_trials=10, bars_per_year=1.0,
    )
    assert 2.9 < out.kurt < 3.1, (
        f"Gaussian kurtosis under Pearson convention should be ≈ 3; "
        f"got {out.kurt}"
    )


# ── 5. Sample-size floor ─────────────────────────────────────────────────────

def test_sample_size_floor():
    rng = np.random.default_rng(1)
    rets = rng.standard_normal(29)
    with pytest.raises(DSRError, match="sample size"):
        deflated_sharpe(
            sr_candidate=1.0, returns=rets, n_trials=1, bars_per_year=1.0,
        )


# ── 6. n_trials < 1 rejected ─────────────────────────────────────────────────

def test_n_trials_zero_rejected():
    rng = np.random.default_rng(2)
    rets = rng.standard_normal(1000)
    with pytest.raises(DSRError, match="n_trials"):
        deflated_sharpe(
            sr_candidate=1.0, returns=rets, n_trials=0, bars_per_year=1.0,
        )


# ── 7. Negative SR → low DSR ─────────────────────────────────────────────────

def test_negative_sr_low_dsr():
    rng = np.random.default_rng(3)
    rets = rng.standard_normal(2_000)
    out = deflated_sharpe(
        sr_candidate=-1.0, returns=rets, n_trials=20, bars_per_year=1.0,
    )
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
        bpy = float(rng.choice([1.0, 365.25, 2191.5, 8766.0]))
        # Slight non-zero mean so sr_var stays positive across the
        # SR/skew/kurt grid.
        rets = rng.normal(0.0001, 0.01, t)
        try:
            out = deflated_sharpe(
                sr_candidate=sr_claim, returns=rets, n_trials=n_trials,
                bars_per_year=bpy,
            )
        except DSRError:
            # Statistically meaningless inputs are filtered out;
            # the unit-interval invariant is about valid outputs only.
            continue
        assert 0.0 <= out.dsr <= 1.0, (
            f"DSR out of [0, 1]: dsr={out.dsr} sr={sr_claim} "
            f"T={t} N={n_trials} bpy={bpy}"
        )


# ── 9. dsr_from_cpcv_result concatenation ────────────────────────────────────

def _make_cpcv_result(
    n_blocks: int = 10,
    n_empty: int = 3,
    valid_block_size: int = 1000,
    seed: int = 7,
    candle_duration_h: float = 24.0,
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
        candle_duration_h=candle_duration_h,
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

    # Stub the family stats to a known value (gate spec v2: the
    # adapter reads per-FAMILY count + variance, not the per-strategy
    # count_trials_for_dsr).
    import backtest.families as families
    monkeypatch.setattr(
        families, "family_sharpe_stats",
        lambda strategy_id: _fake_family_stats(n_trials=4, sr_var=0.25),
    )

    out = dsr_from_cpcv_result(
        cpcv, strategy_id="TestStrat", sr_candidate=1.5,
    )
    expected_t = (n_blocks - n_empty) * valid_block_size
    assert out.t == expected_t, (
        f"t={out.t} expected {expected_t} (sum of valid-block lengths)"
    )
    # Pre-append convention: family count 4 + 1 (the trial being
    # deflated).
    assert out.n_trials == 5
    assert out.sr_var_trials == 0.25
    # candle_duration_h=24 → 1d bars.
    assert math.isclose(out.bars_per_year, 365.25, rel_tol=1e-9)
    assert 0.0 <= out.dsr <= 1.0


def test_dsr_from_cpcv_result_explicit_bpy_overrides(monkeypatch):
    import backtest.families as families
    monkeypatch.setattr(
        families, "family_sharpe_stats",
        lambda strategy_id: _fake_family_stats(),
    )
    cpcv = _make_cpcv_result(candle_duration_h=24.0)
    out = dsr_from_cpcv_result(
        cpcv, strategy_id="TestStrat", sr_candidate=1.5,
        bars_per_year=8766.0,
    )
    assert out.bars_per_year == 8766.0


def test_dsr_from_cpcv_result_missing_frequency_raises(monkeypatch):
    """Pre-v2 result objects without candle_duration_h must not be
    silently guessed at — that is how the units bug survived."""
    import backtest.families as families
    monkeypatch.setattr(
        families, "family_sharpe_stats",
        lambda strategy_id: _fake_family_stats(),
    )
    cpcv = _make_cpcv_result(candle_duration_h=None)
    with pytest.raises(DSRError, match="bars_per_year"):
        dsr_from_cpcv_result(
            cpcv, strategy_id="TestStrat", sr_candidate=1.0,
        )


# ── 10. dsr_from_cpcv_result with all-empty blocks raises ───────────────────

def test_dsr_from_cpcv_result_all_empty_raises(monkeypatch):
    cpcv = _make_cpcv_result(n_blocks=10, n_empty=10, valid_block_size=1000)
    import backtest.families as families
    monkeypatch.setattr(
        families, "family_sharpe_stats",
        lambda strategy_id: _fake_family_stats(),
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
            sr_candidate=sr, returns=rets, n_trials=1, bars_per_year=1.0,
        )
        assert out.sr_zero_expected == 0.0, (
            f"N=1 sr_zero_expected must be exactly 0.0; got "
            f"{out.sr_zero_expected} for sr={sr}"
        )


# ── GATE SPEC v2: units fix + eq.7 cross-trial variance scaling ──────────────

_EULER = 0.5772156649015329


def _gumbel(n: int) -> float:
    n = float(n)
    return (
        (1.0 - _EULER) * _sps.norm.ppf(1.0 - 1.0 / n)
        + _EULER * _sps.norm.ppf(1.0 - 1.0 / (n * math.e))
    )


def test_bars_per_year_required_keyword():
    """v2 contract change: omitting bars_per_year is a TypeError."""
    rng = np.random.default_rng(6)
    rets = rng.standard_normal(500)
    with pytest.raises(TypeError):
        deflated_sharpe(sr_candidate=1.0, returns=rets, n_trials=1)  # type: ignore[call-arg]


def test_per_bar_conversion_round_trip():
    """deflated_sharpe(annualised SR, bpy) must equal
    deflated_sharpe(per-bar SR, bpy=1) with the cross-trial variance
    scaled into the same per-bar units (V_pb = V_ann / bpy).  Verifies
    the internal conversion is sr_ann / sqrt(bpy) and nothing else."""
    rng = np.random.default_rng(8)
    rets = rng.normal(0.0003, 0.011, 4_000)
    bpy = 365.25
    sr_ann = 1.4
    v_ann = 0.36

    out_ann = deflated_sharpe(
        sr_candidate=sr_ann, returns=rets, n_trials=7,
        bars_per_year=bpy, sr_var_trials=v_ann,
    )
    out_pb = deflated_sharpe(
        sr_candidate=sr_ann / math.sqrt(bpy), returns=rets, n_trials=7,
        bars_per_year=1.0, sr_var_trials=v_ann / bpy,
    )
    assert math.isclose(out_ann.dsr, out_pb.dsr, rel_tol=1e-12), (
        f"round-trip mismatch: ann={out_ann.dsr} pb={out_pb.dsr}"
    )
    assert math.isclose(
        out_ann.sr_candidate_per_bar, sr_ann / math.sqrt(bpy), rel_tol=1e-12,
    )
    # Result echoes both unit systems consistently.
    assert math.isclose(
        out_ann.sr_std_annualised, out_ann.sr_std * math.sqrt(bpy),
        rel_tol=1e-12,
    )
    assert math.isclose(
        out_ann.sr_zero_expected_per_bar,
        out_ann.sr_zero_expected / math.sqrt(bpy),
        rel_tol=1e-12,
    )


def test_sr_zero_expected_is_scaled_gumbel():
    """eq.7 with the v2 scaling: sr_zero = sqrt(V[{SR_n}]) × Gumbel(N),
    in annualised units."""
    rng = np.random.default_rng(9)
    rets = rng.normal(0.0002, 0.01, 2_000)
    for n_trials, v in [(2, 0.25), (8, 1.96), (21, 0.04)]:
        out = deflated_sharpe(
            sr_candidate=1.0, returns=rets, n_trials=n_trials,
            bars_per_year=365.25, sr_var_trials=v,
        )
        expected = math.sqrt(v) * _gumbel(n_trials)
        assert math.isclose(out.sr_zero_expected, expected, rel_tol=1e-12), (
            f"N={n_trials} V={v}: sr_zero={out.sr_zero_expected} "
            f"expected {expected}"
        )


def test_zero_edge_synthetic_does_not_clear_095():
    """Calibration sanity from the 2026-06-11 work order: with the
    units fix, a true-zero-edge synthetic strategy must NOT clear
    DSR >= 0.95.  Under the pre-fix unit mixing, any positive realised
    Sharpe saturated Φ to ~1.0 — this test pins the fix.

    100 independent zero-mean series at 1h frequency, each judged at
    its own realised annualised Sharpe (the most favourable claim a
    zero-edge strategy can honestly make), N=5 trials, V=1.0.
    """
    rng = np.random.default_rng(10)
    bpy = 8766.0
    n_above = 0
    n_total = 0
    for _ in range(100):
        rets = rng.normal(0.0, 0.005, 5_000)
        # Realised annualised Sharpe of this sample (mean/std × √bpy).
        sd = float(rets.std())
        if sd == 0.0:
            continue
        sr_ann = float(rets.mean()) / sd * math.sqrt(bpy)
        try:
            out = deflated_sharpe(
                sr_candidate=sr_ann, returns=rets, n_trials=5,
                bars_per_year=bpy, sr_var_trials=1.0,
            )
        except DSRError:
            continue
        n_total += 1
        if out.dsr >= 0.95:
            n_above += 1
    assert n_total > 90  # sanity: the loop actually ran
    # With the eq.7 haircut at N=5, V=1 (sr_zero ≈ 1.16 annualised),
    # zero-edge samples should essentially never clear 0.95.  Allow
    # ≤ 2 % for randomness headroom; the pre-fix behaviour was ~50 %+
    # (any positive-Sharpe sample saturated).
    assert n_above <= 2, (
        f"{n_above}/{n_total} zero-edge samples cleared DSR>=0.95 — "
        "units fix regression?"
    )


def test_bars_per_year_helpers():
    assert math.isclose(bars_per_year_for_timeframe("1h"), 365.25 * 24)
    assert math.isclose(bars_per_year_for_timeframe("1d"), 365.25)
    assert math.isclose(bars_per_year_for_timeframe("4h"), 365.25 * 6)
    assert math.isclose(bars_per_year_from_candle_hours(24.0), 365.25)
    with pytest.raises(DSRError):
        bars_per_year_for_timeframe("3d")
    with pytest.raises(DSRError):
        bars_per_year_from_candle_hours(0.0)


def test_sr_var_trials_negative_rejected():
    rng = np.random.default_rng(12)
    rets = rng.standard_normal(500)
    with pytest.raises(DSRError, match="sr_var_trials"):
        deflated_sharpe(
            sr_candidate=1.0, returns=rets, n_trials=5,
            bars_per_year=365.25, sr_var_trials=-0.1,
        )


# ── Minimum Track Record Length (BLP eq. 13) ─────────────────────────────────
#
# Tests for `min_track_record_length` and `mintrl_from_cpcv_result`.
# Legacy anchors run at bars_per_year=1.0 (annualised == per-bar); the
# v2 units behaviour has its own anchor below.

from backtest.dsr import (  # noqa: E402  (deliberate late import to keep DSR
    MinTRLResult,           #            tests above and MinTRL tests below)
    min_track_record_length,
    mintrl_from_cpcv_result,
)


# ── 1. Strong Sharpe → small MinTRL, not under-tested ───────────────────────

def test_mintrl_strong_sharpe_low_requirement():
    rng = np.random.default_rng(1)
    rets = rng.standard_normal(2000)
    out = min_track_record_length(
        sr_candidate=2.0, returns=rets, bars_per_year=1.0,
    )
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
    """Weak signal → long required track record → under-tested even at
    meaningful T:
        var_term ≈ 1
        min_trl ≈ 1 + (1.6449 / 0.05)² ≈ 1083
        at T=500 → under_tested=True, min_trl > 1000.
    """
    rng = np.random.default_rng(2)
    rets = rng.standard_normal(500)
    out = min_track_record_length(
        sr_candidate=0.05, returns=rets, bars_per_year=1.0,
    )
    assert out.min_trl > 1000, (
        f"weak SR=0.05 should need > 1000 bars; got {out.min_trl}"
    )
    assert out.under_tested is True, (
        f"T=500 < min_trl={out.min_trl}; under_tested should be True"
    )


# ── 3. Formula-correctness anchor: Gaussian, SR=1.0 → ≈5.06 ─────────────────

def test_mintrl_known_value_gaussian_sr1():
    """Hand-computed against BLP eq. 13 (per-bar SR = 1.0 at bpy=1):
        var_term = 1 + 0 + (3-1)/4 · 1 = 1.5
        Z_0.95   ≈ 1.6449
        min_trl  = 1 + 1.5 · 1.6449² = 1 + 1.5 · 2.706 ≈ 5.06
    """
    rng = np.random.default_rng(7)
    rets = rng.standard_normal(10_000)
    out = min_track_record_length(
        sr_candidate=1.0, returns=rets, bars_per_year=1.0,
    )
    assert 4.0 < out.min_trl < 7.0, (
        f"Gaussian SR=1.0 should give MinTRL ≈ 5.06; got {out.min_trl}"
    )


# ── 3b. v2 units anchor: annualised SR=1.0 on 1d bars ≈ 2.71 years ──────────

def test_mintrl_v2_units_annualised_sr1_daily():
    """Gate spec v2 anchor (audit §4): at annualised SR 1.0 the
    requirement is ≈ Z²·bars_per_year bars = 2.706 years, regardless
    of frequency.  On Gaussian per-bar returns the per-bar moment
    corrections are negligible (sr_pb ≈ 0.052 at 1d).
        min_trl ≈ 1 + (1.6449 / (1/√365.25))² ≈ 1 + 2.706·365.25 ≈ 989
    """
    rng = np.random.default_rng(21)
    rets = rng.normal(0.0, 0.02, 5_000)
    out = min_track_record_length(
        sr_candidate=1.0, returns=rets, bars_per_year=365.25,
    )
    assert 930 < out.min_trl < 1050, (
        f"annualised SR=1.0 at 1d should need ≈ 989 bars; got {out.min_trl}"
    )
    assert 2.5 < out.min_trl_years < 2.9, (
        f"calendar reading should be ≈ 2.71 years; got {out.min_trl_years}"
    )
    assert math.isclose(
        out.sr_candidate_per_bar, 1.0 / math.sqrt(365.25), rel_tol=1e-12,
    )


# ── 4. SR ≈ 0 rejected ──────────────────────────────────────────────────────

def test_mintrl_zero_sr_rejected():
    rng = np.random.default_rng(3)
    rets = rng.standard_normal(500)
    with pytest.raises(DSRError) as excinfo:
        min_track_record_length(
            sr_candidate=0.0, returns=rets, bars_per_year=1.0,
        )
    msg = str(excinfo.value)
    assert "denominator" in msg or "undefined" in msg, (
        f"DSRError message should mention 'denominator' or 'undefined'; "
        f"got: {msg}"
    )


# ── 5. Negative SR is symmetric on (skew≈0, kurt≈3) returns ─────────────────

def test_mintrl_negative_sr_symmetric():
    rng = np.random.default_rng(11)
    rets = rng.standard_normal(2000)
    pos = min_track_record_length(
        sr_candidate=+1.0, returns=rets, bars_per_year=1.0,
    )
    neg = min_track_record_length(
        sr_candidate=-1.0, returns=rets, bars_per_year=1.0,
    )
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
            bars_per_year=1.0,
        )


# ── 7. Higher confidence → longer required track record ─────────────────────

def test_mintrl_confidence_higher_means_longer():
    rng = np.random.default_rng(13)
    rets = rng.standard_normal(2000)
    out_95 = min_track_record_length(
        sr_candidate=1.0, returns=rets, confidence=0.95, bars_per_year=1.0,
    )
    out_99 = min_track_record_length(
        sr_candidate=1.0, returns=rets, confidence=0.99, bars_per_year=1.0,
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
        min_track_record_length(
            sr_candidate=1.0, returns=rets, bars_per_year=1.0,
        )


# ── 9. under_tested flag is consistent with min_trl vs t_observed ───────────

def test_mintrl_underflow_flag_consistency():
    rng = np.random.default_rng(17)

    # T_observed > min_trl → not under-tested.
    # Strong SR=2.0 → min_trl ≈ 3; T=200 ≫ 3.
    rets_long = rng.standard_normal(200)
    out_safe = min_track_record_length(
        sr_candidate=2.0, returns=rets_long, bars_per_year=1.0,
    )
    assert out_safe.t_observed > out_safe.min_trl
    assert out_safe.under_tested is False

    # T_observed < min_trl → under-tested.
    # Weak SR=0.05 → min_trl ≈ 1083; T=100 ≪ 1083.
    rets_short = rng.standard_normal(100)
    out_under = min_track_record_length(
        sr_candidate=0.05, returns=rets_short, bars_per_year=1.0,
    )
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
    # candle_duration_h=24 → bars_per_year 365.25 derived.
    assert math.isclose(out.bars_per_year, 365.25, rel_tol=1e-9)


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
