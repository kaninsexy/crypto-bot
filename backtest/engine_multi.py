"""
backtest/engine_multi.py — Multi-asset daily backtest engine (Phase 4.A).

Long-only basket replay for the TrendFollowing daily multi-asset
substrate.  Each bar:

  1. Slice every symbol's OHLCV up to bar `i` (synchronised on the
     timestamp intersection so all per-symbol slices share length).
  2. Call `strategy.generate_signals(prices_through_i)` once per bar.
  3. For each symbol:
       - BUY  + no position open  → open long at close, size by
                                    `strategy.position_fraction`
                                    × portfolio equity.
       - SELL + position open     → close long at close.
       - Otherwise                → no-op.
  4. Mark-to-market every open position at the bar's close.
  5. Record portfolio equity = cash + Σ(qty_i × close_i).

The engine is the strategy-agnostic replay loop.  Vol-targeting,
per-symbol concentration caps, and the formation-window history check
are all owned by `TrendFollowingMultiStrategy.position_fraction`,
exactly the same separation as `backtest.engine.BacktestEngine` / the
single-symbol strategies.

Paper-mode invariant
────────────────────
This module performs no I/O.  All data is supplied by the caller
(via `holdout.load_dev` → pivoted dict, or via the smoke script's
direct manifest read).  No real OKX API calls are ever issued —
`paper_mode=True` is a structural property of the backtest engines.

Per-bar return shape contract
─────────────────────────────
`BacktestResult.equity_curve.pct_change().dropna()` is the per-bar
return array consumed by `backtest.cpcv_common._sharpe_from_returns`
without modification — same contract as the spot `BacktestEngine.run`
and the perp `engine_perp.run_perp`.  This is what makes
`backtest.cpcv_multi` shape-compatible with the existing DSR /
verdict-tree plumbing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd
from loguru import logger

from backtest.engine import BacktestMetrics, BacktestResult, SLIPPAGE_MARKET
from strategies.trend_following_multi import TrendFollowingMultiStrategy


# ── Defaults ─────────────────────────────────────────────────────────────────

DEFAULT_INITIAL_BALANCE: float = 10_000.0
DEFAULT_WARM_UP_CANDLES: int = 0  # warmup is implicit in the formation window


# ── Trade record (light) ─────────────────────────────────────────────────────

@dataclass
class MultiAssetTrade:
    """Closed-trade record for the multi-asset engine.

    Compatible field shape with `paper_trading.simulator.TradeRecord`
    so downstream metrics (win-rate, profit-factor, avg win/loss) read
    the same attributes the single-asset engine produces.
    """
    symbol: str
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry_price: float
    exit_price: float
    quantity: float
    pnl: float
    pnl_pct: float
    fees: float = 0.0
    reason: str = ""


# ── Public API ───────────────────────────────────────────────────────────────

def run_engine_multi(
    data: dict[str, pd.DataFrame],
    strategy: TrendFollowingMultiStrategy,
    *,
    period_label: str = "full",
    initial_balance: float = DEFAULT_INITIAL_BALANCE,
    slippage_market: float = SLIPPAGE_MARKET,
    target_vol_annual: Optional[float] = None,
) -> BacktestResult:
    """Replay a multi-asset basket through `strategy`.

    Args:
        data:             `{symbol: OHLCV DataFrame}` keyed by every
                          symbol in `strategy.symbols`.  Each frame is
                          UTC-indexed with the canonical
                          `[open, high, low, close, volume]` columns.
                          Frames may have different lengths;
                          synchronisation uses the timestamp
                          intersection so each per-bar step has a
                          consistent universe slice.
        strategy:         A pre-instantiated TrendFollowingMultiStrategy.
        period_label:     Provenance string copied into BacktestResult.
        initial_balance:  Starting USDT cash.
        slippage_market:  Adverse fill % applied to every market-order
                          execution (per-bar close ± slippage).
        target_vol_annual: Optional override for `strategy.target_vol_annual`.
                          When supplied, replaces the strategy-level
                          target before per-bar sizing — used by
                          callers that want to vol-target without
                          mutating the underlying strategy instance.

    Returns:
        BacktestResult with equity_curve (pd.Series indexed on the
        synchronised timeline), trade_history (list[MultiAssetTrade]),
        and BacktestMetrics computed over the equity curve.

    Raises:
        ValueError: `data` has no symbols in common with
                    `strategy.symbols`, the timestamp intersection is
                    empty, or no symbol has at least
                    `strategy.lookback_days + 2` bars.
    """
    if target_vol_annual is not None:
        strategy.target_vol_annual = float(target_vol_annual)

    # 1. Restrict to symbols both the strategy and the data agree on.
    active_symbols: list[str] = [s for s in strategy.symbols if s in data]
    if not active_symbols:
        raise ValueError(
            f"No overlap between strategy.symbols={strategy.symbols!r} "
            f"and data keys {sorted(data.keys())!r}."
        )

    # 2. Build the synchronised bar timeline as the intersection of
    #    per-symbol indices.  This mirrors `cpcv_common._split_blocks_multi`
    #    so per-bar engine state stays universe-aligned.
    common_idx: Optional[pd.Index] = None
    for sym in active_symbols:
        idx = data[sym].sort_index().index
        common_idx = idx if common_idx is None else common_idx.intersection(idx)
    if common_idx is None or len(common_idx) == 0:
        raise ValueError(
            f"No common timestamps across symbols {active_symbols!r}."
        )
    common_idx = common_idx.sort_values()

    # Pre-slice each symbol to the synchronised index for O(1) iloc.
    aligned: dict[str, pd.DataFrame] = {
        sym: data[sym].sort_index().loc[common_idx]
        for sym in active_symbols
    }

    # 3. State.
    cash: float = float(initial_balance)
    positions: dict[str, dict] = {}  # symbol -> {"qty", "entry_price", "entry_time"}
    trade_history: list[MultiAssetTrade] = []
    equity_curve: dict[pd.Timestamp, float] = {}

    n_basket = len(strategy.symbols)
    if n_basket <= 0:
        raise ValueError("strategy.symbols is empty")

    min_history_bars = strategy.lookback_days + 2  # ret needs lookback+1 closes

    logger.info(
        f"[EngineMulti] Starting {period_label} | strategy={strategy.name} | "
        f"symbols={len(active_symbols)}/{n_basket} aligned | "
        f"bars={len(common_idx)} | balance=${initial_balance:,.0f}"
    )

    for i, ts in enumerate(common_idx):
        # 3a. Build per-symbol slices ending at bar i (inclusive).
        prices_through_i: dict[str, pd.DataFrame] = {
            sym: aligned[sym].iloc[: i + 1]
            for sym in active_symbols
        }

        # 3b. Skip signal generation while no symbol has enough history.
        if i + 1 < min_history_bars:
            equity_curve[ts] = _mark_to_market(cash, positions, aligned, i)
            continue

        # 3c. Strategy decides per-symbol action.
        signals = strategy.generate_signals(prices_through_i)

        # Recompute equity BEFORE acting so sizing is on the latest MTM.
        equity_now = _mark_to_market(cash, positions, aligned, i)

        # 3d. Process closes first so their cash flows back in before opens.
        for sym in active_symbols:
            sig = signals.get(sym)
            if sig is None:
                continue
            held = positions.get(sym)
            if sig.action == "SELL" and held is not None:
                close_price = float(aligned[sym].iloc[i]["close"])
                fill = close_price * (1.0 - slippage_market)
                qty = held["qty"]
                proceeds = qty * fill
                pnl = proceeds - (qty * held["entry_price"])
                pnl_pct = (
                    (fill / held["entry_price"] - 1.0) * 100.0
                    if held["entry_price"] > 0 else 0.0
                )
                cash += proceeds
                trade_history.append(MultiAssetTrade(
                    symbol=sym,
                    entry_time=held["entry_time"],
                    exit_time=ts,
                    entry_price=held["entry_price"],
                    exit_price=fill,
                    quantity=qty,
                    pnl=pnl,
                    pnl_pct=pnl_pct,
                    reason=sig.reason or "tsmom-exit",
                ))
                positions.pop(sym, None)

        # 3e. Process opens after closes so per-bar capital reflects exits.
        equity_after_closes = _mark_to_market(cash, positions, aligned, i)
        for sym in active_symbols:
            sig = signals.get(sym)
            if sig is None:
                continue
            if sig.action != "BUY" or sym in positions:
                continue
            df_sym = prices_through_i[sym]
            frac = strategy.position_fraction(
                df_sym, n_active=n_basket,
            )
            if frac <= 0 or equity_after_closes <= 0:
                continue
            target_notional = frac * equity_after_closes
            target_notional = min(target_notional, cash)
            if target_notional <= 0:
                continue
            close_price = float(aligned[sym].iloc[i]["close"])
            fill = close_price * (1.0 + slippage_market)
            if fill <= 0:
                continue
            qty = target_notional / fill
            cash -= qty * fill
            positions[sym] = {
                "qty": qty,
                "entry_price": fill,
                "entry_time": ts,
            }

        # 3f. Snapshot equity AFTER all per-bar actions.
        equity_curve[ts] = _mark_to_market(cash, positions, aligned, i)

    # 4. Force-close any open positions at the final bar so the metrics
    #    reflect realised pnl rather than carrying paper open positions.
    if positions and len(common_idx) > 0:
        last_i = len(common_idx) - 1
        last_ts = common_idx[last_i]
        for sym, held in list(positions.items()):
            close_price = float(aligned[sym].iloc[last_i]["close"])
            fill = close_price * (1.0 - slippage_market)
            qty = held["qty"]
            proceeds = qty * fill
            pnl = proceeds - (qty * held["entry_price"])
            pnl_pct = (
                (fill / held["entry_price"] - 1.0) * 100.0
                if held["entry_price"] > 0 else 0.0
            )
            cash += proceeds
            trade_history.append(MultiAssetTrade(
                symbol=sym,
                entry_time=held["entry_time"],
                exit_time=last_ts,
                entry_price=held["entry_price"],
                exit_price=fill,
                quantity=qty,
                pnl=pnl,
                pnl_pct=pnl_pct,
                reason="backtest_end",
            ))
            positions.pop(sym, None)
        equity_curve[last_ts] = cash

    equity_series = pd.Series(equity_curve)
    metrics = _compute_metrics(
        equity_series=equity_series,
        trade_history=trade_history,
        symbol="basket",
        strategy_name=strategy.name,
        index=common_idx,
        period_label=period_label,
        initial_balance=initial_balance,
    )

    logger.info(
        f"[EngineMulti] {period_label} done | "
        f"return={metrics.total_return_pct:+.2f}% | "
        f"sharpe={metrics.sharpe_ratio:.3f} | "
        f"trades={metrics.total_trades} | "
        f"final_cash=${cash:,.2f}"
    )

    return BacktestResult(
        metrics=metrics,
        equity_curve=equity_series,
        trade_history=trade_history,
        period_label=period_label,
    )


# ── Helpers ──────────────────────────────────────────────────────────────────

def _mark_to_market(
    cash: float,
    positions: dict[str, dict],
    aligned: dict[str, pd.DataFrame],
    i: int,
) -> float:
    total = float(cash)
    for sym, p in positions.items():
        close_i = float(aligned[sym].iloc[i]["close"])
        total += p["qty"] * close_i
    return total


def _infer_candle_hours(index: pd.Index) -> float:
    if len(index) < 2:
        return 1.0
    delta = index[1] - index[0]
    hours = delta.total_seconds() / 3600
    return max(hours, 1 / 60)


def _compute_metrics(
    equity_series: pd.Series,
    trade_history: list[MultiAssetTrade],
    symbol: str,
    strategy_name: str,
    index: pd.Index,
    period_label: str,
    initial_balance: float,
) -> BacktestMetrics:
    """Same shape and formula as `backtest.engine._compute_metrics`."""
    if equity_series.empty:
        return _zero_metrics(symbol, strategy_name, index, period_label)

    start_equity = initial_balance
    end_equity = float(equity_series.iloc[-1])
    total_return = (end_equity - start_equity) / start_equity * 100

    n_candles = len(equity_series)
    candle_duration_h = _infer_candle_hours(index)
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
            total_fees_usdt=0.0,
            start_date=str(index[0])[:10],
            end_date=str(index[-1])[:10],
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
        total_fees_usdt=0.0,
        start_date=str(index[0])[:10],
        end_date=str(index[-1])[:10],
        n_candles=n_candles,
        symbol=symbol,
        strategy_name=strategy_name,
    )


def _zero_metrics(
    symbol: str, strategy_name: str, index: pd.Index, period_label: str,
) -> BacktestMetrics:
    return BacktestMetrics(
        total_return_pct=0.0, annualised_return_pct=0.0,
        max_drawdown_pct=0.0, sharpe_ratio=0.0, calmar_ratio=0.0,
        volatility_pct=0.0, total_trades=0, win_rate_pct=0.0,
        profit_factor=0.0, avg_win_pct=0.0, avg_loss_pct=0.0,
        avg_trade_pct=0.0, best_trade_pct=0.0, worst_trade_pct=0.0,
        total_fees_usdt=0.0,
        start_date=str(index[0])[:10] if len(index) else "N/A",
        end_date=str(index[-1])[:10] if len(index) else "N/A",
        n_candles=0, symbol=symbol, strategy_name=strategy_name,
    )
