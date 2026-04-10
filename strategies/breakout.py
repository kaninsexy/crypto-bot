"""
strategies/breakout.py — Breakout + Volume Confirmation Strategy

HOW IT WORKS:
  Breakout trading catches the moment price escapes a defined range with
  conviction (volume). The core idea: if price has been stuck between a
  floor (support) and a ceiling (resistance), and then breaks through with
  a surge in volume, a strong directional move usually follows.

  Without volume confirmation, most breakouts are fake-outs (price spikes
  through a level then immediately reverses). Volume is the evidence that
  real buyers/sellers are committed to the move.

SIGNAL LOGIC:
  1. Look back N candles to find:
       - Resistance = highest high (potential breakout level UP)
       - Support    = lowest low  (potential breakdown level DOWN)
  2. Breakout UP:   current close > resistance AND volume > avg_volume × multiplier
  3. Breakdown DOWN: current close < support AND volume > avg_volume × multiplier
  4. Multi-timeframe (MTF) check: 4h Supertrend must agree with direction
     (only if mtf_enabled=True)

STOP LOSS & TAKE PROFIT:
  - Stop loss:   ATR-based. Entry ± (atr_stop_multiplier × ATR)
                 This adapts to volatility — wider in choppy markets,
                 tighter in calm ones.
  - Take profit: stop_distance × reward_ratio (default 2.0 = 1:2 risk/reward)

DYNAMIC LEVERAGE (for futures):
  Leverage scales inversely with volatility (ATR ratio):
    - ATR is low (calm)   → up to max_leverage (3x)
    - ATR is medium       → 2x
    - ATR is high (chaotic) → 1x or skip
  Additional rule: only use full leverage when BOTH volume and MTF confirm.

SENTIMENT FILTER (optional, uses LunarCrush):
  When sentiment_score is provided (0-100):
    - sentiment > 60 → full position size
    - sentiment 40-60 → 75% position size (mild uncertainty)
    - sentiment < 40 → 50% position size (negative buzz, be cautious)
  Never blocks a trade outright — only scales size.

PARAMETERS:
  lookback        : Candles to look back for support/resistance (default: 20)
  volume_mult     : Volume must exceed avg by this factor (default: 1.5)
  atr_period      : ATR period for stop sizing (default: 14)
  atr_stop_mult   : ATR multiplier for stop distance (default: 2.0)
  reward_ratio    : Take profit = stop × this ratio (default: 2.0)
  max_leverage    : Maximum futures leverage (default: 3)
  mtf_enabled     : Require 4h Supertrend confirmation (default: True)
"""

import pandas as pd
import numpy as np
import ta
from loguru import logger

from strategies.base import BaseStrategy, Signal
import config


