"""strategies/illiquidity_premium.py -- Phase 4 sq-014 strategy.

Cross-sectional illiquidity-premium strategy on a 10-symbol crypto
basket at 1D. Hypothesis: cryptocurrencies with higher Amihud
illiquidity generate positive excess returns vs BTC buy-and-hold on a
long-only basket.

Algorithm per bar (daily):

  1. For each symbol, compute the Amihud illiquidity over the trailing
     ``illiquidity_window`` bars:

         illiq[sym] = mean(|log_return| / volume)

  2. Rank symbols by illiquidity descending.
  3. Hold the top ``top_n`` most illiquid symbols (equal weight).
  4. BUY signal for symbols that should be held but have no position.
  5. SELL signal for symbols currently held but no longer in top N.
  6. Otherwise HOLD.

Long-only; equal weight across the held basket.

Citations:
- Youssef, M. & El Wajdi, F. (2023). "Illiquidity premium in
  cryptocurrency markets." Research in International Business and
  Finance.
- Dan, X., Wang, P., & Zhou, Y. (2020). "Liquidity and asset prices in
  the cryptocurrency market." Finance Research Letters.
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np
import pandas as pd

from strategies.base import Signal


class IlliquidityPremiumStrategy:
    """Long-only top-N illiquidity-premium basket strategy."""

    def __init__(
        self,
        symbols: list[str],
        timeframe: str = "1d",
        # CITATION: illiquidity-premium-literature
        # Youssef/El Wajdi (2023) use a 30-day Amihud illiquidity window
        # for the cross-sectional sort on crypto.
        illiquidity_window: int = 30,
        # CITATION: illiquidity-premium-literature
        # Top-N basket sizing -- 3 of 10 sits in the high-illiquidity
        # tertile per Dan/Wang/Zhou (2020).
        top_n: int = 3,
        # CITATION: illiquidity-premium-literature
        # Engine default initial_balance for the Phase 4 backtest harness.
        notional_capital: float = 10_000.0,
        name: str = "IlliquidityPremium",
    ):
        if not symbols or len(symbols) < 2:
            raise ValueError("symbols must contain at least 2 entries")
        # CITATION: input-validation guard, not a tuned parameter
        if illiquidity_window < 5:
            raise ValueError("illiquidity_window must be >= 5")
        if top_n < 1 or top_n > len(symbols):
            raise ValueError(
                f"top_n must satisfy 1 <= top_n <= len(symbols)={len(symbols)}; "
                f"got {top_n}"
            )

        self.name = str(name)
        self.symbols: list[str] = list(symbols)
        self.timeframe = str(timeframe)
        self.illiquidity_window = int(illiquidity_window)
        self.top_n = int(top_n)
        self.notional_capital = float(notional_capital)

        # engine_multi.min_history_bars = strategy.lookback_days + 2.
        # We need illiquidity_window bars of returns -> illiquidity_window + 1
        # closes; rounded up to give the engine a small buffer.
        self.lookback_days: int = self.illiquidity_window + 1

        # Position state mirrors what the engine reports back via the
        # signals -> trade-history loop. We track held_set so we can
        # emit SELL on rotation.
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

        # 1. Compute Amihud illiquidity per symbol over the trailing
        #    window. Keep symbols with insufficient history out of the
        #    ranking and emit HOLD for them.
        illiq_scores: dict[str, float] = {}
        prices_now: dict[str, float] = {}
        for sym in self.symbols:
            df = prices.get(sym)
            price = self._latest_close(df)
            prices_now[sym] = price
            if df is None or len(df) < self.illiquidity_window + 1:
                continue
            close = df["close"].astype(float).to_numpy()
            volume = df["volume"].astype(float).to_numpy()
            if np.any(close[-(self.illiquidity_window + 1):] <= 0):
                continue
            log_close = np.log(close[-(self.illiquidity_window + 1):])
            returns = np.diff(log_close)
            vols = volume[-self.illiquidity_window:]
            if returns.size != vols.size or returns.size == 0:
                continue
            # Amihud (2002) crypto adaptation: |return| / volume.
            # Replace zero-volume bars with NaN so they do not
            # spuriously dominate the mean (zero-vol => infinite illiq).
            with np.errstate(divide="ignore", invalid="ignore"):
                amihud_per_bar = np.where(
                    vols > 0,
                    np.abs(returns) / vols,
                    np.nan,
                )
            if not np.any(np.isfinite(amihud_per_bar)):
                continue
            illiq = float(np.nanmean(amihud_per_bar))
            if math.isfinite(illiq) and illiq >= 0:
                illiq_scores[sym] = illiq

        # 2. If we cannot rank yet, HOLD everything and exit.
        if len(illiq_scores) < self.top_n:
            for sym in self.symbols:
                out[sym] = Signal(
                    action="HOLD", strategy=self.name,
                    price=prices_now.get(sym, 0.0),
                    reason=(
                        f"warmup | scored={len(illiq_scores)} < "
                        f"top_n={self.top_n}"
                    ),
                )
            return out

        # 3. Rank descending; pick top_n.
        ranked = sorted(
            illiq_scores.items(), key=lambda kv: kv[1], reverse=True,
        )
        target_set: set[str] = {sym for sym, _ in ranked[: self.top_n]}

        # 4. Emit signals.
        for sym in self.symbols:
            price = prices_now.get(sym, 0.0)
            score = illiq_scores.get(sym)
            score_str = (
                f"illiq={score:.3e}" if score is not None else "illiq=NA"
            )

            in_target = sym in target_set
            in_held = sym in self._held

            if in_target and not in_held:
                self._held.add(sym)
                out[sym] = Signal(
                    action="BUY", strategy=self.name, price=price,
                    reason=f"enter-top-{self.top_n} | {score_str}",
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
