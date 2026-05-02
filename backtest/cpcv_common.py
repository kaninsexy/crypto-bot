"""
backtest/cpcv_common.py — Shared CPCV primitives (sacred-harness adjacent).

Block construction, Sharpe computation, purge/embargo, summary, and
the dataclasses (CPCVConfig, CPCVResult, CPCVError) live here so the
spot-only block-Sharpe runner in `backtest.cpcv` and the perp+spot
two-leg runner in `backtest.cpcv_perp` can share identical statistical
discipline.  Engine-coupled helpers (per-block engine invocation,
per-block return extraction from BacktestResult) stay in their
respective runners — only the engine-agnostic primitives are here.

Methodology and "why block Sharpe rather than path-CPCV" are documented
in `docs/validation_framework.md` § "Block Sharpe distribution".  This
module is the implementation surface for that spec; modifying its
public/contract behavior requires the same human approval as
`backtest.cpcv` itself.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd


# ── Defaults (placeholders pending Phase 3b empirical calibration) ────────────

_DEFAULT_N_BLOCKS: int = 10
_DEFAULT_K_HELD_OUT: int = 2  # reserved; unused in block-Sharpe mode
_DEFAULT_PURGE_PERIODS: int = 0
_DEFAULT_EMBARGO_PERIODS: int = 0

# Engine-run knobs.  Match the BacktestEngine defaults so a block of
# `_MIN_BLOCK_CANDLES` rows always satisfies the engine's own
# `len(df) >= warm_up_candles + 10` precondition.
_ENGINE_INITIAL_BALANCE: float = 10_000.0
_ENGINE_WARM_UP_CANDLES: int = 50
_MIN_BLOCK_CANDLES: int = _ENGINE_WARM_UP_CANDLES + 10  # = 60

# A block must produce at least this many trades to contribute a
# valid Sharpe; below this the block's Sharpe is NaN.
_MIN_TRADES_PER_BLOCK: int = 5


# ── Exceptions ────────────────────────────────────────────────────────────────

class CPCVError(RuntimeError):
    """A CPCV runtime problem: a block too small to run, more than
    half the blocks below the trade threshold, or any other
    block-construction / Sharpe-computation failure that makes the
    output statistically unreliable."""


# ── Configuration container ───────────────────────────────────────────────────

@dataclass(frozen=True)
class CPCVConfig:
    """Configuration for one block-Sharpe run.

    Attributes:
        n_blocks:         Number of non-overlapping dev-window blocks.
                          Must be ≥ 4 — fewer blocks produce too few
                          Sharpe samples for the distribution to be
                          informative.
        k_held_out:       RESERVED, unused in block-Sharpe mode.
                          Preserved for forward compatibility if a
                          fit/predict-capable strategy class is added
                          in the future, at which point true
                          path-CPCV may be re-introduced.  Validation
                          still enforces 1 ≤ k_held_out < n_blocks so
                          configs round-trip cleanly between this
                          mode and any future path-CPCV mode.
        purge_periods:    Bars zeroed at the START of each block's
                          return series before Sharpe computation,
                          neutralising feature-engineering windows
                          that straddle the boundary.  Defaults to 0.
        embargo_periods:  Bars zeroed at the END of each block's
                          return series before Sharpe computation,
                          absorbing serial-correlation leakage.
                          Defaults to 0.
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
    """Outcome of one block-Sharpe run.

    Field names are kept stable for trials.log compatibility, but
    their interpretation in block-Sharpe mode is:

    Attributes:
        n_paths:              Equals `n_blocks`.  Each block
                              contributes one Sharpe sample.
        sharpe_distribution:  Summary dict matching the
                              `cpcv.sharpe_distribution` shape used
                              in trials.log; produced by `summarize`
                              over the valid (non-NaN) per-block
                              Sharpes.
        per_path_sharpes:     Per-block Sharpe values, in block order.
                              NaN where the block's trade count fell
                              below `_MIN_TRADES_PER_BLOCK`.  Use the
                              `per_block_sharpes` property as a more
                              accurate alias.
        trades_per_path:      Per-block trade counts, same length and
                              order as `per_path_sharpes`.
        per_block_returns:    Per-block per-bar return arrays, in
                              block order.  Always length `n_blocks`.
                              For NaN-Sharpe blocks (insufficient
                              trades) the entry is an empty
                              `np.ndarray` (`size == 0`), never None
                              and never missing.  For valid blocks
                              the entry is the same array passed to
                              `_sharpe_from_returns`: post
                              `pct_change().dropna()` and
                              post-purge/embargo.  Consumed by
                              `backtest.dsr.dsr_from_cpcv_result`,
                              which concatenates the non-empty
                              entries to drive T / skew / kurtosis
                              for DSR.
    """
    n_paths: int
    sharpe_distribution: dict
    per_path_sharpes: list[float]
    trades_per_path: list[int]
    per_block_returns: list[np.ndarray]

    @property
    def per_block_sharpes(self) -> list[float]:
        """Alias for `per_path_sharpes` with semantically accurate
        naming under block-Sharpe mode."""
        return self.per_path_sharpes