class BreakoutStrategy(BaseStrategy):
    """
    Volume-confirmed breakout strategy with dynamic leverage,
    multi-timeframe confirmation, and optional sentiment sizing.
    """

    def __init__(
        self,
        symbol: str = None,
        timeframe: str = None,
        lookback: int = 20,
        volume_mult: float = 2.0,   # Raised to 2.0× (research: 1.5× too many fake-outs)
        atr_period: int = 14,
        atr_stop_mult: float = 2.0,
        reward_ratio: float = 2.0,
        max_leverage: int = 3,
        mtf_enabled: bool = True,
        adx_filter: bool = True,
        adx_period: int = 14,
        adx_threshold: float = 25.0,
        # Regime filter: only trade when market has directional momentum
        regime_filter: bool = True,
    ):
        """
        Args:
            symbol:        Trading pair, e.g. "BTC/USDT".
            timeframe:     Primary candle size, e.g. "1h".
            lookback:      How many candles to scan for support/resistance.
            volume_mult:   Volume threshold relative to average.
                           1.5 = volume must be 50% above average.
            atr_period:    ATR lookback for dynamic stop sizing.
            atr_stop_mult:  Stop placed this many ATRs from entry.
            reward_ratio:   Take profit = atr_stop_mult × reward_ratio ATRs from entry.
            max_leverage:   Maximum futures leverage (dynamically reduced by volatility).
            mtf_enabled:    If True, require 4h Supertrend to agree with breakout direction.
            adx_filter:     If True, require ADX > adx_threshold before signalling.
                            Filters out false breakouts in choppy/ranging markets.
                            ADX measures trend STRENGTH (not direction) — above 25
                            means a genuine trend is forming, making breakouts reliable.
            adx_period:     ADX lookback period (default 14).
            adx_threshold:  Minimum ADX value to allow a trade (default 25.0).
                            25 = weak trend forming, 40+ = strong trend.
            regime_filter:  If True (default), only fire signals in STRONG_BULL
                            or BULL regimes. Breakout needs directional momentum
                            to follow through — in ranging/bear markets most
                            breakouts fail. Call update_regime() each candle.
        """
        super().__init__(
            name="Breakout",
            symbol=symbol or config.TRADING_PAIR,
            timeframe=timeframe or config.TIMEFRAME,
        )
        self.lookback      = lookback
        self.volume_mult   = volume_mult
        self.atr_period    = atr_period
        self.atr_stop_mult = atr_stop_mult
        self.reward_ratio  = reward_ratio
        self.max_leverage  = max_leverage
        self.mtf_enabled   = mtf_enabled
        self.adx_filter    = adx_filter
        self.adx_period    = adx_period
        self.adx_threshold = adx_threshold
        self.regime_filter = regime_filter

        # State updated externally before generate_signal()
        self._htf_trend: int = 0              # 1 = 4h uptrend, -1 = 4h downtrend, 0 = unknown
        self._sentiment_score: float = 50.0   # 0-100, default neutral
        self._current_regime: str = "BULL"    # Updated via update_regime() each candle

        logger.info(
            f"Breakout | {self.symbol} | lookback={lookback} | "
            f"vol×{volume_mult} | ATR({atr_period})×{atr_stop_mult} | "
            f"RR={reward_ratio} | max_lev={max_leverage}x | "
            f"MTF={'ON' if mtf_enabled else 'OFF'} | "
            f"ADX={'ON (>' + str(adx_threshold) + ')' if adx_filter else 'OFF'}"
        )

    # ─── External state setters ───────────────────────────────────────────────

    def update_htf_trend(self, htf_df: pd.DataFrame) -> int:
        """
        Compute 4h Supertrend direction for MTF confirmation.
        Call this before generate_signal() when mtf_enabled=True.

        Args:
            htf_df: OHLCV DataFrame on the higher timeframe (4h recommended).

        Returns:
            1 if uptrend, -1 if downtrend.
        """
        close = htf_df["close"]
        high = htf_df["high"]
        low = htf_df["low"]

        atr = ta.volatility.AverageTrueRange(
            high=high, low=low, close=close, window=self.atr_period
        ).average_true_range()

        hl2 = (high + low) / 2.0
        upper = (hl2 + 3.5 * atr).ffill()
        lower = (hl2 - 3.5 * atr).ffill()

        # Simplified: if close > upper band midpoint, uptrend
        mid = (upper + lower) / 2
        direction = 1 if float(close.iloc[-1]) > float(mid.iloc[-1]) else -1
        self._htf_trend = direction

        label = "↑ 4h UPTREND" if direction == 1 else "↓ 4h DOWNTREND"
        logger.debug(f"MTF check: {label}")
        return direction

    def update_sentiment(self, score: float) -> None:
        """
        Update the sentiment score for position sizing.

        Args:
            score: 0-100 sentiment score from LunarCrush.
                   >60 = bullish, 40-60 = neutral, <40 = bearish.
        """
        self._sentiment_score = max(0.0, min(100.0, score))
        logger.debug(f"Sentiment updated: {self._sentiment_score:.0f}/100")

    def update_regime(self, regime: str) -> None:
        """
        Update the current market regime for the regime filter.

        Call this from the portfolio manager before generate_signal() on every
        candle.  When regime_filter=True, signals only fire in STRONG_BULL
        and BULL — all other regimes return HOLD immediately.

        Args:
            regime: One of "STRONG_BULL", "BULL", "RANGE", "VOLATILE",
                    "BEAR", "CRASH" from the RegimeDetector.
        """
        self._current_regime = regime

    # ─── Dynamic leverage calculator ─────────────────────────────────────────

    def _calculate_leverage(self, atr: float, current_price: float, confirmed: bool) -> int:
        """
        Return the appropriate leverage based on current volatility.

        Logic:
          ATR as % of price tells us how volatile the market is right now.
          We compare it to a "normal" threshold (1% of price = calm,
          3%+ = very volatile).

          If signal is fully confirmed (volume + MTF agree) → full leverage.
          If only partially confirmed → one step lower.

        Args:
            atr:           Current ATR value.
            current_price: Current price.
            confirmed:     True if all confirmations (volume + MTF) passed.

        Returns:
            Integer leverage: 1, 2, or 3 (capped at max_leverage).
        """
        atr_pct = (atr / current_price) * 100

        if atr_pct < 1.0:
            raw_leverage = self.max_leverage        # Calm market → full leverage
        elif atr_pct < 2.0:
            raw_leverage = min(2, self.max_leverage)  # Moderate → 2x
        else:
            raw_leverage = 1                        # Volatile → 1x (protect capital)

        # Drop one level if not fully confirmed
        if not confirmed:
            raw_leverage = max(1, raw_leverage - 1)

        return raw_leverage

    def _sentiment_size_factor(self) -> float:
        """Return position size multiplier based on sentiment (0.5 to 1.0)."""
        if self._sentiment_score > 60:
            return 1.0    # Full size
        elif self._sentiment_score > 40:
            return 0.75   # 75% size — mild uncertainty
        else:
            return 0.5    # 50% size — negative buzz

    # ─── Core signal generation ───────────────────────────────────────────────

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        """
        Identify breakouts with volume confirmation and return BUY/SELL/HOLD.

        Args:
            df: OHLCV DataFrame on the primary timeframe (e.g. 1h).

        Returns:
            Signal. Metadata includes: leverage, sentiment_factor, confirmed.
        """
        min_rows = max(self.lookback, self.atr_period, self.adx_period) + 10
        self.validate_dataframe(df, min_rows=min_rows)

        close = df["close"]
        open_ = df["open"]
        high = df["high"]
        low = df["low"]
        volume = df["volume"]
        current_price  = float(close.iloc[-1])
        current_open   = float(open_.iloc[-1])
        current_volume = float(volume.iloc[-1])

        # ── Regime filter ──────────────────────────────────────────────────────
        # Breakout signals only fire in directional (bull) regimes.
        # In ranging/volatile/bear markets most breakouts are fake-outs that
        # quickly reverse — the strategy needs follow-through momentum.
        if self.regime_filter and self._current_regime not in ("STRONG_BULL", "BULL"):
            return self.hold(
                current_price,
                reason=(
                    f"Regime={self._current_regime} — Breakout only active in "
                    f"STRONG_BULL / BULL (no directional momentum to sustain move)"
                )
            )

        # ── Support & Resistance ──────────────────────────────────────────────
        # Use the N candles BEFORE the current one (exclude current candle)
        lookback_slice = df.iloc[-(self.lookback + 1):-1]
        resistance = float(lookback_slice["high"].max())
        support = float(lookback_slice["low"].min())

        # ── ATR ───────────────────────────────────────────────────────────────
        atr_series = ta.volatility.AverageTrueRange(
            high=high, low=low, close=close, window=self.atr_period
        ).average_true_range()
        current_atr = float(atr_series.iloc[-1])

        # ── ADX filter — trend strength + rising confirmation ─────────────────
        # ADX measures trend STRENGTH, not direction. Values:
        #   < 20  = no meaningful trend (ranging/choppy — breakouts likely fake-outs)
        #   20-25 = weak trend forming
        #   25-40 = clear trend — breakouts are reliable
        #   > 40  = strong trend
        # TWO conditions required:
        #   1. ADX > adx_threshold (trend is strong enough)
        #   2. ADX is rising (trend is ACCELERATING — breakout has momentum)
        #      ADX falling even above 25 means trend is weakening → skip signal
        current_adx  = 0.0
        prev_adx     = 0.0
        adx_ok       = True
        adx_rising   = True
        if self.adx_filter:
            try:
                adx_indicator = ta.trend.ADXIndicator(
                    high=high, low=low, close=close, window=self.adx_period
                )
                adx_series   = adx_indicator.adx()
                current_adx  = float(adx_series.iloc[-1])
                prev_adx     = float(adx_series.iloc[-2])
                adx_rising   = current_adx > prev_adx      # Trend must be strengthening
                adx_ok       = (current_adx >= self.adx_threshold) and adx_rising
            except Exception:
                adx_ok = True    # If ADX fails to compute, don't block

        # ── Candle body filter ────────────────────────────────────────────────
        # A real breakout candle should have a substantial body (not just a wick).
        # Rule: |close − open| > 0.5 × ATR
        # This filters doji/spinning-top candles where price "touched" the level
        # but had no conviction — those are the most common fake-out pattern.
        body_size = abs(current_price - current_open)
        body_ok   = body_size > (0.5 * current_atr)

        # ── RSI momentum filter ───────────────────────────────────────────────
        # RSI > 50 for longs: confirms that buyers have control (not a dead-cat bounce).
        # RSI < 50 for shorts: confirms that sellers have control.
        # A breakout into new highs while RSI < 50 is a red flag — low conviction.
        rsi_series  = ta.momentum.RSIIndicator(close, window=14).rsi()
        current_rsi = float(rsi_series.iloc[-1])

        # ── Volume confirmation ────────────────────────────────────────────────
        avg_volume = float(volume.iloc[-self.lookback - 1:-1].mean())
        volume_confirmed = current_volume >= (avg_volume * self.volume_mult)
        volume_ratio = current_volume / avg_volume if avg_volume > 0 else 0

        # ── Breakout detection ────────────────────────────────────────────────
        broke_up = current_price > resistance
        broke_down = current_price < support

        # ── MTF confirmation ──────────────────────────────────────────────────
        if self.mtf_enabled and self._htf_trend == 0:
            logger.warning("MTF trend not set — call update_htf_trend() first. Treating as neutral.")

        mtf_bullish = (not self.mtf_enabled) or (self._htf_trend == 1)
        mtf_bearish = (not self.mtf_enabled) or (self._htf_trend == -1)

        # ── Sentiment ─────────────────────────────────────────────────────────
        size_factor = self._sentiment_size_factor()

        # ── BUY: Upside breakout ──────────────────────────────────────────────
        # All 5 conditions must hold: breakout + volume + MTF + ADX(rising) + body + RSI>50
        if broke_up and volume_confirmed and mtf_bullish and adx_ok and body_ok and current_rsi > 50:
            fully_confirmed = volume_confirmed and mtf_bullish
            leverage = self._calculate_leverage(current_atr, current_price, fully_confirmed)

            stop_loss = current_price - (self.atr_stop_mult * current_atr)
            take_profit = current_price + (self.atr_stop_mult * self.reward_ratio * current_atr)

            # Trail SL at (atr_stop_mult × ATR) / price — same ratio as initial SL
            trail_sl_pct = (self.atr_stop_mult * current_atr) / current_price
            return self.buy(
                price=current_price,
                reason=(
                    f"Upside breakout above resistance={resistance:.4f} | "
                    f"Volume={volume_ratio:.1f}×avg | "
                    f"ATR={current_atr:.4f} ({current_atr/current_price*100:.2f}%) | "
                    f"Body={body_size:.4f} ({body_size/current_atr*100:.0f}% of ATR) | "
                    f"RSI={current_rsi:.1f} | "
                    f"ADX={current_adx:.1f} (prev={prev_adx:.1f}, {'↑ rising' if adx_rising else '↓ falling'}) | "
                    f"4h={'↑' if self._htf_trend == 1 else '?'} | "
                    f"Leverage={leverage}x | Sentiment={self._sentiment_score:.0f}/100 "
                    f"(size={size_factor*100:.0f}%)"
                ),
                stop_loss=stop_loss,
                take_profit=take_profit,
                trailing_sl=True,
                trail_sl_pct=trail_sl_pct,
            )

        # ── SELL: Downside breakdown (futures short opportunity) ──────────────
        # All 5 conditions must hold: breakdown + volume + MTF + ADX(rising) + body + RSI<50
        if broke_down and volume_confirmed and mtf_bearish and adx_ok and body_ok and current_rsi < 50:
            fully_confirmed = volume_confirmed and mtf_bearish
            leverage = self._calculate_leverage(current_atr, current_price, fully_confirmed)

            # For a short: stop is above entry, take profit is below entry
            stop_loss = current_price + (self.atr_stop_mult * current_atr)
            take_profit = current_price - (self.atr_stop_mult * self.reward_ratio * current_atr)

            return self.sell(
                price=current_price,
                reason=(
                    f"Breakdown below support={support:.4f} | "
                    f"Volume={volume_ratio:.1f}×avg | "
                    f"ATR={current_atr:.4f} ({current_atr/current_price*100:.2f}%) | "
                    f"Body={body_size:.4f} ({body_size/current_atr*100:.0f}% of ATR) | "
                    f"RSI={current_rsi:.1f} | "
                    f"ADX={current_adx:.1f} (prev={prev_adx:.1f}, {'↑ rising' if adx_rising else '↓ falling'}) | "
                    f"4h={'↓' if self._htf_trend == -1 else '?'} | "
                    f"Leverage={leverage}x | Sentiment={self._sentiment_score:.0f}/100 "
                    f"(size={size_factor*100:.0f}%)"
                ),
                stop_loss=stop_loss,
                take_profit=take_profit,
            )

        # ── HOLD: Reasons why no signal fired ────────────────────────────────
        reasons = []

        if self.adx_filter and not adx_ok:
            if current_adx < self.adx_threshold:
                reasons.append(
                    f"ADX={current_adx:.1f} < {self.adx_threshold} "
                    f"(no clear trend — breakout likely a fake-out)"
                )
            else:
                # ADX above threshold but falling
                reasons.append(
                    f"ADX={current_adx:.1f}↓ (was {prev_adx:.1f}) — "
                    f"trend weakening, not safe for breakout entry"
                )
        elif broke_up and not body_ok:
            reasons.append(
                f"Breakout above {resistance:.2f} but candle body too small "
                f"({body_size:.4f} < 0.5×ATR={0.5*current_atr:.4f}) — likely a wick"
            )
        elif broke_down and not body_ok:
            reasons.append(
                f"Breakdown below {support:.2f} but candle body too small "
                f"({body_size:.4f} < 0.5×ATR={0.5*current_atr:.4f}) — likely a wick"
            )
        elif broke_up and current_rsi <= 50:
            reasons.append(
                f"Breakout above {resistance:.2f} but RSI={current_rsi:.1f} ≤ 50 "
                f"(no buyer momentum — high false-breakout risk)"
            )
        elif broke_down and current_rsi >= 50:
            reasons.append(
                f"Breakdown below {support:.2f} but RSI={current_rsi:.1f} ≥ 50 "
                f"(no seller momentum — high false-breakdown risk)"
            )
        elif broke_up and not volume_confirmed:
            reasons.append(f"Weak breakout above {resistance:.2f} (vol={volume_ratio:.1f}×, need {self.volume_mult}×)")
        elif broke_down and not volume_confirmed:
            reasons.append(f"Weak breakdown below {support:.2f} (vol={volume_ratio:.1f}×, need {self.volume_mult}×)")
        elif broke_up and not mtf_bullish:
            reasons.append(f"Breakout above {resistance:.2f} blocked by 4h downtrend")
        elif broke_down and not mtf_bearish:
            reasons.append(f"Breakdown below {support:.2f} blocked by 4h uptrend")
        else:
            reasons.append(
                f"Ranging | Support={support:.2f} | Resistance={resistance:.2f} | "
                f"Vol={volume_ratio:.1f}×avg | RSI={current_rsi:.1f} | "
                f"ADX={current_adx:.1f} | ATR%={current_atr/current_price*100:.2f}%"
            )

        return self.hold(
            price=current_price,
            reason=" | ".join(reasons)
        )
