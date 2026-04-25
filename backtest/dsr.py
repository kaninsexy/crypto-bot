"""
backtest/dsr.py — Deflated Sharpe Ratio (sacred harness).

Reference: Bailey, D.H. & López de Prado, M. (2014).
"The Deflated Sharpe Ratio".

This module is part of the validation harness — modifying it requires
human approval per CLAUDE.md.

Methodology
───────────
DSR adjusts an observed Sharpe for:
  - Number of trials N (multiple-testing inflation, BLP eq. 7)
  - Skewness and kurtosis of the underlying return series
    (non-Gaussian variance correction, BLP eq. 9)
  - Sample size T (finite-T variance of the SR estimator, BLP eq. 9)

Output: P(SR* > 0 | observed SR, N trials), the probability that the
observed Sharpe is non-spurious given the multiple-testing context.

This is the keep/reject gate per docs/validation_framework.md
§ "Deflated Sharpe Ratio".

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


# ── Result + exception ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class DSRResult:
    """Outcome of one DSR computation.

    Attributes:
      dsr:               P(SR* > 0 | observed SR, N trials) ∈ [0, 1].
                         The keep/reject gate.
      sr_candidate:      The Sharpe being deflated (echoed back).
      sr_std:            SE of the SR estimator (BLP eq. 9, sqrt of
                         variance term).
      sr_zero_expected:  E[max | null, N trials] (BLP eq. 7).  Set to
                         exactly 0.0 when N == 1 — at one trial there
                         is no multiple-testing inflation, the Gumbel
                         approximation is degenerate, and we deflate
                         against a null Sharpe of zero.
      t:                 Sample size used (return count after any
                         caller-side trimming).
      skew:              Pearson skew of the returns.
      kurt:              Pearson kurtosis of the returns
                         (Gaussian = 3; Fisher convention would give
                         ~ 0 — guarded against in the BLP variance
                         formula by `kurt - 1`).
      n_trials:          Multiple-testing trial count passed in.
    """
    dsr: float
    sr_candidate: float
    sr_std: float
    sr_zero_expected: float
    t: int
    skew: float
    kurt: float
    n_trials: int


class DSRError(RuntimeError):
    """A DSR runtime problem: insufficient sample, non-finite inputs,
    n_trials < 1, or sr_var non-positive (skew/kurt/SR combination
    that breaks the BLP variance formula).  Indicates the deflation
    is statistically meaningless on these inputs."""


# ── Core computation ─────────────────────────────────────────────────────────

def deflated_sharpe(
    sr_candidate: float,
    returns: np.ndarray,
    n_trials: int,
) -> DSRResult:
    """Compute the Deflated Sharpe Ratio.

    Args:
      sr_candidate:  The Sharpe being put forward as the claim.
                     Pass the engine-reported full-window Sharpe (the
                     headline backtest number), NOT a block-Sharpe
                     mean or other CV statistic.  This keeps
                     `dsr_validation` and `dsr_holdout` symmetric —
                     both deflate the headline claim, only the return
                     series differs in provenance.
      returns:       Per-bar return series underlying the candidate.
                     For `dsr_validation`: concatenate
                     `CPCVResult.per_block_returns`, skipping empty
                     arrays.  For `dsr_holdout`: holdout-window
                     `equity_curve.pct_change().dropna()`.
                     T = len(this).
      n_trials:      Number of variations tested for this strategy
                     (per-strategy count from trials.log via
                     `backtest.trials.count_trials_for_dsr`).
                     Must be ≥ 1.

    Returns:
      DSRResult with `dsr` ∈ [0, 1] and intermediate quantities.

    Raises:
      DSRError: T < 30, n_trials < 1, non-finite inputs, or sr_var
                non-positive.
    """
    # 1. Input validation.
    if n_trials < 1:
        raise DSRError(f"n_trials must be ≥ 1; got {n_trials}")
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

    # 2. Higher moments (Pearson convention; Gaussian kurt = 3).
    skew = float(stats.skew(arr))
    kurt = float(stats.kurtosis(arr, fisher=False))

    # 3. SE of SR estimator (BLP eq. 9).
    sr_var = (
        1.0
        - skew * sr_candidate
        + (kurt - 1.0) / 4.0 * sr_candidate ** 2
    ) / (t - 1)
    if sr_var <= 0 or not math.isfinite(sr_var):
        raise DSRError(
            f"sr_var = {sr_var} non-positive or non-finite "
            f"(skew={skew}, kurt={kurt}, sr={sr_candidate}, T={t})"
        )
    sr_std = math.sqrt(sr_var)

    # 4. E[max | null, N trials] (BLP eq. 7).
    if n_trials == 1:
        # No multiple-testing inflation: skip the Gumbel approximation
        # (degenerate at N=1, since norm.ppf(1 - 1/1) = +inf) and
        # deflate against a null Sharpe of zero.
        sr_zero_expected = 0.0
        z_score = sr_candidate / sr_std
    else:
        n = float(n_trials)
        sr_zero_expected = (
            (1.0 - _EULER_MASCHERONI) * stats.norm.ppf(1.0 - 1.0 / n)
            + _EULER_MASCHERONI * stats.norm.ppf(1.0 - 1.0 / (n * math.e))
        )
        z_score = (sr_candidate - sr_zero_expected) / sr_std

    dsr = float(stats.norm.cdf(z_score))

    return DSRResult(
        dsr=dsr,
        sr_candidate=float(sr_candidate),
        sr_std=float(sr_std),
        sr_zero_expected=float(sr_zero_expected),
        t=int(t),
        skew=skew,
        kurt=kurt,
        n_trials=int(n_trials),
    )


# ── CPCV adapter ─────────────────────────────────────────────────────────────

def dsr_from_cpcv_result(
    result: CPCVResult,
    strategy_id: str,
    sr_candidate: float,
) -> DSRResult:
    """Compute DSR from a CPCV block-Sharpe run plus trials.log lookup.

    Concatenates valid (non-empty) per-block return arrays into a
    single return series, fetches `n_trials` from trials.log, and
    delegates to `deflated_sharpe`.

    Args:
      result:        CPCVResult from `run_cpcv`.
      strategy_id:   Strategy ID for trials.log lookup.
      sr_candidate:  REQUIRED.  Pass the engine-reported full-dev-
                     window Sharpe (headline number from the
                     iteration backtest), NOT
                     `result.sharpe_distribution['mean']`.  The mean
                     across blocks is a CV statistic; the headline
                     is what's being claimed and what BLP deflates.

    Returns:
      DSRResult.

    Raises:
      DSRError: all blocks empty, plus everything `deflated_sharpe`
                raises.
    """
    # Lazy import to dodge any future circular-import risk if
    # trials.py grows a back-reference to dsr.py.
    from backtest import trials

    valid = [r for r in result.per_block_returns if r.size > 0]
    if not valid:
        raise DSRError(
            "CPCVResult has no valid (non-empty) per-block returns; "
            "DSR cannot be computed.  Did run_cpcv enter the > 50 % "
            "NaN branch?"
        )
    concat = np.concatenate(valid)
    n_trials = trials.count_trials_for_dsr(strategy_id)
    return deflated_sharpe(
        sr_candidate=sr_candidate,
        returns=concat,
        n_trials=n_trials,
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
      min_trl:        Minimum required sample size, in bars.
      t_observed:     The actual sample size of the input returns.
      under_tested:   True iff t_observed < min_trl.  This is the
                      third gate state per validation_framework.md
                      § Minimum Track Record Length — distinct from
                      DSR pass/fail and used by Phase 3c gate logic.
      sr_candidate:   The Sharpe being judged (echoed for trials.log
                      consistency).
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


def min_track_record_length(
    sr_candidate: float,
    returns: np.ndarray,
    confidence: float = 0.95,
) -> MinTRLResult:
    """Compute the minimum track record length for the candidate Sharpe.

    BLP eq. 13:
        MinTRL = 1 + (1 - skew·SR + (kurt-1)/4 · SR²) · (Z_α / SR)²

    Args:
      sr_candidate:  The Sharpe being judged.  Pass the engine-reported
                     full-window Sharpe (same convention as
                     `deflated_sharpe`'s `sr_candidate`) — the headline
                     claim, not a CV mean.
      returns:       Per-bar return series.  T_observed = len(returns).
                     For dsr_validation context: concatenated
                     `CPCVResult.per_block_returns` (skip empty
                     entries).  For dsr_holdout context:
                     holdout-window
                     `equity_curve.pct_change().dropna()`.
      confidence:    One-sided confidence level (1 - α) for Z_α.
                     Default 0.95 → Z_α ≈ 1.6449.  Override with 0.99
                     for stricter under-tested triage.

    Returns:
      MinTRLResult with `min_trl` (in bars), `t_observed`,
      `under_tested` flag, and the moments used.

    Raises:
      DSRError: T < 30, sr_candidate ≈ 0 (formula has SR in
                denominator — undefined at zero), non-finite inputs,
                confidence not in (0, 1), or variance term
                non-positive.
    """
    if not (0.0 < confidence < 1.0):
        raise DSRError(
            f"confidence must be in (0, 1); got {confidence}"
        )
    if not math.isfinite(sr_candidate):
        raise DSRError("sr_candidate is non-finite")
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

    skew_val = float(stats.skew(arr))
    kurt_val = float(stats.kurtosis(arr, fisher=False))
    z_alpha = float(stats.norm.ppf(confidence))

    # Eq. 13 squares (Z_α / SR), so the formula is symmetric in |SR|.
    # Use abs() on the SR term so the result stays positive for
    # negative-edge strategies — same number, different
    # interpretation (length to be confidently negative).
    sr_abs = abs(sr_candidate)
    variance_term = (
        1.0
        - skew_val * sr_candidate
        + (kurt_val - 1.0) / 4.0 * sr_candidate ** 2
    )
    if variance_term <= 0 or not math.isfinite(variance_term):
        raise DSRError(
            f"variance term {variance_term} non-positive or "
            f"non-finite (skew={skew_val}, kurt={kurt_val}, "
            f"sr={sr_candidate}); MinTRL undefined"
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
    )


def mintrl_from_cpcv_result(
    result: CPCVResult,
    sr_candidate: float,
    confidence: float = 0.95,
) -> MinTRLResult:
    """Compute MinTRL from a CPCV block-Sharpe run.

    Same input contract as `dsr_from_cpcv_result`: concatenate valid
    (non-empty) per-block return arrays into a single return series,
    delegate to `min_track_record_length`.  No `trials.log` lookup —
    MinTRL is per-strategy, not multi-test.

    Args:
      result:        CPCVResult from `run_cpcv`.
      sr_candidate:  REQUIRED.  Engine-reported full-dev-window
                     Sharpe — the headline number, not a CV mean.
      confidence:    Forwarded to `min_track_record_length`.

    Returns:
      MinTRLResult.

    Raises:
      DSRError: all blocks empty, plus everything
                `min_track_record_length` raises.
    """
    valid = [r for r in result.per_block_returns if r.size > 0]
    if not valid:
        raise DSRError(
            "CPCVResult has no valid per-block returns; MinTRL "
            "cannot be computed."
        )
    concat = np.concatenate(valid)
    return min_track_record_length(
        sr_candidate=sr_candidate,
        returns=concat,
        confidence=confidence,
    )
