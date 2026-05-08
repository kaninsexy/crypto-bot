"""strategies/exchange_listing_drift.py -- sq-037 strategy.

ExchangeListingDrift: long-only event-driven drift on BTC/USDT 1D.
Hypothesis-of-record: a major-exchange listing announcement triggers
abnormal volume + positive price drift over the days that follow,
which can be captured by buying on the announcement day and
exiting after roughly the documented post-announcement horizon
(5 days per Le et al. 2021, 10 days per Mazabel & Sciandra 2022).

Data-source design:

  No live Coinbase / Binance announcement feed is wired into this
  repo, so the strategy class is data-source agnostic and consumes a
  pre-aligned ``event_score`` Series (one float per OHLCV bar) plus
  a binary entry-rule that fires when ``event_score`` exceeds an
  ``entry_threshold``. The trial script supplies the series; in this
  repo the trial script computes it from OHLCV as a deterministic
  proxy for "an announcement-like event hit today" (rolling
  abnormal-volume z-score on positive-return bars). Swapping in a
  real announcement feed (Coinbase blog scrape, Binance announcement
  RSS) requires changing only the trial-script helper; the strategy
  class is unchanged.

Algorithm per bar (daily):

  1. Look up the latest event_score value at df.index[-1] from the
     pre-aligned series.
  2. Long signal (BUY) when not already in a position and the score
     exceeds the ``entry_threshold``: an announcement-like event was
     detected today.
  3. Exit (SELL) after ``holding_period`` bars since entry. Time-
     based exit is the literal "sell-the-news at the listing date"
     mechanism in the hypothesis-of-record (Le et al. 2021 5-day
     window, Mazabel & Sciandra 2022 10-day window).
  4. Long-only on spot per project conventions (no short leg).

Citations:

- Le, H.; Nguyen, T.; Park, D. (2021). "The Coinbase Effect: An
  Analysis of Cryptocurrency Listing Announcements." Finance
  Research Letters. Significant average abnormal return of
  approximately 29% over the first five days after a Coinbase
  listing announcement.
- Mazabel, F.; Sciandra, A. (2022). "The Crypto-Listing Pop: An
  Empirical Analysis of the 'Binance Effect'." SSRN. Cumulative
  average abnormal return of around 41% over the 10 days
  surrounding the announcement, with the run-up beginning before
  the actual listing date.
- Corbet, S.; Meegan, A.; Larkin, C.; Lucey, B.; Yarovaya, L.
  (2020). "What Moves Crypto Prices?" Review of Financial Studies.
  News of exchange listings is one of the most significant
  specific events explaining jumps and extreme price movements in
  cryptocurrencies.
- Han, C.; Kang, B.; Ryu, J. (2024). "Time-Series and Cross-
  Sectional Momentum in the Cryptocurrency Market: A Comprehensive
  Analysis under Realistic Assumptions." SSRN. Crypto loser-shorts
  are punished by rebound moves -- precedent for the long-only
  adaptation in this trial.
"""

from __future__ import annotations

import math
from typing import Optional

import pandas as pd

from strategies.base import BaseStrategy, Signal


EVENT_SCORE_COLUMN = "event_score"


class ExchangeListingDriftStrategy(BaseStrategy):
    """Long-only event-driven drift on a major-exchange-listing proxy."""

    def __init__(
        self,
        symbol: str = "BTC/USDT",
        timeframe: str = "1d",
        event_score_series: Optional[pd.Series] = None,
        # CITATION: exchange-listing-drift-literature
        # Le et al. (2021) measure abnormal returns starting from the
        # listing-announcement day; Mazabel & Sciandra (2022) document
        # the run-up beginning before the actual listing date. The
        # entry_threshold targets the right tail of the OHLCV-derived
        # event-score distribution, where the abnormal-volume z-score
        # is large enough to flag an announcement-like event. +2.0
        # corresponds to the top ~2.3% of a standard normal, the
        # standard "extreme event" threshold in the related
        # event-study literature (Corbet et al. 2020 use a 2-sigma
        # jump filter to identify listing-news-driven jumps).
        entry_threshold: float = 2.0,
        # CITATION: exchange-listing-drift-literature
        # Le et al. (2021) measure cumulative abnormal returns over
        # the first 5 days after a Coinbase listing announcement
        # (CAR ~29%); the holding_period default of 5 bars implements
        # that exact horizon on 1D candles. The "sell the news" exit
        # is timed to occur at the end of the documented post-
        # announcement drift window per the hypothesis-of-record.
        holding_period: int = 5,
    ):
        super().__init__(
            name="ExchangeListingDrift",
            symbol=symbol,
            timeframe=timeframe,
        )
        if holding_period < 1:
            raise ValueError("holding_period must be >= 1")
        if not math.isfinite(entry_threshold):
            raise ValueError("entry_threshold must be a finite float")

        self.entry_threshold = float(entry_threshold)
        self.holding_period = int(holding_period)
        self._event_score_series = event_score_series

        # Per-instance long-only state -- resets to closed on every
        # fresh instantiation. Critical for CPCV block-boundary
        # correctness (the trial script's strategy_factory builds a
        # new instance per block).
        self._position_open: bool = False
        self._bars_in_position: int = 0

    def generate_signal(self, df: Optional[pd.DataFrame]) -> Signal:
        if df is None or df.empty:
            return self.hold(price=0.0, reason="empty df")

        price = float(df["close"].iloc[-1])

        if (
            self._event_score_series is None
            or len(self._event_score_series) == 0
        ):
            return self.hold(price=price, reason="no-event-score-series")

        last_ts = df.index[-1]
        window_end = self._event_score_series.loc[: last_ts]
        if len(window_end) == 0:
            return self.hold(
                price=price,
                reason="event-score-series-empty-up-to-now",
            )

        current = float(window_end.iloc[-1])
        if not math.isfinite(current):
            return self.hold(
                price=price,
                reason=f"event-score-non-finite | score={current}",
            )

        # Position management -------------------------------------------------
        if self._position_open:
            self._bars_in_position += 1
            if self._bars_in_position >= self.holding_period:
                self._position_open = False
                bars_held = self._bars_in_position
                self._bars_in_position = 0
                return self.sell(
                    price=price,
                    reason=(
                        f"listing-drift-time-exit | bars_held={bars_held} "
                        f">= holding_period={self.holding_period} | "
                        f"score={current:+.3f}"
                    ),
                    order_type="market",
                )
            return self.hold(
                price=price,
                reason=(
                    f"holding-listing-drift | score={current:+.3f} | "
                    f"bars={self._bars_in_position}/{self.holding_period}"
                ),
            )

        # Event-driven long entry on extreme-positive event score.
        if current > self.entry_threshold:
            self._position_open = True
            self._bars_in_position = 0
            return self.buy(
                price=price,
                reason=(
                    f"listing-drift-entry | score={current:+.3f} > "
                    f"{self.entry_threshold} | hold={self.holding_period}d"
                ),
                order_type="market",
            )

        return self.hold(
            price=price,
            reason=f"no-event | score={current:+.3f}",
        )
