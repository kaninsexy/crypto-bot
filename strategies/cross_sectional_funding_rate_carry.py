"""strategies/cross_sectional_funding_rate_carry.py -- sq-038 strategy.

Cross-sectional funding-rate carry on a 7-symbol crypto basket at 1D.
Hypothesis: cryptocurrencies whose perpetual futures carry the lowest
(most-negative) funding rates outperform those with the highest funding
rates over the subsequent rebalance period; long the bottom tercile of
the funding-rate cross-section captures the carry premium.

Algorithm per bar (daily):

  1. On each rebalance bar (every ``holding_period`` bars), for every
     symbol look up the latest aggregated funding-rate value at
     df.index[-1] from a pre-fetched, OHLCV-aligned per-symbol Series
     passed at construction time. The strategy is data-source agnostic;
     the trial script computes the daily-aggregated rate from the
     8-hour OKX funding history.
  2. Optionally average the rate over the last ``funding_window``
     daily bars; ``funding_window=1`` (default) implements the literal
     prior-day aggregation in the hypothesis-of-record and Bianchi et
     al. (2022).
  3. Rank symbols by the aggregated funding rate ASCENDING (lowest /
     most-negative first); pick the bottom-N (default 2 of 7 = bottom
     tercile, mirroring the long leg of the Bianchi et al. (2022)
     dollar-neutral tercile sort).
  4. BUY for symbols entering the carry-winner portfolio with no
     position.
  5. SELL for symbols leaving the carry-winner portfolio (rotation).
  6. Otherwise HOLD.

Long-only adaptation rationale (mirrors sq-013 / sq-016 / sq-018 /
sq-020 / news-sentiment-momentum precedent):

  Bianchi et al. (2022) and Abedifar et al. (2023) baseline
  specifications are dollar-neutral long-short quintile / tercile
  sorts (long lowest funding, short highest funding). This trial
  tests only the LONG (lowest-funding) leg because (a)
  backtest.engine_multi is structurally long-only (no short
  execution path); (b) Han, Kang & Ryu (2024) document that crypto
  loser-shorts get punished by rebound moves -- the same precedent
  applied to sq-013 (CrossSectionalReversal), sq-016
  (CrossSectionalSkewness), sq-018 (AttentionMomentum), sq-020
  (CrossSectionalMomentum), and news-sentiment-momentum; and (c)
  Bianchi et al. (2022) report that the long leg of the funding-rate
  carry portfolio carries the bulk of the unconditional alpha,
  supporting the long-only adaptation.

Citations:
- Bianchi, D.; Babiak, M.; Ciner, C. (2022). "Carry trades in
  cryptocurrency markets." Journal of International Financial
  Markets, Institutions and Money. A dollar-neutral strategy
  sorting perpetual futures on their funding rates yields
  significant monthly alphas of 2.19% and an annualized Sharpe
  ratio of 1.34.
- Abedifar, P.; Fica, O.; Imbierowicz, B. (2023). "Taming the
  Basis: A Cross-Sectional Perspective on Cryptocurrency Carry."
  SSRN. A dollar-neutral strategy going long perpetuals with a low
  basis and shorting those with a high basis generates significant
  risk-adjusted returns of up to 2.5% per week, unexplained by
  common risk factors.
- ryanczm (2024). "Crypto Stat Arb." github.com. Practitioner
  research outlining a cross-sectional statistical arbitrage
  strategy on perpetual futures that explicitly includes a 'carry'
  factor derived from funding rates.
- Han, C.; Kang, B.; Ryu, J. (2024). "Time-Series and
  Cross-Sectional Momentum in the Cryptocurrency Market: A
  Comprehensive Analysis under Realistic Assumptions." SSRN.
  Crypto loser-shorts are punished by rebound moves -- justifies
  long-only adaptation of the long-short funding-rate sort.
"""

from __future__ import annotations

import math
from typing import Optional

import pandas as pd

from strategies.base import Signal


