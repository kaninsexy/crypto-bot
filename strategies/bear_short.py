"""
strategies/bear_short.py — BEAR-regime Short Strategy

PURPOSE
───────
All other strategies in the portfolio are long-only. In BEAR and CRASH
regimes the bot currently sits out or DCA-buys the dip. This strategy
fills the gap: it actively shorts (sells borrowed) the downtrend and
closes when the move exhausts.

In paper trading this works out-of-the-box. In live trading it requires
a futures or margin account (e.g. Binance USD-M Futures, OKX Swap).

HOW IT WORKS
────────────
  ┌─────────────────────────────────────────────────────────┐
  │  SHORT ENTRY — all 3 conditions must hold:              │
  │                                                         │
  │  1. Supertrend is BEARISH  (price < Supertrend line)    │
  │     → primary trend filter, ATR-adaptive               │
  │                                                         │
  │  2. EMA(fast) < EMA(slow)  (e.g. EMA20 < EMA50)        │
  │     → confirms medium-term downtrend, not just noise    │
  │                                                         │
  │  3. RSI < rsi_entry_max  (default 55)                   │
  │     → avoids shorting into already-oversold bounces;    │
  │       40–55 is the ideal entry zone (momentum but not   │
  │       yet at capitulation levels)                       │
  └─────────────────────────────────────────────────────────┘

  ┌─────────────────────────────────────────────────────────┐
  │  SHORT EXIT — first condition that triggers wins:       │
  │                                                         │
  │  1. Supertrend FLIPS BULLISH  → trend reversal         │
  │  2. RSI > rsi_exit (default 70) → shorts covering,     │
  │     short-squeeze risk                                  │
  │  3. SL hit → simulator tick_ohlcv_candle fires against  │
  │     candle HIGH (ATR above entry price)                 │
  │  4. TP hit → fixed ATR-based reward target              │
  └─────────────────────────────────────────────────────────┘

STOP LOSS & TAKE PROFIT
───────────────────────
  For shorts, SL is ABOVE entry price (loss if price rises).
  SL  = entry_price + (atr_stop_mult  × ATR)   default: 1.5 × ATR
  TP  = entry_price − (atr_tp_mult    × ATR)   default: 3.0 × ATR

  This gives a 2:1 reward-to-risk ratio by default.
  The simulator fires SL when candle HIGH >= stop_loss (correct for shorts).

INTEGRATION WITH PORTFOLIO MANAGER
────────────────────────────────────
  This strategy is only allocated capital in BEAR/CRASH regimes.
  In BULL/RANGE/VOLATILE, its allocation is 0% so it never fires.
  Add it to REGIME_ALLOCATIONS["BEAR"]["bearshort"] = 0.15 to give it
  15% of BEAR-regime capital (taken from the cash reserve to keep
  total deployment unchanged).

  See portfolio/manager.py: add "BearShort"/"bearshort" to STRATEGY_KEYS
  and BUCKET_KEYS alongside the regime allocation change.

PARAMETERS
──────────
  atr_period       : ATR lookback (default 14)
  atr_stop_mult    : SL = entry ± this × ATR (default 1.5)
  atr_tp_mult      : TP = entry ∓ this × ATR (default 3.0)
  supertrend_period: Supertrend ATR lookback (default 10)
  supertrend_mult  : Supertrend band multiplier (default 3.0)
  ema_fast         : Fast EMA period (default 20)
  ema_slow         : Slow EMA period (default 50)
  rsi_period       : RSI lookback (default 14)
  rsi_entry_max    : Max RSI allowed for entry (default 55)
  rsi_exit         : RSI above this → force exit (default 70)
  leverage         : Futures leverage (default 1 = spot margin)
  macd_confirm     : If True, require MACD histogram < 0 for entry (default True).
                     Ensures bearish momentum is accelerating, not just starting.
                     Eliminates shorting into a consolidation that merely looks bearish.
  macd_fast        : MACD fast EMA period (default 12)
  macd_slow        : MACD slow EMA period (default 26)
  macd_signal      : MACD signal EMA period (default 9)
"""

import numpy as np
import pandas as pd
import ta
from loguru import logger

