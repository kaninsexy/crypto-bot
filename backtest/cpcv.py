"""
backtest/cpcv.py — Block Sharpe distribution (sacred harness).

References:
  Bailey, D.H. & López de Prado, M. (2014). The Deflated Sharpe Ratio.
  López de Prado, M. (2018). Advances in Financial Machine Learning,
    chapter 7 (Cross-Validation in Finance).

This module is part of the validation harness — modifying it requires
human approval per CLAUDE.md.

Methodology
───────────
The dev window (returned by `backtest.holdout.load_dev`) is split
into N non-overlapping equal-row blocks.  The strategy runs through
each block as a standalone `engine.run()` call with a fresh
`strategy_factory()` instance — no state carryover between blocks.
Each block produces a Sharpe via the same formula as
`backtest/engine.py:_compute_metrics`.  The resulting N-element
Sharpe distribution is the input to DSR.

Why block Sharpe rather than López de Prado path-CPCV
─────────────────────────────────────────────────────
Standard CPCV path machinery generates path variance by fitting
different models on different combinations of training blocks and
producing different predictions on held-out blocks.  That is an ML
construct — for rule-based strategies with no fit, the same block
data produces the same returns regardless of which combination it
is nominally part of, so all reconstructed paths collapse to
identical Sharpes.  Running the engine on concatenated held-out
blocks per combination would produce non-degenerate variance, but
only via artificial time-adjacency at the gluing boundaries — a
leakage artifact, not a property of the underlying data.

The block Sharpe distribution avoids both failures.  Blocks are
independent runs with fresh strategy state, the variance reflects
genuine across-period dispersion of strategy performance, and the
distribution feeds DSR with honest inputs.  Structurally similar to
walk-forward validation but produces N Sharpes per strategy instead
of one, preserving the multi-sample basis DSR requires.

See `docs/validation_framework.md` § "Block Sharpe distribution" for
the authoritative spec.

Skip / NaN policy
─────────────────
A block must clear `_MIN_TRADES_PER_BLOCK = 5` trades to contribute.
A block below that threshold produces NaN.  If more than 50 % of
blocks are NaN, `run_cpcv` raises `CPCVError`; the result is
statistically unreliable.

This module must NOT write to trials.log.  Callers package the trial
event from a `CPCVResult` and call `backtest.trials.record_trial`.
Keeping the runner separate from the writer is what makes the
multiple-testing count authoritative.
"""

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

import backtest.holdout as _holdout
from backtest.engine import BacktestEngine, BacktestResult
from strategies.base import BaseStrategy


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


# ── Engine integration ────────────────────────────────────────────────────────

def _run_engine_per_block(
    strategy_factory: Callable[[], BaseStrategy],
    blocks: list,
    primary_symbol: Optional[str],
    is_multi_symbol: bool,
) -> list[BacktestResult]:
    """Run `BacktestEngine.run` once per block.

    For single-symbol blocks, `blocks` is a list of DataFrames and the
    engine is called with `universe_dfs=None`.  For multi-symbol
    blocks, `blocks` is a list of `{symbol: DataFrame}` dicts; the
    engine receives the primary symbol's per-block frame as `df` and
    the full per-block dict as `universe_dfs`.

    A fresh strategy instance is created per block via
    `strategy_factory()` to eliminate state leakage between blocks —
    the same discipline `WalkForwardBacktester` uses.
    """
    engine = BacktestEngine(
        initial_balance=_ENGINE_INITIAL_BALANCE,
        warm_up_candles=_ENGINE_WARM_UP_CANDLES,
        verbose=False,
    )
    results: list[BacktestResult] = []
    for i, block in enumerate(blocks):
        strategy = strategy_factory()
        if is_multi_symbol:
            block_dict = block  # dict[symbol, DataFrame]
            primary_df = block_dict[primary_symbol]
            r = engine.run(
                df=primary_df,
                strategy=strategy,
                period_label=f"cpcv-block-{i}",
                universe_dfs=block_dict,
            )
        else:
            r = engine.run(
                df=block,
                strategy=strategy,
                period_label=f"cpcv-block-{i}",
            )
        results.append(r)
    return results


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


# ── Block-returns extraction ──────────────────────────────────────────────────

def _block_returns_from_result(result: BacktestResult) -> np.ndarray:
    """Extract per-bar returns from a block's BacktestResult.

    Uses `equity_curve.pct_change().dropna()` so the leading NaN is
    discarded, matching the shape consumed by `_sharpe_from_returns`.
    Empty equity curves return an empty array.
    """
    eq = result.equity_curve
    if eq.empty or len(eq) < 2:
        return np.array([], dtype=float)
    return eq.pct_change().dropna().values.astype(float)


# ── Public surface ────────────────────────────────────────────────────────────

