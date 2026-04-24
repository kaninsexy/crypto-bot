"""
strategies/supertrend.py — Supertrend Strategy (ATR-based Trend Following)

HOW IT WORKS:
  Supertrend is a trend-following indicator that uses ATR (Average True Range)
  to set a dynamic stop level that adapts to market volatility. It's widely
  considered superior to simple EMA crossovers for crypto because:
    - ATR means the stop widens in volatile markets (avoids getting stopped out
      by normal crypto noise) and tightens in calm markets.
    - It gives a single clean line: when price is above it = uptrend (BUY),
      below it = downtrend (SELL). No ambiguity.

THE MATH (don't worry, the code handles this):
  1. ATR = Average True Range over `atr_period` candles (measures volatility)
  2. Midpoint = (high + low) / 2
  3. Basic Upper Band = Midpoint + multiplier × ATR
  4. Basic Lower Band = Midpoint − multiplier × ATR
  5. Final bands adjust to never move against the trend (ratchet effect)
  6. Direction: UP if close > upper band, DOWN if close < lower band

RECOMMENDED SETTINGS FOR CRYPTO (4h candles):
  ATR period:  14  (standard)
  Multiplier:  3.5  (wider than stock default of 3 to handle crypto volatility)

BTC FILTER (for altcoin trading):
  Altcoins bleed hardest when BTC is bearish. The BTC filter prevents opening
  altcoin longs when BTC's own Supertrend is pointing down. This is the single
  most effective rule for avoiding altcoin blow-ups.

  Usage:
    # Before running strategy on SOL/USDT:
    strategy.update_btc_trend(btc_df)
    signal = strategy.generate_signal(sol_df)

SIGNALS:
  BUY:  Supertrend flips from DOWN → UP (trend reversal bullish)
  SELL: Supertrend flips from UP → DOWN (trend reversal bearish)
  HOLD: Trend continues in same direction (no flip)

STOP LOSS:
  The Supertrend line itself IS the trailing stop loss.
  For longs: stop = current Supertrend line value (moves up as trend continues)
  For shorts (futures): stop = current Supertrend line value
"""

import numpy as np
import pandas as pd
import ta
from loguru import logger

from strategies.base import BaseStrategy, Signal
import config


