"""
backtest/cpcv_perp.py — Block Sharpe distribution for two-leg perp+spot strategies.

Sibling of `backtest.cpcv.run_cpcv` for `legs`-typed manifest entries
(perp + spot).  Reuses every shared CPCV primitive from
`backtest.cpcv_common` so the statistical contract — block-Sharpe
distribution, NaN policy, purge/embargo, summary shape — is
identical to the spot-only runner.  The only difference is the
per-block engine call: `engine_perp.run_perp(df_spot, df_perp,
funding_history, …)` instead of `engine.run(df, …)`.

Funding history loading
───────────────────────
Funding settlements are fetched via
`data.okx_funding.load_or_fetch_funding_history` for the perp leg
of the manifest entry.  The settlements are clipped to the dev
window (`< holdout_start`) before block construction so blocks
never receive holdout-window settlements.

Module surface
──────────────
Public:
    run_cpcv_perp(strategy_id, params, config, strategy_factory) -> CPCVResult

Reads but does not write `backtest/holdout.py` state; never appends
to `backtest/trials.log`.  Same separation-of-concerns rule as
`backtest.cpcv`.
"""

from __future__ import annotations

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
    _MIN_BLOCK_CANDLES,
    _MIN_TRADES_PER_BLOCK,
    _apply_purge_embargo,
    _infer_candle_hours,
    _sharpe_from_returns,
    summarize,
)
from backtest.engine_perp import (
    DEFAULT_INITIAL_BALANCE,
    DEFAULT_WARM_UP_CANDLES,
    run_perp,
)
from data.okx_funding import load_or_fetch_funding_history
from strategies.base import BaseStrategy


# ── Public surface ────────────────────────────────────────────────────────────

