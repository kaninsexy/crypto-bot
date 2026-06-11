"""
backtest/dsr.py — Deflated Sharpe Ratio (sacred harness).

Reference: Bailey, D.H. & López de Prado, M. (2014).
"The Deflated Sharpe Ratio".

This module is part of the validation harness — modifying it requires
human approval per CLAUDE.md.  The 2026-06-11 gate-spec-v2 contract
change (explicit `bars_per_year`; eq.7 cross-trial variance scaling)
was human-pre-authorized in the 2026-06-11 work order implementing
docs/gate_recalibration_audit_2026-06.md.

Methodology
───────────
DSR adjusts an observed Sharpe for:
  - Number of trials N (multiple-testing inflation, BLP eq. 7),
    scaled by the realized cross-trial Sharpe dispersion
    sqrt(V[{SR_n}]) (per-family; see `backtest/families.py`)
  - Skewness and kurtosis of the underlying return series
    (non-Gaussian variance correction, BLP eq. 9)
  - Sample size T (finite-T variance of the SR estimator, BLP eq. 9)

Output: P(SR* > 0 | observed SR, N trials), the probability that the
observed Sharpe is non-spurious given the multiple-testing context.

This is the keep/reject gate per docs/validation_framework.md
§ "Deflated Sharpe Ratio".

Units (gate spec v2, 2026-06-11)
────────────────────────────────
BLP eq. 9 (SR-estimator variance) and eq. 13 (MinTRL) operate in
PER-BAR units: the SR in those formulas must be on the same period
basis as the return series whose skew/kurt/T they consume.  Every
caller in this codebase reports ANNUALISED Sharpes (engine.py
`sharpe_ratio` docstring: "Annualised Sharpe (rf = 0)").  The pre-v2
implementation plugged the annualised SR straight into the per-bar
formulas, inflating the eq.9 z-score by ~sqrt(bars_per_year) (≈19× at
1d, ≈94× at 1h) — which is why every positive-Sharpe trial recorded
dsr_validation = 1.0 and absurdly small mintrl values.  See
docs/gate_recalibration_audit_2026-06.md §1 "Additional finding".

v2 contract: callers pass the engine-reported ANNUALISED Sharpe plus
an explicit `bars_per_year`; this module converts to per-bar
(sr_pb = sr_ann / sqrt(bars_per_year)) before the BLP formulas and
echoes both unit systems on the result dataclasses.  All persisted
`dsr_validation` / `mintrl` values written BEFORE this change are
units-invalid and must not be compared against post-fix values
(docs/validation_framework.md § Gate spec v2).

Provenance
──────────
The same `deflated_sharpe` function backs both trials.log fields:

  dsr_validation:  inputs from CPCV block-Sharpe machinery via
                   `dsr_from_cpcv_result`. Concatenated valid-block
                   returns drive T/skew/kurt; the headline full-dev-
                   window Sharpe is passed by the caller as
                   `sr_candidate`.

  dsr_holdout:     inputs from a single holdout-window engine.run.
                   Caller passes the run's reported Sharpe and the
                   per-bar returns (eq.curve.pct_change().dropna())
                   directly to `deflated_sharpe`. CPCV is bypassed
                   entirely — the holdout is a single window with
                   no block distribution.
"""

import math
from dataclasses import dataclass
from typing import Optional

import numpy as np
from scipy import stats

from backtest.cpcv import CPCVResult


# ── Constants ────────────────────────────────────────────────────────────────

# Below this T, the BLP variance formula's finite-sample assumption
# is too thin for the result to mean anything. Hard-floored at 30 by
# convention; calibration of a stricter floor is a Phase 3b item.
_MIN_SAMPLE_SIZE: int = 30

# Euler–Mascheroni constant, used in the Gumbel approximation of
# E[max | null] for N > 1 (BLP eq. 7).
_EULER_MASCHERONI: float = 0.5772156649015329

# Hours in a (mean) year — matches engine.py / baseline.py / cpcv
# annualisation (365.25 * 24).
_HOURS_PER_YEAR: float = 365.25 * 24

