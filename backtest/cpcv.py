"""
backtest/cpcv.py — Combinatorial Purged Cross-Validation (sacred harness).

References:
  Bailey, D.H. & López de Prado, M. (2014). The Deflated Sharpe Ratio.
  López de Prado, M. (2018). Advances in Financial Machine Learning,
    chapter 7 (Cross-Validation in Finance).

This module is part of the validation harness — modifying it requires
human approval per CLAUDE.md.

Status
──────
SKELETON.  CPCVConfig, CPCVResult, the public surface of `run_cpcv`,
and the standalone `summarize` helper are pinned here.  The iterative
path-construction implementation lands in Phase 3b chunk 6 (next
session); calling `run_cpcv` raises NotImplementedError until then.

Methodology
───────────
- Partition the dev window (returned by `backtest.holdout.load_dev`)
  into N non-overlapping contiguous blocks.
- For every choice of k held-out blocks (n_blocks-choose-k_held_out
  combinations), train a fresh strategy instance on the n_blocks - k
  training blocks and evaluate on the k held-out blocks only.
- Purge the strategy's lookback window on either side of each
  held-out block to prevent feature-engineering leakage; embargo a
  short window after each held-out block to absorb serial-correlation
  leakage.
- Each combination yields one out-of-sample reconstructed path; that
  path's Sharpe is appended to `per_path_sharpes`.  The collection
  forms the CPCV Sharpe distribution that DSR consumes.

The block construction operates on the dev window only — never on the
holdout window — because the holdout is the deploy gate and a CPCV
peek would invalidate it (`docs/validation_framework.md`).

This module must NOT write to trials.log itself.  The caller packages
the trial event from a `CPCVResult` and calls
`backtest.trials.record_trial`.  Keeping the runner separate from the
writer is what makes the multiple-testing count authoritative.
"""

from dataclasses import dataclass

import numpy as np


# ── Defaults (placeholders pending Phase 3b empirical calibration) ────────────

_DEFAULT_N_BLOCKS: int = 10
_DEFAULT_K_HELD_OUT: int = 2
_DEFAULT_PURGE_PERIODS: int = 0
_DEFAULT_EMBARGO_PERIODS: int = 0


# ── Configuration container ───────────────────────────────────────────────────

@dataclass(frozen=True)
class CPCVConfig:
    """Configuration for one CPCV run.

    Attributes:
        n_blocks:         Number of non-overlapping dev-window blocks.
                          Must be ≥ 4 — fewer blocks produce too few
                          path combinations for the Sharpe distribution
                          to be informative.
        k_held_out:       Blocks held out per combination.  Must be in
                          [1, n_blocks).
        purge_periods:    Bars dropped from training data on either
                          side of each held-out block.  Set to the
                          strategy's longest feature-engineering
                          lookback to prevent leakage.
        embargo_periods:  Bars dropped immediately after each held-out
                          block to absorb serial-correlation leakage.
    """
    n_blocks: int = _DEFAULT_N_BLOCKS
    k_held_out: int = _DEFAULT_K_HELD_OUT
    purge_periods: int = _DEFAULT_PURGE_PERIODS
    embargo_periods: int = _DEFAULT_EMBARGO_PERIODS

    def validate(self) -> None:
        """Raise ValueError if any field is out of its admissible range."""
        if self.n_blocks < 4:
            raise ValueError(
                f"n_blocks must be ≥ 4; got {self.n_blocks}"
            )
        if not (1 <= self.k_held_out < self.n_blocks):
            raise ValueError(
                f"k_held_out must satisfy 1 ≤ k_held_out < n_blocks; "
                f"got k_held_out={self.k_held_out}, n_blocks={self.n_blocks}"
            )
        if self.purge_periods < 0:
            raise ValueError(
                f"purge_periods must be ≥ 0; got {self.purge_periods}"
            )
        if self.embargo_periods < 0:
            raise ValueError(
                f"embargo_periods must be ≥ 0; got {self.embargo_periods}"
            )


