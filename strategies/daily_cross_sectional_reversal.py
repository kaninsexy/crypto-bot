"""strategies/daily_cross_sectional_reversal.py -- Phase 4 sq-026 strategy.

Daily cross-sectional reversal on a less-liquid alt basket at 1D.
Hypothesis: cryptocurrencies with the lowest prior 1-day returns
(the 'loser' tail) outperform those with the highest prior 1-day
returns on the subsequent day, with the effect concentrated in the
less-liquid coins -- BTC, the most-liquid asset, is excluded from the
traded universe because its short-term reversal can flip to momentum
(per Zaremba 2021 and Ficura 2023).

Algorithm per bar (daily):

  1. For each alt in the traded universe, compute the prior 1-day
     return:

         ret[sym] = close[t] / close[t-1] - 1

  2. Rank symbols by prior return ASCENDING (most-negative first).
  3. Hold the bottom-N (lowest) returns equal-weight (the 'loser'
     portfolio).  N = top_n of len(symbols); 2 of 9 alts approximates
     the bottom quintile per Zaremba (2021).
  4. BUY for symbols entering the loser portfolio with no position.
  5. SELL for symbols leaving the loser portfolio (rotation).
  6. Otherwise HOLD.

Long-only.  The sq-026 implementation specification calls for a
long-short portfolio (long bottom quintile, short top quintile).
This trial tests only the LONG leg because (a) backtest.engine_multi
is structurally long-only (no short execution path); (b) Han et al.
(2024) document that crypto loser-shorts get punished by rebound
moves -- the same precedent applied to sq-013 (CrossSectionalReversal)
and sq-016 (CrossSectionalSkewness); and (c) Zaremba (2021) and
Ficura (2023) report that the reversal premium is concentrated on
the long-loser leg of the cross-section, especially among less-liquid
coins, which is exactly what this implementation tests.

BTC/USDT is intentionally NOT in the traded universe.  The hypothesis
explicitly notes the effect can reverse to momentum on the most
liquid asset, so excluding BTC is a structural pre-trial gate, not a
parameter search.

Citations:
- Zaremba, A.; Bilgin, M. H.; Long, H.; Mercik, A.; Szczygielski, J. J.
  (2021). "Up or down? Short-term reversal, momentum, and liquidity
  effects in cryptocurrency markets." International Review of
  Financial Analysis.  Cross-sectional portfolio of cryptocurrencies
  with low last-day returns significantly outperforms one with high
  last-day returns; the effect is driven by illiquid coins.
- Caporale, G. M.; Plastun, A. (2019). "Price overreactions in the
  cryptocurrency market." Journal of Economic Studies.  The crypto
  market exhibits significant price overreactions consistent with
  behavioural biases, leading to subsequent corrections (reversals).
- Ficura, M. (2023). "Impact of size and volume on cryptocurrency
  momentum and reversal." FFA Working Papers.  Size and volume are
  critical factors determining the direction of return predictability;
  reversal effects are prominent in smaller, less-traded coins.
- Han, C.; Kang, B.; Ryu, J. (2024). "Time-Series and Cross-Sectional
  Momentum in the Cryptocurrency Market: A Comprehensive Analysis
  under Realistic Assumptions." SSRN.  Crypto loser-shorts get
  punished by rebound moves -- justifies long-only adaptation of the
  long-short specification.
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np
import pandas as pd

from strategies.base import Signal


class DailyCrossSectionalReversalStrategy:
    """Long-only bottom-N (loser-portfolio) daily cross-sectional reversal.

    Variant of CrossSectionalReversalStrategy that excludes BTC from
    the traded universe to focus on the less-liquid alts where the
    cross-sectional reversal premium is documented to be strongest.
    Engine-multi compatible (generate_signals + position_fraction).
    """

    def __init__(
        self,
        symbols: list[str],
        timeframe: str = "1d",
        # CITATION: daily-cross-sectional-reversal-literature
        # Zaremba (2021) and Caporale/Plastun (2019) document the
        # short-term reversal effect using a 1-day prior-return
        # lookback at daily rebalance frequency.
        lookback_period: int = 1,
        # CITATION: daily-cross-sectional-reversal-literature
        # Bottom quintile of a 9-alt universe (excluding BTC) = 2
        # holdings; matches Zaremba (2021) / Ficura (2023) bottom-tail
        # specification while concentrating exposure on less-liquid
        # coins per the hypothesis.
        top_n: int = 2,
        # CITATION: daily-cross-sectional-reversal-literature
        # Engine default initial_balance for the Phase 4 backtest
        # harness.
        notional_capital: float = 10_000.0,
        name: str = "DailyCrossSectionalReversal",
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