_TIMEFRAME_HOURS: dict[str, float] = {
    "1m": 1 / 60, "5m": 5 / 60, "15m": 15 / 60, "30m": 0.5,
    "1h": 1.0, "2h": 2.0, "4h": 4.0, "8h": 8.0, "12h": 12.0,
    "1d": 24.0, "1w": 168.0,
}


def bars_per_year_from_candle_hours(candle_hours: float) -> float:
    """Bars per year for a candle duration in hours (engine
    annualisation convention: 365.25 × 24 hours per year)."""
    if candle_hours <= 0 or not math.isfinite(candle_hours):
        raise DSRError(
            f"candle_hours must be finite and > 0; got {candle_hours}"
        )
    return _HOURS_PER_YEAR / candle_hours


def bars_per_year_for_timeframe(timeframe: str) -> float:
    """Bars per year for a manifest timeframe string ('1h', '4h',
    '1d', ...).  Raises DSRError on an unknown timeframe."""
    h = _TIMEFRAME_HOURS.get(timeframe)
    if h is None:
        raise DSRError(
            f"unknown timeframe {timeframe!r}; known: "
            f"{sorted(_TIMEFRAME_HOURS)}"
        )
    return _HOURS_PER_YEAR / h


# ── Result + exception ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class DSRResult:
    """Outcome of one DSR computation.

    Annualised-unit fields (engine convention — comparable with
    trials.log `sharpe` and `buy_and_hold_sharpe`):
      sr_candidate:          The annualised Sharpe being deflated
                             (echoed back).
      sr_zero_expected:      sqrt(V[{SR_n}]) × E[max | null, N]
                             (BLP eq. 7), in ANNUALISED units —
                             directly comparable with sr_candidate.
                             Exactly 0.0 when N == 1.
      sr_std_annualised:     SE of the ANNUALISED SR estimator
                             (= sr_std × sqrt(bars_per_year)).

    Per-bar fields (the units the BLP formulas run in):
      sr_candidate_per_bar:  sr_candidate / sqrt(bars_per_year).
      sr_zero_expected_per_bar: sr_zero_expected / sqrt(bars_per_year).
      sr_std:                SE of the PER-BAR SR estimator (BLP
                             eq. 9, sqrt of variance term).  Field
                             name kept from v1; the v1 value was
                             unit-corrupt, not differently-scaled —
                             do not compare across the fix.

    Shared:
      dsr:               P(SR* > 0 | observed SR, N trials) ∈ [0, 1].
                         The keep/reject gate.  z is computed in
                         per-bar units; Φ(z) is unit-free.
      t:                 Sample size used (return count after any
                         caller-side trimming).
      skew:              Pearson skew of the per-bar returns.
      kurt:              Pearson kurtosis (Gaussian = 3).
      n_trials:          Multiple-testing trial count passed in
                         (per-FAMILY under gate spec v2).
      sr_var_trials:     V[{SR_n}] used in the eq.7 scaling
                         (annualised-Sharpe units; 1.0 fallback).
      bars_per_year:     The conversion factor used.
    """
    dsr: float
    sr_candidate: float
    sr_std: float
    sr_zero_expected: float
    t: int
    skew: float
    kurt: float
    n_trials: int
    bars_per_year: float
    sr_candidate_per_bar: float
    sr_zero_expected_per_bar: float
    sr_std_annualised: float
    sr_var_trials: float


class DSRError(RuntimeError):
    """A DSR runtime problem: insufficient sample, non-finite inputs,
    n_trials < 1, bad bars_per_year / sr_var_trials, or sr_var
    non-positive (skew/kurt/SR combination that breaks the BLP
    variance formula).  Indicates the deflation is statistically
    meaningless on these inputs."""


# ── Core computation ─────────────────────────────────────────────────────────

