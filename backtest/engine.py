"""
backtest/engine.py — Core backtesting engine.

Replays historical OHLCV candles through any BaseStrategy, simulates trade
execution with realistic fees and slippage, and computes a full suite of
performance metrics.

ARCHITECTURE
────────────
  BacktestEngine.run(df, strategy, simulator)
      │
      ├─ warm-up: feed first warm_up_candles rows to get indicators ready
      │           (no trades taken during warm-up)
      │
      └─ trading period: for each candle i in [warm_up .. len(df)):
              df_slice = df.iloc[0 : i+1]      ← growing window
              signal   = strategy.generate_signal(df_slice)
              price    = fill price (see fill model below)
              simulator.execute_signal(signal, price)
              simulator.tick_ohlcv_candle(H, L, C)  ← OHLC-accurate SL/TP
              record equity snapshot

FILL MODEL
──────────
  Default (next_candle_fill=False):
    BUY/SELL fill at signal candle's close ± slippage.
    Simple, consistent with most retail backtests.

  Realistic (next_candle_fill=True):
    BUY fills at the NEXT candle's open ± slippage.
    Rationale: the signal is generated at candle i's close, so the
    earliest realistic execution is candle i+1's open (the next bar).
    SELL/SL/TP fills remain at the signal candle — these are limit/market
    orders already placed and waiting to trigger.
    Enable this for more honest strategy evaluation; it will reduce
    apparent performance by 0.5–3% on most strategies.

SL / TP SIMULATION
──────────────────
  tick_ohlcv_candle() is used instead of tick(close) so that:
    • Stop-loss fires against candle LOW  → catches SL wicks (more fills)
    • Take-profit fires against candle HIGH → catches TP wicks (more fills)
    • Trailing SL ratchets using candle HIGH → correct peak tracking
  This is significantly more accurate than close-only tick().

SLIPPAGE MODEL
──────────────
  Market orders:  ± SLIPPAGE_MARKET (default 0.05% of price)
  Limit orders:   ± SLIPPAGE_LIMIT  (default 0.02% of price)
  Direction: buys fill slightly above signal price, sells slightly below.

OUTPUT
──────
  BacktestResult dataclass containing:
    - equity_curve: pd.Series (datetime → portfolio equity in USDT)
    - trade_history: list[TradeRecord]
    - metrics: BacktestMetrics (Sharpe, drawdown, win-rate, etc.)
"""

import math
import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import Optional
from loguru import logger

from strategies.base import BaseStrategy, Signal
from strategies.dca import DCAStrategy
from paper_trading.simulator import PaperTrading, TradeRecord


# ── Slippage constants ──────────────────────────────────────────────────────
SLIPPAGE_MARKET = 0.0005   # 0.05% — taker fills at slightly worse price
SLIPPAGE_LIMIT  = 0.0002   # 0.02% — limit fills with small adverse tick


# ── Result dataclasses ──────────────────────────────────────────────────────

@dataclass
class BacktestMetrics:
    """
    Performance summary for one backtest run.

    All return/drawdown figures are percentages (e.g. 12.5 = 12.5%).
    Sharpe ratio is annualised, assumes risk-free rate of 0.
    """
    # Return
    total_return_pct: float         # Equity return over the full period
    annualised_return_pct: float    # Annualised equivalent

    # Risk
    max_drawdown_pct: float         # Largest peak-to-trough decline
    sharpe_ratio: float             # Annualised Sharpe (rf = 0)
    calmar_ratio: float             # Ann. return / max drawdown (0 if no DD)
    volatility_pct: float           # Annualised equity-curve volatility

    # Trade stats
    total_trades: int
    win_rate_pct: float             # % of closed trades with PnL > 0
    profit_factor: float            # Gross profit / gross loss (inf if no loss)
    avg_win_pct: float              # Average winning trade return (%)
    avg_loss_pct: float             # Average losing trade return (%)
    avg_trade_pct: float            # Average trade return (%)
    best_trade_pct: float
    worst_trade_pct: float
    total_fees_usdt: float

    # Period
    start_date: str
    end_date: str
    n_candles: int
    symbol: str
    strategy_name: str


