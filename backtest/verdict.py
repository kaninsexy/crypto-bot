"""
backtest/verdict.py — Composition layer for the strategy keep/retire gate.

Pure orchestration over the sacred validation modules — does not modify
their semantics, only composes their outputs into the binary verdict
specified for Phase 3c.  Not a sacred module itself, but it imports
sacred surfaces (`backtest.dsr.deflated_sharpe`,
`backtest.dsr.min_track_record_length`, `backtest.baseline`) without
modifying them.

Verdict spec
────────────
Two preconditions gate the quality assessment:

  trade_count_pass  := total_trades >= min_trade_count
  mintrl_pass       := not min_track_record_length(...).under_tested

If either fails, verdict = "under_tested" — the result is recorded but
the strategy is neither kept nor retired; Phase 3c keeps it on paper
for further data collection per docs/validation_framework.md
§ Minimum Track Record Length.

When both preconditions pass, two quality gates decide keep vs retire:

  mt_mean_pass    := sr_candidate > deflated_sharpe(...).sr_zero_expected
  baseline_pass   := beats_baseline(sr_candidate, baseline.sharpe)
  verdict = "keep" iff (mt_mean_pass AND baseline_pass) else "retire"

Why no `monitor` state
──────────────────────
At production T (~20k bars) the DSR's Φ(z) saturates to a step function
at sr_zero_expected(N), so the natural strict bar (Gumbel-p95 ≈ 3.0 SR
for N=20) sits beyond every realistic strategy in this portfolio.
Phase 4 paper monitoring is the real "monitor" state; bolting one onto
the gate would just dilute the signal.  See docs/research_log.md
discussion adjacent to chunk 8 calibration.

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

import math
from dataclasses import dataclass
from typing import Literal, Optional

import numpy as np
import pandas as pd

from backtest.baseline import beats_baseline, buy_and_hold_sharpe
from backtest.dsr import (
    DSRError,
    deflated_sharpe,
    min_track_record_length,
)


Verdict = Literal["keep", "retire", "under_tested"]


@dataclass(frozen=True)
class VerdictResult:
    """Outcome of one keep/retire gate evaluation.

    Attributes:
      verdict:                  Literal["keep", "retire", "under_tested"].
      trade_count_pass:         total_trades >= min_trade_count.  Always
                                computed (preserved for forensics) even
                                when signal_event_count is the active
                                precondition.
      signal_event_count_pass:  signal_event_count >= min_signal_event_count.
                                None when no signal_event_count was
                                supplied (legacy / single-leg path —
                                trade_count_pass governs the
                                precondition).
      mintrl_pass:              MinTRL.under_tested == False (or
                                False if min_track_record_length
                                raised, e.g. on sr ≈ 0).
      mt_mean_pass:             sr_candidate > sr_zero_expected; None
                                when preconditions cut off computation.
      baseline_pass:            sr_candidate > baseline_sharpe (strict);
                                None when preconditions cut off
                                computation.
      sr_observed:              The Sharpe being judged (the engine's
                                headline number).
      sr_zero_expected_at_eval: BLP eq.7 Gumbel haircut at evaluation
                                time, NaN if not computed.
      mintrl_required_at_eval:  BLP eq.13 minimum sample size; NaN if
                                MinTRL raised.
      baseline_sharpe_at_eval:  Buy-and-hold Sharpe over the same
                                window.
      total_trades:             Trade count from the BacktestResult.
      signal_event_count:       Per-strategy signal-event count when
                                supplied; None when not (legacy path).
      t_observed:               len(returns) — number of per-bar
                                returns fed to DSR/MinTRL.
      sr_margin_vs_mt_mean:     sr_observed − sr_zero_expected_at_eval;
                                None when not computed.
      sr_margin_vs_baseline:    sr_observed − baseline_sharpe_at_eval;
                                None when not computed.
      dsr:                      Full Φ(z) DSR for forensics.  NaN if
                                preconditions cut off computation.
      n_trials:                 Multiple-testing trial count fed in.
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
    # Track 2 (2026-05-02) — additive optional fields.  Legacy callers
    # that don't pass `signal_event_count` see `signal_event_count`
    # default to None and `signal_event_count_pass` default to None;
    # the precondition gate falls back to `trade_count_pass`, so the
    # legacy verdict is preserved bit-for-bit.
    signal_event_count: Optional[int] = None
    signal_event_count_pass: Optional[bool] = None


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
) -> VerdictResult:
    """Compose dsr / baseline / mintrl into a single keep/retire verdict.

    Args:
      strategy_id:      Manifest key for the strategy under test.
                        Currently used only for record-keeping (DSR's
                        n_trials is supplied by the caller).
      sr_candidate:     Engine-reported headline Sharpe — the number
                        being claimed.  NOT a CV mean.
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
      n_trials:         Per-strategy multiple-testing count from
                        trials.log.  Forwarded to deflated_sharpe.
      min_trade_count:  Heuristic floor pairing with MinTRL — caught
                        low-trade strategies (DCA, MeanReversion) that
                        bar-level MinTRL does not flag.  Default 30.
                        Used as the precondition floor when
                        `signal_event_count` is None (legacy path);
                        otherwise preserved for forensics on
                        VerdictResult.trade_count_pass.
      confidence:       Forwarded to min_track_record_length.
      signal_event_count:    Optional per-strategy signal-event count
                        (Track 2 / 2026-05-02 additive).  When supplied
                        (Phase 4.B funding-rate harvest and any future
                        two-leg / continuous-hold variation), this is
                        the precondition floor that gates `under_tested`
                        instead of the trade count.  When None
                        (legacy / single-leg path), `total_trades` is
                        the precondition floor and the verdict is
                        identical to the pre-Track-2 behaviour
                        bit-for-bit.
      min_signal_event_count: Floor for `signal_event_count`.  Defaults
                        to 30 (matches `min_trade_count` default) so
                        two-leg strategies see the same baseline
                        statistical floor as single-leg strategies.

    Returns:
      VerdictResult.  All numeric fields are populated with the actual
      values used in the decision, or NaN where the precondition cut
      computation off.

    Raises:
      Lets `deflated_sharpe`'s n_trials < 1 / non-finite-input errors
      propagate (those are caller bugs, not strategy outcomes).
      `min_track_record_length`'s sr ≈ 0 is intercepted and folded into
      the under_tested verdict — sr ≈ 0 is itself a degenerate
      strategy claim, not an input bug.
    """
    arr = np.asarray(returns, dtype=float)
    t_observed = int(arr.size)

    # Baseline Sharpe over the same window.  If the frame is malformed
    # (`BaselineError`), let it propagate — that's a caller bug.
    baseline_result = buy_and_hold_sharpe(baseline_df)
    baseline_sharpe = float(baseline_result.sharpe)

    # MinTRL precondition.  sr ≈ 0 is a documented under-tested signal
    # rather than an error here — record min_trl as NaN and roll into
    # the under_tested branch.
    try:
        mintrl_result = min_track_record_length(
            sr_candidate=sr_candidate,
            returns=arr,
            confidence=confidence,
        )
        mintrl_required = float(mintrl_result.min_trl)
        mintrl_pass = not mintrl_result.under_tested
    except DSRError:
        mintrl_required = float("nan")
        mintrl_pass = False

    trade_count_pass = total_trades >= min_trade_count

    # Track 2 precondition: when signal_event_count is provided, it
    # SUBSTITUTES for the trade count in the precondition gate.
    # `trade_count_pass` is preserved on the result for forensics
    # either way.
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
        )

    # Quality gates.
    dsr_result = deflated_sharpe(
        sr_candidate=sr_candidate,
        returns=arr,
        n_trials=n_trials,
    )
    sr_zero_expected = float(dsr_result.sr_zero_expected)
    mt_mean_pass = bool(sr_candidate > sr_zero_expected)
    baseline_pass = beats_baseline(sr_candidate, baseline_sharpe)

    verdict: Verdict = "keep" if (mt_mean_pass and baseline_pass) else "retire"

    return VerdictResult(
        verdict=verdict,
        trade_count_pass=trade_count_pass,
        mintrl_pass=mintrl_pass,
        mt_mean_pass=mt_mean_pass,
        baseline_pass=baseline_pass,
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
    )


__all__ = ["VerdictResult", "Verdict", "compute_verdict"]
