"""
strategies/volatility_breakout.py — Larry Williams Volatility Breakout Strategy

WHAT IS THIS?
─────────────
Larry Williams popularised this strategy in the 1980s after winning the World
Trading Championship with it.  The idea is elegantly simple:

  Each day the market sets a "fair value" reference range from the PREVIOUS
  candle's high-low spread.  A fraction (K) of that range is added to TODAY's
  open to create an entry trigger.

  Entry formula:
      Entry level = Today's open  +  K × (Previous High − Previous Low)

  If price climbs ABOVE that trigger level during the session, it means buyers
  have committed beyond the day's "expected" range → momentum entry signal.

WHY DOES IT WORK?
─────────────────
The previous candle's range measures recent volatility.  Adding a fraction of
that range to today's open creates a threshold that:
  1. Filters out ordinary noise (small moves that don't exceed the threshold)
  2. Captures breakouts driven by genuine buying pressure

K = 0.5 is the classic value (Williams himself used 0.4–0.6 depending on the
market).  Smaller K → more signals, more noise.  Larger K → fewer, cleaner.

FILTERS APPLIED:
────────────────
  1. EMA(ema_period) trend filter:
       Only take long entries when close > EMA — ensures we're trading with
       the medium-term trend, not against it.

  2. ATR volatility gate:
       ATR(14) / close > min_atr_pct (default 0.5%).
       Skips entries on flat, illiquid days where the "breakout" would be
       tiny and unlikely to produce enough profit to cover fees.

  3. Regime filter:
       Only active in BULL, STRONG_BULL, and RANGE regimes.
       In bear markets / crashes the volatility breakout fires on short-lived
       dead-cat bounces and loses consistently.

EXIT RULE:
──────────
Sell at the OPEN of the NEXT candle (original Williams approach).
This is a swing/intraday strategy — hold for exactly one candle period.
The PM calls generate_signal() once per candle; if a position is open,
the next call returns SELL at that candle's open price.

PARAMETERS:
───────────
  k             : Fraction of (prev_high - prev_low) to add to open (default 0.5)
  ema_period    : EMA period for trend filter (default 50)
  atr_period    : ATR period for volatility gate (default 14)
  min_atr_pct   : Minimum ATR/price ratio to allow entry (default 0.005 = 0.5%)
  regime_filter : If True, only trade in BULL / STRONG_BULL / RANGE (default True)
"""

import pandas as pd
import numpy as np
import ta
from loguru import logger

from strategies.base import BaseStrategy, Signal
import config