def deflated_sharpe(
    sr_candidate: float,
    returns: np.ndarray,
    n_trials: int,
    *,
    bars_per_year: float,
    sr_var_trials: float = 1.0,
) -> DSRResult:
    """Compute the Deflated Sharpe Ratio.

    Args:
      sr_candidate:  The ANNUALISED Sharpe being put forward as the
                     claim.  Pass the engine-reported full-window
                     Sharpe (the headline backtest number), NOT a
                     block-Sharpe mean or other CV statistic.  This
                     keeps `dsr_validation` and `dsr_holdout`
                     symmetric — both deflate the headline claim,
                     only the return series differs in provenance.
      returns:       Per-bar return series underlying the candidate.
                     For `dsr_validation`: concatenate
                     `CPCVResult.per_block_returns`, skipping empty
                     arrays.  For `dsr_holdout`: holdout-window
                     `equity_curve.pct_change().dropna()`.
                     T = len(this).
      n_trials:      Multiple-testing trial count.  Gate spec v2:
                     the per-FAMILY count from
                     `backtest.families.family_sharpe_stats` plus 1
                     for the trial being deflated.  Must be ≥ 1.
      bars_per_year: REQUIRED (v2 contract change).  Bars per year of
                     the return series, e.g. 8766 for 1h, 365.25 for
                     1d.  Use `bars_per_year_for_timeframe` /
                     `bars_per_year_from_candle_hours`.  The
                     annualised `sr_candidate` is converted to
                     per-bar before the BLP eq.7/9 formulas.
      sr_var_trials: V[{SR_n}] — realized variance of observed
                     ANNUALISED Sharpes across trials in the same
                     strategy family (BLP eq.7's variance term).
                     Default 1.0 = the conservative fallback for
                     families with < 2 finite trials.  Must be ≥ 0.

    Returns:
      DSRResult with `dsr` ∈ [0, 1] and intermediate quantities in
      both unit systems.

    Raises:
      DSRError: T < 30, n_trials < 1, non-finite inputs, bad
                bars_per_year / sr_var_trials, or sr_var
                non-positive.
    """
    # 1. Input validation.
    if n_trials < 1:
        raise DSRError(f"n_trials must be ≥ 1; got {n_trials}")
    if not math.isfinite(bars_per_year) or bars_per_year <= 0:
        raise DSRError(
            f"bars_per_year must be finite and > 0; got {bars_per_year}"
        )
    if not math.isfinite(sr_var_trials) or sr_var_trials < 0:
        raise DSRError(
            f"sr_var_trials must be finite and ≥ 0; got {sr_var_trials}"
        )
    arr = np.asarray(returns, dtype=float)
    t = arr.size
    if t < _MIN_SAMPLE_SIZE:
        raise DSRError(
            f"sample size T = {t} below floor of {_MIN_SAMPLE_SIZE}; "
            "DSR not statistically meaningful"
        )
    if not math.isfinite(sr_candidate) or not np.all(np.isfinite(arr)):
        raise DSRError(
            "non-finite values in inputs (sr_candidate or returns)"
        )

    # 2. Per-bar conversion (v2 units fix).  All BLP formulas below
    #    run on sr_pb; annualised echoes are derived at the end.
    sqrt_bpy = math.sqrt(bars_per_year)
    sr_pb = sr_candidate / sqrt_bpy

    # 3. Higher moments (Pearson convention; Gaussian kurt = 3).
    skew = float(stats.skew(arr))
    kurt = float(stats.kurtosis(arr, fisher=False))

    # 4. SE of the per-bar SR estimator (BLP eq. 9).
    sr_var = (
        1.0
        - skew * sr_pb
        + (kurt - 1.0) / 4.0 * sr_pb ** 2
    ) / (t - 1)
    if sr_var <= 0 or not math.isfinite(sr_var):
        raise DSRError(
            f"sr_var = {sr_var} non-positive or non-finite "
            f"(skew={skew}, kurt={kurt}, sr_pb={sr_pb}, T={t})"
        )
    sr_std_pb = math.sqrt(sr_var)

    # 5. E[max | null, N trials] (BLP eq. 7), scaled by the realized
    #    cross-trial Sharpe dispersion.  V[{SR_n}] is measured on
    #    ANNUALISED Sharpes (that is what trials.log records), so the
    #    Gumbel product is annualised; convert to per-bar for the z.
    if n_trials == 1:
        # No multiple-testing inflation: skip the Gumbel approximation
        # (degenerate at N=1, since norm.ppf(1 - 1/1) = +inf) and
        # deflate against a null Sharpe of zero.
        sr_zero_ann = 0.0
    else:
        n = float(n_trials)
        gumbel = (
            (1.0 - _EULER_MASCHERONI) * stats.norm.ppf(1.0 - 1.0 / n)
            + _EULER_MASCHERONI * stats.norm.ppf(1.0 - 1.0 / (n * math.e))
        )
        sr_zero_ann = math.sqrt(sr_var_trials) * gumbel
    sr_zero_pb = sr_zero_ann / sqrt_bpy

    z_score = (sr_pb - sr_zero_pb) / sr_std_pb
    dsr = float(stats.norm.cdf(z_score))

    return DSRResult(
        dsr=dsr,
        sr_candidate=float(sr_candidate),
        sr_std=float(sr_std_pb),
        sr_zero_expected=float(sr_zero_ann),
        t=int(t),
        skew=skew,
        kurt=kurt,
        n_trials=int(n_trials),
        bars_per_year=float(bars_per_year),
        sr_candidate_per_bar=float(sr_pb),
        sr_zero_expected_per_bar=float(sr_zero_pb),
        sr_std_annualised=float(sr_std_pb * sqrt_bpy),
        sr_var_trials=float(sr_var_trials),
    )


