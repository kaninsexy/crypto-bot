"""strategies/short_term_cross_sectional_momentum.py -- Phase 4 sq-024 strategy.

Short-term cross-sectional momentum on a 10-symbol crypto basket at 1D.
Hypothesis: cryptocurrencies that performed best over the prior 7-day
formation window continue to outperform on the subsequent 7-day
holding period; the long winner-quintile portfolio captures a
positive risk-adjusted return relative to BTC buy-and-hold.

Algorithm per bar (daily):

  1. On each rebalance bar (every `holding_period` bars), for every
     symbol compute the prior `lookback_period` return:

         ret[sym] = close[t] / close[t - lookback_period] - 1

  2. Rank symbols by prior return DESCENDING (best-performing first).
  3. Hold the top-N (highest) returns equal-weight (the winner
     portfolio).  Default N=2 of 10 = top quintile.
  4. BUY for symbols entering the winner portfolio with no position.
  5. SELL for symbols leaving the winner portfolio (rotation).
  6. Otherwise HOLD.

Between rebalance bars, every symbol receives HOLD so existing
positions are kept for the full holding_period.

Long-only; equal weight across the held basket.  No short positions
are taken in losers (Han et al. 2024 -- losers tend to rebound).

This file is structurally identical to
strategies/cross_sectional_momentum.py but is registered as a
distinct strategy class with its own manifest entry so the short-
term (7d/7d) configuration occupies its own multiple-testing slot
and trial-log lineage, separate from the 30d/7d Drogen baseline.

Citations:
- Drogen, L.; Hoffstein, C.; Otte, K. (2023).  "Cross-sectional
  Momentum in Cryptocurrency Markets." SSRN.  Long-only top-quintile
  short-lookback / 7-day-hold momentum delivered excess returns vs
  BTC buy-and-hold.
- Zaremba, A.; Bilgin, M. H.; Long, H.; Mercik, A.; Szczygielski,
  J. J. (2021).  "Up or down? Short-term reversal, momentum, and
  liquidity effects in cryptocurrency markets." International
  Review of Financial Analysis.  The handful of largest and most
  liquid coins exhibit short-horizon momentum (T+1 outperformance
  after a high return at T).
- Tzouvanas, P.; Kizys, R.; Tsend-Ayush, B. (2020).  "Momentum
  trading in cryptocurrencies: Short-term returns and
  diversification benefits." Economics Letters.  The momentum
  effect is highly significant for short-term portfolios (7-day
  formation and 7-day holding) but disappears over longer
  horizons.
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np
import pandas as pd

from strategies.base import Signal


class ShortTermCrossSectionalMomentumStrategy:
    """Long-only top-N short-lookback cross-sectional momentum."""

    def __init__(
        self,
        symbols: list[str],
        timeframe: str = "1d",
        # CITATION: short-term-cross-sectional-momentum-literature
        # Tzouvanas et al. (2020) document the strongest cross-
        # sectional momentum at a 7-day formation window in
        # cryptocurrencies; Drogen et al. (2023) and Zaremba et al.
        # (2021) corroborate the short-horizon variant.
        lookback_period: int = 7,
        # CITATION: short-term-cross-sectional-momentum-literature
        # Top quintile of a 10-symbol universe = 2 holdings, the
        # winner tail Drogen et al. (2023) and Han et al. (2024)
        # report generates the strongest momentum premium.
        top_n: int = 2,
        # CITATION: short-term-cross-sectional-momentum-literature
        # Tzouvanas et al. (2020) hold the formed winner portfolio
        # for 7 days before re-ranking; Drogen et al. (2023) match
        # the 7-day rebalance cadence.
        holding_period: int = 7,
        # CITATION: short-term-cross-sectional-momentum-literature
        # Engine default initial_balance for the Phase 4 backtest harness.
        notional_capital: float = 10_000.0,
        name: str = "ShortTermCrossSectionalMomentum",
    ):
        if not symbols or len(symbols) < 2:
            raise ValueError("symbols must contain at least 2 entries")
        if lookback_period < 1:
            raise ValueError("lookback_period must be >= 1")
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
        self.lookback_period = int(lookback_period)
        self.top_n = int(top_n)
        self.holding_period = int(holding_period)
        self.notional_capital = float(notional_capital)

        # engine_multi.min_history_bars = strategy.lookback_days + 2.
        # We need lookback_period + 1 closes to form one return; pad
        # via lookback_days so the engine waits for at least one
        # well-defined return per symbol before the first signal.
        self.lookback_days: int = self.lookback_period + 1

        # Held set drives BUY / SELL / HOLD emission on rotation bars.
        self._held: set[str] = set()
        # Rebalance scheduler -- index of the most recent rebalance is
        # tracked via a counter incremented per generate_signals call.
        # `_first_signal` ensures the first call after engine warmup
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
          - Subsequent calls: HOLD until `_bars_since_rebalance`
            reaches `holding_period`, then rebalance again and reset.
        """
        prices_now: dict[str, float] = {
            sym: self._latest_close(prices.get(sym)) for sym in self.symbols
        }

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
        prior_returns: dict[str, float] = {}
        for sym in self.symbols:
            df = prices.get(sym)
            if df is None or len(df) < self.lookback_period + 1:
                continue
            close = df["close"].astype(float).to_numpy()
            window = close[-(self.lookback_period + 1):]
            if np.any(window <= 0) or not np.all(np.isfinite(window)):
                continue
            prior_close = float(window[0])
            current_close = float(window[-1])
            if prior_close <= 0:
                continue
            ret = (current_close / prior_close) - 1.0
            if math.isfinite(ret):
                prior_returns[sym] = ret

        out_rb: dict[str, Signal] = {}

        if len(prior_returns) < self.top_n:
            for sym in self.symbols:
                out_rb[sym] = Signal(
                    action="HOLD", strategy=self.name,
                    price=prices_now.get(sym, 0.0),
                    reason=(
                        f"warmup | scored={len(prior_returns)} < "
                        f"top_n={self.top_n}"
                    ),
                )
            return out_rb

        ranked = sorted(
            prior_returns.items(), key=lambda kv: kv[1], reverse=True,
        )
        target_set: set[str] = {sym for sym, _ in ranked[: self.top_n]}

        for sym in self.symbols:
            price = prices_now.get(sym, 0.0)
            score = prior_returns.get(sym)
            score_str = (
                f"prior_ret={score * 100:+.3f}%"
                if score is not None else "prior_ret=NA"
            )

            in_target = sym in target_set
            in_held = sym in self._held

            if in_target and not in_held:
                self._held.add(sym)
                out_rb[sym] = Signal(
                    action="BUY", strategy=self.name, price=price,
                    reason=(
                        f"enter-winner-top-{self.top_n} | {score_str}"
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
                    reason = f"holding-winner | {score_str}"
                else:
                    reason = f"flat | {score_str}"
                out_rb[sym] = Signal(
                    action="HOLD", strategy=self.name, price=price,
                    reason=reason,
                )

        return out_rb

    # -- Helpers --------------------------------------------------------------

    @staticmethod
    def _latest_close(df: Optional[pd.DataFrame]) -> float:
        if df is None or len(df) == 0:
            return 0.0
        return float(df["close"].iloc[-1])