class VolatilityBreakoutStrategy(BaseStrategy):
    """
    Larry Williams Volatility Breakout: entry above open + K × previous range.

    Simple, rule-based, one-candle hold. Works best in trending/ranging markets
    with sufficient volatility. Registered in portfolio manager alongside the
    other strategies; regime filter restricts it to bull/range conditions.
    """

    def __init__(
        self,
        symbol: str = None,
        timeframe: str = None,
        k: float = 0.5,           # Williams K factor (0.4–0.6 typical range)
        ema_period: int = 50,     # EMA trend direction filter
        atr_period: int = 14,     # ATR volatility gate
        min_atr_pct: float = 0.005,  # Minimum ATR/price = 0.5% (skip flat days)
        stop_loss_pct: float = 0.03,  # 3% hard stop from entry
        regime_filter: bool = True,
    ):
        """
        Args:
            symbol:       Trading pair, e.g. "BTC/USDT".
            timeframe:    Candle size, e.g. "1h" or "4h".
            k:            Williams K factor. Entry = open + k × (prev_high - prev_low).
                          Classic value = 0.5.  Range 0.4–0.6 depending on asset.
            ema_period:   EMA period for trend filter (default 50-candle EMA).
                          Only take longs when close > EMA (avoid buying into downtrends).
            atr_period:   ATR lookback for volatility gate (default 14).
            min_atr_pct:  Minimum ATR / close ratio to allow entry.
                          0.005 = 0.5%.  Skips entry on days with < 0.5% expected move.
                          Protects against paying fees on tiny moves that can't profit.
            stop_loss_pct: Hard stop loss % below entry (default 3%).
            regime_filter: If True, only signal in BULL, STRONG_BULL, or RANGE.
        """
        super().__init__(
            name="VolatilityBreakout",
            symbol=symbol or config.TRADING_PAIR,
            timeframe=timeframe or config.TIMEFRAME,
        )
        self.k             = k
        self.ema_period    = ema_period
        self.atr_period    = atr_period
        self.min_atr_pct   = min_atr_pct
        self.stop_loss_pct = stop_loss_pct
        self.regime_filter = regime_filter

        # State
        self._in_position:    bool  = False
        self._entry_price:    float = 0.0
        self._entry_level:    float = 0.0   # The trigger threshold we entered on
        self._current_regime: str   = "BULL"

        logger.info(
            f"VolatilityBreakout | {self.symbol} | K={k} | "
            f"EMA({ema_period}) trend filter | "
            f"ATR({atr_period}) min_vol={min_atr_pct*100:.1f}% | "
            f"SL={stop_loss_pct*100:.1f}% | "
            f"Regime filter: {'ON' if regime_filter else 'OFF'}"
        )

    # ── External state updater ────────────────────────────────────────────────

    def update_regime(self, regime: str) -> None:
        """
        Set current market regime for the regime filter.

        Call this before generate_signal() each candle from the portfolio manager.

        Args:
            regime: One of "STRONG_BULL", "BULL", "RANGE", "VOLATILE", "BEAR", "CRASH".
        """
        self._current_regime = regime

    # ── Signal generation ─────────────────────────────────────────────────────

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        """
        Check if price has crossed above the Williams VB entry level.

        Returns BUY when entry conditions are met, SELL on the next candle
        (exit at open), HOLD otherwise.

        Args:
            df: OHLCV DataFrame indexed by timestamp. Must have at least
                ema_period + 5 rows.
        """
        min_rows = self.ema_period + 5
        self.validate_dataframe(df, min_rows=min_rows)

        close         = df["close"]
        open_         = df["open"]
        high          = df["high"]
        low           = df["low"]
        current_price = float(close.iloc[-1])
        current_open  = float(open_.iloc[-1])

        # ── Regime filter ─────────────────────────────────────────────────────
        # Only active in bullish or ranging regimes.
        active_regimes = ("STRONG_BULL", "BULL", "RANGE")
        if self.regime_filter and self._current_regime not in active_regimes:
            if self._in_position:
                # Close any open position when regime turns hostile
                self._in_position = False
                self._entry_price = 0.0
                return self.sell(
                    current_open,
                    reason=(
                        f"VolBreakout: Regime → {self._current_regime} "
                        f"(not in {active_regimes}) — forced exit at candle open"
                    ),
                )
            return self.hold(
                current_price,
                reason=(
                    f"Regime={self._current_regime} — VolBreakout only active in "
                    f"STRONG_BULL / BULL / RANGE"
                ),
            )

        # ── Exit: sell at open of the NEXT candle after entry ─────────────────
        # Williams' original rule: "exit at tomorrow's open".
        # We detect this by checking _in_position at the start of each new candle.
        # At the beginning of a new candle the "open" is today's open, and we
        # haven't yet checked entry conditions — so selling at current_open is
        # equivalent to "exit at today's open = next candle after entry open".
        if self._in_position:
            exit_price    = current_open   # Sell at today's open (next candle)
            entry         = self._entry_level
            pnl_pct       = (exit_price - self._entry_price) / self._entry_price * 100
            self._in_position = False
            self._entry_price = 0.0
            self._entry_level = 0.0
            logger.info(
                f"VolBreakout EXIT @ open={exit_price:.4f} | "
                f"Entry was {self._entry_price if self._entry_price else 'unknown'} | "
                f"P&L≈{pnl_pct:.2f}%"
            )
            return self.sell(
                exit_price,
                reason=(
                    f"VolBreakout: Next-candle exit at open={exit_price:.4f} | "
                    f"Entry trigger was {entry:.4f}"
                ),
            )

        # ── Williams VB entry level ───────────────────────────────────────────
        # Formula: Entry = Today's open + K × (Yesterday's high − Yesterday's low)
        # Use the candle BEFORE the current one (iloc[-2]) as "yesterday".
        prev_high   = float(high.iloc[-2])
        prev_low    = float(low.iloc[-2])
        prev_range  = prev_high - prev_low
        entry_level = current_open + self.k * prev_range

        # ── ATR volatility gate ───────────────────────────────────────────────
        # Skip entries on flat, thin days (less than min_atr_pct of price).
        # On such days the potential gain is too small to cover fees reliably.
        atr_val = float(
            ta.volatility.AverageTrueRange(
                high=high, low=low, close=close, window=self.atr_period
            ).average_true_range().iloc[-1]
        )
        atr_pct   = atr_val / current_price if current_price > 0 else 0.0
        atr_ok    = atr_pct >= self.min_atr_pct

        # ── EMA trend filter ──────────────────────────────────────────────────
        # Only take longs when price is above the EMA — with the trend.
        ema_val     = float(close.ewm(span=self.ema_period, adjust=False).mean().iloc[-1])
        ema_bullish = current_price > ema_val

        # ── Entry check ───────────────────────────────────────────────────────
        # Long when: close crossed above entry_level AND EMA bullish AND vol gate OK
        broke_above = current_price > entry_level

        if broke_above and atr_ok and ema_bullish:
            stop_loss = current_price * (1.0 - self.stop_loss_pct)
            self._in_position = True
            self._entry_price = current_price
            self._entry_level = entry_level

            logger.info(
                f"VolBreakout ENTRY | price={current_price:.4f} > "
                f"trigger={entry_level:.4f} (open={current_open:.4f} + "
                f"{self.k}×range={prev_range:.4f}) | "
                f"ATR/price={atr_pct*100:.2f}% | EMA{self.ema_period}={ema_val:.4f} | "
                f"SL={stop_loss:.4f}"
            )
            return self.buy(
                price=current_price,
                reason=(
                    f"VolBreakout: price={current_price:.4f} > "
                    f"entry level={entry_level:.4f} "
                    f"(open + {self.k}×{prev_range:.4f}) | "
                    f"EMA{self.ema_period}={ema_val:.4f} (bullish) | "
                    f"ATR={atr_val:.4f} ({atr_pct*100:.2f}%) | "
                    f"Regime={self._current_regime}"
                ),
                stop_loss=stop_loss,
                max_hold_candles=1,   # Force exit after 1 candle if simulator misses
            )

        # ── HOLD: log why no entry ────────────────────────────────────────────
        reasons = []
        if not broke_above:
            reasons.append(
                f"price={current_price:.4f} ≤ entry={entry_level:.4f} "
                f"(open={current_open:.4f} + {self.k}×range={prev_range:.4f})"
            )
        if not ema_bullish:
            reasons.append(f"EMA{self.ema_period}={ema_val:.4f} > price (downtrend)")
        if not atr_ok:
            reasons.append(
                f"ATR/price={atr_pct*100:.2f}% < {self.min_atr_pct*100:.1f}% "
                f"(market too flat, fee risk)"
            )

        return self.hold(
            current_price,
            reason="VolBreakout: " + " | ".join(reasons) if reasons else
                   f"VolBreakout: No breakout above {entry_level:.4f}",
        )

    def sync_state(self, simulator_has_position: bool) -> None:
        """
        Sync internal flag with simulator after external SL/TP closes.
        Called by PortfolioManager after each tick.
        """
        if self._in_position and not simulator_has_position:
            logger.info(
                "VolBreakout: simulator closed position externally (SL/TP). "
                "Resetting _in_position."
            )
            self._in_position = False
            self._entry_price = 0.0
            self._entry_level = 0.0