def run_cpcv(
    strategy_id: str,
    params: dict,
    config: CPCVConfig,
    strategy_factory: Callable[[], BaseStrategy],
) -> CPCVResult:
    """Run the block-Sharpe distribution on a strategy's dev window.

    See module docstring and `docs/validation_framework.md` §
    "Block Sharpe distribution" for the methodology.

    Algorithm:
      1. Validate config; load manifest; reject unknown strategy_id.
      2. Decide single- vs multi-symbol from the manifest entry.
      3. Load dev data via `holdout.load_dev(strategy_id)`.
      4. Equal-row split into `config.n_blocks` blocks; validate
         block sizes against `_MIN_BLOCK_CANDLES`.
      5. Run `engine.run` once per block via `_run_engine_per_block`,
         using a fresh `strategy_factory()` per block.
      6. For each block:
         a. Extract per-bar returns from the BacktestResult.
         b. Apply purge / embargo trim per `config`.
         c. If trade count < `_MIN_TRADES_PER_BLOCK`, the block's
            Sharpe is NaN; otherwise compute Sharpe via
            `_sharpe_from_returns`.
      7. If more than 50 % of blocks are NaN, raise `CPCVError`.
      8. `summarize` the valid Sharpes into the trials.log
         distribution shape.

    Args:
        strategy_id:      Manifest key for the strategy under test.
        params:           Strategy parameters; passed through
                          unchanged.  Caller is responsible for
                          ensuring `strategy_factory()` produces a
                          strategy configured with these params.
        config:           CPCVConfig instance.  `config.k_held_out`
                          is reserved and unused in this mode.
        strategy_factory: Zero-arg callable returning a fresh
                          BaseStrategy instance.  Called once per
                          block so no state leaks between blocks.

    Returns:
        CPCVResult with per-block Sharpes (in `per_path_sharpes` /
        `per_block_sharpes`), per-block trade counts, and a summary
        distribution.

    Raises:
        StrategyNotInManifest: strategy_id is not a manifest key.
        CPCVError:             a block falls below
                               `_MIN_BLOCK_CANDLES`, or more than
                               50 % of blocks have insufficient
                               trades.
        ValueError:            CPCVConfig.validate() rejected the
                               config.
    """
    config.validate()

    manifest = _holdout.load_manifest()
    if strategy_id not in manifest:
        raise _holdout.StrategyNotInManifest(
            f"'{strategy_id}' not found in holdout manifest."
        )
    entry = manifest[strategy_id]
    is_multi_symbol = "symbols" in entry
    symbols = (
        list(entry["symbols"]) if is_multi_symbol else [entry["symbol"]]
    )

    # Determine the strategy's primary symbol so we know which
    # per-symbol block to feed engine.run as `df`.  This costs one
    # throwaway strategy_factory() call; per-block fresh instances are
    # created independently below.
    sample_strategy = strategy_factory()
    primary_symbol = sample_strategy.symbol
    if is_multi_symbol and primary_symbol not in symbols:
        raise CPCVError(
            f"strategy_factory().symbol={primary_symbol!r} is not in "
            f"manifest entry symbols={symbols}; cannot determine which "
            "per-symbol block to feed engine.run as `df`."
        )

    # 1. Load dev data and split into blocks.
    dev_df = _holdout.load_dev(strategy_id)
    if is_multi_symbol:
        blocks_multi = _split_blocks_multi(dev_df, config.n_blocks, symbols)
        _validate_block_sizes_multi(blocks_multi)
        primary_dev_df = dev_df[dev_df["symbol"] == primary_symbol].sort_index()
        candle_duration_h = _infer_candle_hours(primary_dev_df)
        engine_results = _run_engine_per_block(
            strategy_factory=strategy_factory,
            blocks=blocks_multi,
            primary_symbol=primary_symbol,
            is_multi_symbol=True,
        )
    else:
        blocks_single = _split_blocks(dev_df, config.n_blocks)
        _validate_block_sizes_single(blocks_single)
        candle_duration_h = _infer_candle_hours(dev_df)
        engine_results = _run_engine_per_block(
            strategy_factory=strategy_factory,
            blocks=blocks_single,
            primary_symbol=None,
            is_multi_symbol=False,
        )

    # 2. Per-block Sharpe, trade count, and post-trim returns.
    block_sharpes: list[float] = []
    trade_counts: list[int] = []
    block_returns: list[np.ndarray] = []

    for r in engine_results:
        n_trades = r.metrics.total_trades
        trade_counts.append(n_trades)

        if n_trades < _MIN_TRADES_PER_BLOCK:
            block_sharpes.append(float("nan"))
            block_returns.append(np.array([], dtype=float))
            continue

        rets = _block_returns_from_result(r)
        rets = _apply_purge_embargo(
            rets,
            config.purge_periods,
            config.embargo_periods,
        )
        block_sharpes.append(_sharpe_from_returns(rets, candle_duration_h))
        block_returns.append(rets)

    # 3. > 50 % NaN check.
    valid = sum(1 for s in block_sharpes if not math.isnan(s))
    total = len(block_sharpes)
    if valid * 2 < total:
        raise CPCVError(
            f"more than 50% of blocks have insufficient trades; "
            f"CPCV unreliable (valid {valid}/{total} blocks)"
        )

    # 4. Summarise valid Sharpes.  The > 50 % check guarantees at
    #    least one valid Sharpe, so summarize won't see an empty list.
    valid_sharpes = [s for s in block_sharpes if not math.isnan(s)]
    distribution = summarize(valid_sharpes)

    return CPCVResult(
        n_paths=config.n_blocks,
        sharpe_distribution=distribution,
        per_path_sharpes=block_sharpes,
        trades_per_path=trade_counts,
        per_block_returns=block_returns,
    )


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
