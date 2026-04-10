"""
strategies/dca.py — Advanced DCA Strategy (3Commas-style, price-deviation based)

HOW IT WORKS:
  This is a major upgrade from simple time-based DCA. Instead of buying on
  a fixed schedule, we buy more when price drops — accumulating at better
  average prices. This is how 3Commas' DCA bot works.

  ┌─────────────────────────────────────────────────────────────────┐
  │  ENTRY FLOW                                                      │
  │                                                                  │
  │  1. Base order: first buy at current price                       │
  │  2. Safety order 1: price drops deviation_pct% → buy more        │
  │  3. Safety order 2: price drops another step% → buy more (1.5×)  │
  │  4. ...up to max_safety_orders (default: 5)                     │
  │                                                                  │
  │  Each safety order is safety_scale× the previous                 │
  │  (e.g. $100, $150, $225, $337, $506) — more capital deployed    │
  │  as price drops more, lowering average cost significantly.       │
  │                                                                  │
  │  INDICATOR CONFIRMATION on safety orders:                        │
  │  RSI must be < rsi_threshold (default: 42) to confirm oversold.  │
  │  Prevents buying into a coin that's in freefall, not just dipping│
  └─────────────────────────────────────────────────────────────────┘

  EXIT FLOW (tranche-based — never leave money on the table):

    Tranche 1 (30%):  Sell at avg_cost × (1 + tp1_pct)  ← quick profit lock
    Tranche 2 (30%):  Sell at avg_cost × (1 + tp2_pct)  ← mid profit
    Tranche 3 (40%):  Trailing TP — follow price up,    ← ride the move
                      sell when it retraces trail_pct% from peak

  ADDITIONAL SAFETY FEATURES:
    Panic protection: Require 2 consecutive closes below stop-loss
                      before exiting. Avoids stop-out on wicks.
    Time exit:        If position open > max_hold_candles and price
                      is at break-even or better → exit to free capital.
    Compound mode:    Flag for portfolio manager to reinvest profits.

EXAMPLE (BTC, base $100, 5 safety orders, 2% deviation, 1.5× scaling):

  Buy 1 (base):    $100 @ $67,000  →  0.001493 BTC
  Buy 2 (safety1): $150 @ $65,660  →  0.002284 BTC  (drop 2%)
  Buy 3 (safety2): $225 @ $64,347  →  0.003496 BTC  (drop 4%)
  Buy 4 (safety3): $337 @ $63,060  →  0.005344 BTC  (drop 6%)
  Buy 5 (safety4): $506 @ $61,799  →  0.008189 BTC  (drop 8%)
  Buy 6 (safety5): $759 @ $60,563  →  0.012534 BTC  (drop 10%)

  Total spent:  $2,077  |  Total BTC: 0.033340
  Avg cost:     $62,294  (vs $67,000 if we had bought all at base)
  Avg cost improvement: -7.02%  ← significant edge

PARAMETERS:
  base_amount       : USDT for the first buy (default: 100)
  deviation_pct     : % drop to trigger each safety order (default: 2.0)
  step_scale        : Extra % per safety step (default: 1.0 = equal spacing)
  safety_scale      : Each safety order is this × previous (default: 1.5)
  max_safety_orders : Max safety buys before stopping (default: 5)
  rsi_period        : RSI lookback (default: 14)
  rsi_threshold     : Max RSI to allow safety order (default: 42)
  tp1_pct           : First tranche take profit % (default: 0.03 = 3%)
  tp2_pct           : Second tranche take profit % (default: 0.06 = 6%)
  trail_pct         : Trail 3rd tranche this % from peak (default: 0.025)
  stop_loss_pct     : Hard stop below avg cost (default: 0.12 = 12%)
  panic_protection  : Require 2 closes below SL (default: True)
  max_hold_candles  : Time-based exit at break-even (default: 336 = 14d on 1h)
  compound          : Flag profits for reinvestment (default: True)
"""

import pandas as pd
import ta
from loguru import logger
from dataclasses import dataclass, field
from typing import Optional

from strategies.base import BaseStrategy, Signal
from strategies.divergence import bullish_divergence
import config


@dataclass
class DCAEntry:
    """Records a single buy (base or safety order)."""
    order_num: int        # 0 = base, 1-5 = safety orders
    price: float
    amount_usdt: float
    quantity: float       # amount_usdt / price
    candle_index: int     # For time-based exit tracking