# ── CPCV adapter ─────────────────────────────────────────────────────────────

def _bars_per_year_from_cpcv(
    result: CPCVResult,
    bars_per_year: Optional[float],
) -> float:
    """Resolve bars_per_year for a CPCV adapter call: explicit wins;
    otherwise derive exactly from the CPCVResult's candle duration
    (populated by every v2 runner).  Raises DSRError when neither is
    available — silently guessing a frequency is how the v1 units bug
    survived for a year."""
    if bars_per_year is not None:
        return float(bars_per_year)
    cdh = getattr(result, "candle_duration_h", None)
    if cdh is None:
        raise DSRError(
            "bars_per_year not supplied and CPCVResult.candle_duration_h "
            "is unset (pre-v2 result object?).  Pass bars_per_year "
            "explicitly, e.g. bars_per_year_for_timeframe('1d')."
        )
    return bars_per_year_from_candle_hours(float(cdh))


def dsr_from_cpcv_result(
    result: CPCVResult,
    strategy_id: str,
    sr_candidate: float,
    bars_per_year: Optional[float] = None,
) -> DSRResult:
    """Compute DSR from a CPCV block-Sharpe run plus trials.log lookup.

    Concatenates valid (non-empty) per-block return arrays into a
    single return series, fetches the per-FAMILY trial count and
    cross-trial Sharpe variance via
    `backtest.families.family_sharpe_stats` (gate spec v2; the
    pre-v2 per-strategy `count_trials_for_dsr` underestimated N for
    families probed through many sibling strategies), and delegates
    to `deflated_sharpe`.

    Args:
      result:        CPCVResult from `run_cpcv`.
      strategy_id:   Strategy ID for the family lookup.
      sr_candidate:  REQUIRED.  Pass the engine-reported full-dev-
                     window ANNUALISED Sharpe (headline number from
                     the iteration backtest), NOT
                     `result.sharpe_distribution['mean']`.  The mean
                     across blocks is a CV statistic; the headline
                     is what's being claimed and what BLP deflates.
      bars_per_year: Optional explicit bar frequency.  When omitted,
                     derived exactly from
                     `result.candle_duration_h` (set by every v2
                     CPCV runner from the data's own index).

    Returns:
      DSRResult.

    Raises:
      DSRError: all blocks empty, missing frequency information,
                plus everything `deflated_sharpe` raises.
    """
    # Lazy import to dodge any future circular-import risk.
    from backtest import families as _families

    valid = [r for r in result.per_block_returns if r.size > 0]
    if not valid:
        raise DSRError(
            "CPCVResult has no valid (non-empty) per-block returns; "
            "DSR cannot be computed.  Did run_cpcv enter the > 50 % "
            "NaN branch?"
        )
    concat = np.concatenate(valid)
    bpy = _bars_per_year_from_cpcv(result, bars_per_year)

    stats_f = _families.family_sharpe_stats(strategy_id)
    # +1: the trial being deflated is itself part of the budget
    # (pre-append convention, same as the May-2026 trial scripts).
    n_trials = max(stats_f.n_trials + 1, 1)

    return deflated_sharpe(
        sr_candidate=sr_candidate,
        returns=concat,
        n_trials=n_trials,
        bars_per_year=bpy,
        sr_var_trials=stats_f.sr_var,
    )


