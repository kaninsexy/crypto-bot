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
      trade_count_pass:         total_trades >= min_trade_count.
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
      confidence:       Forwarded to min_track_record_length.

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

    # Precondition gate: either failure → under_tested, no quality
    # computation.
    if not (trade_count_pass and mintrl_pass):
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
    )


__all__ = ["VerdictResult", "Verdict", "compute_verdict"]