def run_cpcv_perp(
    strategy_id: str,
    params: dict,
    config: CPCVConfig,
    strategy_factory: Callable[[], BaseStrategy],
) -> CPCVResult:
    """Block-Sharpe CPCV for a two-leg (perp + spot) manifest entry.

    Mirrors `backtest.cpcv.run_cpcv`'s signature shape and statistical
    contract, but routes through `engine_perp.run_perp` per block.
    The output `CPCVResult` is shape-compatible with
    `backtest.dsr.dsr_from_cpcv_result` so existing DSR / verdict-tree
    plumbing accepts it unchanged.

    Args:
        strategy_id:      Manifest key for the two-leg strategy.
        params:           Strategy parameters; passed through unchanged.
                          May carry strategy-specific knobs that
                          `strategy_factory()` reads.
        config:           CPCVConfig.  `config.k_held_out` is reserved.
        strategy_factory: Zero-arg callable returning a fresh
                          BaseStrategy.  Called once per block.

    Returns:
        CPCVResult with per-block Sharpes (length n_blocks; NaN for
        blocks below `_MIN_TRADES_PER_BLOCK`), per-block trade counts,
        and a summary distribution.

    Raises:
        StrategyNotInManifest:  strategy_id is not in the manifest.
        CPCVError:              entry is not a `legs` entry, a block
                                falls below `_MIN_BLOCK_CANDLES`, or
                                more than 50 % of blocks have
                                insufficient trades.
        ValueError:             CPCVConfig.validate() rejected the
                                config.
    """
    config.validate()

    manifest = _holdout.load_manifest()
    if strategy_id not in manifest:
        raise _holdout.StrategyNotInManifest(
            f"'{strategy_id}' not found in holdout manifest."
        )
    entry = manifest[strategy_id]
    legs = _holdout._get_legs(entry)
    if legs is None:
        raise CPCVError(
            f"manifest entry for '{strategy_id}' is not a 'legs' "
            "entry; route single-symbol and rotation strategies through "
            "backtest.cpcv.run_cpcv instead."
        )

    # 1. Load both legs' dev frames via the holdout accessor.  Returns
    #    {"spot": DataFrame, "perp": DataFrame}.
    dev = _holdout.load_dev(strategy_id)
    if not isinstance(dev, dict):  # pragma: no cover — defensive
        raise CPCVError(
            f"holdout.load_dev for '{strategy_id}' returned a "
            f"{type(dev).__name__}; expected dict for legs entry."
        )
    df_spot = dev["spot"]
    df_perp = dev["perp"]

    # 2. Build block boundaries on the timestamp intersection so each
    #    block covers the same calendar window on both legs.
    common_idx = df_spot.index.intersection(df_perp.index).sort_values()
    if len(common_idx) == 0:
        raise CPCVError(
            f"two-leg blocking found no common timestamps for "
            f"'{strategy_id}': spot rows={len(df_spot)}, perp rows="
            f"{len(df_perp)}."
        )
    spot_blocks, perp_blocks = _split_legs_blocks(
        df_spot=df_spot,
        df_perp=df_perp,
        common_idx=common_idx,
        n_blocks=config.n_blocks,
    )

    # Block-size validation: every leg's per-block frame must clear
    # `_MIN_BLOCK_CANDLES` so the engine has room to warm up.
    for i, (sb, pb) in enumerate(zip(spot_blocks, perp_blocks)):
        if len(sb) < _MIN_BLOCK_CANDLES:
            raise CPCVError(
                f"block too small: {len(sb)} candles, minimum "
                f"{_MIN_BLOCK_CANDLES} (block {i}, leg spot)"
            )
        if len(pb) < _MIN_BLOCK_CANDLES:
            raise CPCVError(
                f"block too small: {len(pb)} candles, minimum "
                f"{_MIN_BLOCK_CANDLES} (block {i}, leg perp)"
            )

    # 3. Load funding history for the dev window (entire window — the
    #    per-block run_perp filter clips to its own bar range
    #    internally).  Manifest's perp leg symbol drives the fetch.
    holdout_start = pd.Timestamp(entry["holdout_start"])
    funding_full = load_or_fetch_funding_history(
        legs["perp"], months=_funding_months_window(common_idx),
    )
    funding_dev = funding_full[funding_full.index < holdout_start]

    funding_cadence_hours = entry.get("funding_cadence_hours", 8)
    flip_exit_n = int(params.get("flip_exit_n", 3))
    flip_exit_threshold = float(params.get("flip_exit_threshold", 0.0))
    cushion_threshold = float(params.get("cushion_threshold", 0.5))
    leverage = float(params.get("leverage", 5.0))
    margin_mode = str(params.get("margin_mode", "cross"))
    initial_balance = float(
        params.get("initial_balance", DEFAULT_INITIAL_BALANCE)
    )
    warm_up_candles = int(
        params.get("warm_up_candles", DEFAULT_WARM_UP_CANDLES)
    )

    candle_duration_h = _infer_candle_hours(df_perp)

    # 4. Run engine_perp once per block with a fresh strategy_factory
    #    instance.  Capture per-block return arrays for Sharpe.
    block_sharpes: list[float] = []
    trade_counts: list[int] = []
    block_returns: list[np.ndarray] = []

    for i, (sb, pb) in enumerate(zip(spot_blocks, perp_blocks)):
        strategy = strategy_factory()
        block_funding = funding_dev[
            (funding_dev.index >= pb.index[0])
            & (funding_dev.index <= pb.index[-1])
        ]
        result = run_perp(
            df_spot=sb,
            df_perp=pb,
            funding_history=block_funding,
            strategy=strategy,
            period_label=f"cpcv-perp-block-{i}",
            initial_balance=initial_balance,
            warm_up_candles=warm_up_candles,
            leverage=leverage,
            margin_mode=margin_mode,
            spot_symbol=legs["spot"],
            perp_symbol=legs["perp"],
            flip_exit_n=flip_exit_n,
            flip_exit_threshold=flip_exit_threshold,
            cushion_threshold=cushion_threshold,
        )

        n_trades = result.metrics.total_trades
        trade_counts.append(n_trades)
        if n_trades < _MIN_TRADES_PER_BLOCK:
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

    # 5. > 50 % NaN check — same threshold as run_cpcv.
    valid = sum(1 for s in block_sharpes if not math.isnan(s))
    total = len(block_sharpes)
    if valid * 2 < total:
        raise CPCVError(
            f"more than 50% of blocks have insufficient trades; "
            f"CPCV unreliable (valid {valid}/{total} blocks)"
        )

    valid_sharpes = [s for s in block_sharpes if not math.isnan(s)]
    distribution = summarize(valid_sharpes)

    return CPCVResult(
        n_paths=config.n_blocks,
        sharpe_distribution=distribution,
        per_path_sharpes=block_sharpes,
        trades_per_path=trade_counts,
        per_block_returns=block_returns,
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _split_legs_blocks(
    df_spot: pd.DataFrame,
    df_perp: pd.DataFrame,
    common_idx: pd.Index,
    n_blocks: int,
) -> tuple[list[pd.DataFrame], list[pd.DataFrame]]:
    """Split spot and perp dev frames into `n_blocks` blocks aligned
    on `common_idx`.

    Mirrors `cpcv_common._split_blocks_multi`'s intersection
    discipline: per-leg block lengths are identical so engine_perp
    can replay them bar-for-bar without extra alignment.
    """
    n_total = len(common_idx)
    block_size = n_total // n_blocks
    spot_blocks: list[pd.DataFrame] = []
    perp_blocks: list[pd.DataFrame] = []
    for i in range(n_blocks):
        start = i * block_size
        end = (i + 1) * block_size if i < n_blocks - 1 else n_total
        ts_range = common_idx[start:end]
        spot_blocks.append(df_spot.loc[ts_range])
        perp_blocks.append(df_perp.loc[ts_range])
    return spot_blocks, perp_blocks


def _funding_months_window(common_idx: pd.Index) -> int:
    """Convert the dev-window calendar span into a rounded-up month
    count for `load_or_fetch_funding_history(months=...)`.

    The funding loader fetches a trailing window of `months` months;
    we round up to ensure the entire dev window is covered.
    """
    if len(common_idx) < 2:
        return 1
    span_days = (common_idx[-1] - common_idx[0]).days
    months = max(1, int(math.ceil(span_days / 30.44)))
    return months