# ── Minimum Track Record Length (BLP eq. 13) ─────────────────────────────────
#
# MinTRL answers a different question from DSR: "given an observed Sharpe,
# how many bars of data do I need before I can call that Sharpe
# statistically distinguishable from zero?"  When T_observed < MinTRL the
# strategy is *under-tested* — a third gate state distinct from
# pass/fail per docs/validation_framework.md § "Minimum Track Record
# Length".  The Phase 3c gate logic decides what to do (typically: keep
# on paper, do not deploy live).
#
# This belongs in dsr.py because the math is from the same paper as DSR
# (Bailey & López de Prado 2014, eq. 13 vs eqs. 7+9) and it consumes the
# same skew/kurt/T inputs.  The "sacred harness" restriction is on
# semantics (validation correctness), not file boundaries.


@dataclass(frozen=True)
class MinTRLResult:
    """Outcome of one MinTRL computation.

    Attributes:
      min_trl:        Minimum required sample size, in bars (per-bar
                      units — v2 units fix; pre-fix persisted values
                      are units-invalid).
      t_observed:     The actual sample size of the input returns.
      under_tested:   True iff t_observed < min_trl.  This is the
                      third gate state per validation_framework.md
                      § Minimum Track Record Length — distinct from
                      DSR pass/fail and used by Phase 3c gate logic.
      sr_candidate:   The ANNUALISED Sharpe being judged (echoed for
                      trials.log consistency).
      sr_candidate_per_bar:  sr_candidate / sqrt(bars_per_year) — the
                      value eq.13 actually ran on.
      bars_per_year:  The conversion factor used.
      min_trl_years:  min_trl / bars_per_year — calendar-time reading
                      (frequency-independent to first order; see the
                      audit's MinTRL pre-check).
      skew:           Pearson skew of returns.
      kurt:           Pearson kurtosis (Gaussian = 3).
      confidence:     Confidence level (1 - α) used to derive Z_α.
                      Default 0.95 (one-sided), Z_α ≈ 1.6449.
    """
    min_trl: float
    t_observed: int
    under_tested: bool
    sr_candidate: float
    skew: float
    kurt: float
    confidence: float
    bars_per_year: float
    sr_candidate_per_bar: float
    min_trl_years: float


