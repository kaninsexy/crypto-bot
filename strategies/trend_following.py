"""
strategies/trend_following.py — Trend Following Strategy (EMA Crossover + MACD)

HOW IT WORKS:
  Trend following is based on a simple idea: "the trend is your friend."
  We use two tools together to identify and confirm trends:

  1. EMA CROSSOVER (primary signal):
     We track two Exponential Moving Averages (EMAs):
     - Fast EMA (default: 9 periods) — reacts quickly to price changes
     - Slow EMA (default: 21 periods) — smoother, shows the bigger trend

     BUY signal:  Fast EMA crosses ABOVE Slow EMA → uptrend starting
     SELL signal: Fast EMA crosses BELOW Slow EMA → downtrend starting

     Why EMA instead of SMA (Simple Moving Average)?
     EMA gives more weight to recent prices, so it reacts faster to
     trend changes while still filtering out random noise.

  2. MACD CONFIRMATION (filter):
     MACD (Moving Average Convergence Divergence) is a momentum indicator.
     We use it to confirm that the EMA crossover signal is genuine and not
     a false signal (whipsaw).

     MACD Line     = 12-period EMA - 26-period EMA
     Signal Line   = 9-period EMA of MACD Line
     Histogram     = MACD Line - Signal Line

     We only act on EMA crossover signals when MACD confirms:
     - For BUY: MACD histogram is positive (momentum is bullish)
     - For SELL: MACD histogram is negative (momentum is bearish)

PARAMETERS:
  fast_ema    : Fast EMA period (default: 9)
  slow_ema    : Slow EMA period (default: 21)
  macd_fast   : MACD fast period (default: 12)
  macd_slow   : MACD slow period (default: 26)
  macd_signal : MACD signal period (default: 9)

VISUAL EXAMPLE:
  Price:     ──────╮╭──────
  Fast EMA:  ────╮╭─────── (crosses above slow = BUY)
  Slow EMA:  ──────────────
                 ↑ BUY here
"""

import pandas as pd
import ta
from loguru import logger

from strategies.base import BaseStrategy, Signal
import config


class TrendFollowingStrategy(BaseStrategy):
    """
    Trend following using EMA crossover confirmed by MACD.
    """

    def __init__(
        self,
        symbol: str = None,
        timeframe: str = None,
        fast_ema: int = 9,
        slow_ema: int = 21,
        macd_fast: int = 12,
        macd_slow: int = 26,
        macd_signal: int = 9,
    ):
        """
        Args:
            symbol:      Trading pair, e.g. "BTC/USDT".
            timeframe:   Candle size, e.g. "1h".
            fast_ema:    Fast EMA period. Smaller = more sensitive to price.
            slow_ema:    Slow EMA period. Must be > fast_ema.
            macd_fast:   MACD fast EMA period.
            macd_slow:   MACD slow EMA period.
            macd_signal: MACD signal line period.
        """
        super().__init__(
            name="TrendFollowing",
            symbol=symbol or config.TRADING_PAIR,
            timeframe=timeframe or config.TIMEFRAME,
        )

        if fast_ema >= slow_ema:
            raise ValueError(f"fast_ema ({fast_ema}) must be less than slow_ema ({slow_ema})")

        self.fast_ema = fast_ema
        self.slow_ema = slow_ema
        self.macd_fast = macd_fast
        self.macd_slow = macd_slow
        self.macd_signal = macd_signal

        logger.info(
            f"Trend Following | EMA({fast_ema}/{slow_ema}) + MACD({macd_fast},{macd_slow},{macd_signal})"
        )

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        """
        Compute EMA crossover and MACD, then return BUY/SELL/HOLD.

        Args:
            df: OHLCV DataFrame. Needs at least slow_ema + macd_slow rows.

        Returns:
            Signal with action BUY, SELL, or HOLD.
        """
        min_rows = max(self.slow_ema, self.macd_slow) + 10
        self.validate_dataframe(df, min_rows=min_rows)

        close = df["close"]
        current_price = float(close.iloc[-1])

        # ── Compute indicators ────────────────────────────────────────────────

        # EMA values using the 'ta' library
        ema_fast = ta.trend.ema_indicator(close, window=self.fast_ema)
        ema_slow = ta.trend.ema_indicator(close, window=self.slow_ema)

        # MACD
        macd = ta.trend.MACD(
            close,
            window_fast=self.macd_fast,
            window_slow=self.macd_slow,
            window_sign=self.macd_signal,
        )
        macd_histogram = macd.macd_diff()  # Positive = bullish momentum

        # Get the last two rows to detect crossovers
        # A crossover is when the relationship between fast/slow FLIPS
        ema_fast_prev = float(ema_fast.iloc[-2])
        ema_fast_curr = float(ema_fast.iloc[-1])
        ema_slow_prev = float(ema_slow.iloc[-2])
        ema_slow_curr = float(ema_slow.iloc[-1])
        histogram_curr = float(macd_histogram.iloc[-1])

        # ── Detect crossovers ─────────────────────────────────────────────────

        # Bullish crossover: fast was BELOW slow, now fast is ABOVE slow
        bullish_cross = (ema_fast_prev <= ema_slow_prev) and (ema_fast_curr > ema_slow_curr)

        # Bearish crossover: fast was ABOVE slow, now fast is BELOW slow
        bearish_cross = (ema_fast_prev >= ema_slow_prev) and (ema_fast_curr < ema_slow_curr)

        # MACD confirmation
        macd_bullish = histogram_curr > 0
        macd_bearish = histogram_curr < 0

        # ── Generate signal ───────────────────────────────────────────────────

        if bullish_cross and macd_bullish:
            stop_loss = current_price * (1 - config.STOP_LOSS_PCT)
            return self.buy(
                price=current_price,
                reason=(
                    f"Bullish EMA crossover: EMA{self.fast_ema}={ema_fast_curr:.2f} "
                    f"crossed above EMA{self.slow_ema}={ema_slow_curr:.2f} | "
                    f"MACD histogram: {histogram_curr:.4f} (bullish)"
                ),
                stop_loss=stop_loss,
                trailing_sl=True,
                trail_sl_pct=config.STOP_LOSS_PCT,  # Trail at same % as initial SL
            )

        elif bearish_cross and macd_bearish:
            return self.sell(
                price=current_price,
                reason=(
                    f"Bearish EMA crossover: EMA{self.fast_ema}={ema_fast_curr:.2f} "
                    f"crossed below EMA{self.slow_ema}={ema_slow_curr:.2f} | "
                    f"MACD histogram: {histogram_curr:.4f} (bearish)"
                ),
            )

        # No crossover — report current state
        trend = "↑ UPTREND" if ema_fast_curr > ema_slow_curr else "↓ DOWNTREND"
        return self.hold(
            price=current_price,
            reason=(
                f"No crossover | {trend} | "
                f"EMA{self.fast_ema}={ema_fast_curr:.2f}, EMA{self.slow_ema}={ema_slow_curr:.2f} | "
                f"MACD hist={histogram_curr:.4f}"
            ),
        )
