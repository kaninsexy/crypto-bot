"""strategies/cross_sectional_reversal.py -- Phase 4 sq-013 strategy.

Cross-sectional short-term reversal strategy on a 10-symbol crypto
basket at 1D. Hypothesis: cryptocurrencies that performed worst over
the prior 1-day window outperform on the subsequent day; the long
'loser' portfolio captures positive risk-adjusted returns.

Algorithm per bar (daily):

  1. For each symbol, compute the prior 1-day return:

         ret[sym] = close[t] / close[t-1] - 1

  2. Rank symbols by prior return ASCENDING (most-negative first).
  3. Hold the bottom-N (lowest) returns equal-weight (the 'loser'
     portfolio).  N = top_n of len(symbols); 2 of 10 = bottom
     quintile per Zaremba (2021).
  4. BUY for symbols entering the loser portfolio with no position.
  5. SELL for symbols leaving the loser portfolio (rotation).
  6. Otherwise HOLD.

Long-only; equal weight across the held basket.

Citations:
- Zaremba, A.; Bilgin, M. H.; Long, H.; Mercik, A.; Szczygielski, J. J.
  (2021). "Up or down? Short-term reversal, momentum, and liquidity
  effects in cryptocurrency markets."
  International Review of Financial Analysis.
- Nakagawa, K.; Sakemoto, R. (2024). "Cross-sectional reversal
  portfolios in the cryptocurrency market and market uncertainty."
  SSRN.
- Han, C.; Kang, B.; Ryu, J. (2023). "Time-Series and Cross-Sectional
  Momentum in the Cryptocurrency Market: A Comprehensive Analysis
  under Realistic Assumptions." SSRN.
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np
import pandas as pd

from strategies.base import Signal


class CrossSectionalReversalStrategy:
    """Long-only bottom-N (loser-portfolio) cross-sectional reversal."""

    def __init__(
        self,
        symbols: list[str],
        timeframe: str = "1d",
        # CITATION: cross-sectional-reversal-literature
        # Zaremba (2021) and Nakagawa/Sakemoto (2024) document the
        # reversal effect using a 1-day prior-return lookback at daily
        # rebalance frequency.
        lookback_period: int = 1,
        # CITATION: cross-sectional-reversal-literature
        # Bottom quintile of a 10-symbol universe = 2 holdings, the
        # 'loser' tail Zaremba (2021) and Han et al. (2023) report
        # generates the strongest reversal premium.
        top_n: int = 2,
        # CITATION: cross-sectional-reversal-literature
        # Engine default initial_balance for the Phase 4 backtest harness.
        notional_capital: float = 10_000.0,
        name: str = "CrossSectionalReversal",
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

        self.name = str(name)
        self.symbols: list[str] = list(symbols)
        self.timeframe = str(timeframe)
        self.lookback_period = int(lookback_period)
        self.top_n = int(top_n)
        self.notional_capital = float(notional_capital)

        # engine_multi.min_history_bars = strategy.lookback_days + 2.
        # We need lookback_period + 1 closes to form one return; pad
        # via lookback_days so the engine waits for at least one
        # well-defined return per symbol before signal generation.
        self.lookback_days: int = self.lookback_period + 1

        # Held set drives BUY / SELL / HOLD emission on rotation.
        self._held: set[str] = set()

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
        """Map of symbol -> Signal for the current bar."""
        out: dict[str, Signal] = {}

        # 1. Compute prior lookback_period return per symbol.
        prior_returns: dict[str, float] = {}
        prices_now: dict[str, float] = {}
        for sym in self.symbols:
            df = prices.get(sym)
            price = self._latest_close(df)
            prices_now[sym] = price
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

        # 2. If we cannot rank yet, HOLD everything and exit.
        if len(prior_returns) < self.top_n:
            for sym in self.symbols:
                out[sym] = Signal(
                    action="HOLD", strategy=self.name,
                    price=prices_now.get(sym, 0.0),
                    reason=(
                        f"warmup | scored={len(prior_returns)} < "
                        f"top_n={self.top_n}"
                    ),
                )
            return out

        # 3. Rank ASCENDING (most-negative first); pick bottom-N losers.
        ranked = sorted(prior_returns.items(), key=lambda kv: kv[1])
        target_set: set[str] = {sym for sym, _ in ranked[: self.top_n]}

        # 4. Emit signals.
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
                out[sym] = Signal(
                    action="BUY", strategy=self.name, price=price,
                    reason=(
                        f"enter-loser-bottom-{self.top_n} | {score_str}"
                    ),
                    order_type="market",
                )
            elif (not in_target) and in_held:
                self._held.discard(sym)
                out[sym] = Signal(
                    action="SELL", strategy=self.name, price=price,
                    reason=f"exit-rotation | {score_str}",
                    order_type="market",
                )
            else:
                if in_held:
                    reason = f"holding | {score_str}"
                else:
                    reason = f"flat | {score_str}"
                out[sym] = Signal(
                    action="HOLD", strategy=self.name, price=price,
                    reason=reason,
                )

        return out

    # -- Helpers --------------------------------------------------------------

    @staticmethod
    def _latest_close(df: Optional[pd.DataFrame]) -> float:
        if df is None or len(df) == 0:
            return 0.0
        return float(df["close"].iloc[-1])
