"""strategies/news_sentiment_momentum.py -- NewsSentimentMomentum strategy.

Cross-sectional news-sentiment momentum on a 7-symbol crypto basket
at 1D. Hypothesis: cryptocurrencies with the highest 24-hour
aggregated news-sentiment score outperform those with the lowest
over the subsequent daily rebalance period; long the top tercile
captures positive risk-adjusted returns.

Algorithm per bar (daily):

  1. On each rebalance bar (every ``holding_period`` bars), for every
     symbol look up the latest ``news_score`` value at df.index[-1]
     from a pre-fetched, OHLCV-aligned per-symbol Series passed at
     construction time.  The strategy is data-source agnostic; the
     trial script computes the score from whichever source is wired
     up (paid news API, public news + transformer scorer, or a
     volume-scaled-return proxy when no paid feed is available).
  2. Optionally average the score over the last ``sentiment_window``
     daily bars; ``sentiment_window=1`` (default) implements the
     literal "past 24 hours" aggregation in the hypothesis-of-record
     and Chen/Hafner/Weber (2023).
  3. Rank symbols by the aggregated score DESCENDING (highest news
     sentiment first); pick the top-N (default 2 of 7 = top tercile,
     mirroring the long-tercile leg in Chen/Hafner/Weber 2023).
  4. BUY for symbols entering the winner portfolio with no position.
  5. SELL for symbols leaving the winner portfolio (rotation).
  6. Otherwise HOLD.

Long-only adaptation rationale (mirrors sq-013 / sq-016 / sq-018 /
sq-020 precedent):

  The Chen/Hafner/Weber (2023) and Kalamara et al. (2022) baseline
  specifications are dollar-neutral long-short tercile / decile
  sorts. This trial tests only the LONG leg because (a)
  backtest.engine_multi is structurally long-only (no short
  execution path); (b) Han, Kang & Ryu (2024) document that crypto
  loser-shorts get punished by rebound moves -- the same precedent
  applied to sq-013 (CrossSectionalReversal), sq-016
  (CrossSectionalSkewness), sq-018 (AttentionMomentum), and sq-020
  (CrossSectionalMomentum); and (c) Burggraf (2022) reports
  significant out-of-sample annualised returns on the long
  high-sentiment leg for BTC, supporting the long-only adaptation.

Citations:
- Chen, Y.; Hafner, C.M.; Weber, W. (2023). "Sentiment-driven
  cryptocurrency returns." Journal of International Financial
  Markets, Institutions and Money. Long-short tercile-sort news
  sentiment strategy generates 0.17% daily return at Sharpe 1.15.
- Kalamara, E.; Papadimitriou, A.D.; Tziamprias, T.A.;
  Androulidakis, G.S. (2022). "News sentiment and crypto
  cross-section." Journal of International Financial Markets,
  Institutions and Money. Long top-decile / short bottom-decile
  cross-sectional news-sentiment strategy generates a monthly
  Sharpe of 0.44.
- Burggraf, T. (2022). "News-based sentiment and Bitcoin returns."
  Finance Research Letters. Trading simulation using news sentiment
  to predict Bitcoin returns yields significant annualised returns
  out-of-sample.
- Han, C.; Kang, B.; Ryu, J. (2024). "Time-Series and
  Cross-Sectional Momentum in the Cryptocurrency Market: A
  Comprehensive Analysis under Realistic Assumptions." SSRN.
  Crypto loser-shorts are punished by rebound moves -- justifies
  long-only adaptation of the long-short tercile sort.
"""

from __future__ import annotations

import math
from typing import Optional

import pandas as pd

from strategies.base import Signal