class SupertrendStrategy(BaseStrategy):
    """
    Supertrend indicator strategy with optional BTC trend filter for altcoins.
    """

    def __init__(
        self,
        symbol: str = None,
        timeframe: str = None,
        atr_period: int = 10,       # Updated from 14: faster response to trend changes
        multiplier: float = 2.5,    # Updated from 3.0: tighter bands, more signals on 1H
        btc_filter: bool = True,
        htf_filter: bool = True,    # 4H direction filter: only long when 4H agrees
    ):
        """
        Args:
            symbol:     Trading pair, e.g. "BTC/USDT", "SOL/USDT".
            timeframe:  Candle size, e.g. "4h", "1h".
            atr_period: ATR lookback period (default 14 = standard).
            multiplier: How many ATRs to offset the bands (default 3.5 for crypto).
                        Higher = wider bands = fewer but more reliable signals.
            btc_filter: If True and symbol is not BTC, only allow longs when
                        BTC Supertrend is also bullish. Always allow shorts.
        """
        super().__init__(
            name="Supertrend",
            symbol=symbol or config.TRADING_PAIR,
            timeframe=timeframe or config.TIMEFRAME,
        )
        self.atr_period = atr_period
        self.multiplier = multiplier
        self.btc_filter = btc_filter
        self.htf_filter = htf_filter

        # ── ATR trailing stop (independent of Supertrend ATR params) ─────────
        # The Supertrend line (st_value) acts as the static floor stop.
        # An additional ATR(14)×3.0 trailing stop is layered on top:
        #   initial stop = max(st_value, entry − ATR(14)×3.0)
        # The trail only activates if it rises above the Supertrend floor.
        # trail_sl_pct encodes ATR×3.0 as a % of entry price; the simulator
        # ratchets SL = peak × (1 − trail_sl_pct), only ever moving UP.
        self._atr_trail_period: int   = 14
        self._atr_trail_mult:   float = 3.0

        # BTC filter state — updated externally via update_btc_trend()
        self._btc_trend_up: bool = True   # Default to True for BTC/USDT itself

        # 4H direction filter state — updated externally via update_htf_trend()
        self._htf_bullish: bool = True    # Default permissive until first update

        # Track previous direction to detect flips
        self._prev_direction: int = 0     # 1 = up, -1 = down, 0 = unknown

        is_btc = "BTC" in (symbol or config.TRADING_PAIR).upper()
        if is_btc:
            self.btc_filter = False  # BTC doesn't need to filter itself

        logger.info(
            f"Supertrend | {self.symbol} | ATR({atr_period}) × {multiplier} | "
            f"BTC filter: {'ON' if self.btc_filter else 'OFF'} | "
            f"4H filter: {'ON' if self.htf_filter else 'OFF'}"
        )

    # ─── BTC Filter ───────────────────────────────────────────────────────────

    def update_btc_trend(self, btc_df: pd.DataFrame) -> str:
        """
        Compute BTC's Supertrend and store whether it's bullish or bearish.
        Call this BEFORE generate_signal() when trading non-BTC pairs.

        Args:
            btc_df: OHLCV DataFrame for BTC/USDT.

        Returns:
            "up" if BTC is in uptrend, "down" if in downtrend.
        """
        _, direction = self._compute_supertrend(btc_df)
        self._btc_trend_up = bool(direction.iloc[-1] == 1)
        status = "↑ BTC UPTREND" if self._btc_trend_up else "↓ BTC DOWNTREND"
        logger.debug(f"BTC filter updated: {status}")
        return "up" if self._btc_trend_up else "down"

    def update_htf_trend(self, htf_df: pd.DataFrame) -> str:
        """
        Compute Supertrend on the 4H candles and store whether it's bullish.
        Call this BEFORE generate_signal() each candle.

        Args:
            htf_df: OHLCV DataFrame for the same symbol on 4H timeframe.

        Returns:
            "up" if 4H trend is bullish, "down" if bearish.
        """
        if htf_df is None or len(htf_df) < self.atr_period + 5:
            return "up"  # Permissive default during warmup
        _, direction = self._compute_supertrend(htf_df)
        self._htf_bullish = bool(direction.iloc[-1] == 1)
        status = "↑ 4H BULLISH" if self._htf_bullish else "↓ 4H BEARISH"
        logger.debug(f"4H filter updated: {status}")
        return "up" if self._htf_bullish else "down"

    # ─── Core Supertrend Calculation ──────────────────────────────────────────

    def _compute_supertrend(
        self, df: pd.DataFrame
    ) -> tuple[pd.Series, pd.Series]:
        """
        Compute the Supertrend line and direction for a given OHLCV DataFrame.

        Returns:
            supertrend: Series of Supertrend line values (the dynamic stop level)
            direction:  Series of 1 (uptrend) or -1 (downtrend)
        """
        high = df["high"]
        low = df["low"]
        close = df["close"]

        # Step 1: ATR
        atr = ta.volatility.AverageTrueRange(
            high=high, low=low, close=close, window=self.atr_period
        ).average_true_range()

        # Step 2: Basic bands
        hl2 = (high + low) / 2.0
        basic_upper = hl2 + self.multiplier * atr
        basic_lower = hl2 - self.multiplier * atr

        # Step 3 & 4: Final bands + direction — numpy arrays for C-speed indexing.
        # The recurrence (each value depends on the previous) prevents full
        # vectorization, but raw numpy indexing is ~10x faster than pandas .iloc.
        n = len(df)
        bu = basic_upper.to_numpy(dtype=np.float64)
        bl = basic_lower.to_numpy(dtype=np.float64)
        cl = close.to_numpy(dtype=np.float64)

        fu = bu.copy()
        fl = bl.copy()

        for i in range(1, n):
            fu[i] = bu[i] if (bu[i] < fu[i - 1] or cl[i - 1] > fu[i - 1]) else fu[i - 1]
            fl[i] = bl[i] if (bl[i] > fl[i - 1] or cl[i - 1] < fl[i - 1]) else fl[i - 1]

        st  = np.empty(n, dtype=np.float64)
        dir_ = np.empty(n, dtype=np.int64)
        st[0]   = fu[0]
        dir_[0] = -1

        for i in range(1, n):
            if st[i - 1] == fu[i - 1]:
                # Was in downtrend
                if cl[i] > fu[i]:
                    dir_[i] = 1
                    st[i]   = fl[i]
                else:
                    dir_[i] = -1
                    st[i]   = fu[i]
            else:
                # Was in uptrend
                if cl[i] < fl[i]:
                    dir_[i] = -1
                    st[i]   = fu[i]
                else:
                    dir_[i] = 1
                    st[i]   = fl[i]

        return pd.Series(st, index=df.index), pd.Series(dir_, index=df.index)

    # ─── Signal Generation ────────────────────────────────────────────────────

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        """
        Compute Supertrend and return BUY on bullish flip, SELL on bearish flip.

        A BUY is only generated when:
          - Supertrend flips from DOWN → UP
          - BTC filter is satisfied (if enabled): BTC must also be in uptrend

        A SELL is generated when:
          - Supertrend flips from UP → DOWN
          - (No BTC filter on sells — always exit or short)

        Args:
            df: OHLCV DataFrame. Needs at least atr_period + 10 rows.

        Returns:
            Signal with action BUY, SELL, or HOLD.
        """
        min_rows = self.atr_period + 20
        self.validate_dataframe(df, min_rows=min_rows)

        current_price = float(df["close"].iloc[-1])

        # Compute Supertrend
        supertrend, direction = self._compute_supertrend(df)

        curr_dir = int(direction.iloc[-1])       # 1 = up, -1 = down
        prev_dir = int(direction.iloc[-2])       # Previous candle direction
        st_value = float(supertrend.iloc[-1])    # Current Supertrend line (= dynamic stop)

        # Detect trend flip
        bullish_flip = (prev_dir == -1) and (curr_dir == 1)
        bearish_flip = (prev_dir == 1) and (curr_dir == -1)

        trend_label = "↑ UPTREND" if curr_dir == 1 else "↓ DOWNTREND"

        # ── ATR(14) for trailing stop — computed once per candle ──────────────
        # Uses a fixed 14-period ATR, independent of the Supertrend's own ATR
        # (which uses atr_period=10 and multiplier=2.5 for the signal itself).
        atr_14 = ta.volatility.AverageTrueRange(
            high=df["high"], low=df["low"], close=df["close"],
            window=self._atr_trail_period,
        ).average_true_range()
        current_atr = float(atr_14.iloc[-1])

        # ── BUY Signal ────────────────────────────────────────────────────────
        if bullish_flip:
            # BTC filter: skip longs on altcoins if BTC is bearish
            if self.btc_filter and not self._btc_trend_up:
                return self.hold(
                    price=current_price,
                    reason=(
                        f"Bullish flip detected but BTC filter blocked long | "
                        f"Supertrend={st_value:.2f} | BTC is in downtrend"
                    )
                )
            # 4H filter: only take 1H longs when 4H Supertrend agrees
            if self.htf_filter and not self._htf_bullish:
                return self.hold(
                    price=current_price,
                    reason=(
                        f"Bullish flip detected but 4H filter blocked long | "
                        f"Supertrend={st_value:.2f} | 4H trend is bearish"
                    )
                )

            # Supertrend line = static floor stop (the band that just flipped)
            # ATR(14)×3.0 stop = entry − ATR×3.0 (volatility-adaptive)
            # Use max so the floor is always respected; trail only activates above it.
            atr_stop     = current_price - (current_atr * self._atr_trail_mult)
            stop_loss    = max(st_value, atr_stop)
            trail_sl_pct = (current_atr * self._atr_trail_mult) / current_price

            return self.buy(
                price=current_price,
                reason=(
                    f"Supertrend flipped BULLISH | Line={st_value:.2f} (floor stop) | "
                    f"ATR({self._atr_trail_period})={current_atr:.4f} | "
                    f"SL={stop_loss:.4f} ({'ATR' if atr_stop >= st_value else 'Supertrend'} floor) | "
                    f"trail={trail_sl_pct*100:.2f}% (ATR×{self._atr_trail_mult})"
                ),
                stop_loss=stop_loss,
                take_profit=None,            # No fixed TP — ride until bearish flip
                trailing_sl=True,
                trail_sl_pct=trail_sl_pct,
            )

        # ── SELL Signal ───────────────────────────────────────────────────────
        if bearish_flip:
            return self.sell(
                price=current_price,
                reason=f"Supertrend flipped BEARISH | Line={st_value:.2f} | Consider futures short",
            )

        # ── HOLD ─────────────────────────────────────────────────────────────
        btc_status = ""
        if self.btc_filter:
            btc_status = f" | BTC: {'↑' if self._btc_trend_up else '↓'}"

        return self.hold(
            price=current_price,
            reason=(
                f"{trend_label} | Supertrend line={st_value:.2f} | "
                f"Distance from stop: {abs(current_price - st_value):.2f} "
                f"({abs(current_price - st_value) / current_price * 100:.2f}%)"
                f"{btc_status}"
            )
        )
