"""
strategies/vwap.py — VWAP Mean-Reversion Strategy

WHAT IS VWAP?
─────────────
VWAP (Volume Weighted Average Price) is the average price weighted by volume
over a rolling window.  It is the single most widely used institutional
benchmark — market makers, hedge funds, and algorithmic desks ALL use VWAP
as a reference.

  VWAP = Σ(typical_price × volume) / Σ(volume)
  Typical price = (high + low + close) / 3

WHY IT WORKS
────────────
Institutions often execute large orders "around VWAP" to avoid moving price.
When price deviates significantly BELOW VWAP, it creates a buy opportunity
because institutional buyers will re-enter to maintain their average.
When price deviates significantly ABOVE VWAP, price tends to revert back down.

This strategy exploits that mean-reversion to VWAP, which is conceptually
similar to Bollinger Bands but anchored to actual traded volume rather than
pure price history.

ENTRY LOGIC
───────────
  BUY when ALL of:
    1. Price is ≥ entry_dev_pct% BELOW rolling VWAP
       (e.g. -1.5% below VWAP = oversold relative to fair value)
    2. RSI < rsi_entry (momentum still hasn't turned, but we're near VWAP support)
    3. Volume ≥ 80% of 20-period average (not thin air)
    4. Not in post-loss cooldown

EXIT LOGIC
──────────
  SELL when ANY of:
    1. Price returns to VWAP (full reversion achieved)
    2. Price is ≥ exit_dev_pct% ABOVE VWAP (extended too far)
    3. RSI > rsi_exit (overbought — momentum likely to stall)
    4. SL: entry × (1 − stop_loss_pct) (hard floor)

ROLLING VS SESSION VWAP
────────────────────────
Traditional VWAP resets at market open.  Crypto trades 24/7 so we use a
ROLLING window (default: last 24 candles on 1h = one trading day equivalent).
This ensures VWAP always reflects the most recent full-cycle of trading activity.

For 4h charts, use vwap_period=6 (24h).
For 1h charts, use vwap_period=24 (24h).
For 15m charts, use vwap_period=96 (24h).

PARAMETERS
──────────
  vwap_period:    Rolling window for VWAP calculation (default 24)
  entry_dev_pct:  % below VWAP to trigger BUY (default 1.5%)
  exit_dev_pct:   % above VWAP to trigger SELL (default 0.5%)
  rsi_period:     RSI lookback (default 14)
  rsi_entry:      RSI must be BELOW this to enter (default 50)
  rsi_exit:       RSI above this triggers exit (default 65)
  stop_loss_pct:  Hard SL below entry (default 3%)
  atr_period:     ATR period for adaptive trailing on exit (default 14)
  cooldown_candles: Candles to wait after a SL loss (default 6)
"""

import pandas as pd
import ta
from loguru import logger
from typing import Optional

from strategies.base import BaseStrategy, Signal
import config