class NewsSentimentMomentumStrategy:
    """Long-only top-N cross-sectional news-sentiment momentum."""

    def __init__(
        self,
        symbols: list[str],
        sentiment_data: dict[str, pd.Series],
        timeframe: str = "1d",
        # CITATION: news-sentiment-momentum-literature
        # Chen/Hafner/Weber (2023) and Kalamara et al. (2022) compute
        # the cross-sectional news-sentiment signal over the most
        # recent 24-hour window. With 1D candles, sentiment_window=1
        # implements the literal "past 24 hours" aggregation in the
        # hypothesis-of-record. Larger windows are available for
        # smoothing experiments but are NOT the headline configuration.
        sentiment_window: int = 1,
        # CITATION: news-sentiment-momentum-literature
        # Top tercile of a 7-symbol traded universe = top 2.
        # Chen/Hafner/Weber (2023) build the long leg from the top
        # tercile and report Sharpe 1.15 on the long-short version;
        # the long-only leg of the same sort is what this
        # implementation tests (engine_multi is long-only).
        top_n: int = 2,
        # CITATION: news-sentiment-momentum-literature
        # Daily rebalance per the strategy description ("Rebalance the
        # portfolio at the chosen frequency", with daily as the
        # documented case in Chen/Hafner/Weber 2023).
        holding_period: int = 1,
        # CITATION: news-sentiment-momentum-literature
        # Engine default initial_balance for the Phase 4 backtest harness.
        notional_capital: float = 10_000.0,
        name: str = "NewsSentimentMomentum",
    ):
        if not symbols or len(symbols) < 2:
            raise ValueError("symbols must contain at least 2 entries")
        if sentiment_window < 1:
            raise ValueError("sentiment_window must be >= 1")
        if top_n < 1 or top_n > len(symbols):
            raise ValueError(
                f"top_n must satisfy 1 <= top_n <= len(symbols)={len(symbols)}; "
                f"got {top_n}"
            )
        if holding_period < 1:
            raise ValueError("holding_period must be >= 1")

        self.name = str(name)
        self.symbols: list[str] = list(symbols)
        self.timeframe = str(timeframe)
        self.sentiment_window = int(sentiment_window)
        self.top_n = int(top_n)
        self.holding_period = int(holding_period)
        self.notional_capital = float(notional_capital)

        # Per-symbol pre-fetched sentiment Series (already aligned to
        # the OHLCV daily index by the trial script before being
        # handed to the strategy). The strategy does not own the
        # data-source choice; the trial script does.
        self._sentiment_data: dict[str, pd.Series] = {
            sym: (sentiment_data.get(sym) if sentiment_data else None)
            for sym in self.symbols
        }

        # engine_multi.min_history_bars = strategy.lookback_days + 2.
        # Need sentiment_window daily bars to form one well-defined
        # mean per symbol; pad via lookback_days so the engine waits
        # for at least one well-defined score per symbol before the
        # first signal.
        self.lookback_days: int = self.sentiment_window + 1

        # Held set drives BUY / SELL / HOLD emission on rotation bars.
        self._held: set[str] = set()
        # Rebalance scheduler -- index of the most recent rebalance is
        # tracked via a counter incremented per generate_signals call.
        # ``_first_signal`` ensures the first call after engine warmup
        # is treated as a rebalance bar (counter starts at 0 then).
        self._bars_since_rebalance: int = 0
        self._first_signal: bool = True

    # -- Engine sizing hook ---------------------------------------------------

    def position_fraction(
        self,
        df: pd.DataFrame,
        n_active: int,
        max_concentration_mult: float = 2.0,
    ) -> float:
        """Equal weight across the top_n held symbols -- 1 / top_n.

        engine_multi only invokes position_fraction on symbols that
        receive a BUY this bar, so 1 / top_n correctly sizes each new
        leg as one-Nth of equity.
        """
        if self.top_n <= 0:
            return 0.0
        return float(1.0 / float(self.top_n))

    # -- Signal generation ----------------------------------------------------

    def generate_signals(
        self,
        prices: dict[str, pd.DataFrame],
    ) -> dict[str, Signal]:
        """Map of symbol -> Signal for the current bar.

        Rebalance logic:
          - First call after engine warmup: rebalance.
          - Subsequent calls: HOLD until ``_bars_since_rebalance``
            reaches ``holding_period``, then rebalance again and reset.
        """
        prices_now: dict[str, float] = {
            sym: self._latest_close(prices.get(sym)) for sym in self.symbols
        }

        # Decide whether this bar is a rebalance bar.
        if self._first_signal:
            self._first_signal = False
            self._bars_since_rebalance = 0
            is_rebalance = True
        else:
            self._bars_since_rebalance += 1
            if self._bars_since_rebalance >= self.holding_period:
                self._bars_since_rebalance = 0
                is_rebalance = True
            else:
                is_rebalance = False

        if not is_rebalance:
            # Mid-holding bar: HOLD every symbol. Engine treats HOLD
            # as no-op so existing positions are preserved through
            # the holding_period without further trades.
            out: dict[str, Signal] = {}
            for sym in self.symbols:
                in_held = sym in self._held
                reason = (
                    f"holding | bars_since_rebalance="
                    f"{self._bars_since_rebalance}/{self.holding_period}"
                ) if in_held else (
                    f"flat | bars_since_rebalance="
                    f"{self._bars_since_rebalance}/{self.holding_period}"
                )
                out[sym] = Signal(
                    action="HOLD", strategy=self.name,
                    price=prices_now.get(sym, 0.0),
                    reason=reason,
                )
            return out

        # Rebalance path -------------------------------------------------------
        # 1. For every symbol resolve the current bar timestamp from
        #    its OHLCV slice and compute the aggregated sentiment
        #    score over the last sentiment_window daily bars.
        scores: dict[str, float] = {}
        for sym in self.symbols:
            df = prices.get(sym)
            if df is None or len(df) < self.sentiment_window:
                continue
            last_ts = df.index[-1]
            score = self._compute_sentiment_score(sym, last_ts)
            if score is not None and math.isfinite(score):
                scores[sym] = score

        out_rb: dict[str, Signal] = {}

        # 2. If we cannot rank yet, HOLD everything and exit.
        if len(scores) < self.top_n:
            for sym in self.symbols:
                out_rb[sym] = Signal(
                    action="HOLD", strategy=self.name,
                    price=prices_now.get(sym, 0.0),
                    reason=(
                        f"warmup | scored={len(scores)} < "
                        f"top_n={self.top_n}"
                    ),
                )
            return out_rb

        # 3. Rank DESCENDING (highest sentiment first); pick top-N.
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        target_set: set[str] = {sym for sym, _ in ranked[: self.top_n]}

        # 4. Emit signals.
        for sym in self.symbols:
            price = prices_now.get(sym, 0.0)
            score = scores.get(sym)
            score_str = (
                f"news_score={score:+.4f}"
                if score is not None else "news_score=NA"
            )

            in_target = sym in target_set
            in_held = sym in self._held

            if in_target and not in_held:
                self._held.add(sym)
                out_rb[sym] = Signal(
                    action="BUY", strategy=self.name, price=price,
                    reason=(
                        f"enter-news-top-{self.top_n} | {score_str}"
                    ),
                    order_type="market",
                )
            elif (not in_target) and in_held:
                self._held.discard(sym)
                out_rb[sym] = Signal(
                    action="SELL", strategy=self.name, price=price,
                    reason=f"exit-rotation | {score_str}",
                    order_type="market",
                )
            else:
                if in_held:
                    reason = f"holding-news-winner | {score_str}"
                else:
                    reason = f"flat | {score_str}"
                out_rb[sym] = Signal(
                    action="HOLD", strategy=self.name, price=price,
                    reason=reason,
                )

        return out_rb

    # -- Helpers --------------------------------------------------------------

    def _compute_sentiment_score(
        self,
        symbol: str,
        last_ts: pd.Timestamp,
    ) -> Optional[float]:
        """Mean of the sentiment series over the last sentiment_window
        daily bars ending at last_ts (inclusive).

        Returns None when the sentiment series is missing, warmup is
        incomplete, or any value in the window is non-finite.
        """
        sent = self._sentiment_data.get(symbol)
        if sent is None or len(sent) == 0:
            return None
        window = sent.loc[: last_ts]
        if len(window) < self.sentiment_window:
            return None
        recent = window.iloc[-self.sentiment_window:].astype(float)
        if recent.isna().any():
            return None
        score = float(recent.mean())
        if not math.isfinite(score):
            return None
        return score

    @staticmethod
    def _latest_close(df: Optional[pd.DataFrame]) -> float:
        if df is None or len(df) == 0:
            return 0.0
        return float(df["close"].iloc[-1])
