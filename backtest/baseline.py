"""
backtest/baseline.py — Buy-and-hold baseline for strategy validation.

NOT part of the sacred validation harness — this module is a comparison
floor, not a statistical gate. It computes a passive-hold Sharpe on a
single OHLCV window so a strategy's reported Sharpe can be compared
against doing nothing but holding the pair.

Spec: docs/validation_framework.md § "Baseline comparison".

Methodology
───────────
buy_and_hold_sharpe(df, ...) extracts close.pct_change().dropna() and
feeds it to the same annualised-Sharpe formula
backtest/cpcv.py:_sharpe_from_returns uses (which mirrors
backtest/engine.py:_compute_metrics).  This guarantees the strategy's
reported Sharpe and the baseline's Sharpe are directly comparable on
the same scale and time basis.

Multi-symbol routing
────────────────────
This module accepts a single DataFrame.  For multi-symbol strategies
(e.g. DualMomentum), the caller picks which symbol's frame to use —
typically the primary symbol the strategy reports its trades against,
or the most-held symbol over the evaluation window.  That decision is
a Phase 3c rescue judgement, not a baseline-module concern.

Comparison criterion
────────────────────
beats_baseline returns True iff strategy_sr > baseline_sr (strict).
Matching the baseline is not clearing the floor — the strategy carries
fees and operational risk the passive alternative does not, so a tie
is a loss in net-of-cost terms.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd


# ── Constants ─────────────────────────────────────────────────────────────────

_BARS_PER_YEAR: float = 365.25 * 24


# ── Result container ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class BaselineResult:
    """Outcome of one buy-and-hold baseline computation.

    Attributes:
      sharpe:        Annualised buy-and-hold Sharpe over the window.
                     Same formula as backtest/cpcv.py:_sharpe_from_returns.
      total_return:  Compound return over the window
                     (np.prod(1+r) - 1, as a decimal not %).
      n_bars:        Bar count used for the computation
                     (= len(close.pct_change().dropna()) = len(df) - 1).
      candle_hours:  Inferred candle duration in hours (from index).
    """
    sharpe: float
    total_return: float
    n_bars: int
    candle_hours: float


# ── Exceptions ────────────────────────────────────────────────────────────────

class BaselineError(RuntimeError):
    """A baseline-computation problem: empty frame, missing close
    column, non-finite prices, or insufficient bars."""


# ── Helpers (intentionally mirror backtest/cpcv.py) ───────────────────────────

def _infer_candle_hours(df: pd.DataFrame) -> float:
    """Mirror of `backtest.cpcv._infer_candle_hours`.  Estimates candle
    duration in hours from the first two index timestamps; floors at
    1 minute (1/60 h)."""
    if len(df) < 2:
        return 1.0
    delta = df.index[1] - df.index[0]
    hours = delta.total_seconds() / 3600
    return max(hours, 1 / 60)


def _sharpe_from_returns(
    returns: np.ndarray,
    candle_duration_h: float,
) -> float:
    """Mirror of `backtest.cpcv._sharpe_from_returns` so the baseline
    Sharpe is computed on the same scale as the strategy's reported
    Sharpe.  Kept duplicated rather than imported because cpcv.py is
    sacred and its private API is not a stable surface for outside
    callers — duplicating ~15 lines is cheaper than coupling this
    module to a sacred-harness internal."""
    arr = np.asarray(returns, dtype=float)
    n = arr.size
    if n == 0:
        return 0.0
    years = (n * candle_duration_h) / _BARS_PER_YEAR
    if years <= 0:
        return 0.0
    total = float(np.prod(1.0 + arr) - 1.0)
    if total <= -1.0:
        return 0.0
    try:
        ann_ret = ((1.0 + total) ** (1.0 / years) - 1.0) * 100.0
    except OverflowError:
        # On absurdly short windows (e.g. years ≪ 1) the annualised
        # extrapolation overflows float64.  cpcv.py never hits this
        # because run_cpcv enforces a minimum block size; baseline
        # accepts arbitrary frame sizes, so we degrade to "Sharpe
        # undefined" rather than crash.  Tests for tiny frames
        # exercise this path.
        return 0.0
    bars_per_year = _BARS_PER_YEAR / candle_duration_h
    vol = float(arr.std()) * math.sqrt(bars_per_year) * 100.0
    if vol <= 0 or not math.isfinite(ann_ret):
        return 0.0
    return ann_ret / vol


# ── Public surface ────────────────────────────────────────────────────────────

def buy_and_hold_sharpe(
    df: pd.DataFrame,
    *,
    close_col: str = "close",
) -> BaselineResult:
    """Compute the buy-and-hold Sharpe over an OHLCV window.

    Args:
      df:        OHLCV DataFrame indexed by timestamp.  Must contain
                 `close_col` and have at least 2 bars (for one
                 pct_change return).  For multi-symbol strategies the
                 caller passes the chosen symbol's frame; this module
                 does not route across symbols.
      close_col: Name of the close-price column.  Default "close".

    Returns:
      BaselineResult with sharpe, total_return, n_bars, candle_hours.

    Raises:
      BaselineError: df is empty, missing the close column, has fewer
                     than 2 bars, or contains non-finite prices.
    """
    if df is None or len(df) == 0:
        raise BaselineError("DataFrame is empty")
    if close_col not in df.columns:
        raise BaselineError(
            f"close column '{close_col}' not in DataFrame; "
            f"have {list(df.columns)}"
        )
    if len(df) < 2:
        raise BaselineError(
            f"need at least 2 bars to compute one return; got {len(df)}"
        )

    close = df[close_col].astype(float)
    if not np.all(np.isfinite(close.values)):
        raise BaselineError(
            "non-finite values in close prices (NaN or inf)"
        )

    # pct_change drops the leading NaN so n_bars = len(df) - 1.
    returns = close.pct_change().dropna().values.astype(float)
    candle_h = _infer_candle_hours(df)
    sharpe = _sharpe_from_returns(returns, candle_h)
    total_return = float(np.prod(1.0 + returns) - 1.0)

    return BaselineResult(
        sharpe=sharpe,
        total_return=total_return,
        n_bars=int(returns.size),
        candle_hours=float(candle_h),
    )


def beats_baseline(strategy_sharpe: float, baseline_sharpe: float) -> bool:
    """Return True iff strategy_sharpe > baseline_sharpe (strict).

    Spec is in `docs/validation_framework.md` § Baseline comparison:
    "merely matches buy-and-hold is not adding value."  Tie or worse
    fails the floor.

    Non-finite inputs return False — a strategy that produces NaN or
    +inf Sharpe never beats anything, since the comparison is
    ill-defined and we don't want to silently pass either way.
    """
    if not (math.isfinite(strategy_sharpe) and math.isfinite(baseline_sharpe)):
        return False
    return strategy_sharpe > baseline_sharpe