def min_track_record_length(
    sr_candidate: float,
    returns: np.ndarray,
    confidence: float = 0.95,
    *,
    bars_per_year: float,
) -> MinTRLResult:
    """Compute the minimum track record length for the candidate Sharpe.

    BLP eq. 13 (per-bar units):
        MinTRL = 1 + (1 - skew·SR_pb + (kurt-1)/4 · SR_pb²) · (Z_α / SR_pb)²

    Args:
      sr_candidate:  The ANNUALISED Sharpe being judged.  Pass the
                     engine-reported full-window Sharpe (same
                     convention as `deflated_sharpe`'s
                     `sr_candidate`) — the headline claim, not a CV
                     mean.  Converted to per-bar internally (v2
                     units fix).
      returns:       Per-bar return series.  T_observed = len(returns).
                     For dsr_validation context: concatenated
                     `CPCVResult.per_block_returns` (skip empty
                     entries).  For dsr_holdout context:
                     holdout-window
                     `equity_curve.pct_change().dropna()`.
      confidence:    One-sided confidence level (1 - α) for Z_α.
                     Default 0.95 → Z_α ≈ 1.6449.  Override with 0.99
                     for stricter under-tested triage.
      bars_per_year: REQUIRED (v2 contract change).  Bars per year of
                     the return series.

    Returns:
      MinTRLResult with `min_trl` (in bars), `t_observed`,
      `under_tested` flag, and the moments used.

    Raises:
      DSRError: T < 30, sr_candidate ≈ 0 (formula has SR in
                denominator — undefined at zero), non-finite inputs,
                confidence not in (0, 1), bad bars_per_year, or
                variance term non-positive.
    """
    if not (0.0 < confidence < 1.0):
        raise DSRError(
            f"confidence must be in (0, 1); got {confidence}"
        )
    if not math.isfinite(sr_candidate):
        raise DSRError("sr_candidate is non-finite")
    if not math.isfinite(bars_per_year) or bars_per_year <= 0:
        raise DSRError(
            f"bars_per_year must be finite and > 0; got {bars_per_year}"
        )
    if abs(sr_candidate) < 1e-9:
        raise DSRError(
            f"sr_candidate ≈ 0 ({sr_candidate}); MinTRL undefined "
            "(formula has SR in denominator — 'how long to be sure "
            "the Sharpe is non-zero' is undefined when the Sharpe "
            "itself is zero)"
        )

    arr = np.asarray(returns, dtype=float)
    t_observed = arr.size
    if t_observed < _MIN_SAMPLE_SIZE:
        raise DSRError(
            f"T_observed = {t_observed} below floor of "
            f"{_MIN_SAMPLE_SIZE}; moments unreliable"
        )
    if not np.all(np.isfinite(arr)):
        raise DSRError("non-finite values in returns")

    sqrt_bpy = math.sqrt(bars_per_year)
    sr_pb = sr_candidate / sqrt_bpy

    skew_val = float(stats.skew(arr))
    kurt_val = float(stats.kurtosis(arr, fisher=False))
    z_alpha = float(stats.norm.ppf(confidence))

    # Eq. 13 squares (Z_α / SR), so the formula is symmetric in |SR|.
    # Use abs() on the SR term so the result stays positive for
    # negative-edge strategies — same number, different
    # interpretation (length to be confidently negative).
    sr_abs = abs(sr_pb)
    variance_term = (
        1.0
        - skew_val * sr_pb
        + (kurt_val - 1.0) / 4.0 * sr_pb ** 2
    )
    if variance_term <= 0 or not math.isfinite(variance_term):
        raise DSRError(
            f"variance term {variance_term} non-positive or "
            f"non-finite (skew={skew_val}, kurt={kurt_val}, "
            f"sr_pb={sr_pb}); MinTRL undefined"
        )

    min_trl = 1.0 + variance_term * (z_alpha / sr_abs) ** 2
    under_tested = t_observed < min_trl

    return MinTRLResult(
        min_trl=float(min_trl),
        t_observed=int(t_observed),
        under_tested=bool(under_tested),
        sr_candidate=float(sr_candidate),
        skew=skew_val,
        kurt=kurt_val,
        confidence=float(confidence),
        bars_per_year=float(bars_per_year),
        sr_candidate_per_bar=float(sr_pb),
        min_trl_years=float(min_trl / bars_per_year),
    )


def mintrl_from_cpcv_result(
    result: CPCVResult,
    sr_candidate: float,
    confidence: float = 0.95,
    bars_per_year: Optional[float] = None,
) -> MinTRLResult:
    """Compute MinTRL from a CPCV block-Sharpe run.

    Same input contract as `dsr_from_cpcv_result`: concatenate valid
    (non-empty) per-block return arrays into a single return series,
    delegate to `min_track_record_length`.  No `trials.log` lookup —
    MinTRL is per-strategy, not multi-test.

    Args:
      result:        CPCVResult from `run_cpcv`.
      sr_candidate:  REQUIRED.  Engine-reported full-dev-window
                     ANNUALISED Sharpe — the headline number, not a
                     CV mean.
      confidence:    Forwarded to `min_track_record_length`.
      bars_per_year: Optional explicit bar frequency; derived from
                     `result.candle_duration_h` when omitted.

    Returns:
      MinTRLResult.

    Raises:
      DSRError: all blocks empty, missing frequency information, plus
                everything `min_track_record_length` raises.
    """
    valid = [r for r in result.per_block_returns if r.size > 0]
    if not valid:
        raise DSRError(
            "CPCVResult has no valid per-block returns; MinTRL "
            "cannot be computed."
        )
    concat = np.concatenate(valid)
    bpy = _bars_per_year_from_cpcv(result, bars_per_year)
    return min_track_record_length(
        sr_candidate=sr_candidate,
        returns=concat,
        confidence=confidence,
        bars_per_year=bpy,
    )