from strategies.base import BaseStrategy, Signal
import config


class BearShortStrategy(BaseStrategy):
    """
    Supertrend + EMA-confirmation short strategy for BEAR/CRASH regimes.

    Shorts into confirmed downtrends and exits on trend reversal or RSI extreme.
    Designed to profit when every other strategy in the portfolio is bleeding.
    """

    def __init__(
        self,
        symbol:            str   = None,
        timeframe:         str   = None,
        # Supertrend
        supertrend_period: int   = 10,
        supertrend_mult:   float = 3.0,
        # EMA trend confirmation
        ema_fast:          int   = 20,
        ema_slow:          int   = 50,
        # RSI entry/exit filters
        rsi_period:        int   = 14,
        rsi_entry_max:     float = 55.0,   # Only short if RSI < this (not already oversold)
        rsi_exit:          float = 70.0,   # Exit short if RSI spikes above this
        # Risk / sizing
        atr_period:        int   = 14,
        atr_stop_mult:     float = 1.5,    # SL = entry + 1.5 × ATR  (above entry for short)
        atr_tp_mult:       float = 3.0,    # TP = entry − 3.0 × ATR  (below entry for short)
        leverage:          int   = 1,
        # MACD momentum confirmation
        macd_confirm:      bool  = True,   # Require MACD hist < 0 at entry
        macd_fast:         int   = 12,
        macd_slow:         int   = 26,
        macd_signal:       int   = 9,
    ):
        super().__init__(
            name="BearShort",
            symbol=symbol or config.TRADING_PAIR,
            timeframe=timeframe or config.TIMEFRAME,
        )
        self.supertrend_period = supertrend_period
        self.supertrend_mult   = supertrend_mult
        self.ema_fast          = ema_fast
        self.ema_slow          = ema_slow
        self.rsi_period        = rsi_period
        self.rsi_entry_max     = rsi_entry_max
        self.rsi_exit          = rsi_exit
        self.atr_period        = atr_period
        self.atr_stop_mult     = atr_stop_mult
        self.atr_tp_mult       = atr_tp_mult
        self.leverage          = leverage
        self.macd_confirm      = macd_confirm
        self.macd_fast         = macd_fast
        self.macd_slow         = macd_slow
        self.macd_signal       = macd_signal

        # Internal state
        self._in_short:    bool  = False
        self._entry_price: float = 0.0   # Tracks entry price for P&L reporting
        self._prev_direction: int = 0    # 1 = up, -1 = down

        logger.info(
            f"BearShort | ST({supertrend_period}, {supertrend_mult}) | "
            f"EMA({ema_fast}/{ema_slow}) | RSI entry<{rsi_entry_max} exit>{rsi_exit} | "
            f"SL={atr_stop_mult}×ATR  TP={atr_tp_mult}×ATR | Leverage={leverage}x | "
            f"MACD confirm={'ON' if macd_confirm else 'OFF'}"
        )

    # ── Supertrend computation ────────────────────────────────────────────────

    def _compute_supertrend(
        self, df: pd.DataFrame
    ) -> tuple[pd.Series, pd.Series]:
        """
        Compute the Supertrend indicator.

        Returns:
            (supertrend_line, direction)
            direction: +1 = uptrend (price above line), -1 = downtrend
        """
        high  = df["high"]
        low   = df["low"]
        close = df["close"]

        atr_series = ta.volatility.AverageTrueRange(
            high=high, low=low, close=close, window=self.supertrend_period
        ).average_true_range()

        mid        = (high + low) / 2
        upper_raw  = mid + self.supertrend_mult * atr_series
        lower_raw  = mid - self.supertrend_mult * atr_series

        supertrend = pd.Series(index=df.index, dtype=float)
        direction  = pd.Series(index=df.index, dtype=int)

        supertrend.iloc[0] = upper_raw.iloc[0]
        direction.iloc[0]  = -1

        for i in range(1, len(df)):
            prev_close = close.iloc[i - 1]
            prev_upper = supertrend.iloc[i - 1] if direction.iloc[i - 1] == -1 else upper_raw.iloc[i]
            prev_lower = supertrend.iloc[i - 1] if direction.iloc[i - 1] ==  1 else lower_raw.iloc[i]

            # Lower band (uptrend support): ratchets up, never falls
            curr_lower = lower_raw.iloc[i]
            if curr_lower > prev_lower or prev_close < prev_lower:
                final_lower = curr_lower
            else:
                final_lower = prev_lower

            # Upper band (downtrend resistance): ratchets down, never rises
            curr_upper = upper_raw.iloc[i]
            if curr_upper < prev_upper or prev_close > prev_upper:
                final_upper = curr_upper
            else:
                final_upper = prev_upper

            prev_dir = direction.iloc[i - 1]
            if prev_dir == -1 and close.iloc[i] > final_upper:
                direction.iloc[i]  = 1           # Flip to uptrend
                supertrend.iloc[i] = final_lower
            elif prev_dir == 1 and close.iloc[i] < final_lower:
                direction.iloc[i]  = -1          # Flip to downtrend
                supertrend.iloc[i] = final_upper
            elif prev_dir == -1:
                direction.iloc[i]  = -1
                supertrend.iloc[i] = final_upper
            else:
                direction.iloc[i]  = 1
                supertrend.iloc[i] = final_lower

        return supertrend, direction

    # ── Signal generation ─────────────────────────────────────────────────────

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        """
        Generate SHORT, CLOSE, or HOLD signal based on Supertrend + EMA + RSI.

        SHORT opens via BUY with is_short=True.
        CLOSE exits via SELL.
        """
        min_rows = max(
            self.supertrend_period + 10,
            self.ema_slow + 5,
            self.rsi_period + 5,
            self.atr_period + 5,
        )
        self.validate_dataframe(df, min_rows=min_rows)

        close         = df["close"]
        current_price = float(close.iloc[-1])

        # ── Indicators ────────────────────────────────────────────────────────
        supertrend, direction = self._compute_supertrend(df)
        curr_dir  = int(direction.iloc[-1])
        prev_dir  = int(direction.iloc[-2])
        st_line   = float(supertrend.iloc[-1])

        ema_fast_val = float(
            close.ewm(span=self.ema_fast, adjust=False).mean().iloc[-1]
        )
        ema_slow_val = float(
            close.ewm(span=self.ema_slow, adjust=False).mean().iloc[-1]
        )

        rsi_val = float(
            ta.momentum.RSIIndicator(close, window=self.rsi_period)
            .rsi().iloc[-1]
        )

        current_atr = float(
            ta.volatility.AverageTrueRange(
                high=df["high"], low=df["low"], close=close,
                window=self.atr_period,
            ).average_true_range().iloc[-1]
        )

        # MACD histogram — negative means bearish momentum is dominant.
        # We compute it regardless of macd_confirm so it can appear in logs.
        macd_ind  = ta.trend.MACD(
            close,
            window_fast=self.macd_fast,
            window_slow=self.macd_slow,
            window_sign=self.macd_signal,
        )
        macd_hist = float(macd_ind.macd_diff().iloc[-1])
        macd_ok   = (not self.macd_confirm) or (macd_hist < 0)

        trend_bearish = curr_dir == -1                     # Supertrend says downtrend
        ema_bearish   = ema_fast_val < ema_slow_val        # EMA confirms downtrend

        # ── EXIT existing short ───────────────────────────────────────────────
        if self._in_short:
            st_flipped_bullish = (curr_dir == 1 and prev_dir == -1)
            rsi_extreme        = rsi_val > self.rsi_exit

            if st_flipped_bullish or rsi_extreme:
                self._in_short    = False
                self._entry_price = 0.0
                exit_reason = (
                    f"Supertrend flipped BULLISH @ {current_price:.4f}"
                    if st_flipped_bullish
                    else f"RSI={rsi_val:.1f} > {self.rsi_exit} — shorts covering"
                )
                logger.info(f"BearShort EXIT: {exit_reason}")
                return self.sell(
                    price=current_price,
                    reason=exit_reason,
                    quantity_pct=1.0,
                    order_type="market",
                )

            # Still in short — hold and report status
            pnl_dir = "profit" if current_price < self._entry_price else "loss"
            pnl_pct = abs(current_price - self._entry_price) / self._entry_price * 100
            return self.hold(
                current_price,
                reason=(
                    f"Short active @ {self._entry_price:.4f} | "
                    f"Now {current_price:.4f} ({pnl_dir} {pnl_pct:.2f}%) | "
                    f"ST={st_line:.2f} (bearish) | RSI={rsi_val:.1f} | "
                    f"MACD hist={macd_hist:.4f} | "
                    f"Exit: ST flip OR RSI>{self.rsi_exit}"
                )
            )

        # ── ENTER short ───────────────────────────────────────────────────────
        st_just_flipped_bearish = (curr_dir == -1 and prev_dir == 1)
        already_bearish         = (curr_dir == -1)

        # Entry requires:
        #  1. Supertrend is bearish (just flipped or already bearish)
        #  2. EMA confirms downtrend (fast < slow)
        #  3. RSI not yet oversold (avoid entering near capitulation)
        #  4. MACD histogram < 0 (bearish momentum present) — if macd_confirm=True
        entry_ok = (
            (st_just_flipped_bearish or already_bearish)
            and ema_bearish
            and rsi_val < self.rsi_entry_max
            and macd_ok
        )

        if not entry_ok:
            reasons = []
            if not trend_bearish:
                reasons.append("ST=BULLISH")
            if not ema_bearish:
                reasons.append(f"EMA{self.ema_fast}>{self.ema_slow} (uptrend)")
            if rsi_val >= self.rsi_entry_max:
                reasons.append(f"RSI={rsi_val:.1f}>={self.rsi_entry_max} (overbought entry blocked)")
            if not macd_ok:
                reasons.append(f"MACD hist={macd_hist:.4f}>0 (no bearish momentum)")
            return self.hold(
                current_price,
                reason=(
                    f"No short entry: {' | '.join(reasons) or 'conditions not met'} | "
                    f"ST={st_line:.2f} | RSI={rsi_val:.1f} | MACD hist={macd_hist:.4f}"
                )
            )

        # Compute ATR-based SL and TP
        # For shorts: SL is ABOVE entry (price rising = loss)
        #             TP is BELOW entry (price falling = profit)
        stop_loss   = current_price + self.atr_stop_mult * current_atr
        take_profit = current_price - self.atr_tp_mult  * current_atr
        rr_ratio    = self.atr_tp_mult / self.atr_stop_mult

        self._in_short    = True
        self._entry_price = current_price
        entry_type = "flip" if st_just_flipped_bearish else "continuation"

        logger.info(
            f"BearShort ENTER ({entry_type}): "
            f"price={current_price:.4f} | "
            f"SL={stop_loss:.4f} (+{self.atr_stop_mult}×ATR) | "
            f"TP={take_profit:.4f} (-{self.atr_tp_mult}×ATR) | "
            f"R:R={rr_ratio:.1f} | RSI={rsi_val:.1f} | "
            f"MACD hist={macd_hist:.4f} | ATR={current_atr:.4f}"
        )

        return self.buy(
            price       = current_price,
            reason      = (
                f"BearShort {entry_type}: ST bearish | "
                f"EMA{self.ema_fast}({ema_fast_val:.2f})<EMA{self.ema_slow}({ema_slow_val:.2f}) | "
                f"RSI={rsi_val:.1f} | MACD hist={macd_hist:.4f} | "
                f"ATR={current_atr:.4f} | R:R={rr_ratio:.1f}"
            ),
            stop_loss   = stop_loss,
            take_profit = take_profit,
            is_short    = True,
            leverage    = self.leverage,
            order_type  = "market",   # Market fill for shorts (faster execution)
        )

    def sync_state(self, simulator_has_position: bool) -> None:
        """
        Sync internal flag with simulator after external SL/TP closes.
        Called by PortfolioManager after each tick.
        """
        if self._in_short and not simulator_has_position:
            logger.info(
                "BearShort: simulator closed short externally (SL/TP). "
                "Resetting _in_short to allow re-entry."
            )
            self._in_short    = False
            self._entry_price = 0.0