# ── Result container ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CPCVResult:
    """Outcome of one CPCV run.

    Attributes:
        n_paths:              Number of reconstructed paths
                              (n_blocks-choose-k_held_out).
        sharpe_distribution:  Summary dict matching the
                              `cpcv.sharpe_distribution` shape used in
                              trials.log; produced by `summarize`.
        per_path_sharpes:     Raw out-of-sample per-path Sharpe values.
        trades_per_path:      Trade count for each reconstructed path,
                              same order as `per_path_sharpes`.
    """
    n_paths: int
    sharpe_distribution: dict
    per_path_sharpes: list[float]
    trades_per_path: list[int]


# ── Public functions ──────────────────────────────────────────────────────────

def run_cpcv(strategy_id: str, params: dict, config: CPCVConfig) -> CPCVResult:
    """Run combinatorial purged cross-validation on a strategy's dev window.

    Held-out-only path Sharpe interpretation
    ─────────────────────────────────────────
    For each combination, a fresh strategy instance is configured with
    `params`, fit on the n_blocks - k_held_out training blocks, and
    evaluated on the k_held_out held-out blocks only.  The path's
    Sharpe is computed against those held-out returns alone
    (interpretation A from the design discussion).  In-sample Sharpes
    are never aggregated into the distribution — that would defeat the
    purpose of CPCV as a false-discovery guard, since a strategy that
    over-fits in-sample would receive credit it has not earned out of
    sample.

    Block construction
    ──────────────────
    Blocks are contiguous, non-overlapping slices of the DataFrame
    returned by `backtest.holdout.load_dev(strategy_id)`.  Block
    boundaries are evenly spaced; the last block absorbs any remainder
    rows so all dev rows belong to exactly one block.

    Purge / embargo
    ───────────────
    `config.purge_periods` bars on either side of each held-out block
    are dropped from the training set to prevent leakage from
    feature-engineering windows that straddle the boundary.
    `config.embargo_periods` bars immediately after each held-out
    block are dropped from the training set to absorb serial-
    correlation leakage between adjacent observations.

    Reassembly
    ──────────
    Each (n_blocks-choose-k_held_out) combination produces one path of
    out-of-sample bars by stitching the held-out blocks together in
    time order.  One path Sharpe and one trade count are computed per
    combination and collected into the returned CPCVResult.

    This function MUST NOT write to trials.log.  Callers are responsible
    for packaging the trial event (combining CPCVResult with
    strategy/run metadata) and calling `backtest.trials.record_trial`.
    Keeping the runner separate from the writer is what allows the
    multiple-testing count to remain authoritative.

    Args:
        strategy_id: Manifest key for the strategy under test.
        params:      Strategy parameters; must be JSON-serialisable so
                     trials.py can hash them canonically.
        config:      CPCVConfig instance.

    Returns:
        CPCVResult with per-path Sharpes and a summary distribution.

    Raises:
        NotImplementedError: until the implementation lands in Phase 3b
                             chunk 6.  See docs/validation_framework.md.
    """
    raise NotImplementedError(
        "CPCV implementation lands in Phase 3b chunk 6. "
        "See docs/validation_framework.md."
    )


def summarize(per_path_sharpes: list[float]) -> dict:
    """Summarise a list of per-path Sharpes into the trials.log schema.

    Returns the dict shape consumed by `backtest.trials.record_trial`
    under `cpcv.sharpe_distribution`:

        {"mean": float,
         "std": float,
         "quantiles": {"p05": float, "p25": float, "p50": float,
                        "p75": float, "p95": float}}

    Quantiles are computed via `numpy.percentile(..., method="linear")`.
    For a single-element input, std is 0.0 and all quantiles equal the
    sole value.  For an empty input, ValueError is raised — a CPCV run
    that produced no paths is a runner bug, not a valid summary input.
    """
    if len(per_path_sharpes) == 0:
        raise ValueError(
            "summarize() requires at least one Sharpe value."
        )
    arr = np.asarray(per_path_sharpes, dtype=float)
    if arr.size == 1:
        v = float(arr[0])
        return {
            "mean": v,
            "std": 0.0,
            "quantiles": {
                "p05": v, "p25": v, "p50": v, "p75": v, "p95": v,
            },
        }
    qs = np.percentile(arr, [5, 25, 50, 75, 95], method="linear")
    return {
        "mean": float(arr.mean()),
        "std": float(arr.std()),
        "quantiles": {
            "p05": float(qs[0]),
            "p25": float(qs[1]),
            "p50": float(qs[2]),
            "p75": float(qs[3]),
            "p95": float(qs[4]),
        },
    }
