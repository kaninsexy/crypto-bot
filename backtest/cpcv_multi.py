"""
backtest/cpcv_multi.py — Block-Sharpe CPCV for multi-asset basket strategies.

Sibling of `backtest.cpcv.run_cpcv` and `backtest.cpcv_perp.run_cpcv_perp`
for multi-symbol manifest entries (e.g. TrendFollowing daily multi-asset
on a 12-instrument USDT spot basket).  Reuses every shared CPCV
primitive from `backtest.cpcv_common` so the statistical contract
(block-Sharpe distribution, NaN policy, purge/embargo, summary shape)
is identical across spot single-symbol, perp+spot two-leg, and
multi-asset basket runners.

Block construction
──────────────────
Per-symbol bars synchronise on the timestamp intersection across the
basket (mirroring `cpcv_common._split_blocks_multi`).  Block sizes are
computed against this intersection so engine_multi's per-bar replay
sees a symbol-aligned slice for every block.

Effective-block downshift
─────────────────────────
The TrendFollowing daily substrate has a 126-day formation window per
Hurst/Ooi/Pedersen (2017); a naive 10-block split of the ~880-day dev
window would leave each block with insufficient history to ever fire
a signal.  `cpcv_common.compute_effective_n_blocks` formalises the
downshift: when `config.strategy_warmup_candles > 0` (the new field on
CPCVConfig from this same Phase 4.A change) the runner reduces
`n_blocks` so each block has room for both the formation window AND
`config.min_tradeable_candles_per_block` post-warmup bars.  When the
field is 0 (every existing caller) the helper returns `config.n_blocks`
verbatim and the path is unchanged.

Module surface
──────────────
Public:
    run_cpcv_multi(data, strategy, config, initial_balance=10_000.0)
        -> CPCVResult

Reads but does not write `backtest/holdout.py` state; never appends
to `backtest/trials.log`.  Same separation-of-concerns rule as
`backtest.cpcv` and `backtest.cpcv_perp`.
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np
import pandas as pd
from loguru import logger

from backtest.cpcv_common import (
    CPCVConfig,
    CPCVError,
    CPCVResult,
    _MIN_BLOCK_CANDLES,
    _MIN_EVENTS_PER_BLOCK,
    _apply_purge_embargo,
    _infer_candle_hours,
    _sharpe_from_returns,
    compute_effective_n_blocks,
    summarize,
)
from backtest.engine_multi import (
    DEFAULT_INITIAL_BALANCE,
    run_engine_multi,
)
from strategies.trend_following_multi import TrendFollowingMultiStrategy


# ── Public surface ────────────────────────────────────────────────────────────

def run_cpcv_multi(
    data: dict[str, pd.DataFrame],
    strategy: TrendFollowingMultiStrategy,
    config: CPCVConfig,
    initial_balance: float = DEFAULT_INITIAL_BALANCE,
) -> CPCVResult:
    """Block-Sharpe CPCV for a multi-asset basket strategy.

    Mirrors `backtest.cpcv.run_cpcv`'s signature shape and statistical
    contract, routing through `engine_multi.run_engine_multi` per
    block.  The output `CPCVResult` is shape-compatible with
    `backtest.dsr.dsr_from_cpcv_result` so existing DSR / verdict-tree
    plumbing accepts it unchanged.

    Args:
        data:             `{symbol: OHLCV DataFrame}` keyed by every
                          symbol in `strategy.symbols`.  All frames
                          must share the same timestamp granularity
                          (e.g. daily bars).
        strategy:         A pre-instantiated TrendFollowingMultiStrategy.
                          The strategy is cloned per block via
                          `_clone_strategy(strategy)` so each block
                          starts with a fresh per-symbol position book.
        config:           CPCVConfig.  `config.k_held_out` is reserved.
                          `config.strategy_warmup_candles` is honoured
                          via `compute_effective_n_blocks`.
        initial_balance:  Starting USDT for each block's engine call.

    Returns:
        CPCVResult with per-block Sharpes (length effective_n_blocks;
        NaN for blocks below `_MIN_EVENTS_PER_BLOCK`), per-block trade
        counts, per-block return arrays, and a summary distribution.

    Raises:
        ValueError:  CPCVConfig.validate() rejected the config.
        CPCVError:   no symbols overlap with the data, the timestamp
                     intersection is empty, a block falls below
                     `_MIN_BLOCK_CANDLES`, the effective block count
                     is < 2, or > 50 % of blocks have insufficient
                     trades.
    """
    config.validate()

    active_symbols: list[str] = [s for s in strategy.symbols if s in data]
    if not active_symbols:
        raise CPCVError(
            f"No overlap between strategy.symbols={strategy.symbols!r} "
            f"and data keys {sorted(data.keys())!r}."
        )

    # 1. Build the synchronised timeline as the intersection of
    #    per-symbol indices.  Identical discipline to
    #    `cpcv_common._split_blocks_multi`.
    common_idx: Optional[pd.Index] = None
    for sym in active_symbols:
        idx = data[sym].sort_index().index
        common_idx = idx if common_idx is None else common_idx.intersection(idx)
    if common_idx is None or len(common_idx) == 0:
        raise CPCVError(
            f"multi-asset blocking found no common timestamps across "
            f"symbols={active_symbols}; check the manifest and dev data."
        )
    common_idx = common_idx.sort_values()

    # 2. Resolve the effective block count.  When
    #    `strategy_warmup_candles > 0` the helper downshifts so each
    #    block has room for the formation window plus
    #    `min_tradeable_candles_per_block` post-warmup bars; otherwise
    #    it returns `config.n_blocks` verbatim.
    effective_n_blocks = compute_effective_n_blocks(len(common_idx), config)
    if effective_n_blocks < 2:
        raise CPCVError(
            f"effective_n_blocks={effective_n_blocks} < 2; dev window "
            f"({len(common_idx)} bars) too short for "
            f"strategy_warmup_candles={config.strategy_warmup_candles} + "
            f"min_tradeable_candles_per_block={config.min_tradeable_candles_per_block}."
        )
    logger.info(
        f"[CPCVMulti] common_idx={len(common_idx)} | "
        f"requested_n_blocks={config.n_blocks} | "
        f"warmup={config.strategy_warmup_candles} | "
        f"effective_n_blocks={effective_n_blocks}"
    )

    # 3. Build synchronised per-block descriptors.
    block_size = len(common_idx) // effective_n_blocks
    block_descriptors: list[dict[str, pd.DataFrame]] = []
    for i in range(effective_n_blocks):
        start = i * block_size
        end = (i + 1) * block_size if i < effective_n_blocks - 1 else len(common_idx)
        ts_range = common_idx[start:end]
        block_descriptors.append({
            sym: data[sym].sort_index().loc[ts_range]
            for sym in active_symbols
        })

    # 4. Block-size validation.
    for i, blk in enumerate(block_descriptors):
        for sym, df in blk.items():
            if len(df) < _MIN_BLOCK_CANDLES:
                raise CPCVError(
                    f"block too small: {len(df)} candles, minimum "
                    f"{_MIN_BLOCK_CANDLES} (block {i}, symbol {sym})"
                )

    # 5. Run engine_multi per block.
    _ref_df = data[active_symbols[0]].loc[common_idx]
    candle_duration_h = _infer_candle_hours(_ref_df)
    use_signal_events = config.count_signal_events_per_block is not None
    block_sharpes: list[float] = []
    trade_counts: list[int] = []
    block_returns: list[np.ndarray] = []
    signal_event_counts: list[int] = []

    for i, blk in enumerate(block_descriptors):
        block_strategy = _clone_strategy(strategy)
        result = run_engine_multi(
            data=blk,
            strategy=block_strategy,
            period_label=f"cpcv-multi-block-{i}",
            initial_balance=initial_balance,
        )

        n_trades = result.metrics.total_trades
        trade_counts.append(n_trades)

        if use_signal_events:
            n_events = int(
                config.count_signal_events_per_block(result, blk)
            )
            signal_event_counts.append(n_events)
            event_count = n_events
        else:
            event_count = n_trades

        if event_count < _MIN_EVENTS_PER_BLOCK:
            block_sharpes.append(float("nan"))
            block_returns.append(np.array([], dtype=float))
            continue

        eq = result.equity_curve
        if eq.empty or len(eq) < 2:
            block_sharpes.append(float("nan"))
            block_returns.append(np.array([], dtype=float))
            continue
        rets = eq.pct_change().dropna().values.astype(float)
        rets = _apply_purge_embargo(
            rets, config.purge_periods, config.embargo_periods,
        )
        block_sharpes.append(_sharpe_from_returns(rets, candle_duration_h))
        block_returns.append(rets)

    # 6. > 50 % NaN check — same threshold as run_cpcv / run_cpcv_perp.
    valid = sum(1 for s in block_sharpes if not math.isnan(s))
    total = len(block_sharpes)
    if valid * 2 < total:
        raise CPCVError(
            f"more than 50% of blocks have insufficient "
            + ("signal events" if use_signal_events else "trades")
            + f"; CPCV unreliable (valid {valid}/{total} blocks)"
        )

    valid_sharpes = [s for s in block_sharpes if not math.isnan(s)]
    distribution = summarize(valid_sharpes)

    return CPCVResult(
        n_paths=effective_n_blocks,
        sharpe_distribution=distribution,
        per_path_sharpes=block_sharpes,
        trades_per_path=trade_counts,
        per_block_returns=block_returns,
        signal_events_per_block=(signal_event_counts if use_signal_events else None),
    )


# ── Helpers ──────────────────────────────────────────────────────────────────

def _clone_strategy(
    strategy: TrendFollowingMultiStrategy,
) -> TrendFollowingMultiStrategy:
    """Return a fresh strategy instance with the same config so each
    block runs from a clean state.  TrendFollowingMultiStrategy is
    stateless w.r.t. positions (the engine owns the position book), so
    re-instantiating with the same args is sufficient.
    """
    return TrendFollowingMultiStrategy(
        symbols=list(strategy.symbols),
        timeframe=strategy.timeframe,
        lookback_days=strategy.lookback_days,
        target_vol_annual=strategy.target_vol_annual,
    )