class VWAPStrategy(BaseStrategy):
    """
    VWAP mean-reversion: buy when price deviates significantly below rolling VWAP,
    exit when price reverts to or above VWAP.

    Works best in RANGE and mild BULL regimes — ineffective in strong trends
    where price can deviate from VWAP for extended periods.
    """

    def __init__(
        self,
        symbol:           str   = None,
        timeframe:        str   = None,
        vwap_period:      int   = 24,
        entry_dev_pct:    float = 1.5,
        exit_dev_pct:     float = 0.5,
        rsi_period:       int   = 14,
        rsi_entry:        float = 50.0,
        rsi_exit:         float = 65.0,
        stop_loss_pct:    float = 0.03,
        atr_period:       int   = 14,
        volume_filter:    bool  = True,
        cooldown_candles: int   = 6,
    ):
        """
        Args:
            symbol:           Trading pair, e.g. "BTC/USDT".
            timeframe:        Candle size, e.g. "1h".
            vwap_period:      Rolling window for VWAP (candles). Use 24 for 1h charts.
            entry_dev_pct:    Enter when price is this % below VWAP.
            exit_dev_pct:     Exit when price is this % above VWAP (profit extension).
            rsi_period:       RSI lookback.
            rsi_entry:        Only enter when RSI < this (not already overbought).
            rsi_exit:         Exit when RSI > this (overbought signal).
            stop_loss_pct:    Hard stop-loss % below entry price.
            atr_period:       ATR period for adaptive trailing stop.
            volume_filter:    If True, require above-average volume on entry.
            cooldown_candles: Candles to wait after a stop-loss exit.
        """
        super().__init__(
            name="VWAP",
            symbol=symbol or config.TRADING_PAIR,
            timeframe=timeframe or config.TIMEFRAME,
        )
        self.vwap_period      = vwap_period
        self.entry_dev_pct    = entry_dev_pct
        self.exit_dev_pct     = exit_dev_pct
        self.rsi_period       = rsi_period
        self.rsi_entry        = rsi_entry
        self.rsi_exit         = rsi_exit
        self.stop_loss_pct    = stop_loss_pct
        self.atr_period       = atr_period
        self.volume_filter    = volume_filter
        self.cooldown_candles = cooldown_candles

        # Internal state
        self._in_position: bool = False
        self._entry_price: float = 0.0
        self._vwap_at_entry: float = 0.0

        logger.info(
            f"VWAP Strategy | period={vwap_period} | "
            f"entry<-{entry_dev_pct}% | exit>+{exit_dev_pct}% | "
            f"RSI entry<{rsi_entry} exit>{rsi_exit} | "
            f"SL={stop_loss_pct*100:.1f}% | "
            f"Volume filter: {'ON' if volume_filter else 'OFF'} | "
            f"Cooldown: {cooldown_candles} candles"
        )

    def sync_state(self, simulator_has_position: bool) -> None:
        """
        Sync internal position flag with the simulator's real state.
        Called by the portfolio manager after each tick so that SL/TP exits
        triggered by the simulator are correctly reflected here.
        """
        if self._in_position and not simulator_has_position:
            self._in_position = False
            self._entry_price = 0.0

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        """
        Compute rolling VWAP and return BUY/SELL/HOLD.

        Args:
            df: OHLCV DataFrame with at least vwap_period + rsi_period + 20 rows.

        Returns:
            Signal with action BUY, SELL, or HOLD.
        """
        min_rows = self.vwap_period + self.rsi_period + 25
        self.validate_dataframe(df, min_rows=min_rows)

        # ── Tick cooldown counter ─────────────────────────────────────────────
        self._tick_cooldown()

        current_price = float(df["close"].iloc[-1])

        # ── Compute rolling VWAP ──────────────────────────────────────────────
        # Typical price = (H + L + C) / 3  — the standard VWAP input
        typical_price = (df["high"] + df["low"] + df["close"]) / 3
        tp_vol = typical_price * df["volume"]

        vwap_series = (
            tp_vol.rolling(window=self.vwap_period).sum()
            / df["volume"].rolling(window=self.vwap_period).sum()
        )
        vwap = float(vwap_series.iloc[-1])

        if pd.isna(vwap) or vwap <= 0:
            return self.hold(current_price, reason="VWAP: insufficient data for calculation")

        # % deviation of current price from VWAP
        # Negative = price below VWAP (potentially oversold)
        # Positive = price above VWAP (potentially overbought)
        vwap_dev = (current_price - vwap) / vwap * 100

        # ── Compute RSI ───────────────────────────────────────────────────────
        rsi_val = float(
            ta.momentum.RSIIndicator(df["close"], window=self.rsi_period)
            .rsi().iloc[-1]
        )
        if pd.isna(rsi_val):
            return self.hold(current_price, reason="VWAP: RSI not yet computed (warm-up)")

        # ── EXIT logic (check first if in position) ───────────────────────────
        if self._in_position:
            vwap_at_entry = self._vwap_at_entry
            entry = self._entry_price

            # SL check
            sl_price = entry * (1 - self.stop_loss_pct)
            if current_price <= sl_price:
                self._in_position = False
                self._entry_price = 0.0
                self.start_cooldown(self.cooldown_candles)
                return self.sell(
                    price=current_price,
                    reason=(
                        f"VWAP SL: {current_price:.4f} ≤ SL {sl_price:.4f} | "
                        f"Cooldown {self.cooldown_candles} candles"
                    ),
                    order_type="market",
                )

            # Exit when price returns to VWAP or extends above exit_dev_pct
            reverted_to_vwap = vwap_dev >= 0.0
            extended_above   = vwap_dev >= self.exit_dev_pct
            rsi_overbought   = rsi_val > self.rsi_exit

            if extended_above or rsi_overbought:
                reason_parts = []
                if extended_above:
                    reason_parts.append(
                        f"VWAP extended +{vwap_dev:.2f}% above VWAP={vwap:.4f}"
                    )
                if rsi_overbought:
                    reason_parts.append(f"RSI={rsi_val:.1f} > {self.rsi_exit}")
                pnl_pct = (current_price - entry) / entry * 100
                reason_parts.append(f"PnL≈{pnl_pct:+.2f}%")

                trail_pct = self.adaptive_trail_pct(df, self.atr_period)
                self._in_position = False
                self._entry_price = 0.0

                return self.sell(
                    price=current_price,
                    reason=" | ".join(reason_parts),
                    trailing_tp=True,
                    trail_pct=trail_pct,
                    order_type="limit",
                )

            if reverted_to_vwap:
                pnl_pct = (current_price - entry) / entry * 100
                self._in_position = False
                self._entry_price = 0.0
                return self.sell(
                    price=current_price,
                    reason=(
                        f"VWAP reversion complete: price {current_price:.4f} "
                        f"≥ VWAP {vwap:.4f} (dev={vwap_dev:+.2f}%) | "
                        f"PnL≈{pnl_pct:+.2f}%"
                    ),
                    order_type="limit",
                )

            # Still in position, price below VWAP — wait
            return self.hold(
                current_price,
                reason=(
                    f"VWAP: holding | dev={vwap_dev:+.2f}% from VWAP={vwap:.4f} | "
                    f"RSI={rsi_val:.1f} | "
                    f"SL={entry * (1-self.stop_loss_pct):.4f}"
                ),
            )

        # ── ENTRY logic ────────────────────────────────────────────────────────
        # Block entry during cooldown
        if self.in_cooldown():
            return self.hold(
                current_price,
                reason=(
                    f"VWAP: cooldown active ({self._cooldown_remaining} candles remain) | "
                    f"dev={vwap_dev:+.2f}%"
                ),
            )

        # Condition 1: price sufficiently below VWAP
        below_vwap = vwap_dev <= -self.entry_dev_pct

        # Condition 2: RSI not overbought
        rsi_ok = rsi_val < self.rsi_entry

        # Condition 3: volume confirmation
        vol_ok = self.volume_is_sufficient(df) if self.volume_filter else True

        if below_vwap and rsi_ok and vol_ok:
            stop_loss  = current_price * (1 - self.stop_loss_pct)
            take_profit = vwap * (1 + self.exit_dev_pct / 100)
            trail_pct  = self.adaptive_trail_pct(df, self.atr_period)

            self._in_position  = True
            self._entry_price  = current_price
            self._vwap_at_entry = vwap

            return self.buy(
                price=current_price,
                reason=(
                    f"VWAP entry: dev={vwap_dev:.2f}% below VWAP={vwap:.4f} | "
                    f"RSI={rsi_val:.1f} < {self.rsi_entry} | "
                    f"TP≈{take_profit:.4f} | SL={stop_loss:.4f}"
                ),
                stop_loss=stop_loss,
                take_profit=take_profit,
                trailing_sl=True,
                trail_sl_pct=trail_pct,
                order_type="limit",
            )

        # HOLD: log useful diagnostic info
        blocking = []
        if not below_vwap:
            blocking.append(f"dev={vwap_dev:+.2f}% (need <-{self.entry_dev_pct}%)")
        if not rsi_ok:
            blocking.append(f"RSI={rsi_val:.1f} (need <{self.rsi_entry})")
        if not vol_ok:
            blocking.append("low volume")

        return self.hold(
            current_price,
            reason=(
                f"VWAP={vwap:.4f} | dev={vwap_dev:+.2f}% | "
                f"RSI={rsi_val:.1f} | "
                + ("Blocked: " + ", ".join(blocking) if blocking else "waiting for setup")
            ),
        )
