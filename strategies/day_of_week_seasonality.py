"""strategies/day_of_week_seasonality.py -- Phase 4 sq-025 strategy.

Cross-sectional day-of-week seasonality on a 10-symbol crypto basket
at 1D. Hypothesis (Long et al. 2020): for each weekday W, the
cross-section of average past same-weekday returns positively
predicts next-day cryptocurrency performance. Long the assets whose
historical mean return on weekday W is highest.

Algorithm per bar (daily):

  1. Identify the current bar's weekday W (Monday=0 .. Sunday=6).
  2. For each symbol, gather returns within the trailing
     `lookback_period` days that fell on weekday W. Compute the
     mean of those returns.
  3. Rank symbols by mean same-weekday return DESCENDING.
     Hold the top-N (the 'winner' tail). N = top_n; default 2 of 10
     (top quintile per Long 2020 cross-sectional formation).
  4. BUY for symbols entering the winner portfolio with no position.
  5. SELL for symbols leaving the winner portfolio (rotation).
  6. Otherwise HOLD.

Long-only; equal weight across the held basket; rebalance every bar.

Citations:
- Long, H.; Zaremba, A.; Demir, E.; Szczygielski, J. J.; Vasenin, M.
  (2020). 'Seasonality in the Cross-Section of Cryptocurrency
  Returns.' Finance Research Letters.
- Caporale, G. M.; Plastun, A. (2019). 'The day of the week effect
  in the cryptocurrency market.' Finance Research Letters.
- Shanaev, S.; Ghimire, B. (2022). 'A generalised seasonality test
  and applications for cryptocurrency and stock market seasonality.'
  Quarterly Review of Economics and Finance.
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np
import pandas as pd

from strategies.base import Signal


class DayOfWeekSeasonalityStrategy:
    """Long-only cross-sectional day-of-week seasonality (winner tail)."""

    def __init__(
        self,
        symbols: list[str],
        timeframe: str = "1d",
        # CITATION: day-of-week-seasonality-literature
        # Long et al. (2020) form same-weekday-mean rankings on
        # multi-month windows; 56 days = 8 occurrences per weekday,
        # the smallest window with sample size sufficient to rank
        # cross-sectionally without single-week dominance.
        lookback_period: int = 56,
        # CITATION: day-of-week-seasonality-literature
        # Minimum same-weekday observations required for a symbol to
        # be eligible for the cross-sectional ranking on a given bar.
        # 4 occurrences = ~1 month; below this the per-symbol
        # weekday mean is too noisy.
        min_weekday_obs: int = 4,
        # CITATION: day-of-week-seasonality-literature
        # Long et al. (2020) use top quintile (top 20%) of cross-
        # sectional ranks; 2 of 10 symbols matches this convention
        # and aligns with the CrossSectionalReversal substrate.
        top_n: int = 2,
        # CITATION: day-of-week-seasonality-literature
        # Engine default initial_balance for the Phase 4 backtest
        # harness (matches BacktestEngine.initial_balance default).
        notional_capital: float = 10_000.0,
        name: str = "DayOfWeekSeasonality",
    ):
        if not symbols or len(symbols) < 2:
            raise ValueError("symbols must contain at least 2 entries")
        if lookback_period < 7:
            raise ValueError("lookback_period must be >= 7 days")
        if min_weekday_obs < 1:
            raise ValueError("min_weekday_obs must be >= 1")
        if top_n < 1 or top_n > len(symbols):
            raise ValueError(
                f"top_n must satisfy 1 <= top_n <= len(symbols)="
                f"{len(symbols)}; got {top_n}"
            )

        self.name = str(name)
        self.symbols: list[str] = list(symbols)
        self.timeframe = str(timeframe)
        self.lookback_period = int(lookback_period)
        self.min_weekday_obs = int(min_weekday_obs)
        self.top_n = int(top_n)
        self.notional_capital = float(notional_capital)

        # engine_multi.min_history_bars = strategy.lookback_days + 2.
        # We need lookback_period + 1 closes to compute returns over
        # the full window. lookback_days = lookback_period + 1 keeps
        # the engine from calling generate_signals before the window
        # is fully populated.
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

        # 1. Identify current weekday from any symbol's last index
        # (all symbols share the same bar timeline under engine_multi).
        current_weekday: Optional[int] = None
        prices_now: dict[str, float] = {}
        for sym in self.symbols:
            df = prices.get(sym)
            prices_now[sym] = self._latest_close(df)
            if current_weekday is None and df is not None and len(df) > 0:
                ts = df.index[-1]
                current_weekday = int(ts.weekday())

        if current_weekday is None:
            for sym in self.symbols:
                out[sym] = Signal(
                    action="HOLD", strategy=self.name,
                    price=prices_now.get(sym, 0.0),
                    reason="warmup | no timestamp",
                )
            return out

        weekday_label = (
            "Mon Tue Wed Thu Fri Sat Sun".split()[current_weekday]
        )

        # 2. Compute mean past same-weekday return per symbol over
        # the trailing lookback_period days.
        weekday_means: dict[str, float] = {}
        weekday_obs_counts: dict[str, int] = {}
        for sym in self.symbols:
            df = prices.get(sym)
            if df is None or len(df) < self.lookback_period + 1:
                continue
            window = df.iloc[-(self.lookback_period + 1):]
            close = window["close"].astype(float).to_numpy()
            if np.any(close <= 0) or not np.all(np.isfinite(close)):
                continue
            returns = (close[1:] / close[:-1]) - 1.0
            weekdays = np.array(
                [int(ts.weekday()) for ts in window.index[1:]],
                dtype=np.int64,
            )
            mask = weekdays == current_weekday
            same_weekday_returns = returns[mask]
            same_weekday_returns = same_weekday_returns[
                np.isfinite(same_weekday_returns)
            ]
            n_obs = int(same_weekday_returns.size)
            weekday_obs_counts[sym] = n_obs
            if n_obs < self.min_weekday_obs:
                continue
            mean_ret = float(np.mean(same_weekday_returns))
            if math.isfinite(mean_ret):
                weekday_means[sym] = mean_ret

        # 3. If we cannot rank yet, HOLD everything and exit.
        if len(weekday_means) < self.top_n:
            for sym in self.symbols:
                out[sym] = Signal(
                    action="HOLD", strategy=self.name,
                    price=prices_now.get(sym, 0.0),
                    reason=(
                        f"warmup | weekday={weekday_label} | "
                        f"scored={len(weekday_means)} < top_n={self.top_n}"
                    ),
                )
            return out

        # 4. Rank DESCENDING (highest mean first); pick top-N winners.
        ranked = sorted(
            weekday_means.items(), key=lambda kv: kv[1], reverse=True
        )
        target_set: set[str] = {sym for sym, _ in ranked[: self.top_n]}

        # 5. Emit signals.
        for sym in self.symbols:
            price = prices_now.get(sym, 0.0)
            mean = weekday_means.get(sym)
            n_obs = weekday_obs_counts.get(sym, 0)
            if mean is not None:
                score_str = (
                    f"weekday={weekday_label} | "
                    f"mean_ret={mean * 100:+.3f}% | n_obs={n_obs}"
                )
            else:
                score_str = (
                    f"weekday={weekday_label} | mean_ret=NA | n_obs={n_obs}"
                )

            in_target = sym in target_set
            in_held = sym in self._held

            if in_target and not in_held:
                self._held.add(sym)
                out[sym] = Signal(
                    action="BUY", strategy=self.name, price=price,
                    reason=(
                        f"enter-winner-top-{self.top_n} | {score_str}"
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