@dataclass
class BacktestResult:
    """Complete result from a single backtest run."""
    metrics: BacktestMetrics
    equity_curve: pd.Series                  # datetime → USDT equity
    trade_history: list = field(default_factory=list)
    period_label: str = "full"               # "in-sample" | "out-of-sample" | "full"


# ── Engine ──────────────────────────────────────────────────────────────────

class BacktestEngine:
    """
    Backtesting engine: replays OHLCV data through a strategy + simulator.

    Usage:
        engine = BacktestEngine(initial_balance=10_000)
        result = engine.run(df, strategy, period_label="in-sample")
        print(result.metrics)
    """

    def __init__(
        self,
        initial_balance: float = 10_000.0,
        warm_up_candles: int = 50,
        slippage_market: float = SLIPPAGE_MARKET,
        slippage_limit: float = SLIPPAGE_LIMIT,
        next_candle_fill: bool = False,
        verbose: bool = False,
    ):
        """
        Args:
            initial_balance:   Starting USDT for paper trading.
            warm_up_candles:   Candles at the start used ONLY for indicator
                               warm-up. No trades are taken during this period.
                               Should be >= max indicator lookback in your strategy.
            slippage_market:   Adverse fill % for market orders.
            slippage_limit:    Adverse fill % for limit orders.
            next_candle_fill:  If True, BUY orders fill at the NEXT candle's open
                               (+ slippage) rather than the current close. More
                               realistic but lowers apparent returns by 0.5-3%.
                               Default False to maintain backward compatibility.
            verbose:           If True, log every candle's signal. Very noisy.
        """
        self.initial_balance = initial_balance
        self.warm_up_candles = warm_up_candles
        self.slippage_market = slippage_market
        self.slippage_limit = slippage_limit
        self.next_candle_fill = next_candle_fill
        self.verbose = verbose

    # ── Public API ─────────────────────────────────────────────────────────

    def run(
        self,
        df: pd.DataFrame,
        strategy: BaseStrategy,
        period_label: str = "full",
    ) -> BacktestResult:
        """
        Run a full backtest.

        Args:
            df:            OHLCV DataFrame (full period, enough for warm-up).
            strategy:      A freshly instantiated BaseStrategy subclass.
            period_label:  Label for reporting, e.g. "in-sample".

        Returns:
            BacktestResult with equity curve, trade history, and metrics.
        """
        if len(df) < self.warm_up_candles + 10:
            raise ValueError(
                f"Not enough data: need at least {self.warm_up_candles + 10} candles, "
                f"got {len(df)}"
            )

        sim = PaperTrading(initial_balance=self.initial_balance, symbol=strategy.symbol)

        equity_curve: dict = {}
        signals_fired: list[dict] = []

        logger.info(
            f"[Backtest] Starting {period_label} run | "
            f"Strategy: {strategy.name} | Symbol: {strategy.symbol} | "
            f"Candles: {len(df)} | Warm-up: {self.warm_up_candles} | "
            f"Balance: ${self.initial_balance:,.0f}"
        )

        for i in range(self.warm_up_candles, len(df)):
            df_slice = df.iloc[: i + 1]
            candle = df_slice.iloc[-1]
            ts = df_slice.index[-1]
            close_price = float(candle["close"])
            candle_high  = float(candle["high"])
            candle_low   = float(candle["low"])

            # ── Generate signal (suppress warm-up errors gracefully) ──────
            try:
                signal = strategy.generate_signal(df_slice)
            except ValueError as e:
                # Happens right at warm-up boundary — skip quietly
                logger.debug(f"[Backtest] Warm-up skip at candle {i}: {e}")
                equity_curve[ts] = sim.get_equity(close_price)
                continue

            # ── Determine fill price ──────────────────────────────────────
            # For BUY signals with next_candle_fill=True: fill at the NEXT
            # candle's open (+ slippage). This is more realistic because the
            # signal fires at the close; the earliest real fill is the next bar.
            # For all other signals and when next_candle_fill=False: fill at
            # the current candle's close + slippage (backward-compatible).
            if (
                self.next_candle_fill
                and signal.action == "BUY"
                and i + 1 < len(df)
            ):
                next_open = float(df.iloc[i + 1]["open"])
                slip = self.slippage_market if signal.order_type == "market" else self.slippage_limit
                fill_price = next_open * (1.0 + slip)
            else:
                fill_price = self._apply_slippage(signal, close_price)

            # ── Execute in simulator ──────────────────────────────────────
            if signal.action != "HOLD":
                if self.verbose:
                    logger.debug(f"[Backtest] Candle {i} | {signal}")
                sim.execute_signal(signal, fill_price)

            # ── Tick: OHLCV-accurate SL / TP / trail / time-exit ─────────
            # Using tick_ohlcv_candle instead of tick(close) means:
            #   • SL checks against candle LOW  → catches wick stop-outs
            #   • TP checks against candle HIGH → catches wick take-profits
            #   • TSL ratchets via candle HIGH  → correct peak for trailing SL
            # This removes the close-only optimism bias from the backtest.
            sim.tick_ohlcv_candle(
                high=candle_high,
                low=candle_low,
                close=close_price,
            )

            # ── DCA state sync (mirrors main.py logic) ────────────────────
            if isinstance(strategy, DCAStrategy):
                strategy.sync_state(simulator_has_position=sim.position is not None)

            # ── Record equity ─────────────────────────────────────────────
            equity_curve[ts] = sim.get_equity(close_price)

            if signal.action != "HOLD":
                signals_fired.append({
                    "ts": ts,
                    "action": signal.action,
                    "price": fill_price,
                    "reason": signal.reason,
                })

        # ── Force-close any open position at last candle ──────────────────
        if sim.position is not None:
            last_price = float(df["close"].iloc[-1])
            logger.info(
                f"[Backtest] Closing open position at end of period @ {last_price:.4f}"
            )
            sim._handle_full_sell(None, last_price, "backtest_end", order_type="market")
            # Update last equity entry
            if equity_curve:
                last_ts = list(equity_curve.keys())[-1]
                equity_curve[last_ts] = sim.get_equity(last_price)

        equity_series = pd.Series(equity_curve)

        metrics = self._compute_metrics(
            equity_series=equity_series,
            trade_history=sim.trade_history,
            total_fees=sim.total_fees_paid,
            symbol=strategy.symbol,
            strategy_name=strategy.name,
            df=df,
            period_label=period_label,
        )

        logger.info(
            f"[Backtest] {period_label} complete | "
            f"Return: {metrics.total_return_pct:+.2f}% | "
            f"MaxDD: {metrics.max_drawdown_pct:.2f}% | "
            f"Sharpe: {metrics.sharpe_ratio:.3f} | "
            f"Trades: {metrics.total_trades} | "
            f"WinRate: {metrics.win_rate_pct:.1f}%"
        )

        return BacktestResult(
            metrics=metrics,
            equity_curve=equity_series,
            trade_history=sim.trade_history,
            period_label=period_label,
        )

    # ── Slippage ────────────────────────────────────────────────────────────

    def _apply_slippage(self, signal: Signal, price: float) -> float:
        """
        Return a realistic fill price with adverse slippage.

        Buys fill slightly above close, sells slightly below close.
        Market orders get more slippage than limit orders.
        """
        if signal.action == "HOLD":
            return price

        slip_pct = (
            self.slippage_market
            if signal.order_type == "market"
            else self.slippage_limit
        )

        if signal.action == "BUY":
            return price * (1 + slip_pct)
        else:  # SELL
            return price * (1 - slip_pct)

    # ── Metrics ─────────────────────────────────────────────────────────────

    def _compute_metrics(
        self,
        equity_series: pd.Series,
        trade_history: list,
        total_fees: float,
        symbol: str,
        strategy_name: str,
        df: pd.DataFrame,
        period_label: str,
    ) -> BacktestMetrics:
        """Compute all performance metrics from equity curve and trade history."""

        if equity_series.empty:
            # Degenerate case — return zeros
            return self._zero_metrics(symbol, strategy_name, df, period_label)

        # ── Return metrics ────────────────────────────────────────────────
        start_equity = self.initial_balance
        end_equity   = float(equity_series.iloc[-1])
        total_return = (end_equity - start_equity) / start_equity * 100

        # Annualise: work out fraction of a year covered
        n_candles = len(equity_series)
        candle_duration_h = self._infer_candle_hours(df)
        years = (n_candles * candle_duration_h) / (365.25 * 24)
        if years > 0:
            ann_return = ((end_equity / start_equity) ** (1 / years) - 1) * 100
        else:
            ann_return = 0.0

        # ── Drawdown ──────────────────────────────────────────────────────
        running_max = equity_series.cummax()
        drawdown = (equity_series - running_max) / running_max * 100
        max_drawdown = float(drawdown.min())  # negative number, e.g. -12.5

        # ── Volatility & Sharpe ───────────────────────────────────────────
        returns = equity_series.pct_change().dropna()
        candles_per_year = (365.25 * 24) / candle_duration_h
        vol = float(returns.std() * math.sqrt(candles_per_year)) * 100  # %

        if vol > 0:
            # Annualised Sharpe = (ann_return - 0) / ann_vol
            sharpe = ann_return / vol
        else:
            sharpe = 0.0

        # ── Calmar ────────────────────────────────────────────────────────
        calmar = ann_return / abs(max_drawdown) if max_drawdown != 0 else 0.0

        # ── Trade statistics ──────────────────────────────────────────────
        all_trades = trade_history
        n_trades = len(all_trades)

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

        wins   = [t for t in all_trades if t.pnl > 0]
        losses = [t for t in all_trades if t.pnl <= 0]

        win_rate = len(wins) / n_trades * 100

        gross_profit = sum(t.pnl for t in wins)
        gross_loss   = abs(sum(t.pnl for t in losses))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        pnl_pcts = [t.pnl_pct for t in all_trades]
        avg_win  = float(np.mean([t.pnl_pct for t in wins])) if wins else 0.0
        avg_loss = float(np.mean([t.pnl_pct for t in losses])) if losses else 0.0
        avg_all  = float(np.mean(pnl_pcts))
        best     = float(max(pnl_pcts))
        worst    = float(min(pnl_pcts))

        return BacktestMetrics(
            total_return_pct=round(total_return, 3),
            annualised_return_pct=round(ann_return, 3),
            max_drawdown_pct=round(abs(max_drawdown), 3),
            sharpe_ratio=round(sharpe, 4),
            calmar_ratio=round(calmar, 4),
            volatility_pct=round(vol, 3),
            total_trades=n_trades,
            win_rate_pct=round(win_rate, 2),
            profit_factor=round(profit_factor, 4) if profit_factor != float("inf") else 9999.0,
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

    def _infer_candle_hours(self, df: pd.DataFrame) -> float:
        """Estimate candle duration in hours from the DataFrame's index."""
        if len(df) < 2:
            return 1.0
        delta = df.index[1] - df.index[0]
        hours = delta.total_seconds() / 3600
        return max(hours, 1 / 60)  # floor at 1 minute

    def _zero_metrics(
        self, symbol: str, strategy_name: str, df: pd.DataFrame, period_label: str
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


# ── Walk-Forward Backtester ──────────────────────────────────────────────────

@dataclass
class WalkForwardWindow:
    """One walk-forward window result."""
    window_idx:   int
    train_start:  str
    train_end:    str
    test_start:   str
    test_end:     str
    train_result: BacktestResult
    test_result:  BacktestResult


@dataclass
class WalkForwardReport:
    """
    Aggregated results across all walk-forward windows.

    Key stability checks:
      - Is the test Sharpe consistently positive?
      - Is the test win-rate close to the train win-rate (no overfitting)?
      - Is max drawdown across test windows acceptable?
    """
    strategy_name: str
    symbol:        str
    windows:       list[WalkForwardWindow]

    # Aggregate test metrics (mean across windows)
    mean_test_return_pct:  float
    mean_test_sharpe:      float
    mean_test_drawdown_pct: float
    mean_test_win_rate:    float
    pct_windows_profitable: float   # % of test windows with positive return

    def summary(self) -> str:
        lines = [
            "═" * 62,
            f"  WALK-FORWARD BACKTEST  |  {self.strategy_name}  |  {self.symbol}",
            "═" * 62,
            f"  Windows run          : {len(self.windows)}",
            f"  Mean test return     : {self.mean_test_return_pct:+.2f}%",
            f"  Mean test Sharpe     : {self.mean_test_sharpe:.3f}",
            f"  Mean test max DD     : {self.mean_test_drawdown_pct:.2f}%",
            f"  Mean test win rate   : {self.mean_test_win_rate:.1f}%",
            f"  Profitable windows   : {self.pct_windows_profitable:.0f}%",
            "─" * 62,
            f"  {'Window':<8} {'Train Period':<24} {'Test Period':<24} {'T.Ret':>7} {'T.Sharpe':>9}",
            "─" * 62,
        ]
        for w in self.windows:
            tr = w.test_result.metrics.total_return_pct
            ts = w.test_result.metrics.sharpe_ratio
            sign = "+" if tr >= 0 else ""
            lines.append(
                f"  {w.window_idx:<8} "
                f"{w.train_start[:10]}–{w.train_end[:10]}  "
                f"{w.test_start[:10]}–{w.test_end[:10]}  "
                f"{sign}{tr:>6.2f}%  {ts:>8.3f}"
            )
        lines.append("═" * 62)
        return "\n".join(lines)


class WalkForwardBacktester:
    """
    Walk-forward (rolling-window) backtester.

    Splits the full OHLCV data into N rolling windows. For each window:
      - Train period: first train_pct% of the window (for indicator warm-up)
      - Test  period: last  test_pct% of the window  (out-of-sample performance)

    The strategy instance is RE-CREATED fresh for each window via strategy_factory
    to eliminate any state leakage between windows.

    WHY THIS MATTERS
    ────────────────
    A single backtest can look great simply because the strategy's parameters
    happened to suit that specific period. Walk-forward testing reveals whether
    the strategy *consistently* works across many market conditions —
    the hallmark of a robust, non-overfit system.

    USAGE
    ─────
        def make_dca():
            return DCAStrategy(deviation_pct=2.0, safety_scale=1.5)

        wf = WalkForwardBacktester(
            strategy_factory=make_dca,
            initial_balance=10_000,
            n_windows=6,
            train_pct=0.70,
        )
        report = wf.run(df)
        print(report.summary())
    """

    def __init__(
        self,
        strategy_factory,          # Callable[[], BaseStrategy] — called fresh each window
        initial_balance: float = 10_000.0,
        n_windows: int = 6,        # Number of rolling windows to test
        train_pct: float = 0.70,   # % of each window used for training
        warm_up_candles: int = 50,
        verbose: bool = False,
    ):
        """
        Args:
            strategy_factory:  Zero-arg callable that returns a fresh strategy instance.
                               e.g. lambda: DCAStrategy(deviation_pct=2.0)
            initial_balance:   Starting capital for each window's paper simulator.
            n_windows:         How many rolling windows to run. More = slower but more robust.
            train_pct:         Fraction of each window that is training data (e.g. 0.70 = 70%).
                               The remaining (1 - train_pct) is the out-of-sample test.
            warm_up_candles:   Passed to BacktestEngine.
            verbose:           Log every candle signal (very noisy).
        """
        self.strategy_factory  = strategy_factory
        self.initial_balance   = initial_balance
        self.n_windows         = n_windows
        self.train_pct         = train_pct
        self.warm_up_candles   = warm_up_candles
        self.verbose           = verbose
        self._engine           = BacktestEngine(
            initial_balance=initial_balance,
            warm_up_candles=warm_up_candles,
            verbose=verbose,
        )

    def run(self, df: pd.DataFrame) -> WalkForwardReport:
        """
        Execute the walk-forward test over the full dataset.

        Args:
            df: Full OHLCV DataFrame (must be long enough for n_windows windows).

        Returns:
            WalkForwardReport with per-window and aggregate metrics.
        """
        n = len(df)
        window_size = n // self.n_windows

        if window_size < self.warm_up_candles + 20:
            raise ValueError(
                f"Dataset too short for {self.n_windows} windows. "
                f"Each window would be only {window_size} candles "
                f"(need at least {self.warm_up_candles + 20}). "
                f"Reduce n_windows or provide more data."
            )

        windows: list[WalkForwardWindow] = []

        for i in range(self.n_windows):
            start_idx = i * window_size
            end_idx   = start_idx + window_size if i < self.n_windows - 1 else n

            window_df  = df.iloc[start_idx:end_idx]
            split_idx  = int(len(window_df) * self.train_pct)

            train_df   = window_df.iloc[:split_idx]
            test_df    = window_df.iloc[split_idx:]

            if len(train_df) < self.warm_up_candles + 10 or len(test_df) < 10:
                logger.warning(f"[WalkForward] Window {i+1}: too small — skipping.")
                continue

            logger.info(
                f"[WalkForward] Window {i+1}/{self.n_windows} | "
                f"Train: {str(train_df.index[0])[:10]}–{str(train_df.index[-1])[:10]} "
                f"({len(train_df)} candles) | "
                f"Test:  {str(test_df.index[0])[:10]}–{str(test_df.index[-1])[:10]} "
                f"({len(test_df)} candles)"
            )

            # Fresh strategy instance per window — no state leakage
            train_strategy = self.strategy_factory()
            test_strategy  = self.strategy_factory()

            try:
                train_result = self._engine.run(train_df, train_strategy, period_label=f"train-w{i+1}")
            except Exception as exc:
                logger.warning(f"[WalkForward] Window {i+1} train failed: {exc}")
                continue

            try:
                test_result  = self._engine.run(test_df,  test_strategy,  period_label=f"test-w{i+1}")
            except Exception as exc:
                logger.warning(f"[WalkForward] Window {i+1} test failed: {exc}")
                continue

            windows.append(WalkForwardWindow(
                window_idx  = i + 1,
                train_start = str(train_df.index[0])[:10],
                train_end   = str(train_df.index[-1])[:10],
                test_start  = str(test_df.index[0])[:10],
                test_end    = str(test_df.index[-1])[:10],
                train_result = train_result,
                test_result  = test_result,
            ))

        if not windows:
            raise RuntimeError("Walk-forward test produced no valid windows.")

        # ── Aggregate test metrics ────────────────────────────────────────────
        test_returns  = [w.test_result.metrics.total_return_pct for w in windows]
        test_sharpes  = [w.test_result.metrics.sharpe_ratio     for w in windows]
        test_dds      = [w.test_result.metrics.max_drawdown_pct for w in windows]
        test_winrates = [w.test_result.metrics.win_rate_pct     for w in windows]

        strategy_obj = self.strategy_factory()
        report = WalkForwardReport(
            strategy_name          = strategy_obj.name,
            symbol                 = strategy_obj.symbol,
            windows                = windows,
            mean_test_return_pct   = round(float(np.mean(test_returns)), 3),
            mean_test_sharpe       = round(float(np.mean(test_sharpes)), 4),
            mean_test_drawdown_pct = round(float(np.mean(test_dds)), 3),
            mean_test_win_rate     = round(float(np.mean(test_winrates)), 2),
            pct_windows_profitable = round(
                sum(1 for r in test_returns if r > 0) / len(test_returns) * 100, 1
            ),
        )

        logger.info(f"\n{report.summary()}")
        return report
