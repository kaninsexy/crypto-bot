"""
backtest/engine_perp.py — Two-leg perp+spot backtest engine (Phase 4.B).

Sibling of `backtest.engine.BacktestEngine.run` for delta-neutral
funding-rate-harvest strategies.  Replays a paired (perp, spot)
candle stream through a `paper_trading.perp_simulator.PerpSimulator`
and applies funding settlements at their exact timestamps.

Per-bar return shape contract
─────────────────────────────
The returned BacktestResult's `equity_curve` is a `pd.Series` whose
`.pct_change().dropna()` produces a per-bar return array that
`backtest.cpcv_common._sharpe_from_returns` consumes WITHOUT
modification.  See § "Block Sharpe distribution" of
`docs/validation_framework.md` for the contract.

If the two-leg PnL accounting needed a different per-bar return
formula to feed Sharpe correctly, that would be a STOP-AND-CONSULT
event per the bundle prompt — instead the equity_curve here is the
combined (spot + perp + accrued funding) MTM equity over time, which
is mathematically the same kind of `pct_change().dropna()` series
the spot engine produces, just for a two-leg portfolio.

Paper-mode invariant
────────────────────
Like `backtest.engine`, this module performs no I/O.  All OHLCV and
funding data is supplied by the caller (sourced upstream from the
`data.okx_perp` / `data.okx_funding` parquet caches).
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np
import pandas as pd
from loguru import logger

from backtest.engine import (
    BacktestMetrics,
    BacktestResult,
    SLIPPAGE_LIMIT,
    SLIPPAGE_MARKET,
)
from paper_trading.perp_simulator import PerpSimulator
from strategies.base import BaseStrategy, Signal


# ── Defaults ─────────────────────────────────────────────────────────────────

DEFAULT_INITIAL_BALANCE: float = 10_000.0
DEFAULT_WARM_UP_CANDLES: int = 50


# ── Public API ───────────────────────────────────────────────────────────────

def run_perp(
    df_spot: pd.DataFrame,
    df_perp: pd.DataFrame,
    funding_history: pd.DataFrame,
    strategy: BaseStrategy,
    *,
    period_label: str = "full",
    initial_balance: float = DEFAULT_INITIAL_BALANCE,
    warm_up_candles: int = DEFAULT_WARM_UP_CANDLES,
    leverage: float = 5.0,
    slippage_market: float = SLIPPAGE_MARKET,
    slippage_limit: float = SLIPPAGE_LIMIT,
    spot_symbol: str = "BTC/USDT",
    perp_symbol: str = "BTC/USDT",
    margin_mode: str = "cross",
    flip_exit_n: int = 3,
    flip_exit_threshold: float = 0.0,
    cushion_threshold: Optional[float] = None,
    exit_mr_ratio_threshold: Optional[float] = None,
) -> BacktestResult:
    """Replay perp+spot+funding through a PerpSimulator.

    Args:
        df_spot:          Spot OHLCV with DatetimeIndex (UTC) and the
                          standard `[open, high, low, close, volume]`
                          columns.  Must align bar-for-bar with
                          df_perp on the intersection of timestamps.
        df_perp:          Perp OHLCV with the same shape as df_spot.
                          Used as both the price series for signal
                          generation and the mark series for risk
                          checks (cushion, liquidation).
        funding_history:  DataFrame indexed by funding settlement
                          timestamp (UTC), columns
                          `[funding_rate, mark_price]`.  Source:
                          `data.okx_funding.load_or_fetch_funding_history`.
        strategy:         Pre-instantiated BaseStrategy.  Must be
                          fresh — no state carryover from prior runs.
        period_label:     Label propagated onto the BacktestResult
                          for trial-row provenance.
        initial_balance:  Starting USDT.
        warm_up_candles:  Bars at the start used only for indicator
                          warmup; no trades fire here.
        leverage:         Perp leg leverage; passed to PerpSimulator.
        slippage_market / slippage_limit:  Adverse fill % at signal
                          execution; mirrors backtest.engine.
        spot_symbol / perp_symbol:  Manifest notation propagated
                          into the PerpSimulator and its trade
                          records.
        margin_mode:      "cross" (default) or "isolated".
        flip_exit_n / flip_exit_threshold / cushion_threshold:
                          PerpSimulator exit-trigger parameters.

    Returns:
        BacktestResult with combined-leg equity_curve, trade_history,
        and BacktestMetrics computed over the equity curve.

    Raises:
        ValueError: df_spot and df_perp have no common timestamps,
                    or either is shorter than warm_up_candles + 10.
    """
    # 1. Align spot / perp on the intersection of timestamps.
    common_idx = df_spot.index.intersection(df_perp.index).sort_values()
    if len(common_idx) < warm_up_candles + 10:
        raise ValueError(
            f"Not enough aligned candles: need ≥{warm_up_candles + 10}, "
            f"got {len(common_idx)} (spot={len(df_spot)}, perp={len(df_perp)})"
        )
    df_spot_aligned = df_spot.loc[common_idx]
    df_perp_aligned = df_perp.loc[common_idx]

    # 2. Pre-build a funding-by-timestamp lookup for O(1) per-bar checks.
    if not funding_history.empty:
        # Restrict to settlements within the run window.
        funding_window = funding_history[
            (funding_history.index >= common_idx[0])
            & (funding_history.index <= common_idx[-1])
        ]
    else:
        funding_window = funding_history

    # 3. Construct simulator.  PerpSimulator enforces mutual
    # exclusion of cushion_threshold / exit_mr_ratio_threshold; we
    # forward both verbatim and let it raise on misuse.
    sim = PerpSimulator(
        initial_balance=initial_balance,
        spot_symbol=spot_symbol,
        perp_symbol=perp_symbol,
        leverage=leverage,
        flip_exit_n=flip_exit_n,
        flip_exit_threshold=flip_exit_threshold,
        cushion_threshold=cushion_threshold,
        exit_mr_ratio_threshold=exit_mr_ratio_threshold,
        margin_mode=margin_mode,
    )

    logger.info(
        f"[BacktestPerp] Starting {period_label} run | "
        f"strategy={strategy.name} | spot={spot_symbol} perp={perp_symbol} | "
        f"candles={len(common_idx)} warm_up={warm_up_candles} | "
        f"funding_settlements={len(funding_window)} | "
        f"balance=${initial_balance:,.0f}"
    )

    equity_curve: dict[pd.Timestamp, float] = {}

    for i in range(warm_up_candles, len(common_idx)):
        ts = common_idx[i]
        spot_close = float(df_spot_aligned.iloc[i]["close"])
        perp_high = float(df_perp_aligned.iloc[i]["high"])
        perp_low = float(df_perp_aligned.iloc[i]["low"])
        perp_close = float(df_perp_aligned.iloc[i]["close"])

        # 4. Update spot price BEFORE signal/tick so the simulator
        #    has a fresh spot leg reference for both opening and
        #    cushion math.
        sim.update_spot_close(spot_close)

        # 5. Apply any funding settlements that occurred since the
        #    previous bar (inclusive of this bar's timestamp).
        if len(funding_window) > 0:
            prev_ts = common_idx[i - 1]
            settlements = funding_window[
                (funding_window.index > prev_ts)
                & (funding_window.index <= ts)
            ]
            for s_ts, srow in settlements.iterrows():
                rate = float(srow["funding_rate"])
                mark = float(srow["mark_price"])
                sim.apply_funding_settlement(rate, mark)
                if sim.position is None:
                    # Funding-flip exit fired during this settlement.
                    break

        # 6. Generate strategy signal.  Use the perp slice through
        #    bar `i` so the strategy sees the same bar-aligned price
        #    series the engine is replaying.
        df_slice = df_perp_aligned.iloc[: i + 1]
        try:
            signal = strategy.generate_signal(df_slice)
        except ValueError as exc:
            logger.debug(
                f"[BacktestPerp] Warm-up skip at candle {i}: {exc}"
            )
            equity_curve[ts] = sim.get_equity(perp_close)
            continue

        # 7. Execute signal.  Apply slippage to the perp fill price;
        #    PerpSimulator handles the spot fill from update_spot_close.
        if signal.action != "HOLD":
            fill_price = _apply_slippage(
                signal, perp_close,
                slippage_market=slippage_market,
                slippage_limit=slippage_limit,
            )
            sim.execute_signal(signal, fill_price)

        # 8. Per-bar risk tick — cushion / margin-breach exit on perp HLC.
        sim.tick_ohlcv_candle(
            high=perp_high, low=perp_low, close=perp_close,
        )

        # 9. Record equity AFTER ticks/settlements at the perp close.
        equity_curve[ts] = sim.get_equity(perp_close)

    # 10. Force-close at end of period so trades_per_path and
    #     equity_curve include the final state.
    if sim.position is not None:
        last_ts = common_idx[-1]
        spot_last = float(df_spot_aligned.iloc[-1]["close"])
        perp_last = float(df_perp_aligned.iloc[-1]["close"])
        sim.update_spot_close(spot_last)
        # Synthetic SELL signal so the close routes through
        # PerpSimulator.execute_signal for parity with run-time exits.
        close_signal = Signal(
            action="SELL", strategy=strategy.name,
            price=perp_last, reason="backtest_end",
            order_type="market",
        )
        sim.execute_signal(close_signal, perp_last)
        equity_curve[last_ts] = sim.get_equity(perp_last)

    equity_series = pd.Series(equity_curve)
    metrics = _compute_metrics(
        equity_series=equity_series,
        trade_history=sim.trade_history,
        total_fees=sim.total_fees_paid,
        symbol=sim.symbol,
        strategy_name=strategy.name,
        df=df_perp_aligned,
        period_label=period_label,
        initial_balance=initial_balance,
    )

    logger.info(
        f"[BacktestPerp] {period_label} complete | "
        f"return={metrics.total_return_pct:+.2f}% | "
        f"sharpe={metrics.sharpe_ratio:.3f} | "
        f"trades={metrics.total_trades} | "
        f"funding_violations="
        f"{len(sim.combined_position_sanity_violations)}"
    )

    return BacktestResult(
        metrics=metrics,
        equity_curve=equity_series,
        trade_history=sim.trade_history,
        period_label=period_label,
    )


# ── Slippage helper (mirrors backtest.engine._apply_slippage) ────────────────

def _apply_slippage(
    signal: Signal,
    price: float,
    *,
    slippage_market: float,
    slippage_limit: float,
) -> float:
    if signal.action == "HOLD":
        return price
    slip_pct = (
        slippage_market
        if signal.order_type == "market"
        else slippage_limit
    )
    if signal.action == "BUY":
        return price * (1 + slip_pct)
    return price * (1 - slip_pct)


# ── Metrics (mirror of backtest.engine._compute_metrics) ─────────────────────

def _infer_candle_hours(df: pd.DataFrame) -> float:
    if len(df) < 2:
        return 1.0
    delta = df.index[1] - df.index[0]
    hours = delta.total_seconds() / 3600
    return max(hours, 1 / 60)


def _compute_metrics(
    equity_series: pd.Series,
    trade_history: list,
    total_fees: float,
    symbol: str,
    strategy_name: str,
    df: pd.DataFrame,
    period_label: str,
    initial_balance: float,
) -> BacktestMetrics:
    """Same shape and formula as `backtest.engine._compute_metrics`,
    operating on the combined-leg equity series so the per-bar return
    array `equity_curve.pct_change().dropna()` is consumable by
    `backtest.cpcv_common._sharpe_from_returns` without modification.
    """
    if equity_series.empty:
        return _zero_metrics(symbol, strategy_name, df, period_label)

    start_equity = initial_balance
    end_equity = float(equity_series.iloc[-1])
    total_return = (end_equity - start_equity) / start_equity * 100

    n_candles = len(equity_series)
    candle_duration_h = _infer_candle_hours(df)
    years = (n_candles * candle_duration_h) / (365.25 * 24)
    if years > 0 and end_equity > 0:
        ann_return = ((end_equity / start_equity) ** (1 / years) - 1) * 100
    else:
        ann_return = 0.0

    running_max = equity_series.cummax()
    drawdown = (equity_series - running_max) / running_max * 100
    max_drawdown = float(drawdown.min())

    returns = equity_series.pct_change().dropna()
    candles_per_year = (365.25 * 24) / candle_duration_h
    vol = float(returns.std() * math.sqrt(candles_per_year)) * 100
    sharpe = ann_return / vol if vol > 0 else 0.0
    calmar = ann_return / abs(max_drawdown) if max_drawdown != 0 else 0.0

    n_trades = len(trade_history)
    if n_trades == 0:
        return BacktestMetrics(
            total_return_pct=total_return,
            annualised_return_pct=ann_return,
            max_drawdown_pct=abs(max_drawdown),
            sharpe_ratio=sharpe,
            calmar_ratio=calmar,
            volatility_pct=vol,
            total_trades=0,
            win_rate_pct=0.0,
            profit_factor=0.0,
            avg_win_pct=0.0,
            avg_loss_pct=0.0,
            avg_trade_pct=0.0,
            best_trade_pct=0.0,
            worst_trade_pct=0.0,
            total_fees_usdt=total_fees,
            start_date=str(df.index[0])[:10],
            end_date=str(df.index[-1])[:10],
            n_candles=n_candles,
            symbol=symbol,
            strategy_name=strategy_name,
        )

    wins = [t for t in trade_history if t.pnl > 0]
    losses = [t for t in trade_history if t.pnl <= 0]
    win_rate = len(wins) / n_trades * 100
    gross_profit = sum(t.pnl for t in wins)
    gross_loss = abs(sum(t.pnl for t in losses))
    profit_factor = (
        gross_profit / gross_loss if gross_loss > 0 else float("inf")
    )
    pnl_pcts = [t.pnl_pct for t in trade_history]
    avg_win = (
        float(np.mean([t.pnl_pct for t in wins])) if wins else 0.0
    )
    avg_loss = (
        float(np.mean([t.pnl_pct for t in losses])) if losses else 0.0
    )
    avg_all = float(np.mean(pnl_pcts))
    best = float(max(pnl_pcts))
    worst = float(min(pnl_pcts))

    return BacktestMetrics(
        total_return_pct=round(total_return, 3),
        annualised_return_pct=round(ann_return, 3),
        max_drawdown_pct=round(abs(max_drawdown), 3),
        sharpe_ratio=round(sharpe, 4),
        calmar_ratio=round(calmar, 4),
        volatility_pct=round(vol, 3),
        total_trades=n_trades,
        win_rate_pct=round(win_rate, 2),
        profit_factor=(
            round(profit_factor, 4)
            if profit_factor != float("inf") else 9999.0
        ),
        avg_win_pct=round(avg_win, 3),
        avg_loss_pct=round(avg_loss, 3),
        avg_trade_pct=round(avg_all, 3),
        best_trade_pct=round(best, 3),
        worst_trade_pct=round(worst, 3),
        total_fees_usdt=round(total_fees, 4),
        start_date=str(df.index[0])[:10],
        end_date=str(df.index[-1])[:10],
        n_candles=n_candles,
        symbol=symbol,
        strategy_name=strategy_name,
    )


def _zero_metrics(
    symbol: str, strategy_name: str, df: pd.DataFrame, period_label: str,
) -> BacktestMetrics:
    return BacktestMetrics(
        total_return_pct=0.0, annualised_return_pct=0.0,
        max_drawdown_pct=0.0, sharpe_ratio=0.0, calmar_ratio=0.0,
        volatility_pct=0.0, total_trades=0, win_rate_pct=0.0,
        profit_factor=0.0, avg_win_pct=0.0, avg_loss_pct=0.0,
        avg_trade_pct=0.0, best_trade_pct=0.0, worst_trade_pct=0.0,
        total_fees_usdt=0.0,
        start_date=str(df.index[0])[:10] if len(df) else "N/A",
        end_date=str(df.index[-1])[:10] if len(df) else "N/A",
        n_candles=0, symbol=symbol, strategy_name=strategy_name,
    )
