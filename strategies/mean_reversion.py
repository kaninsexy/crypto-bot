"""
strategies/mean_reversion.py — Mean Reversion Strategy (v2: StochRSI + BB %B + Divergence)

WHAT CHANGED FROM v1
────────────────────
v1 used plain RSI + Bollinger Band touch.  That generated too many false signals
because RSI is slow — price could touch the lower band 10 candles before RSI
caught up, so entries were often too early.

v2 upgrades with three improvements from professional bot research:

  1. STOCHASTIC RSI (StochRSI) instead of plain RSI
     ─────────────────────────────────────────────
     StochRSI applies RSI's formula TO RSI itself, making it 2–3× more
     sensitive.  We require the K line to cross above the D line while
     both are below 25 — this is the exact turning-point confirmation.

     Plain RSI < 35:   "we're somewhere in oversold territory"
     StochRSI K cross: "the turn is happening RIGHT NOW at this candle"

  2. BOLLINGER BAND %B instead of "price touches band"
     ────────────────────────────────────────────────
     %B = (price − lower) / (upper − lower)
     %B = 0.0 → price exactly at lower band
     %B < 0.0 → price BELOW lower band (extension)
     %B > 1.0 → price ABOVE upper band

     Using %B < 0.05 (within 5% of the lower band width) is more precise
     than a binary touch/no-touch check.

  3. RSI DIVERGENCE as optional confirmation
     ────────────────────────────────────────
     When price makes a new low but RSI makes a higher low (bullish
     divergence), the reversal is much more likely to hold.  Enabling
     divergence_confirm adds this as a third required filter.

  4. VOLUME CONFIRMATION (inherited from BaseStrategy)
     ────────────────────────────────────────────────
     Entry is only taken when volume ≥ 80% of the 20-period average.
     Thin-air signals in quiet markets are skipped.

  5. ADAPTIVE TRAILING STOP on exit
     ─────────────────────────────
     Trail percentage scales with ATR (from BaseStrategy.adaptive_trail_pct).
     In volatile markets, trail widens; in calm markets, it tightens.

  6. COOLDOWN after stop-loss
     ────────────────────────
     After a SL exit, waits cooldown_candles before re-entering.

COMBINED ENTRY (ALL required):
  • BB %B < bb_pct_b_entry (price near/below lower band)
  • StochRSI K < stochrsi_oversold AND K > D (oversold + crossing up)
  • Volume ≥ 80% of 20-period average
  • Not in cooldown

COMBINED EXIT (ANY sufficient):
  • BB %B > bb_pct_b_exit (price near/above upper band)
  • StochRSI K > stochrsi_overbought AND K < D (overbought + crossing down)
  • RSI > rsi_overbought (secondary overbought check)
  • SL hit
"""

import pandas as pd
import ta
from ta.momentum import StochRSIIndicator
from loguru import logger
from typing import Optional

from strategies.base import BaseStrategy, Signal
from strategies.divergence import bullish_divergence
import config


