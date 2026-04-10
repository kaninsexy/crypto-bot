"""
paper_trading/simulator.py — Paper trading engine.

Simulates trade execution without real money. Handles:
  - Standard BUY/SELL positions
  - Tranche/partial exits (quantity_pct < 1.0)
  - DCA-style multi-entry positions (accumulate quantity across buys)
  - Trailing take-profit tracking
  - Panic protection (requires 2 SL closes)
  - Time-based exit (max_hold_candles)
  - Binance fee simulation (0.02% limit / 0.04% market)
  - Compound profit tracking
  - Isolated margin accounting for futures
"""

from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime, timezone
import pandas as pd
from loguru import logger

import config
from strategies.base import Signal


# Binance fee rates
FEE_LIMIT  = 0.0002   # 0.02% maker (limit orders)
FEE_MARKET = 0.0004   # 0.04% taker (market orders)


@dataclass
class Position:
    """
    An open trading position. Supports multi-entry (DCA-style)
    where quantity accumulates across multiple BUY signals.
    """
    symbol: str
    side: str                          # "long" or "short"
    quantity: float                    # Total asset quantity held
    avg_entry_price: float             # Weighted avg of all entries
    total_cost: float                  # Total USDT deployed
    entry_time: datetime
    strategy: str

    # Risk controls (updated by latest signal)
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    trailing_tp: bool = False
    trail_pct: float = 0.02
    panic_protection: bool = False
    max_hold_candles: int = 0
    entry_candle: int = 0              # For time-based exit

    # Trailing stop-loss (ratchets SL up as price rises)
    trailing_sl: bool = False
    trail_sl_pct: float = 0.03              # 3% below peak price

    # Trailing state
    peak_price: float = 0.0
    sl_breach_count: int = 0

    # Futures
    leverage: int = 1
    is_short: bool = False
    margin_allocated: float = 0.0     # USDT locked as isolated margin

    def unrealized_pnl(self, price: float) -> float:
        if self.is_short:
            return (self.avg_entry_price - price) * self.quantity
        return (price - self.avg_entry_price) * self.quantity

    def unrealized_pnl_pct(self, price: float) -> float:
        return self.unrealized_pnl(price) / self.total_cost * 100

    def add_entry(self, price: float, quantity: float, cost: float):
        """Accumulate another buy into this position (DCA safety order)."""
        total_qty = self.quantity + quantity
        self.avg_entry_price = (self.total_cost + cost) / total_qty
        self.quantity = total_qty
        self.total_cost += cost
        if price > self.peak_price:
            self.peak_price = price


@dataclass
class TradeRecord:
    """A completed (or partial) trade exit."""
    symbol: str
    strategy: str
    side: str
    entry_price: float
    exit_price: float
    quantity: float
    cost: float
    entry_time: datetime
    exit_time: datetime
    pnl: float
    pnl_pct: float
    fees_paid: float
    exit_reason: str
    is_partial: bool = False
    order_type: str = "limit"
    compounded: bool = False