class DCAStrategy(BaseStrategy):
    """
    Advanced DCA with price-deviation safety orders, tranche exits,
    trailing TP, panic protection, and time-based exit.
    """

    def __init__(
        self,
        symbol: str = None,
        timeframe: str = None,
        # Entry
        base_amount: float = 100.0,
        deviation_pct: float = 2.0,
        step_scale: float = 1.0,
        safety_scale: float = 1.5,
        max_safety_orders: int = 5,
        # Safety order confirmation
        rsi_period: int = 14,
        rsi_threshold: float = 42.0,
        # Exit tranches
        tp1_pct: float = 0.03,
        tp2_pct: float = 0.06,
        trail_pct: float = 0.025,
        # Risk
        stop_loss_pct: float = 0.12,
        panic_protection: bool = True,
        max_hold_candles: int = 336,
        # Dump / pump protection
        dump_protection_pct: float = 4.0,   # Skip safety order if candle dropped > this % in one bar
        # Multi-timeframe entry filter
        mtf_filter_enabled: bool = False,   # If True, only open base order when 4h trend allows
        mtf_rsi_min: float = 35.0,          # Block base order if 4h RSI is below this (crash territory)
        mtf_rsi_max: float = 72.0,          # Block base order if 4h RSI is above this (overbought)
        # Funding rate entry filter
        funding_filter_enabled: bool = False,  # If True, block base order when perp funding is crowded
        funding_rate_max: float = 0.0005,      # Block if funding > this (0.05%/8h = extreme long crowding)
        # MACD deal-start condition (inspired by 3Commas)
        # Only open a new DCA cycle when MACD histogram has turned bullish.
        # Prevents entering at the start of a new leg down.
        macd_filter_enabled: bool = False,
        macd_fast: int = 12,
        macd_slow: int = 26,
        macd_signal_period: int = 9,
        # RSI Divergence gate for base order
        # Only open when bullish divergence is detected — price lower low but RSI higher low.
        # The strongest single entry filter; use when you want high-conviction-only entries.
        divergence_filter_enabled: bool = False,
        divergence_lookback: int = 50,
        # Compounding
        compound: bool = True,
        compound_rate: float = 0.25,        # Reinvest this fraction of cycle profit (0.25 = 25%)
    ):
        super().__init__(
            name="DCA",
            symbol=symbol or config.TRADING_PAIR,
            timeframe=timeframe or config.TIMEFRAME,
        )
        self.base_amount = base_amount
        self._initial_base_amount = base_amount   # Remember original for reporting
        self.deviation_pct = deviation_pct
        self.step_scale = step_scale
        self.safety_scale = safety_scale
        self.max_safety_orders = max_safety_orders
        self.rsi_period = rsi_period
        self.rsi_threshold = rsi_threshold
        self.tp1_pct = tp1_pct
        self.tp2_pct = tp2_pct
        self.trail_pct = trail_pct
        self.stop_loss_pct = stop_loss_pct
        self.panic_protection = panic_protection
        self.max_hold_candles = max_hold_candles
        self.dump_protection_pct = dump_protection_pct
        self.mtf_filter_enabled = mtf_filter_enabled
        self.mtf_rsi_min = mtf_rsi_min
        self.mtf_rsi_max = mtf_rsi_max
        self._htf_rsi: float = 50.0          # Latest 4h RSI (updated by portfolio manager)
        self._htf_trend: int = 0             # +1 bull, -1 bear, 0 neutral
        self.funding_filter_enabled = funding_filter_enabled
        self.funding_rate_max = funding_rate_max
        self._funding_rate: float = 0.0      # Latest perp funding rate (updated by portfolio manager)
        self.macd_filter_enabled = macd_filter_enabled
        self.macd_fast = macd_fast
        self.macd_slow = macd_slow
        self.macd_signal_period = macd_signal_period
        self.divergence_filter_enabled = divergence_filter_enabled
        self.divergence_lookback = divergence_lookback
        self.compound = compound
        self.compound_rate = max(0.0, min(1.0, compound_rate))

        # Compounding tracking
        self._total_compounded: float = 0.0   # Cumulative USDT reinvested into base_amount
        self._compound_cycles: int = 0         # Number of times compounding was applied

        # ── Internal state ────────────────────────────────────────────────────
        self._entries: list[DCAEntry] = []       # All buys in current cycle
        self._candle_count: int = 0              # Total candles processed
        self._tp1_taken: bool = False            # First tranche closed?
        self._tp2_taken: bool = False            # Second tranche closed?
        self._peak_price: float = 0.0            # Highest price seen since entry
        self._sl_breach_count: int = 0           # Consecutive closes below SL
        self._base_triggered: bool = False       # Has first buy happened?

        # Pre-compute safety order amounts and trigger drops
        self._safety_amounts = self._compute_safety_amounts()
        self._safety_drops = self._compute_safety_drops()

        mtf_info = (
            f" | MTF RSI [{mtf_rsi_min:.0f}–{mtf_rsi_max:.0f}]"
            if mtf_filter_enabled else ""
        )
        funding_info = (
            f" | FundingFilter<{funding_rate_max*100:.3f}%/8h"
            if funding_filter_enabled else ""
        )
        compound_info = (
            f" | Compound={compound_rate*100:.0f}%/cycle"
            if compound else ""
        )
        logger.info(
            f"DCA | base=${base_amount} | dev={deviation_pct}% | "
            f"safety×{max_safety_orders} scale={safety_scale} | "
            f"TP1={tp1_pct*100:.0f}% TP2={tp2_pct*100:.0f}% Trail={trail_pct*100:.1f}% | "
            f"SL={stop_loss_pct*100:.0f}% | Panic={panic_protection} | "
            f"DumpProtect={dump_protection_pct:.1f}%"
            f"{mtf_info}"
            f"{funding_info}"
            f"{compound_info} | "
            f"MaxHold={max_hold_candles}c"
        )
        self._log_order_plan()

    # ── Setup helpers ─────────────────────────────────────────────────────────

    def _compute_safety_amounts(self) -> list[float]:
        """Compute USDT amount for each safety order using safety_scale."""
        amounts = []
        amt = self.base_amount
        for _ in range(self.max_safety_orders):
            amt = amt * self.safety_scale
            amounts.append(round(amt, 2))
        return amounts

    def _compute_safety_drops(self) -> list[float]:
        """
        Compute cumulative % drop from base price to trigger each safety order.
        With step_scale=1.0 and deviation=2%: [2%, 4%, 6%, 8%, 10%]
        With step_scale=1.2 and deviation=2%: [2%, 4.4%, 7.28%, ...]  (widening)
        """
        drops = []
        cumulative = 0.0
        step = self.deviation_pct
        for _ in range(self.max_safety_orders):
            cumulative += step
            drops.append(round(cumulative, 4))
            step *= self.step_scale
        return drops

    def _log_order_plan(self):
        logger.info("DCA order plan (at hypothetical $67,000 entry):")
        logger.info(f"  Base order:  ${self.base_amount:.0f}")
        for i, (amt, drop) in enumerate(zip(self._safety_amounts, self._safety_drops)):
            trigger = 67000 * (1 - drop / 100)
            logger.info(f"  Safety {i+1}:    ${amt:.0f} @ -{drop:.1f}% (≈${trigger:,.0f})")

    # ── Position accounting ───────────────────────────────────────────────────

    @property
    def _in_position(self) -> bool:
        return len(self._entries) > 0

    @property
    def _avg_cost(self) -> float:
        """Weighted average cost of all entries."""
        if not self._entries:
            return 0.0
        total_qty = sum(e.quantity for e in self._entries)
        total_cost = sum(e.amount_usdt for e in self._entries)
        return total_cost / total_qty if total_qty > 0 else 0.0

    @property
    def _total_quantity(self) -> float:
        return sum(e.quantity for e in self._entries)

    @property
    def _base_price(self) -> float:
        """Price of the initial base order."""
        return self._entries[0].price if self._entries else 0.0

    @property
    def _safety_orders_taken(self) -> int:
        return max(0, len(self._entries) - 1)

    @property
    def _stop_loss_price(self) -> float:
        return self._avg_cost * (1 - self.stop_loss_pct)

    # ── Signal generation ─────────────────────────────────────────────────────

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        """
        Evaluate current candle and return the appropriate DCA signal.

        Priority order:
          1. Panic stop-loss (2 closes below SL)
          2. Time-based exit (break-even or better, max hold reached)
          3. Trailing TP on 3rd tranche
          4. TP2 tranche
          5. TP1 tranche
          6. Safety order entry
          7. Base order entry (if not yet in position)
          8. HOLD
        """
        self.validate_dataframe(df, min_rows=self.rsi_period + 5)
        self._candle_count += 1
        self._tick_cooldown()

        # Store df reference so _enter_base can access it for MACD/divergence checks
        self._current_df = df

        current_price = float(df["close"].iloc[-1])
        candle_open   = float(df["open"].iloc[-1])
        rsi = float(
            ta.momentum.RSIIndicator(df["close"], window=self.rsi_period)
            .rsi().iloc[-1]
        )

        # Dump protection: how far did price DROP in this single candle?
        # Positive = candle closed lower than it opened (bearish candle).
        candle_drop_pct = (candle_open - current_price) / candle_open * 100
        _dump_detected = (
            self.dump_protection_pct > 0
            and candle_drop_pct > self.dump_protection_pct
        )

        # ── 1. BASE ORDER (first entry) ───────────────────────────────────────
        if not self._in_position:
            return self._enter_base(current_price)

        avg = self._avg_cost
        base = self._base_price

        # Track peak price for trailing TP
        if current_price > self._peak_price:
            self._peak_price = current_price

        # ── 2. STOP LOSS (panic protection) ──────────────────────────────────
        if current_price <= self._stop_loss_price:
            self._sl_breach_count += 1
            if not self.panic_protection or self._sl_breach_count >= 2:
                return self._exit_all(
                    current_price,
                    reason=f"Stop-loss hit: price={current_price:.4f} < SL={self._stop_loss_price:.4f} | "
                           f"{'Panic protection: 2nd consecutive close below SL' if self.panic_protection else 'Direct exit'}"
                )
            else:
                logger.warning(
                    f"DCA: 1st close below SL ({current_price:.4f} < {self._stop_loss_price:.4f}) — "
                    f"panic protection waiting for 2nd confirmation"
                )
                return self.hold(current_price, reason="SL breach #1 — waiting for confirmation candle")
        else:
            self._sl_breach_count = 0  # Reset on any close above SL

        # ── 3. TIME-BASED EXIT ────────────────────────────────────────────────
        if self.max_hold_candles > 0 and len(self._entries) > 0:
            candles_held = self._candle_count - self._entries[0].candle_index
            if candles_held >= self.max_hold_candles and current_price >= avg:
                return self._exit_all(
                    current_price,
                    reason=f"Time exit: held {candles_held} candles (max={self.max_hold_candles}) | "
                           f"Exiting at break-even or better"
                )

        # ── 4. TRAILING TP (3rd tranche — 40% of position) ───────────────────
        if self._tp1_taken and self._tp2_taken:
            trail_threshold = self._peak_price * (1 - self.trail_pct)
            if current_price <= trail_threshold and current_price > avg:
                reason = (
                    f"Trailing TP: peak={self._peak_price:.4f} | "
                    f"Trail threshold={trail_threshold:.4f} | "
                    f"Profit: +{(current_price/avg - 1)*100:.2f}%"
                )
                self._reset_state()
                return self.sell(
                    price=current_price, reason=reason,
                    quantity_pct=1.0,    # 100% of remaining (40% of original)
                    order_type="limit",
                    compound_profit=self.compound,
                )

        # ── 5. TP2 TRANCHE (30% of position) ─────────────────────────────────
        if self._tp1_taken and not self._tp2_taken:
            tp2_target = avg * (1 + self.tp2_pct)
            if current_price >= tp2_target:
                self._tp2_taken = True
                return self.sell(
                    price=current_price,
                    reason=f"TP2 tranche (30%): +{self.tp2_pct*100:.0f}% | avg_cost={avg:.4f}",
                    quantity_pct=0.30,
                    order_type="limit",
                )

        # ── 6. TP1 TRANCHE (30% of position) ─────────────────────────────────
        if not self._tp1_taken:
            tp1_target = avg * (1 + self.tp1_pct)
            if current_price >= tp1_target:
                self._tp1_taken = True
                self._peak_price = current_price   # Reset peak for trailing
                return self.sell(
                    price=current_price,
                    reason=f"TP1 tranche (30%): +{self.tp1_pct*100:.0f}% | avg_cost={avg:.4f}",
                    quantity_pct=0.30,
                    order_type="limit",
                )

        # ── 7. SAFETY ORDERS ──────────────────────────────────────────────────
        if self._safety_orders_taken < self.max_safety_orders:
            next_safety_idx = self._safety_orders_taken
            required_drop = self._safety_drops[next_safety_idx] / 100
            trigger_price = base * (1 - required_drop)

            if current_price <= trigger_price:
                # Dump protection: skip safety order if this candle dropped too fast
                # A single-candle dump ≥ dump_protection_pct% suggests a crash,
                # not a healthy dip — wait for the next candle to confirm stabilisation.
                if _dump_detected:
                    logger.warning(
                        f"DCA dump protection: safety {next_safety_idx + 1} price triggered "
                        f"(price={current_price:.4f}) but candle dropped {candle_drop_pct:.2f}% "
                        f"in one bar (threshold={self.dump_protection_pct:.1f}%) — "
                        f"skipping safety order, waiting for stabilisation"
                    )
                    return self.hold(
                        current_price,
                        reason=(
                            f"Safety {next_safety_idx + 1} BLOCKED by dump protection: "
                            f"candle drop {candle_drop_pct:.2f}% > {self.dump_protection_pct:.1f}% | "
                            f"Waiting for next candle to confirm dip (not crash)"
                        )
                    )
                # Indicator confirmation: RSI must be oversold
                if rsi <= self.rsi_threshold:
                    return self._enter_safety(
                        current_price, next_safety_idx, rsi
                    )
                else:
                    return self.hold(
                        current_price,
                        reason=(
                            f"Safety {next_safety_idx + 1} price triggered "
                            f"(price={current_price:.4f} ≤ {trigger_price:.4f}) "
                            f"but RSI={rsi:.1f} > {self.rsi_threshold} — waiting for oversold"
                        )
                    )

        # ── 8. HOLD ───────────────────────────────────────────────────────────
        return self._hold_status(current_price, avg, base, rsi)

    # ── Entry helpers ─────────────────────────────────────────────────────────

    def _enter_base(self, price: float) -> Signal:
        """Open the base order (first buy), with optional MTF entry filter."""
        # ── MTF filter: block entry if 4h RSI is out of acceptable range ──────
        if self.mtf_filter_enabled:
            if self._htf_rsi < self.mtf_rsi_min:
                return self.hold(
                    price,
                    reason=(
                        f"MTF filter: 4h RSI={self._htf_rsi:.1f} < {self.mtf_rsi_min:.0f} "
                        f"(crash/downtrend territory) — delaying DCA base order. "
                        f"Waiting for 4h RSI to recover above {self.mtf_rsi_min:.0f}"
                    )
                )
            if self._htf_rsi > self.mtf_rsi_max:
                return self.hold(
                    price,
                    reason=(
                        f"MTF filter: 4h RSI={self._htf_rsi:.1f} > {self.mtf_rsi_max:.0f} "
                        f"(overbought on 4h) — delaying DCA base order. "
                        f"Waiting for 4h RSI to cool below {self.mtf_rsi_max:.0f}"
                    )
                )

        # ── Funding rate filter: block entry when perps are crowded long ────
        # High positive funding means longs are paying heavily to stay long —
        # the market is over-leveraged and vulnerable to a cascading flush.
        # This is one of the strongest leading indicators in crypto.
        if self.funding_filter_enabled and self._funding_rate > self.funding_rate_max:
            return self.hold(
                price,
                reason=(
                    f"Funding filter: rate={self._funding_rate*100:+.4f}%/8h "
                    f"> threshold {self.funding_rate_max*100:.3f}%/8h "
                    f"(perp longs overcrowded — leveraged flush risk). "
                    f"Waiting for funding to cool below {self.funding_rate_max*100:.3f}%"
                )
            )

        # ── MACD deal-start condition ─────────────────────────────────────────
        # Only open a new DCA cycle when MACD histogram has recently turned
        # from negative to positive, signalling bullish momentum shift.
        # "Recently" = histogram is positive now AND was negative 1–3 candles ago.
        # This avoids entering at the start of a new downtrend leg.
        if self.macd_filter_enabled:
            df_ref = getattr(self, '_current_df', None)
            if df_ref is not None and len(df_ref) >= self.macd_slow + self.macd_signal_period + 5:
                try:
                    hist_series = ta.trend.MACD(
                        close=df_ref["close"],
                        window_fast=self.macd_fast,
                        window_slow=self.macd_slow,
                        window_sign=self.macd_signal_period,
                    ).macd_diff()
                    valid = hist_series.dropna()
                    if len(valid) >= 3:
                        curr_hist = float(hist_series.iloc[-1])
                        # Require: histogram currently positive (bullish zone)
                        # AND was negative within the last 3 candles (recent crossover)
                        recent_negative = any(
                            float(hist_series.iloc[-(i+1)]) < 0
                            for i in range(1, min(4, len(hist_series)))
                        )
                        if not (curr_hist > 0 and recent_negative):
                            return self.hold(
                                price,
                                reason=(
                                    f"MACD filter: histogram={curr_hist:.5f} — "
                                    f"{'negative (bearish momentum)' if curr_hist < 0 else 'no recent bullish crossover'} | "
                                    f"Waiting for MACD to cross above zero"
                                )
                            )
                except Exception as macd_err:
                    logger.debug(f"DCA MACD filter skipped (compute error): {macd_err}")

        # ── RSI Divergence gate ───────────────────────────────────────────────
        # Require bullish divergence (price lower low + RSI higher low) before
        # opening a new cycle.  This is the strongest single entry filter —
        # it catches bottoms and avoids the middle of downtrends.
        # When enabled, expect fewer but higher-quality entries.
        if self.divergence_filter_enabled:
            # We need the full df here — check it was passed to generate_signal
            # The df is accessible via closure from generate_signal → _enter_base
            # but _enter_base only receives price. Store df reference temporarily.
            df_ref = getattr(self, '_current_df', None)
            if df_ref is not None:
                has_div = bullish_divergence(
                    df_ref,
                    rsi_period=self.rsi_period,
                    lookback=self.divergence_lookback,
                )
                if not has_div:
                    return self.hold(
                        price,
                        reason=(
                            f"Divergence filter: no bullish divergence detected in last "
                            f"{self.divergence_lookback} candles | "
                            f"Waiting for price lower-low + RSI higher-low pattern"
                        )
                    )

        qty = self.base_amount / price
        entry = DCAEntry(
            order_num=0, price=price,
            amount_usdt=self.base_amount, quantity=qty,
            candle_index=self._candle_count
        )
        self._entries.append(entry)
        self._peak_price = price

        total_planned = self.base_amount + sum(self._safety_amounts)
        logger.info(
            f"DCA base order: ${self.base_amount} @ {price:.4f} | "
            f"Total capital planned this cycle: ${total_planned:.0f}"
        )
        return self.buy(
            price=price,
            reason=f"DCA base order #{len(self._entries)} | ${self.base_amount:.0f} | "
                   f"Up to {self.max_safety_orders} safety orders ready below",
            stop_loss=price * (1 - self.stop_loss_pct),
            panic_protection=self.panic_protection,
            max_hold_candles=self.max_hold_candles,
            order_type="limit",
            metadata={"amount_usdt": self.base_amount},
        )

    def _enter_safety(self, price: float, idx: int, rsi: float) -> Signal:
        """Open safety order at index idx."""
        amount = self._safety_amounts[idx]
        qty = amount / price
        entry = DCAEntry(
            order_num=idx + 1, price=price,
            amount_usdt=amount, quantity=qty,
            candle_index=self._candle_count
        )
        self._entries.append(entry)

        new_avg = self._avg_cost
        drop_from_base = (self._base_price - price) / self._base_price * 100
        avg_improvement = (self._entries[0].price - new_avg) / self._entries[0].price * 100

        logger.info(
            f"DCA safety order {idx + 1}/{self.max_safety_orders}: "
            f"${amount:.0f} @ {price:.4f} | "
            f"Drop from base: -{drop_from_base:.2f}% | "
            f"New avg cost: {new_avg:.4f} (improved -{avg_improvement:.2f}%) | "
            f"RSI confirmed: {rsi:.1f}"
        )
        return self.buy(
            price=price,
            reason=(
                f"Safety order {idx + 1}/{self.max_safety_orders}: "
                f"${amount:.0f} | -{drop_from_base:.2f}% from base | "
                f"avg_cost={new_avg:.4f} | RSI={rsi:.1f}"
            ),
            stop_loss=new_avg * (1 - self.stop_loss_pct),
            order_type="limit",
            metadata={"amount_usdt": amount},
        )

    def _exit_all(self, price: float, reason: str) -> Signal:
        """Exit the full remaining position."""
        self._reset_state()
        return self.sell(
            price=price, reason=reason,
            quantity_pct=1.0,
            order_type="market",  # Use market on stop-loss for guaranteed fill
        )

    def _reset_state(self):
        """Clear state after a full cycle completes."""
        self._entries.clear()
        self._tp1_taken = False
        self._tp2_taken = False
        self._peak_price = 0.0
        self._sl_breach_count = 0
        logger.info("DCA cycle complete — state reset, ready for new base order")

    def _hold_status(self, price: float, avg: float, base: float, rsi: float) -> Signal:
        """Return a descriptive HOLD with full position context."""
        if self._in_position:
            pnl_pct = (price - avg) / avg * 100
            next_so = self._safety_orders_taken
            safety_info = ""
            if next_so < self.max_safety_orders:
                next_trigger = base * (1 - self._safety_drops[next_so] / 100)
                safety_info = f" | Next safety {next_so+1} @ {next_trigger:.4f} (RSI≤{self.rsi_threshold})"

            tp1_target = avg * (1 + self.tp1_pct)
            tp2_target = avg * (1 + self.tp2_pct)

            return self.hold(
                price,
                reason=(
                    f"In position | avg={avg:.4f} | P&L={pnl_pct:+.2f}% | "
                    f"SL={self._stop_loss_price:.4f} | "
                    f"TP1={'✓' if self._tp1_taken else f'{tp1_target:.4f}'} "
                    f"TP2={'✓' if self._tp2_taken else f'{tp2_target:.4f}'} "
                    f"Trail={self.trail_pct*100:.1f}% | RSI={rsi:.1f}"
                    f"{safety_info}"
                )
            )
        return self.hold(price, reason=f"Waiting for entry | RSI={rsi:.1f}")

    def update_htf_data(self, htf_df: pd.DataFrame) -> None:
        """
        Feed the latest 4h OHLCV data so DCA can use it as an entry filter.

        Called by portfolio/manager.py each candle (mirrors BreakoutStrategy.update_htf_trend).
        If mtf_filter_enabled=False this is a no-op.

        Updates:
          _htf_rsi   — 4h RSI(14)
          _htf_trend — +1 if EMA20 > EMA50 on 4h, -1 if below, 0 otherwise
        """
        if not self.mtf_filter_enabled:
            return
        if htf_df is None or len(htf_df) < 55:
            return
        try:
            close = htf_df["close"]
            # 4h RSI
            delta    = close.diff()
            gain     = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
            loss     = (-delta).clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
            rs       = gain / loss.replace(0, 1e-9)
            self._htf_rsi = float((100 - 100 / (1 + rs)).iloc[-1])
            # 4h trend via EMA20/50 relationship
            ema20 = float(close.ewm(span=20, adjust=False).mean().iloc[-1])
            ema50 = float(close.ewm(span=50, adjust=False).mean().iloc[-1])
            self._htf_trend = 1 if ema20 > ema50 * 1.005 else (-1 if ema20 < ema50 * 0.995 else 0)
        except Exception as exc:
            logger.debug(f"DCA MTF update skipped: {exc}")

    def update_funding_rate(self, rate: float) -> None:
        """
        Feed the latest perpetual futures funding rate so DCA can gate
        base-order entries on leverage crowding.

        Called by PortfolioManager.run_candle() each candle when a
        FundingRateProvider is attached and funding_filter_enabled=True.

        Args:
            rate: Funding rate as a fraction per 8-hour period.
                  e.g. 0.0001 = 0.01%/8h. Positive = longs pay shorts.
        """
        if not self.funding_filter_enabled:
            return
        self._funding_rate = rate

    def sync_state(self, simulator_has_position: bool) -> None:
        """
        Sync DCA's internal state with the simulator.

        If the simulator closed the position externally (e.g. via tick() SL/TP)
        but DCA still thinks it's in position, reset DCA so it can re-enter.
        Call this AFTER simulator.tick() and BEFORE generate_signal() each candle.
        """
        if self._in_position and not simulator_has_position:
            logger.info(
                "DCA: simulator closed position externally (SL/TP via tick). "
                "Resetting DCA state to allow re-entry."
            )
            self._reset_state()

    def reset(self):
        """Manually reset — call if restarting bot mid-cycle."""
        self._reset_state()
        self._candle_count = 0
        logger.info("DCA strategy fully reset.")

    # ── Profit compounding ────────────────────────────────────────────────────

    def apply_compound(self, realized_pnl: float) -> float:
        """
        Reinvest a fraction of cycle profit into the base order size.

        Called by the portfolio manager after a full DCA cycle closes with profit.
        Only applies if self.compound=True and compound_rate > 0.

        The reinvested amount grows base_amount — meaning the NEXT DCA cycle
        will deploy slightly more capital as the "principal" order, compounding
        returns over time.

        Caps base_amount growth to 3× the original so a lucky streak doesn't
        spiral the position size into reckless territory.

        Args:
            realized_pnl: Profit from the completed cycle in USDT.

        Returns:
            Amount actually reinvested (0 if compound is off or PnL was negative).

        Example:
            base_amount=$100, compound_rate=0.25, cycle_profit=$40
            → reinvest = $10 → new base_amount = $110
        """
        if not self.compound or self.compound_rate <= 0 or realized_pnl <= 0:
            return 0.0

        reinvest = round(realized_pnl * self.compound_rate, 2)

        # Hard cap: base_amount cannot exceed 3× original to prevent runaway sizing
        max_base = self._initial_base_amount * 3.0
        new_base = min(self.base_amount + reinvest, max_base)
        actual_reinvest = round(new_base - self.base_amount, 2)

        if actual_reinvest <= 0:
            logger.info(
                f"DCA compound: base_amount already at cap (${self.base_amount:.2f} / "
                f"max ${max_base:.2f}) — skipping reinvestment."
            )
            return 0.0

        self.base_amount = new_base
        self._total_compounded += actual_reinvest
        self._compound_cycles += 1

        # Recompute safety order amounts since base_amount changed
        self._safety_amounts = self._compute_safety_amounts()

        logger.info(
            f"DCA compound cycle {self._compound_cycles}: "
            f"profit=${realized_pnl:.2f} → reinvesting ${actual_reinvest:.2f} "
            f"({self.compound_rate*100:.0f}%) | "
            f"base_amount: ${self.base_amount - actual_reinvest:.2f} → ${self.base_amount:.2f} | "
            f"total_compounded=${self._total_compounded:.2f}"
        )
        return actual_reinvest

    def compound_summary(self) -> str:
        """Return a formatted report of compounding progress."""
        growth_pct = (
            (self.base_amount - self._initial_base_amount) / self._initial_base_amount * 100
            if self._initial_base_amount > 0 else 0.0
        )
        return (
            f"DCA Compound Summary | "
            f"Initial base: ${self._initial_base_amount:.2f} | "
            f"Current base: ${self.base_amount:.2f} (+{growth_pct:.1f}%) | "
            f"Total reinvested: ${self._total_compounded:.2f} | "
            f"Compound cycles: {self._compound_cycles}"
        )

    def restore_position_from_checkpoint(self, position) -> None:
        """
        Called after checkpoint restore so DCA knows it's already in a position.

        Without this, DCA's _entries list is empty on restart even though the
        simulator has a restored position — causing DCA to immediately place a
        new base order ON TOP of the existing one (double entry bug).

        Creates one synthetic DCAEntry representing the full accumulated position
        so _in_position returns True and DCA correctly manages the existing trade.
        The individual safety order history is lost across restarts, but the
        critical state (avg price, stop loss, peak) is fully preserved.
        """
        if position is None or self._in_position:
            return

        synthetic = DCAEntry(
            order_num    = 0,
            price        = position.avg_entry_price,
            amount_usdt  = position.total_cost,
            quantity     = position.quantity,
            candle_index = self._candle_count,
        )
        self._entries.append(synthetic)
        self._base_triggered = True
        self._peak_price     = position.peak_price
        self._sl_breach_count = position.sl_breach_count

        logger.info(
            f"DCA: restored position from checkpoint | "
            f"avg_entry=${position.avg_entry_price:,.2f} | "
            f"qty={position.quantity:.6f} | "
            f"cost=${position.total_cost:.2f} | "
            f"peak=${position.peak_price:,.2f}"
        )