class MeanReversionStrategy(BaseStrategy):
    """
    Mean reversion v2: StochRSI K/D crossover + BB %B + optional divergence.
    Inherits cooldown, volume filter, and adaptive trailing from BaseStrategy.
    """

    def __init__(
        self,
        symbol:              str   = None,
        timeframe:           str   = None,
        # StochRSI settings
        stochrsi_period:     int   = 14,    # RSI period (inner)
        stochrsi_smooth_k:   int   = 3,     # K-line smoothing
        stochrsi_smooth_d:   int   = 3,     # D-line smoothing
        stochrsi_oversold:   float = 10.0,  # K < this = oversold zone (crypto: 0.1 on 0–1 scale)
        stochrsi_overbought: float = 90.0,  # K > this = overbought zone (crypto: 0.9 on 0–1 scale)
        # RSI (kept for secondary checks + divergence)
        rsi_period:          int   = 14,
        rsi_overbought:      float = 80.0,  # Crypto-specific: 80 instead of stock-market 70
        # Bollinger Bands
        bb_period:           int   = 20,
        bb_std:              float = 2.0,
        bb_pct_b_entry:      float = 0.05,  # Enter when %B < this (near lower band)
        bb_pct_b_exit:       float = 0.95,  # Exit when %B > this (near upper band)
        # RSI Divergence confirmation (optional, tightens signal quality)
        divergence_confirm:  bool  = False,
        divergence_lookback: int   = 40,
        # EMA trend filter (prevents buying dips in downtrends)
        ema_filter:          bool  = False,
        ema_fast:            int   = 50,
        ema_slow:            int   = 200,
        # Risk
        stop_loss_pct:       float = 0.04,  # 4% hard stop
        atr_period:          int   = 14,    # For adaptive trailing
        # Cooldown
        cooldown_candles:    int   = 5,
        # Volume filter
        volume_filter:       bool  = True,
    ):
        super().__init__(
            name="MeanReversion",
            symbol=symbol or config.TRADING_PAIR,
            timeframe=timeframe or config.TIMEFRAME,
        )
        self.stochrsi_period     = stochrsi_period
        self.stochrsi_smooth_k   = stochrsi_smooth_k
        self.stochrsi_smooth_d   = stochrsi_smooth_d
        self.stochrsi_oversold   = stochrsi_oversold
        self.stochrsi_overbought = stochrsi_overbought
        self.rsi_period          = rsi_period
        self.rsi_overbought      = rsi_overbought
        self.bb_period           = bb_period
        self.bb_std              = bb_std
        self.bb_pct_b_entry      = bb_pct_b_entry
        self.bb_pct_b_exit       = bb_pct_b_exit
        self.divergence_confirm  = divergence_confirm
        self.divergence_lookback = divergence_lookback
        self.ema_filter          = ema_filter
        self.ema_fast            = ema_fast
        self.ema_slow            = ema_slow
        self.stop_loss_pct       = stop_loss_pct
        self.atr_period          = atr_period
        self.cooldown_candles    = cooldown_candles
        self.volume_filter       = volume_filter

        # Internal state
        self._in_position: bool  = False
        self._entry_price: float = 0.0

        logger.info(
            f"MeanReversion v2 | StochRSI({stochrsi_period},{stochrsi_smooth_k},{stochrsi_smooth_d}) "
            f"[OB:{stochrsi_overbought}/OS:{stochrsi_oversold}] | "
            f"BB({bb_period},{bb_std}σ) %B [entry<{bb_pct_b_entry}/exit>{bb_pct_b_exit}] | "
            f"Divergence: {'ON' if divergence_confirm else 'OFF'} | "
            f"EMA filter: {'ON' if ema_filter else 'OFF'} | "
            f"SL={stop_loss_pct*100:.1f}% | Cooldown={cooldown_candles}c"
        )

    def sync_state(self, simulator_has_position: bool) -> None:
        """Sync with simulator (PM calls this after each tick)."""
        if self._in_position and not simulator_has_position:
            self._in_position = False
            self._entry_price = 0.0

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        """
        Compute StochRSI, BB %B, and optional divergence. Return BUY/SELL/HOLD.
        """
        min_rows = max(
            self.stochrsi_period * 2,
            self.bb_period,
            self.ema_slow if self.ema_filter else 0,
            self.divergence_lookback if self.divergence_confirm else 0,
        ) + 20
        self.validate_dataframe(df, min_rows=min_rows)

        self._tick_cooldown()

        close         = df["close"]
        current_price = float(close.iloc[-1])

        # ── Stochastic RSI ────────────────────────────────────────────────────
        # K line = fast stochastic of RSI  (sensitive, reacts first)
        # D line = smoothed K             (confirmation, reacts second)
        # Entry signal: K < oversold_threshold AND K crossing above D
        stoch = StochRSIIndicator(
            close,
            window=self.stochrsi_period,
            smooth1=self.stochrsi_smooth_k,
            smooth2=self.stochrsi_smooth_d,
        )
        k_series = stoch.stochrsi_k()
        d_series = stoch.stochrsi_d()
        k_curr = float(k_series.iloc[-1])
        k_prev = float(k_series.iloc[-2])
        d_curr = float(d_series.iloc[-1])
        d_prev = float(d_series.iloc[-2])

        # ── Bollinger Bands + %B ──────────────────────────────────────────────
        bb = ta.volatility.BollingerBands(
            close, window=self.bb_period, window_dev=self.bb_std
        )
        bb_upper  = float(bb.bollinger_hband().iloc[-1])
        bb_lower  = float(bb.bollinger_lband().iloc[-1])
        bb_middle = float(bb.bollinger_mavg().iloc[-1])
        bb_pct_b  = float(bb.bollinger_pband().iloc[-1])   # 0.0=lower, 1.0=upper
        bb_width  = bb_upper - bb_lower

        # ── RSI (secondary overbought check + divergence) ─────────────────────
        rsi_val = float(
            ta.momentum.RSIIndicator(close, window=self.rsi_period).rsi().iloc[-1]
        )

        # ── EMA trend filter ──────────────────────────────────────────────────
        in_uptrend = True
        if self.ema_filter:
            ema_f = float(ta.trend.EMAIndicator(close, window=self.ema_fast).ema_indicator().iloc[-1])
            ema_s = float(ta.trend.EMAIndicator(close, window=self.ema_slow).ema_indicator().iloc[-1])
            in_uptrend = ema_f > ema_s

        # ── EXIT logic (if in position) ───────────────────────────────────────
        if self._in_position:
            sl_price = self._entry_price * (1 - self.stop_loss_pct)

            if current_price <= sl_price:
                self._in_position = False
                self._entry_price = 0.0
                self.start_cooldown(self.cooldown_candles)
                return self.sell(
                    price=current_price,
                    reason=(
                        f"MR SL: {current_price:.4f} ≤ SL {sl_price:.4f} | "
                        f"Cooldown {self.cooldown_candles}c"
                    ),
                    order_type="market",
                )

            # K crossing DOWN below D while overbought → exit
            k_crossed_down = k_prev >= d_prev and k_curr < d_curr
            stoch_overbought_exit = k_curr > self.stochrsi_overbought and k_crossed_down

            # BB %B at or above exit threshold
            bb_exit = bb_pct_b > self.bb_pct_b_exit

            # Secondary RSI overbought
            rsi_exit = rsi_val > self.rsi_overbought

            if stoch_overbought_exit or (bb_exit and rsi_exit):
                trail_pct = self.adaptive_trail_pct(df, self.atr_period)
                pnl_pct = (current_price - self._entry_price) / self._entry_price * 100
                self._in_position = False
                self._entry_price = 0.0

                exit_reasons = []
                if stoch_overbought_exit:
                    exit_reasons.append(
                        f"StochRSI K={k_curr:.1f} crossed below D={d_curr:.1f} (OB)"
                    )
                if bb_exit:
                    exit_reasons.append(f"BB %B={bb_pct_b:.2f} > {self.bb_pct_b_exit}")
                if rsi_exit:
                    exit_reasons.append(f"RSI={rsi_val:.1f} > {self.rsi_overbought}")
                exit_reasons.append(f"PnL≈{pnl_pct:+.2f}%")

                return self.sell(
                    price=current_price,
                    reason=" | ".join(exit_reasons),
                    trailing_tp=True,
                    trail_pct=trail_pct,
                    order_type="limit",
                )

            return self.hold(
                current_price,
                reason=(
                    f"Holding | %B={bb_pct_b:.2f} | "
                    f"StochK={k_curr:.1f} StochD={d_curr:.1f} | "
                    f"RSI={rsi_val:.1f} | "
                    f"SL={self._entry_price*(1-self.stop_loss_pct):.4f}"
                ),
            )

        # ── ENTRY logic ───────────────────────────────────────────────────────
        if self.in_cooldown():
            return self.hold(
                current_price,
                reason=f"MR cooldown: {self._cooldown_remaining} candles remain",
            )

        # Condition A: Price near or below lower Bollinger Band
        bb_oversold = bb_pct_b <= self.bb_pct_b_entry

        # Condition B: StochRSI oversold AND K crossing above D (turning point)
        # K crossing above D while both are in oversold zone is the sharpest
        # entry signal — it fires at the EXACT bottom, not 5 candles after.
        k_crossed_up = k_prev <= d_prev and k_curr > d_curr
        stoch_entry  = k_curr < self.stochrsi_oversold and (k_crossed_up or k_curr > k_prev)

        # Condition C: Volume confirmation
        vol_ok = self.volume_is_sufficient(df) if self.volume_filter else True

        # Condition D: Trend filter
        trend_ok = in_uptrend

        # Condition E: Divergence confirmation (optional tighter filter)
        div_ok = True
        if self.divergence_confirm:
            div_ok = bullish_divergence(
                df,
                rsi_period=self.rsi_period,
                lookback=self.divergence_lookback,
            )

        if bb_oversold and stoch_entry and vol_ok and trend_ok and div_ok:
            sl_price    = current_price * (1 - self.stop_loss_pct)
            tp_price    = bb_upper      # Full mean reversion to upper band
            trail_pct   = self.adaptive_trail_pct(df, self.atr_period)

            self._in_position = True
            self._entry_price = current_price

            return self.buy(
                price=current_price,
                reason=(
                    f"MR v2 entry: BB %B={bb_pct_b:.3f} < {self.bb_pct_b_entry} | "
                    f"StochK={k_curr:.1f} > StochD={d_curr:.1f} (OS crossing up) | "
                    f"RSI={rsi_val:.1f} | "
                    + (f"Divergence confirmed | " if self.divergence_confirm else "")
                    + f"TP≈{tp_price:.4f} (upper BB) | SL={sl_price:.4f}"
                ),
                stop_loss=sl_price,
                take_profit=tp_price,
                trailing_sl=True,
                trail_sl_pct=trail_pct,
                order_type="limit",
            )

        # HOLD: explain what's blocking
        blocking = []
        if not bb_oversold:
            blocking.append(f"BB %B={bb_pct_b:.2f} (need <{self.bb_pct_b_entry})")
        if not stoch_entry:
            blocking.append(f"StochK={k_curr:.1f} (need <{self.stochrsi_oversold} crossing up D={d_curr:.1f})")
        if not vol_ok:
            blocking.append("low volume")
        if not trend_ok:
            blocking.append(f"EMA downtrend")
        if not div_ok:
            blocking.append("no bullish divergence")

        return self.hold(
            current_price,
            reason=(
                f"BB %B={bb_pct_b:.2f} | "
                f"StochK={k_curr:.1f} StochD={d_curr:.1f} | "
                f"RSI={rsi_val:.1f} | "
                f"BB=[{bb_lower:.2f}─{bb_middle:.2f}─{bb_upper:.2f}]"
                + (" | Blocked: " + ", ".join(blocking) if blocking else "")
            ),
        )