class PaperTrading:
    """
    Paper trading simulator with full DCA, tranche exits, fee simulation,
    trailing TP, panic protection, and time-based exit support.
    """

    def __init__(self, initial_balance: float = None, symbol: str = None):
        self.initial_balance = initial_balance if initial_balance is not None else config.PAPER_BALANCE
        self.balance: float = self.initial_balance
        self.position: Optional[Position] = None
        self.trade_history: list[TradeRecord] = []
        self.compounded_profit: float = 0.0    # Profit earmarked for reinvestment
        self.total_fees_paid: float = 0.0
        self._candle_count: int = 0
        self.symbol = symbol or config.TRADING_PAIR

        logger.info(
            f"Paper trading started | Balance: ${self.initial_balance:,.2f} USDT | "
            f"Fees: {FEE_LIMIT*100:.2f}% limit / {FEE_MARKET*100:.2f}% market"
        )

    # ── Main execution entry point ────────────────────────────────────────────

    def execute_signal(self, signal: Signal, current_price: float) -> None:
        """
        Process a signal and update the virtual portfolio.

        Handles:
          - New BUY: open position or add to existing (DCA accumulation)
          - Partial SELL (quantity_pct < 1.0): tranche exit
          - Full SELL (quantity_pct == 1.0): close entire position
          - HOLD: update trailing peak price only
        """
        self._candle_count += 1

        if signal.action == "BUY":
            self._handle_buy(signal, current_price)

        elif signal.action == "SELL":
            if signal.quantity_pct < 1.0:
                self._handle_partial_sell(signal, current_price)
            else:
                self._handle_full_sell(signal, current_price, signal.reason)

        else:  # HOLD
            if self.position and current_price > self.position.peak_price:
                self.position.peak_price = current_price

    def tick(self, current_price: float) -> None:
        """
        Call on every candle close (even when strategy says HOLD) to:
          - Update trailing peak
          - Check stop-loss with panic protection
          - Check time-based exit
          - Check trailing TP
        """
        if self.position is None:
            return

        # Update peak for trailing TP/SL
        if current_price > self.position.peak_price:
            self.position.peak_price = current_price

        # Trailing Stop Loss — ratchet SL up as price rises
        # SL is always (peak × (1 − trail_sl_pct)), never moves down
        if self.position.trailing_sl and self.position.peak_price > 0:
            new_sl = self.position.peak_price * (1 - self.position.trail_sl_pct)
            if self.position.stop_loss is None or new_sl > self.position.stop_loss:
                old_sl = self.position.stop_loss
                self.position.stop_loss = new_sl
                if old_sl is None or abs(new_sl - old_sl) / old_sl > 0.001:
                    logger.debug(
                        f"[PAPER] TrailSL ratchet: "
                        f"peak={self.position.peak_price:.4f} → "
                        f"SL {(old_sl or 0):.4f} → {new_sl:.4f} "
                        f"({self.position.trail_sl_pct*100:.1f}% below peak)"
                    )

        # Stop-loss check with panic protection
        if self.position.stop_loss and current_price <= self.position.stop_loss:
            self.position.sl_breach_count += 1
            if not self.position.panic_protection or self.position.sl_breach_count >= 2:
                logger.warning(
                    f"[PAPER] SL triggered @ {current_price:.4f} "
                    f"(SL={self.position.stop_loss:.4f}) | "
                    f"{'Panic confirmed (2nd close)' if self.position.panic_protection else 'Direct stop'}"
                )
                self._handle_full_sell(None, current_price, "stop_loss", order_type="market")
            else:
                logger.warning(
                    f"[PAPER] SL breach #1 @ {current_price:.4f} — "
                    f"panic protection waiting for 2nd close"
                )
            return
        else:
            self.position.sl_breach_count = 0

        # Take-profit check
        if self.position.take_profit and current_price >= self.position.take_profit:
            logger.info(f"[PAPER] TP hit @ {current_price:.4f} (TP={self.position.take_profit:.4f})")
            self._handle_full_sell(None, current_price, "take_profit")
            return

        # Trailing TP check
        if self.position.trailing_tp and self.position.peak_price > 0:
            trail_threshold = self.position.peak_price * (1 - self.position.trail_pct)
            if current_price <= trail_threshold and current_price > self.position.avg_entry_price:
                logger.info(
                    f"[PAPER] Trailing TP: peak={self.position.peak_price:.4f} | "
                    f"threshold={trail_threshold:.4f} | current={current_price:.4f}"
                )
                self._handle_full_sell(None, current_price, "trailing_tp")
                return

        # Time-based exit at break-even or better
        if self.position.max_hold_candles > 0:
            held = self._candle_count - self.position.entry_candle
            if held >= self.position.max_hold_candles:
                if current_price >= self.position.avg_entry_price:
                    logger.info(
                        f"[PAPER] Time exit after {held} candles @ {current_price:.4f} | "
                        f"Break-even or better"
                    )
                    self._handle_full_sell(None, current_price, "time_exit")

    # ── Buy handling ──────────────────────────────────────────────────────────

    def _handle_buy(self, signal: Signal, price: float) -> None:
        """Open new position or add to existing DCA position."""
        fee_rate = FEE_LIMIT if signal.order_type == "limit" else FEE_MARKET

        if self.position is None:
            # New position — honour explicit amount_usdt if strategy provided one
            if "amount_usdt" in signal.metadata:
                # Strategy (e.g. DCA) dictates the exact USDT spend
                cost = min(signal.metadata["amount_usdt"], self.balance)
                quantity = cost / price
            elif signal.stop_loss and signal.stop_loss < price:
                # Risk-based sizing: risk only MAX_RISK_PER_TRADE of balance
                risk_per_unit = price - signal.stop_loss
                risk_amount = self.balance * config.MAX_RISK_PER_TRADE
                quantity = risk_amount / risk_per_unit
                cost = quantity * price
                if cost > self.balance:
                    quantity = self.balance / price
                    cost = self.balance
            else:
                cost = self.balance * config.MAX_RISK_PER_TRADE
                quantity = cost / price

            if quantity <= 0 or cost > self.balance:
                logger.warning(f"[PAPER] Cannot open position: insufficient balance ${self.balance:.2f}")
                return

            fee = cost * fee_rate
            cost_with_fee = cost + fee
            if cost_with_fee > self.balance:
                cost_with_fee = self.balance
                cost = cost_with_fee / (1 + fee_rate)
                quantity = cost / price
                fee = cost * fee_rate

            self.balance -= cost_with_fee
            self.total_fees_paid += fee

            # For futures: allocate isolated margin = cost / leverage
            margin = cost / signal.leverage if signal.leverage > 1 else cost

            self.position = Position(
                symbol=self.symbol,
                strategy=signal.strategy,
                side="short" if signal.is_short else "long",
                quantity=quantity,
                avg_entry_price=price,
                total_cost=cost,
                entry_time=datetime.now(timezone.utc),
                stop_loss=signal.stop_loss,
                take_profit=signal.take_profit,
                trailing_tp=signal.trailing_tp,
                trail_pct=signal.trail_pct,
                trailing_sl=signal.trailing_sl,
                trail_sl_pct=signal.trail_sl_pct,
                panic_protection=signal.panic_protection,
                max_hold_candles=signal.max_hold_candles,
                entry_candle=self._candle_count,
                peak_price=price,
                leverage=signal.leverage,
                is_short=signal.is_short,
                margin_allocated=margin,
            )
            trail_info = ""
            if signal.trailing_tp:
                trail_info += f" TrailTP={signal.trail_pct*100:.1f}%"
            if signal.trailing_sl:
                trail_info += f" TrailSL={signal.trail_sl_pct*100:.1f}%"
            logger.info(
                f"[PAPER] ➕ BUY {quantity:.6f} @ {price:.4f} | "
                f"Cost: ${cost:.2f} | Fee: ${fee:.3f} | "
                f"Balance: ${self.balance:.2f} | "
                f"SL={signal.stop_loss} | TP={signal.take_profit} |"
                f"{trail_info} | "
                f"{'Panic' if signal.panic_protection else ''}"
            )

        else:
            # DCA accumulation — add to existing position
            cost = (
                self._dca_cost_from_signal(signal, price) if signal.stop_loss is not None
                else self.balance * config.MAX_RISK_PER_TRADE * 0.5
            )
            cost = min(cost, self.balance)
            if cost <= 0:
                return
            quantity = cost / price
            fee = cost * fee_rate
            total_deducted = cost + fee
            if total_deducted > self.balance:
                total_deducted = self.balance
                cost = total_deducted / (1 + fee_rate)
                quantity = cost / price
                fee = cost * fee_rate

            self.balance -= total_deducted
            self.total_fees_paid += fee
            self.position.add_entry(price, quantity, cost)

            # Update SL to reflect new avg cost
            if signal.stop_loss:
                self.position.stop_loss = signal.stop_loss

            logger.info(
                f"[PAPER] ➕ DCA ADD {quantity:.6f} @ {price:.4f} | "
                f"Cost: ${cost:.2f} | Fee: ${fee:.3f} | "
                f"New avg: {self.position.avg_entry_price:.4f} | "
                f"Total qty: {self.position.quantity:.6f} | "
                f"Balance: ${self.balance:.2f}"
            )

    def _dca_cost_from_signal(self, signal: Signal, price: float) -> float:
        """Extract intended USDT cost from signal metadata if provided."""
        return signal.metadata.get("amount_usdt", self.balance * config.MAX_RISK_PER_TRADE * 0.5)

    # ── Sell handling ─────────────────────────────────────────────────────────

    def _handle_partial_sell(self, signal: Signal, price: float) -> None:
        """Close a fraction of the position (tranche exit)."""
        if self.position is None:
            logger.debug("[PAPER] No position to partially close.")
            return

        # Capture position attributes before any mutation so the TradeRecord is
        # always populated correctly even if the position drops to zero afterwards.
        pos_side = self.position.side
        pos_avg_entry = self.position.avg_entry_price
        pos_entry_time = self.position.entry_time

        qty_to_sell = self.position.quantity * signal.quantity_pct
        cost_basis = pos_avg_entry * qty_to_sell
        fee_rate = FEE_LIMIT if signal.order_type == "limit" else FEE_MARKET
        proceeds = price * qty_to_sell
        fee = proceeds * fee_rate
        net_proceeds = proceeds - fee
        pnl = net_proceeds - cost_basis
        pnl_pct = pnl / cost_basis * 100

        self.balance += net_proceeds
        self.total_fees_paid += fee

        # Reduce position
        self.position.quantity -= qty_to_sell
        self.position.total_cost -= cost_basis

        if self.position.quantity <= 1e-8:
            self.position = None

        emoji = "✅" if pnl >= 0 else "❌"
        qty_remaining = self.position.quantity if self.position else 0.0
        logger.info(
            f"[PAPER] {emoji} PARTIAL SELL {qty_to_sell:.6f} ({signal.quantity_pct*100:.0f}%) "
            f"@ {price:.4f} | PnL: ${pnl:+.2f} ({pnl_pct:+.2f}%) | "
            f"Fee: ${fee:.3f} | Remaining qty: {qty_remaining:.6f} | "
            f"Balance: ${self.balance:.2f} | {signal.reason}"
        )

        record = TradeRecord(
            symbol=self.symbol,
            strategy=signal.strategy,
            side=pos_side,
            entry_price=pos_avg_entry,
            exit_price=price,
            quantity=qty_to_sell,
            cost=cost_basis,
            entry_time=pos_entry_time,
            exit_time=datetime.now(timezone.utc),
            pnl=pnl, pnl_pct=pnl_pct, fees_paid=fee,
            exit_reason=signal.reason,
            is_partial=True,
            order_type=signal.order_type,
            compounded=signal.compound_profit,
        )
        self.trade_history.append(record)

        if signal.compound_profit and pnl > 0:
            reinvest = pnl * 0.5
            self.compounded_profit += reinvest
            logger.info(f"[PAPER] 🔄 Compound: ${reinvest:.2f} earmarked for reinvestment")

    def _handle_full_sell(
        self,
        signal: Optional[Signal],
        price: float,
        reason: str,
        order_type: str = "limit",
    ) -> None:
        """Close the entire remaining position."""
        if self.position is None:
            logger.debug("[PAPER] No position to close.")
            return

        fee_rate = FEE_LIMIT if order_type == "limit" else FEE_MARKET
        proceeds = price * self.position.quantity
        fee = proceeds * fee_rate
        net_proceeds = proceeds - fee
        pnl = net_proceeds - self.position.total_cost
        pnl_pct = pnl / self.position.total_cost * 100

        self.balance += net_proceeds
        self.total_fees_paid += fee

        emoji = "✅" if pnl >= 0 else "❌"
        logger.info(
            f"[PAPER] {emoji} FULL SELL {self.position.quantity:.6f} @ {price:.4f} | "
            f"PnL: ${pnl:+.2f} ({pnl_pct:+.2f}%) | Fee: ${fee:.3f} | "
            f"Balance: ${self.balance:.2f} | Reason: {reason}"
        )

        compound = signal.compound_profit if signal else False
        record = TradeRecord(
            symbol=self.symbol,
            strategy=self.position.strategy,
            side=self.position.side,
            entry_price=self.position.avg_entry_price,
            exit_price=price,
            quantity=self.position.quantity,
            cost=self.position.total_cost,
            entry_time=self.position.entry_time,
            exit_time=datetime.now(timezone.utc),
            pnl=pnl, pnl_pct=pnl_pct, fees_paid=fee,
            exit_reason=reason,
            is_partial=False,
            order_type=order_type,
            compounded=compound,
        )
        self.trade_history.append(record)

        if compound and pnl > 0:
            reinvest = pnl * 0.5
            self.compounded_profit += reinvest
            logger.info(f"[PAPER] 🔄 Compound: ${reinvest:.2f} earmarked for reinvestment")

        self.position = None

    # ── Reporting ─────────────────────────────────────────────────────────────

    def get_balance(self) -> float:
        return self.balance

    def get_equity(self, current_price: float) -> float:
        equity = self.balance
        if self.position:
            equity += self.position.unrealized_pnl(current_price) + self.position.total_cost
        return equity

    def summary(self, current_price: float = None) -> str:
        total_trades = len(self.trade_history)
        full_closes = [t for t in self.trade_history if not t.is_partial]
        partials = [t for t in self.trade_history if t.is_partial]
        wins = [t for t in self.trade_history if t.pnl > 0]
        losses = [t for t in self.trade_history if t.pnl <= 0]
        total_pnl = sum(t.pnl for t in self.trade_history)
        win_rate = (len(wins) / total_trades * 100) if total_trades > 0 else 0
        # Return based on total equity (cash + open position value) to avoid
        # showing misleading negative % when capital is simply deployed in a position
        equity_now = self.get_equity(current_price) if current_price else self.balance
        portfolio_return = ((equity_now - self.initial_balance) / self.initial_balance) * 100

        lines = [
            "=" * 54,
            "        PAPER TRADING SUMMARY",
            "=" * 54,
            f"  Initial Balance  : ${self.initial_balance:>10,.2f}",
            f"  Current Cash     : ${self.balance:>10,.2f}",
            f"  Total P&L        : ${total_pnl:>+10,.2f}",
            f"  Return           : {portfolio_return:>+9.2f}%",
            f"  Total Fees Paid  : ${self.total_fees_paid:>10,.3f}",
            f"  Compounded Pool  : ${self.compounded_profit:>10,.2f}",
            f"  Total Events     : {total_trades} ({len(full_closes)} full, {len(partials)} partial)",
            f"  Win Rate         : {win_rate:.1f}% ({len(wins)}W / {len(losses)}L)",
        ]

        if current_price and self.position:
            upnl = self.position.unrealized_pnl(current_price)
            upnl_pct = self.position.unrealized_pnl_pct(current_price)
            eq = self.get_equity(current_price)
            lines.append(f"  Open Position    : ${upnl:>+10,.2f} ({upnl_pct:>+.2f}%)")
            lines.append(f"  Total Equity     : ${eq:>10,.2f}")

        lines.append("=" * 54)
        return "\n".join(lines)

    def trade_history_df(self) -> pd.DataFrame:
        if not self.trade_history:
            return pd.DataFrame()
        return pd.DataFrame([vars(t) for t in self.trade_history])

    def deposit(self, amount_usdt: float) -> None:
        """Add funds to the paper trading balance (monthly deposit simulation)."""
        self.balance += amount_usdt
        logger.info(f"[PAPER] 💰 Deposit: ${amount_usdt:,.2f} | New balance: ${self.balance:,.2f}")

    # ── Missed-candle replay ──────────────────────────────────────────────────

    def tick_ohlcv_candle(self, high: float, low: float, close: float) -> None:
        """
        Replay one full candle's price action against the open position.

        Used when the bot was offline and needs to process candles it missed.
        More accurate than tick(close) alone because:
          - SL is checked against candle LOW  (worst intra-candle price for longs)
          - TP is checked against candle HIGH (best intra-candle price for longs)
          - Trailing peak is updated with HIGH before checking close

        Panic protection note: with panic_protection=True, SL requires 2
        consecutive *closes* below the level.  For missed candles we check
        whether the candle's CLOSE is also below the SL (conservative — avoids
        a wick-only breach triggering a panic stop).
        """
        if self.position is None:
            return

        pos = self.position

        # ── Update trailing peak with candle HIGH ─────────────────────────────
        if high > pos.peak_price:
            pos.peak_price = high

        # ── Trailing Stop Loss ratchet ─────────────────────────────────────────
        # Must run BEFORE the SL check so the freshly-ratcheted SL value is used
        # when testing against candle LOW this tick.
        if pos.trailing_sl and pos.peak_price > 0:
            new_sl = pos.peak_price * (1 - pos.trail_sl_pct)
            if pos.stop_loss is None or new_sl > pos.stop_loss:
                pos.stop_loss = new_sl

        # ── Stop-loss check ───────────────────────────────────────────────────
        if pos.stop_loss:
            sl_hit = low <= pos.stop_loss if not pos.is_short else high >= pos.stop_loss

            if sl_hit:
                if pos.panic_protection:
                    # Only trip on a confirmed close below SL
                    close_confirms = (close <= pos.stop_loss) if not pos.is_short else (close >= pos.stop_loss)
                    if close_confirms:
                        pos.sl_breach_count += 1
                        if pos.sl_breach_count >= 2:
                            logger.warning(
                                f"[PAPER] SL triggered (missed candle replay) "
                                f"@ {pos.stop_loss:.2f} | Panic confirmed"
                            )
                            self._handle_full_sell(None, pos.stop_loss, "stop_loss", order_type="market")
                            return
                    else:
                        # Wick touched SL but closed above — reset breach count
                        pos.sl_breach_count = 0
                else:
                    # No panic protection — fire immediately on LOW breach
                    exit_price = pos.stop_loss   # exit at SL price, not lower
                    logger.warning(
                        f"[PAPER] SL triggered (missed candle replay) @ {exit_price:.2f}"
                    )
                    self._handle_full_sell(None, exit_price, "stop_loss", order_type="market")
                    return
            else:
                pos.sl_breach_count = 0

        # ── Take-profit check against HIGH ────────────────────────────────────
        if pos.take_profit:
            tp_hit = high >= pos.take_profit if not pos.is_short else low <= pos.take_profit
            if tp_hit:
                exit_price = pos.take_profit
                logger.info(
                    f"[PAPER] TP hit (missed candle replay) @ {exit_price:.2f}"
                )
                self._handle_full_sell(None, exit_price, "take_profit")
                return

        # ── Trailing TP check using candle close ──────────────────────────────
        if pos.trailing_tp and pos.peak_price > 0:
            trail_threshold = pos.peak_price * (1 - pos.trail_pct)
            if close <= trail_threshold and close > pos.avg_entry_price:
                logger.info(
                    f"[PAPER] Trailing TP (missed candle replay): "
                    f"peak={pos.peak_price:.2f} threshold={trail_threshold:.2f} close={close:.2f}"
                )
                self._handle_full_sell(None, close, "trailing_tp")
                return

        # ── Time exit check ───────────────────────────────────────────────────
        if pos.max_hold_candles > 0:
            self._candle_count += 1
            held = self._candle_count - pos.entry_candle
            if held >= pos.max_hold_candles and close >= pos.avg_entry_price:
                logger.info(
                    f"[PAPER] Time exit (missed candle replay) after {held} candles"
                )
                self._handle_full_sell(None, close, "time_exit")

    # ── Crash recovery ────────────────────────────────────────────────────────

    def get_checkpoint(self) -> dict:
        """
        Serialize the simulator's essential state for persistence.
        Called by PortfolioManager.save_checkpoint() after every candle.
        """
        pos_data = None
        if self.position:
            p = self.position
            pos_data = {
                "symbol":           p.symbol,
                "side":             p.side,
                "quantity":         p.quantity,
                "avg_entry_price":  p.avg_entry_price,
                "total_cost":       p.total_cost,
                "entry_time":       p.entry_time.isoformat(),
                "strategy":         p.strategy,
                "stop_loss":        p.stop_loss,
                "take_profit":      p.take_profit,
                "trailing_tp":      p.trailing_tp,
                "trail_pct":        p.trail_pct,
                "panic_protection": p.panic_protection,
                "max_hold_candles": p.max_hold_candles,
                "entry_candle":     p.entry_candle,
                "peak_price":       p.peak_price,
                "sl_breach_count":  p.sl_breach_count,
                "leverage":         p.leverage,
                "is_short":         p.is_short,
                "margin_allocated": p.margin_allocated,
            }
        return {
            "balance":           self.balance,
            "compounded_profit": self.compounded_profit,
            "total_fees_paid":   self.total_fees_paid,
            "candle_count":      self._candle_count,
            "position":          pos_data,
        }

    def restore_checkpoint(self, data: dict) -> None:
        """
        Restore simulator state from a previously saved checkpoint.
        Called by PortfolioManager.load_checkpoint() on bot startup.

        After this runs:
        - balance is correct (not reset to initial)
        - open position (if any) is fully restored including SL/TP
        - the bot will NOT re-enter a position it was already in
        """
        self.balance           = data.get("balance",           self.balance)
        self.compounded_profit = data.get("compounded_profit", 0.0)
        self.total_fees_paid   = data.get("total_fees_paid",   0.0)
        self._candle_count     = data.get("candle_count",      0)

        pos_data = data.get("position")
        if pos_data:
            self.position = Position(
                symbol           = pos_data["symbol"],
                side             = pos_data["side"],
                quantity         = pos_data["quantity"],
                avg_entry_price  = pos_data["avg_entry_price"],
                total_cost       = pos_data["total_cost"],
                entry_time       = datetime.fromisoformat(pos_data["entry_time"]),
                strategy         = pos_data["strategy"],
                stop_loss        = pos_data.get("stop_loss"),
                take_profit      = pos_data.get("take_profit"),
                trailing_tp      = pos_data.get("trailing_tp",      False),
                trail_pct        = pos_data.get("trail_pct",        0.02),
                panic_protection = pos_data.get("panic_protection", False),
                max_hold_candles = pos_data.get("max_hold_candles", 0),
                entry_candle     = pos_data.get("entry_candle",     0),
                peak_price       = pos_data.get("peak_price",       0.0),
                sl_breach_count  = pos_data.get("sl_breach_count",  0),
                leverage         = pos_data.get("leverage",         1),
                is_short         = pos_data.get("is_short",         False),
                margin_allocated = pos_data.get("margin_allocated", 0.0),
            )
        else:
            self.position = None
