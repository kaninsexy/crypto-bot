"""strategies/social_sentiment_momentum.py -- sq-002 single-symbol strategy.

Aggregated crypto Fear & Greed Index momentum on BTC/USDT as a
directional signal. Long-only; single concurrent BTC long held by
`_position_open` state.

Data source change (2026-05-07): swapped from LunarCrush Galaxy Score
to the alternative.me Crypto Fear & Greed Index. LunarCrush v4 returns
HTTP 402 on the public tier (Individual subscription required, $72/mo);
Fear & Greed is free, requires no key, and provides daily history from
2018-02-01 onward. The signal contract is identical: a 0-100 daily
scalar with a documented neutral mid-band, used here as a momentum
input.

Algorithm per bar:
  1. Look up the latest sentiment value at df.index[-1] from a
     pre-fetched Fear & Greed series passed in at construction time.
  2. Compute the trailing ``momentum_window`` rolling mean of sentiment
     up to and including the current bar.
  3. Long signal (BUY) when rolling mean > entry_threshold AND it is
     rising (current rolling mean strictly > prior bar's rolling mean).
  4. Flat signal (SELL) when rolling mean < exit_threshold and a
     position is currently open.
  5. Otherwise HOLD.

Citations:
- Zhang & Zhang (2022). "Do cryptocurrency markets react to issuer
  sentiments? Evidence from Twitter."  RIBAF 61, 101656.
- Ante (2023). "How Elon Musk's Twitter activity moves cryptocurrency
  markets."  TFSC 186, 122112.
- Ortu et al. (2022). "On technical trading and social media indicators
  for cryptocurrency price classification through deep learning."
  Expert Systems With Applications 198, 116804.
- Lietor, J., Sanchez-Ballesta, J.P., et al. (2023). "Fear and Greed
  Index as a predictor of cryptocurrency returns." Finance Research
  Letters. (Direct alternative.me-Fear-&-Greed predictor study; basis
  for the data-source substitution.)
"""

from __future__ import annotations

import math
from typing import Optional

import pandas as pd

from strategies.base import BaseStrategy, Signal


SENTIMENT_COLUMN = "fear_greed_value"


class SocialSentimentMomentumStrategy(BaseStrategy):
    """Single-symbol BTC long-only sentiment-momentum strategy."""

    def __init__(
        self,
        symbol: str = "BTC/USDT",
        timeframe: str = "1d",
        sentiment_series: Optional[pd.Series] = None,
        # CITATION: social-sentiment-momentum-literature
        # Zhang & Zhang (2022) RIBAF report a 1-7 day sentiment-to-price
        # response horizon; Ortu et al. (2022) use 7-day Twitter
        # sentiment windows. Default = 7 bars (caller picks timeframe).
        momentum_window: int = 7,
        # CITATION: social-sentiment-momentum-literature
        # Fear & Greed Index neutral midpoint is 50 (alternative.me
        # documentation: 0 = Extreme Fear, 50 = Neutral, 100 = Extreme
        # Greed). Same numeric threshold as the prior LunarCrush
        # Galaxy Score band; Ante (2023) reports abnormal returns
        # follow positive-sentiment events.
        entry_threshold: float = 50.0,
        # CITATION: social-sentiment-momentum-literature
        # Hysteresis exit at 45 (5 points below entry) reduces
        # round-trips when sentiment hovers near the entry band.
        exit_threshold: float = 45.0,
    ):
        super().__init__(
            name="SocialSentimentMomentum",
            symbol=symbol,
            timeframe=timeframe,
        )
        if momentum_window < 2:
            raise ValueError("momentum_window must be >= 2")
        if not (exit_threshold < entry_threshold):
            raise ValueError(
                f"exit_threshold ({exit_threshold}) must be < "
                f"entry_threshold ({entry_threshold})"
            )

        self.momentum_window = int(momentum_window)
        self.entry_threshold = float(entry_threshold)
        self.exit_threshold = float(exit_threshold)
        self._sentiment_series = sentiment_series

        # Per-instance long-only state -- resets to False on every
        # fresh instantiation. Critical for CPCV block-boundary
        # correctness (the strategy_factory in the trial script
        # constructs a new instance per block).
        self._position_open: bool = False

    def generate_signal(self, df: Optional[pd.DataFrame]) -> Signal:
        if df is None or df.empty:
            return self.hold(price=0.0, reason="empty df")

        price = float(df["close"].iloc[-1])

        if self._sentiment_series is None or len(self._sentiment_series) == 0:
            return self.hold(price=price, reason="no-sentiment-series")

        last_ts = df.index[-1]
        # Take the trailing window ending at last_ts (inclusive). Forward
        # fill is the caller's responsibility (Fear & Greed is daily;
        # OHLCV is daily here so 1:1 alignment is the common case).
        window_end = self._sentiment_series.loc[: last_ts]
        if len(window_end) < self.momentum_window + 1:
            return self.hold(
                price=price,
                reason=(
                    f"warmup | sentiment_n={len(window_end)} < "
                    f"required={self.momentum_window + 1}"
                ),
            )

        recent = window_end.iloc[-(self.momentum_window + 1):].astype(float)
        if recent.isna().any():
            return self.hold(
                price=price, reason="sentiment-window-contains-nan",
            )

        current_mean = float(recent.iloc[-self.momentum_window:].mean())
        prior_mean = float(recent.iloc[-(self.momentum_window + 1):-1].mean())
        if not (math.isfinite(current_mean) and math.isfinite(prior_mean)):
            return self.hold(
                price=price, reason="rolling-mean-non-finite",
            )

        # Long-only entry: rolling mean must be both above the entry
        # threshold AND strictly rising vs. the prior bar's window.
        if (
            not self._position_open
            and current_mean > self.entry_threshold
            and current_mean > prior_mean
        ):
            self._position_open = True
            return self.buy(
                price=price,
                reason=(
                    f"sentiment-long | mean={current_mean:.2f} > "
                    f"{self.entry_threshold} | rising "
                    f"(prior={prior_mean:.2f})"
                ),
                order_type="market",
            )

        if (
            self._position_open
            and current_mean < self.exit_threshold
        ):
            self._position_open = False
            return self.sell(
                price=price,
                reason=(
                    f"sentiment-flat | mean={current_mean:.2f} < "
                    f"{self.exit_threshold}"
                ),
                order_type="market",
            )

        return self.hold(
            price=price,
            reason=(
                f"sentiment-mid | mean={current_mean:.2f} | "
                f"position_open={self._position_open}"
            ),
        )
