"""
strategies/social_sentiment_momentum.py — sq-002 strategy.

Aggregated social sentiment momentum on a crypto basket. Reads a
`sentiment` column injected alongside OHLCV by the trial script
(LunarCrush Galaxy Score, scored 0-100) and emits long/flat signals
based on the rolling sentiment mean.

Algorithm per bar:
  1. Read each symbol's `sentiment` column.
  2. If column absent for any symbol, that symbol receives HOLD.
  3. Compute rolling mean of sentiment over `sentiment_window` bars.
  4. Long signal (BUY) when rolling mean > `long_threshold`.
  5. Flat signal (SELL) when rolling mean < `flat_threshold`.
  6. Otherwise HOLD.

Single concurrent position per symbol, long-only — the engine
naturally enforces the "one open position per symbol" cap, so no
internal state tracking is needed.

Contract with backtest.engine_multi:

  * `symbols`, `timeframe` exposed as constructor args.
  * `lookback_days = sentiment_window` exposes the warmup floor used
    by engine_multi's `min_history_bars = strategy.lookback_days + 2`.
  * `position_fraction(df, n_active)` returns 1 / n_active so each
    symbol's full sleeve is sized equally across the basket.
  * `generate_signals(prices) -> dict[symbol, Signal]` mirrors the
    multi-asset interface.

Citations:
- Ortu et al. (2022). "On technical trading and social media
  indicators for cryptocurrency price classification through deep
  learning." Expert Systems With Applications 198, 116804.
- Zhang & Zhang (2022). "Do cryptocurrency markets react to issuer
  sentiments?" Research in International Business and Finance 61,
  101656.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from strategies.base import Signal


SENTIMENT_COLUMN = "sentiment"


class SocialSentimentMomentumStrategy:
    """Social-sentiment momentum strategy on a crypto basket."""

    def __init__(
        self,
        symbols: list[str],
        timeframe: str = "1d",
        # CITATION: social-sentiment-momentum-literature
        # Ortu et al. (2022) §4 use 7-day Twitter sentiment windows;
        # Zhang & Zhang (2022) report a 1-7 day sentiment-to-price
        # response horizon. Default = 7 bars (caller picks timeframe).
        sentiment_window: int = 7,
        # CITATION: social-sentiment-momentum-literature
        # Galaxy Score band thresholds: 60/40 are LunarCrush's
        # documented "bullish/bearish" Galaxy Score bands.
        long_threshold: float = 60.0,
        # CITATION: social-sentiment-momentum-literature
        flat_threshold: float = 40.0,
        # CITATION: social-sentiment-momentum-literature
        # Engine default initial_balance for the Phase 4 backtest harness.
        notional_capital: float = 10_000.0,
    ):
        if not symbols:
            raise ValueError("symbols must be a non-empty list")
        if sentiment_window < 1:
            raise ValueError("sentiment_window must be >= 1")
        if not (flat_threshold < long_threshold):
            raise ValueError(
                f"flat_threshold ({flat_threshold}) must be < "
                f"long_threshold ({long_threshold})"
            )

        self.name = "SocialSentimentMomentum"
        self.symbols: list[str] = list(symbols)
        self.timeframe = timeframe
        self.sentiment_window = int(sentiment_window)
        self.long_threshold = float(long_threshold)
        self.flat_threshold = float(flat_threshold)
        self.notional_capital = float(notional_capital)

        # engine_multi.min_history_bars = strategy.lookback_days + 2,
        # so we set lookback_days = sentiment_window to guarantee the
        # rolling mean has a full window before signal generation.
        self.lookback_days = self.sentiment_window

    # ── Engine sizing hook ───────────────────────────────────────────────────

    def position_fraction(
        self,
        df: pd.DataFrame,
        n_active: int,
        max_concentration_mult: float = 2.0,
    ) -> float:
        """1 / n_active sizing — equal-weight basket."""
        if n_active <= 0:
            return 0.0
        return float(1.0 / float(n_active))

    # ── Signal generation ────────────────────────────────────────────────────

    def generate_signals(
        self,
        prices: dict[str, pd.DataFrame],
    ) -> dict[str, Signal]:
        """Map of symbol -> Signal for the current bar."""
        out: dict[str, Signal] = {}

        for sym in self.symbols:
            df = prices.get(sym)
            if df is None or len(df) == 0:
                out[sym] = Signal(
                    action="HOLD", strategy=self.name, price=0.0,
                    reason="missing-data",
                )
                continue

            price = float(df["close"].iloc[-1])

            if SENTIMENT_COLUMN not in df.columns:
                out[sym] = Signal(
                    action="HOLD", strategy=self.name, price=price,
                    reason="sentiment-column-absent",
                )
                continue

            if len(df) < self.sentiment_window:
                out[sym] = Signal(
                    action="HOLD", strategy=self.name, price=price,
                    reason=(
                        f"warmup | n_bars={len(df)} < "
                        f"sentiment_window={self.sentiment_window}"
                    ),
                )
                continue

            sentiment = df[SENTIMENT_COLUMN].astype(float)
            window = sentiment.iloc[-self.sentiment_window:]
            if window.isna().any():
                out[sym] = Signal(
                    action="HOLD", strategy=self.name, price=price,
                    reason="sentiment-window-contains-nan",
                )
                continue

            rolling_mean = float(window.mean())
            if not math.isfinite(rolling_mean):
                out[sym] = Signal(
                    action="HOLD", strategy=self.name, price=price,
                    reason="rolling-mean-not-finite",
                )
                continue

            if rolling_mean > self.long_threshold:
                out[sym] = Signal(
                    action="BUY", strategy=self.name, price=price,
                    reason=(
                        f"sentiment-long | rolling_mean={rolling_mean:.2f} "
                        f"> {self.long_threshold}"
                    ),
                    order_type="market",
                )
            elif rolling_mean < self.flat_threshold:
                out[sym] = Signal(
                    action="SELL", strategy=self.name, price=price,
                    reason=(
                        f"sentiment-flat | rolling_mean={rolling_mean:.2f} "
                        f"< {self.flat_threshold}"
                    ),
                    order_type="market",
                )
            else:
                out[sym] = Signal(
                    action="HOLD", strategy=self.name, price=price,
                    reason=(
                        f"sentiment-mid | rolling_mean={rolling_mean:.2f} "
                        f"in [{self.flat_threshold}, {self.long_threshold}]"
                    ),
                )

        return out