class CrossSectionalFundingRateCarryStrategy:
    """Long-only bottom-N (lowest-funding) cross-sectional carry."""

    def __init__(
        self,
        symbols: list[str],
        funding_data: dict[str, pd.Series],
        timeframe: str = "1d",
        # CITATION: cross-sectional-funding-rate-carry-literature
        # Bianchi et al. (2022) and Abedifar et al. (2023) compute the
        # cross-sectional funding-rate signal from the most recent
        # observed funding rate. With 1D candles, funding_window=1
        # implements the literal prior-day aggregation in the
        # hypothesis-of-record. Larger windows are available for
        # smoothing experiments but are NOT the headline configuration.
        funding_window: int = 1,
        # CITATION: cross-sectional-funding-rate-carry-literature
        # Bottom tercile of a 7-symbol traded universe = bottom 2.
        # Bianchi et al. (2022) build the long leg of their carry
        # portfolio from the lowest-funding tercile / quintile and
        # report Sharpe 1.34 on the long-short version; the
        # long-only leg of the same sort is what this implementation
        # tests (engine_multi is long-only).
        top_n: int = 2,
        # CITATION: cross-sectional-funding-rate-carry-literature
        # Daily rebalance per the implementation note in the trial
        # queue entry ("At each rebalancing period (e.g., daily)").
        # Bianchi et al. (2022) document daily-rebalanced carry
        # portfolios as the headline configuration.
        holding_period: int = 1,
        # CITATION: cross-sectional-funding-rate-carry-literature
        # Engine default initial_balance for the Phase 4 backtest harness.
        notional_capital: float = 10_000.0,
        name: str = "CrossSectionalFundingRateCarry",
    ):
        if not symbols or len(symbols) < 2:
            raise ValueError("symbols must contain at least 2 entries")
        if funding_window < 1:
            raise ValueError("funding_window must be >= 1")
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
        self.funding_window = int(funding_window)
        self.top_n = int(top_n)
        self.holding_period = int(holding_period)
        self.notional_capital = float(notional_capital)

        # Per-symbol pre-fetched funding-rate Series (already aligned to
        # the OHLCV daily index by the trial script before being handed
        # to the strategy). The strategy does not own the data-source
        # choice; the trial script does.
        self._funding_data: dict[str, pd.Series] = {
            sym: (funding_data.get(sym) if funding_data else None)
            for sym in self.symbols
        }

        # engine_multi.min_history_bars = strategy.lookback_days + 2.
        # Need funding_window daily bars to form one well-defined mean
        # per symbol; pad via lookback_days so the engine waits for at
        # least one well-defined score per symbol before signal.
        self.lookback_days: int = self.funding_window + 1

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
        #    its OHLCV slice and compute the aggregated funding-rate
        #    score over the last funding_window daily bars.
        scores: dict[str, float] = {}
        for sym in self.symbols:
            df = prices.get(sym)
            if df is None or len(df) < self.funding_window:
                continue
            last_ts = df.index[-1]
            score = self._compute_funding_score(sym, last_ts)
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

        # 3. Rank ASCENDING (lowest / most-negative funding first);
        #    pick bottom-N -- the carry-winner long leg.
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=False)
        target_set: set[str] = {sym for sym, _ in ranked[: self.top_n]}

        # 4. Emit signals.
        for sym in self.symbols:
            price = prices_now.get(sym, 0.0)
            score = scores.get(sym)
            score_str = (
                f"funding_rate={score:+.6f}"
                if score is not None else "funding_rate=NA"
            )

            in_target = sym in target_set
            in_held = sym in self._held

            if in_target and not in_held:
                self._held.add(sym)
                out_rb[sym] = Signal(
                    action="BUY", strategy=self.name, price=price,
                    reason=(
                        f"enter-carry-bottom-{self.top_n} | {score_str}"
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
                    reason = f"holding-carry-winner | {score_str}"
                else:
                    reason = f"flat | {score_str}"
                out_rb[sym] = Signal(
                    action="HOLD", strategy=self.name, price=price,
                    reason=reason,
                )

        return out_rb

    # -- Helpers --------------------------------------------------------------

    def _compute_funding_score(
        self,
        symbol: str,
        last_ts: pd.Timestamp,
    ) -> Optional[float]:
        """Mean of the funding-rate series over the last funding_window
        daily bars ending at last_ts (inclusive).

        Returns None when the funding series is missing, warmup is
        incomplete, or any value in the window is non-finite.
        """
        funding = self._funding_data.get(symbol)
        if funding is None or len(funding) == 0:
            return None
        window = funding.loc[: last_ts]
        if len(window) < self.funding_window:
            return None
        recent = window.iloc[-self.funding_window:].astype(float)
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
