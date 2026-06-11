"""
backtest/calibration.py — Synthetic DSR threshold calibration.

NOT part of the sacred validation harness — this module is a calibration
tool, not a gate. It consumes `backtest.dsr.deflated_sharpe` to determine
the DSR threshold that separates signal from noise on crypto-realistic
synthetic returns.

Methodology
───────────
For each (distribution, n_trials) cell:
  1. Draw N_noise samples of T returns with true SR ≈ 0.
  2. Draw N_signal samples of T returns with true SR ≈ 1.0
     (moderate edge — strong enough that the N=20 Gumbel haircut
     shouldn't fully crush it, weak enough that the gate isn't
     trivially saturated).
  3. Compute DSR for each sample.
  4. Build ROC from the paired DSR distributions.
  5. Derive three thresholds: Youden's J, FPR ≤ 5%, TPR ≥ 80%.

The FPR ≤ 5% threshold is the production recommendation. Youden's J and
TPR ≥ 80% are sidecars — if all three cluster tightly, the threshold is
robust; if they diverge, the gate is distribution-sensitive and warrants
a closer look before locking in.

Distributions
─────────────
- student_t_df5:        Symmetric heavy-tailed.
- skewed_student_t:     Heavy-tailed and negatively skewed (crypto crash
                        asymmetry).

Pure Gaussian is intentionally skipped: calibrating on it would yield a
threshold too lenient for real crypto returns and defeat DSR's skew/kurt
correction.

Sample size
───────────
T=2000 per sample (~3 months of 1h bars). Calibration is valid for T at
this magnitude; recalibrate if Phase 3c runs on materially different
window sizes.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Optional

import numpy as np
from scipy import stats

from backtest.dsr import DSRError, deflated_sharpe


# ── Constants ─────────────────────────────────────────────────────────────────

_T_BARS: int = 20_000  # matches dsr_validation T = full dev window concat (~3yr × 1h bars)
_N_NOISE: int = 1000
_N_SIGNAL: int = 1000
_TARGET_SIGNAL_SHARPE: float = 1.0  # moderate edge — strong enough that the N=20 Gumbel haircut shouldn't fully crush it, weak enough that the gate isn't trivially saturated
_DAILY_VOL: float = 0.02  # 2% per-bar std, typical 1h crypto
_TRIAL_COUNTS: tuple[int, ...] = (1, 5, 10, 20, 50)
_DEFAULT_SEED: int = 20260425

# Annualisation factor for 1h bars: bars per year.
_BARS_PER_YEAR: float = 365.25 * 24


# ── Result containers ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ThresholdSet:
    """Three thresholds derived from one ROC curve."""
    youden_j: float                  # threshold maximising TPR − FPR
    youden_tpr: float
    youden_fpr: float
    fpr_capped: float                # min τ with FPR ≤ 0.05; NaN if infeasible
    fpr_capped_tpr: float
    fpr_capped_fpr: float
    tpr_floored: float               # max τ with TPR ≥ 0.80; NaN if infeasible
    tpr_floored_tpr: float
    tpr_floored_fpr: float


@dataclass(frozen=True)
class CalibrationCell:
    """One (distribution, n_trials) cell of the calibration grid."""
    distribution: str
    n_trials: int
    noise_dsr_mean: float
    noise_dsr_std: float
    signal_dsr_mean: float
    signal_dsr_std: float
    n_dsr_errors_noise: int          # samples that raised DSRError
    n_dsr_errors_signal: int
    thresholds: ThresholdSet


@dataclass(frozen=True)
class CalibrationReport:
    """Full calibration output."""
    seed: int
    t_bars: int
    n_noise: int
    n_signal: int
    target_signal_sharpe: float
    cells: list[CalibrationCell]
    production_recommendation: dict[str, dict[int, float]]
    # Per-distribution, per-N_trials FPR≤5% threshold table.
    # Shape: {distribution_name: {n_trials: tau}}.  No aggregation
    # across n_trials — calibration is N-sensitive, so the caller
    # picks the row matching the strategy's actual trials.log count.
    # NaN means the criterion was infeasible at that cell.

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, default=_json_default)


def _json_default(o):
    if isinstance(o, float) and not math.isfinite(o):
        return None  # NaN/inf → JSON null
    raise TypeError(f"not JSON serialisable: {type(o)}")


# ── Synthetic return generators ───────────────────────────────────────────────

def _drift_for_target_compound_sr(target_sharpe: float, std: float) -> float:
    """Per-bar drift μ that yields the target *compound* annualised
    Sharpe under `_annualised_sharpe`'s convention, including the
    Jensen / volatility-drag correction.

    The engine's Sharpe (mirrored by `_annualised_sharpe`) is computed
    on compound returns.  For a typical realisation of i.i.d. returns
    with mean μ and std σ:

        E[log(1+r)] ≈ μ − σ²/2
        log(1+total) ≈ T · (μ − σ²/2)
        ann_ret     ≈ exp(bpy · (μ − σ²/2)) − 1
        ann_vol     = σ · √bpy

    Solving target = ann_ret / ann_vol:

        exp(bpy · (μ − σ²/2)) = 1 + target · σ · √bpy
        ⇒ μ = log(1 + target · σ · √bpy) / bpy + σ²/2

    The σ²/2 term compensates the volatility drag so a typical
    realisation hits the target compound Sharpe.  For target_sharpe=0,
    μ = σ²/2 — small positive drift offsetting drag, leaving compound
    SR ≈ 0 by design.  This is the right "no edge" baseline for an
    engine that measures compound returns; using μ=0 instead would
    bias the noise distribution to large negative compound SRs and
    crush DSR symmetry.
    """
    bpy_sqrt = math.sqrt(_BARS_PER_YEAR)
    log_drift = math.log(
        1.0 + target_sharpe * std * bpy_sqrt
    ) / _BARS_PER_YEAR
    return log_drift + 0.5 * std * std


def _generate_student_t(
    rng: np.random.Generator,
    n: int,
    target_sharpe: float,
    df: int = 5,
) -> np.ndarray:
    """Student-t returns rescaled and centred to produce a target
    *compound* annualised Sharpe over T = n bars at 1h cadence.

    Standard t has E[X]=0 in expectation, but a finite sample's mean
    drifts; we recentre to zero before rescaling so the post-drift
    series's mean equals the intended drift exactly.
    """
    raw = rng.standard_t(df=df, size=n)
    raw = raw - raw.mean()
    raw = raw / raw.std() * _DAILY_VOL
    drift = _drift_for_target_compound_sr(target_sharpe, _DAILY_VOL)
    return raw + drift


def _generate_skewed_student_t(
    rng: np.random.Generator,
    n: int,
    target_sharpe: float,
    alpha: float = -4.0,
) -> np.ndarray:
    """Heavy-tailed AND negatively skewed (crypto crash asymmetry).
    Uses scipy.stats.skewnorm scaled to mimic t-like tails — not
    strictly a skewed-t, but captures the two relevant moments
    (kurtosis > 3, skew < 0) which are what DSR's correction acts on.

    skewnorm with α≠0 has a non-zero distributional mean, so we
    recentre before rescaling — otherwise the un-centred bias swamps
    the small drift (e.g. for α=-4 the raw mean is ~−0.78σ, an order
    of magnitude larger than the target_sharpe=1.0 drift, producing
    misleading negative-SR samples in the "signal" arm).
    """
    raw = stats.skewnorm.rvs(a=alpha, size=n, random_state=rng)
    raw = raw - raw.mean()
    raw = raw / raw.std() * _DAILY_VOL
    drift = _drift_for_target_compound_sr(target_sharpe, _DAILY_VOL)
    return raw + drift


_DISTRIBUTIONS: dict[str, Callable] = {
    "student_t_df5": _generate_student_t,
    "skewed_student_t": _generate_skewed_student_t,
}


# ── Sharpe re-computation (matches engine convention) ─────────────────────────

def _annualised_sharpe(returns: np.ndarray, candle_hours: float = 1.0) -> float:
    """Annualised Sharpe matching backtest/engine.py:_compute_metrics shape.
    Used to compute sr_candidate per synthetic sample so the DSR call is
    self-consistent with how engine.run reports Sharpes."""
    n = returns.size
    if n == 0:
        return 0.0
    years = (n * candle_hours) / _BARS_PER_YEAR
    if years <= 0:
        return 0.0
    total = float(np.prod(1.0 + returns) - 1.0)
    if total <= -1.0:
        return 0.0
    ann_ret = ((1.0 + total) ** (1.0 / years) - 1.0) * 100.0
    bars_per_year = _BARS_PER_YEAR / candle_hours
    vol = float(returns.std()) * math.sqrt(bars_per_year) * 100.0
    if vol <= 0:
        return 0.0
    return ann_ret / vol


# ── DSR sampling ──────────────────────────────────────────────────────────────

def _sample_dsrs(
    generator: Callable,
    target_sharpe: float,
    n_samples: int,
    n_trials: int,
    rng: np.random.Generator,
) -> tuple[np.ndarray, int]:
    """Draw n_samples return series, compute DSR on each, return
    (dsr_array, n_errors).  Errors (DSRError, e.g. sr_var ≤ 0 on
    pathological samples) are excluded from the output array."""
    dsrs: list[float] = []
    n_errors = 0
    for _ in range(n_samples):
        rets = generator(rng, _T_BARS, target_sharpe)
        sr = _annualised_sharpe(rets)
        try:
            # Synthetic series are 1h-candle convention (candle_hours
            # = 1.0 in _annualised_sharpe), so bars/year =
            # _BARS_PER_YEAR / 1.0.  Gate spec v2 units contract.
            out = deflated_sharpe(
                sr_candidate=sr, returns=rets, n_trials=n_trials,
                bars_per_year=_BARS_PER_YEAR,
            )
            dsrs.append(out.dsr)
        except DSRError:
            n_errors += 1
    return np.asarray(dsrs, dtype=float), n_errors


# ── Threshold derivation ──────────────────────────────────────────────────────

def _derive_thresholds(
    noise_dsrs: np.ndarray, signal_dsrs: np.ndarray,
) -> ThresholdSet:
    """Build ROC and derive three thresholds.

    Sweep candidate τ ∈ unique(concat(noise, signal)). For each:
        TPR(τ) = mean(signal_dsrs > τ)
        FPR(τ) = mean(noise_dsrs > τ)
    Then derive Youden's J, FPR≤5% min-τ, TPR≥80% max-τ.
    """
    candidates = np.sort(np.unique(np.concatenate([noise_dsrs, signal_dsrs])))
    tprs = np.array([(signal_dsrs > t).mean() for t in candidates])
    fprs = np.array([(noise_dsrs > t).mean() for t in candidates])

    # Youden's J: argmax (TPR - FPR)
    j_idx = int(np.argmax(tprs - fprs))
    youden_tau = float(candidates[j_idx])
    youden_tpr = float(tprs[j_idx])
    youden_fpr = float(fprs[j_idx])

    # FPR ≤ 5%: smallest τ with FPR ≤ 0.05
    feasible = np.where(fprs <= 0.05)[0]
    if feasible.size == 0:
        fpr_tau = float("nan")
        fpr_tpr = float("nan")
        fpr_fpr = float("nan")
    else:
        idx = int(feasible[0])  # candidates sorted ascending → smallest τ
        fpr_tau = float(candidates[idx])
        fpr_tpr = float(tprs[idx])
        fpr_fpr = float(fprs[idx])

    # TPR ≥ 80%: largest τ with TPR ≥ 0.80
    feasible = np.where(tprs >= 0.80)[0]
    if feasible.size == 0:
        tpr_tau = float("nan")
        tpr_tpr = float("nan")
        tpr_fpr = float("nan")
    else:
        idx = int(feasible[-1])
        tpr_tau = float(candidates[idx])
        tpr_tpr = float(tprs[idx])
        tpr_fpr = float(fprs[idx])

    return ThresholdSet(
        youden_j=youden_tau, youden_tpr=youden_tpr, youden_fpr=youden_fpr,
        fpr_capped=fpr_tau, fpr_capped_tpr=fpr_tpr, fpr_capped_fpr=fpr_fpr,
        tpr_floored=tpr_tau, tpr_floored_tpr=tpr_tpr, tpr_floored_fpr=tpr_fpr,
    )


# ── Public surface ────────────────────────────────────────────────────────────

def run_calibration(
    seed: int = _DEFAULT_SEED,
    output_path: Optional[Path] = None,
) -> CalibrationReport:
    """Run the full calibration grid and return a CalibrationReport.

    If output_path is given, also write the report as JSON to that path.
    Defaults: seed=20260425, no file output.

    Pulls T, sample counts, and trial-count grid from module constants.
    Recalibrating with different parameters means editing the constants
    or extending this function with kwargs — keep the constants
    authoritative for the production calibration run.
    """
    rng_master = np.random.default_rng(seed)
    cells: list[CalibrationCell] = []

    for dist_name, generator in _DISTRIBUTIONS.items():
        for n_trials in _TRIAL_COUNTS:
            # Independent rng per cell so cell results are reproducible
            # in isolation (useful when debugging one cell).
            cell_seed = rng_master.integers(0, 2**32 - 1, dtype=np.uint32)
            rng_noise = np.random.default_rng(cell_seed)
            rng_signal = np.random.default_rng(int(cell_seed) + 1)

            noise_dsrs, n_err_noise = _sample_dsrs(
                generator, target_sharpe=0.0,
                n_samples=_N_NOISE, n_trials=n_trials, rng=rng_noise,
            )
            signal_dsrs, n_err_signal = _sample_dsrs(
                generator, target_sharpe=_TARGET_SIGNAL_SHARPE,
                n_samples=_N_SIGNAL, n_trials=n_trials, rng=rng_signal,
            )

            thresholds = _derive_thresholds(noise_dsrs, signal_dsrs)

            cells.append(CalibrationCell(
                distribution=dist_name,
                n_trials=n_trials,
                noise_dsr_mean=float(noise_dsrs.mean()) if noise_dsrs.size else float("nan"),
                noise_dsr_std=float(noise_dsrs.std()) if noise_dsrs.size else float("nan"),
                signal_dsr_mean=float(signal_dsrs.mean()) if signal_dsrs.size else float("nan"),
                signal_dsr_std=float(signal_dsrs.std()) if signal_dsrs.size else float("nan"),
                n_dsr_errors_noise=n_err_noise,
                n_dsr_errors_signal=n_err_signal,
                thresholds=thresholds,
            ))

    # Production recommendation: per-(distribution, n_trials) FPR≤5%
    # threshold table.  No aggregation — the calibration diverges
    # across n_trials (sr_zero_expected grows roughly logarithmically
    # in N), so callers should look up the row matching the
    # strategy's actual trials.log count rather than a single number.
    prod: dict[str, dict[int, float]] = {}
    for dist_name in _DISTRIBUTIONS:
        prod[dist_name] = {}
        for n_trials in _TRIAL_COUNTS:
            cell = next(
                c for c in cells
                if c.distribution == dist_name and c.n_trials == n_trials
            )
            prod[dist_name][n_trials] = cell.thresholds.fpr_capped

    report = CalibrationReport(
        seed=seed,
        t_bars=_T_BARS,
        n_noise=_N_NOISE,
        n_signal=_N_SIGNAL,
        target_signal_sharpe=_TARGET_SIGNAL_SHARPE,
        cells=cells,
        production_recommendation=prod,
    )

    if output_path is not None:
        Path(output_path).write_text(report.to_json())

    return report


# ── CLI entrypoint ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(
        description="Run DSR threshold calibration."
    )
    parser.add_argument("--seed", type=int, default=_DEFAULT_SEED)
    parser.add_argument(
        "--output", type=Path,
        default=Path("backtest/calibration_report.json"),
    )
    args = parser.parse_args()
    report = run_calibration(seed=args.seed, output_path=args.output)
    print(report.to_json())