# ── Block construction ────────────────────────────────────────────────────────

def _split_blocks(df: pd.DataFrame, n_blocks: int) -> list[pd.DataFrame]:
    """Equal-row split of `df` into `n_blocks` contiguous DataFrames.

    The last block absorbs any remainder rows so every input row
    belongs to exactly one block.
    """
    n = len(df)
    block_size = n // n_blocks
    blocks: list[pd.DataFrame] = []
    for i in range(n_blocks):
        start = i * block_size
        end = (i + 1) * block_size if i < n_blocks - 1 else n
        blocks.append(df.iloc[start:end])
    return blocks


def _split_blocks_multi(
    multi_df: pd.DataFrame,
    n_blocks: int,
    symbols: list[str],
) -> list[dict[str, pd.DataFrame]]:
    """Block a multi-symbol dev frame on the unified (intersection)
    timeline so per-symbol blocks line up bar-for-bar.

    The dev frame returned by `holdout.load_dev` for a multi-symbol
    strategy contains one row per (timestamp, symbol) with a 'symbol'
    column.  We:

    1. Partition by 'symbol' into per-symbol DataFrames.
    2. Build the intersection of timestamps across all symbols.
    3. Split the intersection into `n_blocks` equal-row chunks
       (last absorbs remainder).
    4. Slice each per-symbol DataFrame to each chunk's timestamps.

    Using the intersection (rather than the union) keeps per-symbol
    block lengths identical, satisfying `BacktestEngine.run`'s
    requirement that all `universe_dfs` entries share `df`'s index
    length.
    """
    per_symbol = {
        sym: multi_df[multi_df["symbol"] == sym].sort_index()
        for sym in symbols
    }

    common_idx: Optional[pd.Index] = None
    for sym in symbols:
        idx = per_symbol[sym].index
        common_idx = idx if common_idx is None else common_idx.intersection(idx)
    if common_idx is None or len(common_idx) == 0:
        raise CPCVError(
            f"multi-symbol blocking found no common timestamps across "
            f"symbols={symbols}; check the manifest and dev data."
        )
    common_idx = common_idx.sort_values()
    n_total = len(common_idx)
    block_size = n_total // n_blocks

    blocks: list[dict[str, pd.DataFrame]] = []
    for i in range(n_blocks):
        start = i * block_size
        end = (i + 1) * block_size if i < n_blocks - 1 else n_total
        ts_range = common_idx[start:end]
        block_per_symbol = {
            sym: per_symbol[sym].loc[ts_range] for sym in symbols
        }
        blocks.append(block_per_symbol)
    return blocks


def _validate_block_sizes_single(blocks: list[pd.DataFrame]) -> None:
    """Raise CPCVError on the first block below `_MIN_BLOCK_CANDLES`."""
    for i, b in enumerate(blocks):
        if len(b) < _MIN_BLOCK_CANDLES:
            raise CPCVError(
                f"block too small: {len(b)} candles, minimum "
                f"{_MIN_BLOCK_CANDLES} (block {i})"
            )


