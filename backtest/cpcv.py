"""
backtest/cpcv.py — Block Sharpe distribution (sacred harness, single-leg).

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

Module surface
──────────────
The shared block-Sharpe primitives (block construction, purge/embargo,
Sharpe-from-returns, summary, and the CPCVConfig/CPCVResult/CPCVError
dataclasses) live in `backtest.cpcv_common` so the perp-and-spot
two-leg runner in `backtest.cpcv_perp` can reuse them under identical
statistical discipline.  This module re-exports those symbols so
callers (and tests) that import them from `backtest.cpcv` continue
to resolve them.

Skip / NaN policy
─────────────────
A block must clear `_MIN_EVENTS_PER_BLOCK = 5` trades to contribute.
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
from typing import Optional

import numpy as np
import pandas as pd

import backtest.holdout as _holdout
from backtest.cpcv_common import (
    CPCVConfig,
    CPCVError,
    CPCVResult,
    _DEFAULT_EMBARGO_PERIODS,
    _DEFAULT_K_HELD_OUT,
    _DEFAULT_N_BLOCKS,
    _DEFAULT_PURGE_PERIODS,
    _ENGINE_INITIAL_BALANCE,
    _ENGINE_WARM_UP_CANDLES,
    _MIN_BLOCK_CANDLES,
    _MIN_EVENTS_PER_BLOCK,
    _apply_purge_embargo,
    _infer_candle_hours,
    _sharpe_from_returns,
    _split_blocks,
    _split_blocks_multi,
    _validate_block_sizes_multi,
    _validate_block_sizes_single,
    summarize,
)
from backtest.engine import BacktestEngine, BacktestResult
from strategies.base import BaseStrategy

__all__ = [
    "CPCVConfig",
    "CPCVError",
    "CPCVResult",
    "run_cpcv",
    "summarize",
    # Re-exported for backward compatibility with existing tests and
    # downstream tooling that imports private helpers from this module.
    "_apply_purge_embargo",
    "_infer_candle_hours",
    "_run_engine_per_block",
    "_sharpe_from_returns",
    "_split_blocks",
    "_split_blocks_multi",
    "_validate_block_sizes_multi",
    "_validate_block_sizes_single",
]


# ── Engine integration ────────────────────────────────────────────────────────

def _run_engine_per_block(
    strategy_factory: Callable[[], BaseStrategy],
    blocks: list,
    primary_symbol: Optional[str],
    is_multi_symbol: bool,
    warm_up_candles: int = _ENGINE_WARM_UP_CANDLES,
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
        warm_up_candles=warm_up_candles,
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
    warm_up_candles: int = _ENGINE_WARM_UP_CANDLES,
) -> CPCVResult:
    """Run the block-Sharpe distribution on a strategy's dev window.

    See module docstring and `docs/validation_framework.md` §
    "Block Sharpe distribution" for the methodology.

    Algorithm:
      1. Validate config; load manifest; reject unknown strategy_id.
      2. Decide single- vs multi-symbol from the manifest entry.
         A `legs`-typed entry (perp + spot) is rejected — those route
         through `backtest.cpcv_perp.run_cpcv_perp`.
      3. Load dev data via `holdout.load_dev(strategy_id)`.
      4. Equal-row split into `config.n_blocks` blocks; validate
         block sizes against `_MIN_BLOCK_CANDLES`.
      5. Run `engine.run` once per block via `_run_engine_per_block`,
         using a fresh `strategy_factory()` per block.
      6. For each block:
         a. Extract per-bar returns from the BacktestResult.
         b. Apply purge / embargo trim per `config`.
         c. If trade count < `_MIN_EVENTS_PER_BLOCK`, the block's
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
                               `_MIN_BLOCK_CANDLES`, more than
                               50 % of blocks have insufficient
                               trades, or the manifest entry is a
                               two-leg (`legs`) entry that should
                               route through run_cpcv_perp instead.
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
    if "legs" in entry:
        raise CPCVError(
            f"manifest entry for '{strategy_id}' has 'legs' (perp+spot) — "
            "two-leg strategies must use backtest.cpcv_perp.run_cpcv_perp, "
            "not run_cpcv."
        )
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
            warm_up_candles=warm_up_candles,
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
            warm_up_candles=warm_up_candles,
        )

    # 2. Per-block Sharpe, trade count, and post-trim returns.
    # Track 2 (2026-05-02): when CPCVConfig.count_signal_events_per_
    # block is set, the callback's per-block return value SUBSTITUTES
    # for the trade count in the validity check.  Per-block returns
    # array is still cleared on insufficient events so DSR's
    # `per_block_returns` consumer behaves identically across both
    # paths.
    use_signal_events = config.count_signal_events_per_block is not None
    blocks_for_callback = blocks_multi if is_multi_symbol else blocks_single
    block_sharpes: list[float] = []
    trade_counts: list[int] = []
    block_returns: list[np.ndarray] = []
    signal_event_counts: list[int] = [] if use_signal_events else []

    for r, block_input in zip(engine_results, blocks_for_callback):
        n_trades = r.metrics.total_trades
        trade_counts.append(n_trades)

        if use_signal_events:
            n_events = int(config.count_signal_events_per_block(r, block_input))
            signal_event_counts.append(n_events)
            event_count = n_events
        else:
            event_count = n_trades

        if event_count < _MIN_EVENTS_PER_BLOCK:
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
            f"more than 50% of blocks have insufficient "
            + ("signal events" if use_signal_events else "trades")
            + f"; CPCV unreliable (valid {valid}/{total} blocks)"
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
        signal_events_per_block=(signal_event_counts if use_signal_events else None),
        per_block_returns=block_returns,
    )
