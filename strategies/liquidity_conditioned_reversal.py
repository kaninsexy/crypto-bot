"""strategies/liquidity_conditioned_reversal.py -- Phase 4 sq-033 strategy.

Liquidity-conditioned cross-sectional reversal on an 11-symbol crypto
basket at 1D. Hypothesis: cross-sectional short-term reversal
(long past losers) is profitable only when applied to a low-liquidity
subset of the universe -- high-liquidity coins exhibit momentum.

Algorithm per bar (daily):

  1. For each symbol, compute the trailing-window mean dollar volume
     over the last `liquidity_lookback` days (close * volume).
  2. Rank symbols by mean dollar volume ASCENDING and take the
     bottom-`liquidity_bottom_n` (most illiquid) symbols. This is the
     liquidity-conditioned subset.
  3. Within that illiquid subset, compute the prior `reversal_lookback`
     return per symbol.
  4. Rank illiquid symbols by prior return ASCENDING (worst first);
     long-only the bottom-`top_n` (the loser tail of the illiquid
     subset).
  5. BUY for symbols entering the target portfolio with no position.
  6. SELL for symbols leaving the target portfolio (rotation OR they
     left the illiquid subset on this bar).
  7. Otherwise HOLD.

Long-only; equal weight across the held basket.

Citations:
- Ficura, M.; Colak, G. (2023). "Impact of Size and Volume on
  Cryptocurrency Momentum and Reversal." SSRN. Weekly return reversal
  is statistically significant only for small and illiquid coins
  (t-stat = -7.31); large/liquid coins exhibit weekly momentum.
- Zaremba, A.; Bilgin, M. H.; Long, H.; Mercik, A.; Szczygielski, J. J.
  (2021). "Up or down? Short-term reversal, momentum, and liquidity
  effects in cryptocurrency markets." International Review of
  Financial Analysis. Significant daily reversal where coins with low
  prior-day returns outperform; the authors attribute the pattern to
  cross-sectional illiquidity.
- Wen, Z.; Bouri, E.; Xu, Y.; Zhao, Y. (2022). "Intraday Return
  Predictability in the Cryptocurrency Markets: Momentum, Reversal,
  or Both." SSRN. Intraday return predictability flips between
  momentum and reversal as a function of liquidity.
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np
import pandas as pd

from strategies.base import Signal


class LiquidityConditionedReversalStrategy:
    """Long-only loser-portfolio cross-sectional reversal applied
    only inside the bottom-quintile-by-dollar-volume subset of the
    universe.
    """

    def __init__(
        self,
        symbols: list[str],
        timeframe: str = "1d",
        # CITATION: liquidity-conditioned-reversal-literature
        # Ficura & Colak (2023) and Zaremba et al. (2021) compute
        # cross-sectional liquidity ranks over a ~30-day rolling
        # window of mean dollar volume; this is the canonical horizon
        # both papers use to define the small/illiquid tail.
        liquidity_lookback: int = 30,
        # CITATION: liquidity-conditioned-reversal-literature
        # Bottom quintile of an 11-symbol universe = 2 holdings, the
        # 'illiquid tail' Ficura/Colak (2023) and Zaremba et al. (2021)
        # report carries the reversal premium.
        liquidity_bottom_n: int = 2,
        # CITATION: liquidity-conditioned-reversal-literature
        # Zaremba et al. (2021) report daily-frequency reversal with
        # a 1-day prior-return lookback in the illiquid tail; Ficura &
        # Colak (2023) document the same effect at weekly horizon. The
        # 1-day variant is the more conservative starting hypothesis
        # because it produces more rebalance events per CPCV block.
        reversal_lookback: int = 1,
        # CITATION: liquidity-conditioned-reversal-literature
        # The illiquid subset is itself only 2 symbols (bottom
        # quintile). Long-only the single biggest loser within that
        # subset = top_n=1 -- the tightest interpretation of the
        # 'long the losers in the illiquid tail' hypothesis.
        top_n: int = 1,
        # CITATION: liquidity-conditioned-reversal-literature
        # Engine default initial_balance for the Phase 4 backtest harness.
        notional_capital: float = 10_000.0,
        name: str = "LiquidityConditionedReversal",
    ):
        if not symbols or len(symbols) < 2:
            raise ValueError("symbols must contain at least 2 entries")
        if liquidity_lookback < 1:
            raise ValueError("liquidity_lookback must be >= 1")
        if liquidity_bottom_n < 1 or liquidity_bottom_n > len(symbols):
            raise ValueError(
                f"liquidity_bottom_n must satisfy 1 <= liquidity_bottom_n "
                f"<= len(symbols)={len(symbols)}; got {liquidity_bottom_n}"
            )
        if reversal_lookback < 1:
            raise ValueError("reversal_lookback must be >= 1")
        if top_n < 1 or top_n > liquidity_bottom_n:
            raise ValueError(
                f"top_n must satisfy 1 <= top_n <= liquidity_bottom_n="
                f"{liquidity_bottom_n}; got {top_n}"
            )

        self.name = str(name)
        self.symbols: list[str] = list(symbols)
        self.timeframe = str(timeframe)
        self.liquidity_lookback = int(liquidity_lookback)
        self.liquidity_bottom_n = int(liquidity_bottom_n)
        self.reversal_lookback = int(reversal_lookback)
        self.top_n = int(top_n)
        self.notional_capital = float(notional_capital)

        # engine_multi.min_history_bars = strategy.lookback_days + 2.
        # The strategy needs at least liquidity_lookback closes for the
        # rolling dollar-volume mean AND reversal_lookback + 1 closes
        # for the prior return; pad to the larger of the two.
        self.lookback_days: int = (
            max(self.liquidity_lookback, self.reversal_lookback + 1)
        )

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

        prices_now: dict[str, float] = {
            sym: self._latest_close(prices.get(sym)) for sym in self.symbols
        }

        # 1. Rolling mean dollar volume per symbol over the last
        #    liquidity_lookback bars.
        dollar_volumes: dict[str, float] = {}
        for sym in self.symbols:
            df = prices.get(sym)
            if df is None or len(df) < self.liquidity_lookback:
                continue
            close = df["close"].astype(float).to_numpy()
            volume = df["volume"].astype(float).to_numpy()
            close_window = close[-self.liquidity_lookback:]
            volume_window = volume[-self.liquidity_lookback:]
            if (
                np.any(close_window <= 0)
                or np.any(volume_window < 0)
                or not np.all(np.isfinite(close_window))
                or not np.all(np.isfinite(volume_window))
            ):
                continue
            mean_dv = float(np.mean(close_window * volume_window))
            if math.isfinite(mean_dv) and mean_dv > 0:
                dollar_volumes[sym] = mean_dv

        # 2. If we cannot rank the liquidity dimension yet, HOLD all.
        if len(dollar_volumes) < self.liquidity_bottom_n:
            for sym in self.symbols:
                out[sym] = Signal(
                    action="HOLD", strategy=self.name,
                    price=prices_now.get(sym, 0.0),
                    reason=(
                        f"warmup-liquidity | scored="
                        f"{len(dollar_volumes)} < bottom_n="
                        f"{self.liquidity_bottom_n}"
                    ),
                )
            return out

        # 3. Bottom-quintile-by-dollar-volume subset (most illiquid).
        liquidity_ranked = sorted(dollar_volumes.items(), key=lambda kv: kv[1])
        illiquid_subset: set[str] = {
            sym for sym, _ in liquidity_ranked[: self.liquidity_bottom_n]
        }

        # 4. Compute prior reversal_lookback return for the illiquid subset.
        prior_returns: dict[str, float] = {}
        for sym in illiquid_subset:
            df = prices.get(sym)
            if df is None or len(df) < self.reversal_lookback + 1:
                continue
            close = df["close"].astype(float).to_numpy()
            window = close[-(self.reversal_lookback + 1):]
            if np.any(window <= 0) or not np.all(np.isfinite(window)):
                continue
            prior_close = float(window[0])
            current_close = float(window[-1])
            if prior_close <= 0:
                continue
            ret = (current_close / prior_close) - 1.0
            if math.isfinite(ret):
                prior_returns[sym] = ret

        # 5. If we cannot rank reversal within illiquid subset, exit
        #    held positions (they are no longer scored) and HOLD others.
        if len(prior_returns) < self.top_n:
            for sym in self.symbols:
                in_held = sym in self._held
                if in_held:
                    self._held.discard(sym)
                    out[sym] = Signal(
                        action="SELL", strategy=self.name,
                        price=prices_now.get(sym, 0.0),
                        reason=(
                            f"exit-warmup-reversal | scored="
                            f"{len(prior_returns)} < top_n={self.top_n}"
                        ),
                        order_type="market",
                    )
                else:
                    out[sym] = Signal(
                        action="HOLD", strategy=self.name,
                        price=prices_now.get(sym, 0.0),
                        reason=(
                            f"warmup-reversal | scored="
                            f"{len(prior_returns)} < top_n={self.top_n}"
                        ),
                    )
            return out

        # 6. Rank illiquid subset by prior return ASCENDING; long the
        #    bottom-`top_n` (loser tail).
        reversal_ranked = sorted(
            prior_returns.items(), key=lambda kv: kv[1],
        )
        target_set: set[str] = {
            sym for sym, _ in reversal_ranked[: self.top_n]
        }

        # 7. Emit signals.
        for sym in self.symbols:
            price = prices_now.get(sym, 0.0)
            score = prior_returns.get(sym)
            dv = dollar_volumes.get(sym)
            score_str = (
                f"prior_ret={score * 100:+.3f}%"
                if score is not None else "prior_ret=NA"
            )
            dv_str = (
                f"avg_dollar_vol={dv:,.0f}"
                if dv is not None else "avg_dollar_vol=NA"
            )
            in_illiquid = sym in illiquid_subset
            illiquid_str = "illiquid" if in_illiquid else "liquid"

            in_target = sym in target_set
            in_held = sym in self._held

            if in_target and not in_held:
                self._held.add(sym)
                out[sym] = Signal(
                    action="BUY", strategy=self.name, price=price,
                    reason=(
                        f"enter-illiquid-loser-bottom-{self.top_n} | "
                        f"{illiquid_str} | {score_str} | {dv_str}"
                    ),
                    order_type="market",
                )
            elif (not in_target) and in_held:
                self._held.discard(sym)
                out[sym] = Signal(
                    action="SELL", strategy=self.name, price=price,
                    reason=(
                        f"exit-rotation | {illiquid_str} | {score_str} | "
                        f"{dv_str}"
                    ),
                    order_type="market",
                )
            else:
                if in_held:
                    reason = (
                        f"holding | {illiquid_str} | {score_str} | {dv_str}"
                    )
                else:
                    reason = (
                        f"flat | {illiquid_str} | {score_str} | {dv_str}"
                    )
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