def _validate_block_sizes_multi(blocks: list[dict[str, pd.DataFrame]]) -> None:
    """Raise CPCVError on the first per-symbol block below `_MIN_BLOCK_CANDLES`."""
    for i, sym_blocks in enumerate(blocks):
        for sym, b in sym_blocks.items():
            if len(b) < _MIN_BLOCK_CANDLES:
                raise CPCVError(
                    f"block too small: {len(b)} candles, minimum "
                    f"{_MIN_BLOCK_CANDLES} (block {i}, symbol {sym})"
                )


# ── Sharpe & purge / embargo ──────────────────────────────────────────────────

def _infer_candle_hours(df: pd.DataFrame) -> float:
    """Mirror of `BacktestEngine._infer_candle_hours`.

    Estimates candle duration in hours from the first two index
    timestamps; floors at 1 minute (1/60 h).
    """
    if len(df) < 2:
        return 1.0
    delta = df.index[1] - df.index[0]
    hours = delta.total_seconds() / 3600
    return max(hours, 1 / 60)


def _sharpe_from_returns(
    returns: np.ndarray,
    candle_duration_h: float,
) -> float:
    """Annualised Sharpe from a per-bar return series.

    Mirrors the formula in `BacktestEngine._compute_metrics`:

        years = (n * candle_duration_h) / (365.25 * 24)
        total_return = ∏(1 + r) − 1
        ann_return_pct = ((1 + total_return) ** (1/years) − 1) × 100
        candles_per_year = (365.25 * 24) / candle_duration_h
        vol_pct = std(r) × √candles_per_year × 100
        sharpe = ann_return_pct / vol_pct  if vol_pct > 0 else 0.0

    Returns 0.0 when the input is empty, when years is 0, when the
    cumulative return is ≤ −100 % (catastrophic ruin — Sharpe is
    undefined there), or when the series has zero volatility.
    """
    arr = np.asarray(returns, dtype=float)
    n = arr.size
    if n == 0:
        return 0.0
    years = (n * candle_duration_h) / (365.25 * 24)
    if years <= 0:
        return 0.0
    total_return = float(np.prod(1.0 + arr) - 1.0)
    if total_return <= -1.0:
        return 0.0
    ann_return_pct = ((1.0 + total_return) ** (1.0 / years) - 1.0) * 100.0
    candles_per_year = (365.25 * 24) / candle_duration_h
    vol_pct = float(arr.std()) * math.sqrt(candles_per_year) * 100.0
    if vol_pct <= 0:
        return 0.0
    return ann_return_pct / vol_pct


def _apply_purge_embargo(
    returns: np.ndarray,
    purge: int,
    embargo: int,
) -> np.ndarray:
    """Return a copy of `returns` with the first `purge` and last
    `embargo` entries zeroed.  No-op if both are 0 or input is empty.
    """
    if returns.size == 0 or (purge == 0 and embargo == 0):
        return returns.copy() if returns.size > 0 else returns
    out = returns.copy()
    head = min(purge, out.size)
    if head > 0:
        out[:head] = 0.0
    tail = min(embargo, out.size)
    if tail > 0:
        out[-tail:] = 0.0
    return out


# ── Summary ───────────────────────────────────────────────────────────────────

def summarize(per_path_sharpes: list[float]) -> dict:
    """Summarise a list of per-block Sharpes into the trials.log schema.

    Returns the dict shape consumed by `backtest.trials.record_trial`
    under `cpcv.sharpe_distribution`:

        {"mean": float,
         "std": float,
         "quantiles": {"p05": float, "p25": float, "p50": float,
                        "p75": float, "p95": float}}

    Quantiles are computed via `numpy.percentile(..., method="linear")`.
    For a single-element input, std is 0.0 and all quantiles equal the
    sole value.  For an empty input, ValueError is raised — a CPCV run
    that produced no valid blocks is a runner bug, not a valid summary
    input.

    The argument name is preserved for backwards compatibility; in
    block-Sharpe mode the input is the per-block Sharpe series with
    NaNs filtered out by the caller.
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
