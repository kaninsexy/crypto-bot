"""
backtest/verdict.py — Composition layer for the strategy keep/retire gate.

Pure orchestration over the sacred validation modules — does not modify
their semantics, only composes their outputs into the binary verdict.
Not a sacred module itself, but it imports sacred surfaces
(`backtest.dsr.deflated_sharpe`, `backtest.dsr.min_track_record_length`,
`backtest.baseline`) without modifying them.

Verdict spec — GATE SPEC v2 (2026-06-11)
────────────────────────────────────────
Implements the corrected gates from
docs/gate_recalibration_audit_2026-06.md via the 2026-06-11 work
order.  Spec: docs/validation_framework.md § Gate spec v2.

Two preconditions gate the quality assessment (unchanged from v1,
except MinTRL is now units-correct):

  trade_count_pass  := total_trades >= min_trade_count
                       (signal_event_count substitutes when supplied)
  mintrl_pass       := not min_track_record_length(...).under_tested

If either fails, verdict = "under_tested" — the result is recorded but
the strategy is neither kept nor retired.  NOTE: with the v2 units fix
MinTRL is ~2.7 years of bars at |SR_ann| ≈ 1, so under_tested is the
EXPECTED outcome for moderate-Sharpe strategies on the ~29-month dev
windows (audit §4).  That is the honest answer, not a bug.

When both preconditions pass, two quality gates decide keep vs retire:

  mt_mean_pass   := corrected DSR >= dsr_threshold (default 0.95).
                    DSR uses per-bar units and the family-scaled
                    BLP eq.7 haircut (sqrt(V[{SR_n}]) × Gumbel(N)).
                    This subsumes the v1 raw `sr > sr_zero` check
                    (DSR > 0.5 ⇔ sr > sr_zero); the margin fields
                    are still recorded for forensics.
  baseline_pass  := directional/long-only strategies (default):
                      (a) OLS alpha of per-bar strategy returns on
                          same-instrument B&H returns > 0 at 95 %
                          one-sided with Newey-West (HAC) SEs, AND
                      (b) annualised information ratio vs B&H >= 0.5.
                    market/delta-neutral strategies (taxonomy flag
                    `neutral: true` in backtest/strategy_families.json):
                      PSR(SR > 0) >= 0.95 against benchmark 0
                      (a B&H comparison is meaningless for a
                      delta-neutral book).
  verdict = "keep" iff (mt_mean_pass AND baseline_pass) else "retire"

The v1 raw-Sharpe baseline (`beats_baseline`) retired strategies for
failing to beat a 1.94-Sharpe bull-window B&H with no significance
test; audit §6 found three retirements that pass the corrected gates
at the family layer.  The raw comparison is preserved on
`baseline_sharpe_at_eval` / `sr_margin_vs_baseline` as forensics.

Why no `monitor` state
──────────────────────
Phase 4 paper monitoring is the real "monitor" state; bolting one onto
the gate would just dilute the signal.  (The v1 docstring's claim that
DSR "saturates to a step function" was an artifact of the units bug —
corrected DSR discriminates properly — but the design argument stands.)

Why MinTRL gate is paired with a trade-count floor
──────────────────────────────────────────────────
MinTRL operates on per-bar return statistics; bar-level under-testing
flags strategies that lack enough bars relative to their realised SR.
But low-trade strategies (DCA, MeanReversion) can have years of bar
data with very few trades — the bar-level Sharpe is dominated by zero
returns between trades, and MinTRL does not catch this.  The
`min_trade_count` floor (default 30) is a heuristic second
precondition that closes that gap.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from typing import Literal, Optional

import numpy as np
import pandas as pd

from backtest.baseline import (
    AlphaResult,
    BaselineError,
    buy_and_hold_sharpe,
    information_ratio_annualised,
    ols_alpha_newey_west,
)
from backtest.dsr import (
    DSRError,
    bars_per_year_from_candle_hours,
    deflated_sharpe,
    min_track_record_length,
)


Verdict = Literal["keep", "retire", "under_tested"]

# v2 gate constants (docs/validation_framework.md § Gate spec v2).
_DSR_THRESHOLD_DEFAULT: float = 0.95
_IR_THRESHOLD_DEFAULT: float = 0.5
_ALPHA_CONFIDENCE_DEFAULT: float = 0.95
_PSR_THRESHOLD_DEFAULT: float = 0.95


def _infer_candle_hours(df: pd.DataFrame) -> float:
    """Mirror of `backtest.cpcv_common._infer_candle_hours` (duplicated
    rather than imported — cpcv_common is sacred-adjacent and its
    private API is not a stable surface for outside callers)."""
    if len(df) < 2:
        return 1.0
    delta = df.index[1] - df.index[0]
    hours = delta.total_seconds() / 3600
    return max(hours, 1 / 60)


@dataclass(frozen=True)
class VerdictResult:
    """Outcome of one keep/retire gate evaluation (gate spec v2).

    v1 fields keep their names so trials.log final_gate rows stay
    schema-stable; semantics that changed are flagged:

      verdict:                  Literal["keep", "retire", "under_tested"].
      trade_count_pass:         total_trades >= min_trade_count.
      signal_event_count_pass:  signal_event_count >= min_signal_event_count,
                                None when not supplied.
      mintrl_pass:              MinTRL.under_tested == False (units-
                                correct in v2; far stricter than v1).
      mt_mean_pass:             v2 SEMANTICS: corrected DSR >=
                                dsr_threshold.  None when preconditions
                                cut off computation.
      baseline_pass:            v2 SEMANTICS: alpha+IR gate
                                (directional) or PSR gate (neutral).
                                None when preconditions cut off
                                computation.
      sr_observed:              The annualised Sharpe being judged.
      sr_zero_expected_at_eval: Family-scaled BLP eq.7 haircut
                                (ANNUALISED units), NaN if not computed.
      mintrl_required_at_eval:  BLP eq.13 minimum sample size (bars,
                                units-correct); NaN if MinTRL raised.
      baseline_sharpe_at_eval:  Buy-and-hold annualised Sharpe over the
                                same window (forensics — no longer the
                                gate input for directional strategies).
      total_trades:             Trade count from the BacktestResult.
      signal_event_count:       Per-strategy signal-event count when
                                supplied; None when not (legacy path).
      t_observed:               len(returns) — number of per-bar
                                returns fed to DSR/MinTRL.
      sr_margin_vs_mt_mean:     sr_observed − sr_zero_expected_at_eval
                                (forensics; not the gate).
      sr_margin_vs_baseline:    sr_observed − baseline_sharpe_at_eval
                                (forensics; not the gate).
      dsr:                      Corrected DSR (the mt gate input).
                                NaN if preconditions cut off
                                computation.
      n_trials:                 Multiple-testing trial count fed in
                                (per-family + 1 under v2).

    v2 additions (all default-None/NaN so legacy constructions parse):

      baseline_mode:            "directional" | "neutral" | None.
      alpha_annualised:         NW-OLS alpha, annualised decimal/yr.
      alpha_p_value:            One-sided P(alpha <= 0).
      alpha_pass:               alpha > 0 at the configured confidence.
      ir_annualised:            Annualised information ratio vs B&H.
      ir_pass:                  ir_annualised >= ir_threshold.
      psr:                      PSR(SR>0) for neutral strategies.
      psr_pass:                 psr >= psr_threshold.
      sr_var_trials_at_eval:    V[{SR_n}] used in the eq.7 scaling.
      family_at_eval:           Family name used for the scaling.
      bars_per_year_at_eval:    Frequency conversion used.
      benchmark_aligned_bars:   Aligned obs count for alpha/IR; when
                                the strategy and benchmark series had
                                different lengths the gate tail-aligned
                                them (recorded for forensics).
    """
    verdict: Verdict
    trade_count_pass: bool
    mintrl_pass: bool
    mt_mean_pass: Optional[bool]
    baseline_pass: Optional[bool]
    sr_observed: float
    sr_zero_expected_at_eval: float
    mintrl_required_at_eval: float
    baseline_sharpe_at_eval: float
    total_trades: int
    t_observed: int
    sr_margin_vs_mt_mean: Optional[float]
    sr_margin_vs_baseline: Optional[float]
    dsr: float
    n_trials: int
    # Track 2 (2026-05-02) — additive optional fields.
    signal_event_count: Optional[int] = None
    signal_event_count_pass: Optional[bool] = None
    # Gate spec v2 (2026-06-11) — additive optional fields.
    baseline_mode: Optional[str] = None
    alpha_annualised: Optional[float] = None
    alpha_p_value: Optional[float] = None
    alpha_pass: Optional[bool] = None
    ir_annualised: Optional[float] = None
    ir_pass: Optional[bool] = None
    psr: Optional[float] = None
    psr_pass: Optional[bool] = None
    sr_var_trials_at_eval: float = float("nan")
    family_at_eval: Optional[str] = None
    bars_per_year_at_eval: float = float("nan")
    benchmark_aligned_bars: Optional[int] = None


def _directional_baseline_gate(
    returns: np.ndarray,
    benchmark_returns: np.ndarray,
    bars_per_year: float,
    alpha_confidence: float,
    ir_threshold: float,
) -> tuple[bool, dict]:
    """Run the v2 directional baseline gate: NW alpha + IR, both
    required.  Returns (passed, forensics-dict).  Length mismatches
    are tail-aligned (the engine drops warmup bars the benchmark
    keeps); the aligned count is recorded.
    """
    rs = np.asarray(returns, dtype=float)
    rb = np.asarray(benchmark_returns, dtype=float)
    n = min(rs.size, rb.size)
    rs_a, rb_a = rs[-n:], rb[-n:]

    alpha_res: AlphaResult = ols_alpha_newey_west(
        rs_a, rb_a, bars_per_year=bars_per_year,
    )
    ir = information_ratio_annualised(
        rs_a, rb_a, bars_per_year=bars_per_year,
    )
    alpha_pass = alpha_res.p_value_one_sided < (1.0 - alpha_confidence)
    ir_pass = ir >= ir_threshold
    return alpha_pass and ir_pass, {
        "alpha_annualised": float(alpha_res.alpha_annualised),
        "alpha_p_value": float(alpha_res.p_value_one_sided),
        "alpha_pass": bool(alpha_pass),
        "ir_annualised": float(ir),
        "ir_pass": bool(ir_pass),
        "benchmark_aligned_bars": int(n),
    }


def compute_verdict(
    *,
    strategy_id: str,
    sr_candidate: float,
    returns: np.ndarray,
    total_trades: int,
    baseline_df: pd.DataFrame,
    n_trials: int,
    min_trade_count: int = 30,
    confidence: float = 0.95,
    signal_event_count: Optional[int] = None,
    min_signal_event_count: int = 30,
    bars_per_year: Optional[float] = None,
    benchmark_returns: Optional[np.ndarray] = None,
    sr_var_trials: Optional[float] = None,
    dsr_threshold: float = _DSR_THRESHOLD_DEFAULT,
    ir_threshold: float = _IR_THRESHOLD_DEFAULT,
    alpha_confidence: float = _ALPHA_CONFIDENCE_DEFAULT,
    psr_threshold: float = _PSR_THRESHOLD_DEFAULT,
    neutral: Optional[bool] = None,
) -> VerdictResult:
    """Compose dsr / baseline / mintrl into a single keep/retire verdict
    under gate spec v2.

    Args:
      strategy_id:      Manifest key for the strategy under test.
                        Used for the family taxonomy lookups
                        (cross-trial variance scaling; neutral flag).
      sr_candidate:     Engine-reported headline ANNUALISED Sharpe —
                        the number being claimed.  NOT a CV mean.
      returns:          Per-bar return series for the evaluation
                        window.  For final_gate this is
                        equity_curve.pct_change().dropna() from the
                        holdout-window engine run.
      total_trades:     Closed-trade count from the same engine run.
      baseline_df:      OHLCV frame the strategy ran against (its
                        primary symbol over the same window).  For
                        multi-symbol strategies the caller picks which
                        symbol to compare to — that's a Phase 3c
                        judgement, not a verdict-module concern.
      n_trials:         Multiple-testing count.  Gate spec v2 callers
                        pass the per-FAMILY count + 1 (see
                        `backtest.families.family_sharpe_stats`).
      min_trade_count:  Heuristic floor pairing with MinTRL.  Default
                        30.  Used as the precondition floor when
                        `signal_event_count` is None (legacy path).
      confidence:       Forwarded to min_track_record_length.
      signal_event_count / min_signal_event_count:
                        Track 2 (2026-05-02) — when supplied, the
                        signal-event count substitutes for the trade
                        count in the precondition gate.
      bars_per_year:    Bar frequency of `returns`.  When None,
                        inferred EXACTLY from baseline_df's
                        DatetimeIndex spacing (same data, same bars).
      benchmark_returns: Per-bar benchmark return series aligned with
                        `returns` for the alpha/IR gate.  When None,
                        derived as baseline_df close.pct_change()
                        .dropna() and tail-aligned to `returns` (the
                        engine's warmup trim makes the strategy series
                        shorter; the alignment count is recorded on
                        the result).  Item-4 per-bar persistence makes
                        exact aligned pairs available going forward.
      sr_var_trials:    V[{SR_n}] override.  When None, looked up via
                        `backtest.families.family_sharpe_stats`
                        (1.0 fallback + warning for thin families).
      dsr_threshold:    mt gate floor on corrected DSR.  Default 0.95.
      ir_threshold / alpha_confidence / psr_threshold:
                        v2 gate knobs; defaults are the spec values.
      neutral:          Override for the delta-neutral branch.  When
                        None, read from the family taxonomy's
                        `neutral` flag (default False).

    Returns:
      VerdictResult.  All numeric fields are populated with the actual
      values used in the decision, or NaN where the precondition cut
      computation off.

    Raises:
      Lets `deflated_sharpe`'s n_trials < 1 / non-finite-input errors
      propagate (those are caller bugs, not strategy outcomes).
      `min_track_record_length`'s sr ≈ 0 is intercepted and folded into
      the under_tested verdict.  A BaselineError from the alpha/IR
      machinery is likewise folded into baseline_pass = False with a
      warning rather than crashing the gate — an unevaluable baseline
      claim is a failed claim, not a harness crash.
    """
    # Lazy import: families.py reads trials.log via backtest.trials;
    # importing at call time keeps module import order trivial.
    from backtest import families as _families

    arr = np.asarray(returns, dtype=float)
    t_observed = int(arr.size)

    if bars_per_year is None:
        bars_per_year = bars_per_year_from_candle_hours(
            _infer_candle_hours(baseline_df)
        )
    bars_per_year = float(bars_per_year)

    # Baseline Sharpe over the same window (forensics field).  If the
    # frame is malformed (`BaselineError`), let it propagate — that's
    # a caller bug.
    baseline_result = buy_and_hold_sharpe(baseline_df)
    baseline_sharpe = float(baseline_result.sharpe)

    # Family stats: cross-trial variance for the eq.7 scaling + the
    # neutral flag.
    family = _families.family_of(strategy_id)
    if sr_var_trials is None:
        fam_stats = _families.family_sharpe_stats(strategy_id)
        sr_var_trials = fam_stats.sr_var
    sr_var_trials = float(sr_var_trials)
    if neutral is None:
        neutral = _families.is_neutral(strategy_id)

    # MinTRL precondition (units-correct).  sr ≈ 0 is a documented
    # under-tested signal rather than an error here.
    try:
        mintrl_result = min_track_record_length(
            sr_candidate=sr_candidate,
            returns=arr,
            confidence=confidence,
            bars_per_year=bars_per_year,
        )
        mintrl_required = float(mintrl_result.min_trl)
        mintrl_pass = not mintrl_result.under_tested
    except DSRError:
        mintrl_required = float("nan")
        mintrl_pass = False

    trade_count_pass = total_trades >= min_trade_count

    signal_event_count_pass: Optional[bool]
    if signal_event_count is None:
        signal_event_count_pass = None
        precondition_count_pass = trade_count_pass
    else:
        signal_event_count_pass = bool(
            signal_event_count >= min_signal_event_count
        )
        precondition_count_pass = signal_event_count_pass

    # Precondition gate: either failure → under_tested, no quality
    # computation.
    if not (precondition_count_pass and mintrl_pass):
        return VerdictResult(
            verdict="under_tested",
            trade_count_pass=trade_count_pass,
            mintrl_pass=mintrl_pass,
            mt_mean_pass=None,
            baseline_pass=None,
            sr_observed=float(sr_candidate),
            sr_zero_expected_at_eval=float("nan"),
            mintrl_required_at_eval=mintrl_required,
            baseline_sharpe_at_eval=baseline_sharpe,
            total_trades=int(total_trades),
            t_observed=t_observed,
            sr_margin_vs_mt_mean=None,
            sr_margin_vs_baseline=None,
            dsr=float("nan"),
            n_trials=int(n_trials),
            signal_event_count=(
                int(signal_event_count) if signal_event_count is not None else None
            ),
            signal_event_count_pass=signal_event_count_pass,
            baseline_mode="neutral" if neutral else "directional",
            sr_var_trials_at_eval=sr_var_trials,
            family_at_eval=family,
            bars_per_year_at_eval=bars_per_year,
        )

    # Quality gate 1: corrected DSR (multiple-testing).
    dsr_result = deflated_sharpe(
        sr_candidate=sr_candidate,
        returns=arr,
        n_trials=n_trials,
        bars_per_year=bars_per_year,
        sr_var_trials=sr_var_trials,
    )
    sr_zero_expected = float(dsr_result.sr_zero_expected)
    mt_mean_pass = bool(dsr_result.dsr >= dsr_threshold)

    # Quality gate 2: baseline — branch on neutral vs directional.
    baseline_mode = "neutral" if neutral else "directional"
    alpha_fields: dict = {}
    psr_value: Optional[float] = None
    psr_pass: Optional[bool] = None

    if neutral:
        # PSR(SR > 0): deflated_sharpe at n_trials=1 IS the PSR — the
        # eq.9 SE with a zero null and no multiplicity haircut.
        psr_result = deflated_sharpe(
            sr_candidate=sr_candidate,
            returns=arr,
            n_trials=1,
            bars_per_year=bars_per_year,
            sr_var_trials=1.0,
        )
        psr_value = float(psr_result.dsr)
        psr_pass = bool(psr_value >= psr_threshold)
        baseline_pass: bool = psr_pass
    else:
        if benchmark_returns is None:
            benchmark_returns = (
                baseline_df["close"].astype(float)
                .pct_change().dropna().values.astype(float)
            )
        try:
            baseline_pass, alpha_fields = _directional_baseline_gate(
                arr,
                benchmark_returns,
                bars_per_year=bars_per_year,
                alpha_confidence=alpha_confidence,
                ir_threshold=ir_threshold,
            )
        except BaselineError as exc:
            warnings.warn(
                f"[verdict] alpha/IR gate unevaluable for "
                f"{strategy_id!r} ({exc}); baseline_pass=False",
                stacklevel=2,
            )
            baseline_pass = False
            alpha_fields = {}

    verdict: Verdict = "keep" if (mt_mean_pass and baseline_pass) else "retire"

    return VerdictResult(
        verdict=verdict,
        trade_count_pass=trade_count_pass,
        mintrl_pass=mintrl_pass,
        mt_mean_pass=mt_mean_pass,
        baseline_pass=bool(baseline_pass),
        sr_observed=float(sr_candidate),
        sr_zero_expected_at_eval=sr_zero_expected,
        mintrl_required_at_eval=mintrl_required,
        baseline_sharpe_at_eval=baseline_sharpe,
        total_trades=int(total_trades),
        t_observed=t_observed,
        sr_margin_vs_mt_mean=float(sr_candidate - sr_zero_expected),
        sr_margin_vs_baseline=float(sr_candidate - baseline_sharpe),
        dsr=float(dsr_result.dsr),
        n_trials=int(n_trials),
        signal_event_count=(
            int(signal_event_count) if signal_event_count is not None else None
        ),
        signal_event_count_pass=signal_event_count_pass,
        baseline_mode=baseline_mode,
        alpha_annualised=alpha_fields.get("alpha_annualised"),
        alpha_p_value=alpha_fields.get("alpha_p_value"),
        alpha_pass=alpha_fields.get("alpha_pass"),
        ir_annualised=alpha_fields.get("ir_annualised"),
        ir_pass=alpha_fields.get("ir_pass"),
        psr=psr_value,
        psr_pass=psr_pass,
        sr_var_trials_at_eval=sr_var_trials,
        family_at_eval=family,
        bars_per_year_at_eval=bars_per_year,
        benchmark_aligned_bars=alpha_fields.get("benchmark_aligned_bars"),
    )


__all__ = ["VerdictResult", "Verdict", "compute_verdict"]
