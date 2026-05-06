"""strategies/contrarian_search_volume.py -- sq-011 strategy.

Contrarian Google Trends search-volume strategy on BTC/USDT 1D.
Hypothesis: abnormally high Google Trends search volume for "bitcoin"
predicts negative subsequent returns; staying flat during high-search
periods and long during normal/low-search periods outperforms BTC
buy-and-hold.

Algorithm per bar:
  1. Look up the latest search_volume value at df.index[-1] from a
     pre-fetched, OHLCV-aligned series passed at construction time.
  2. Compute Abnormal Search Volume Index (ASVI) over the trailing
     ``asvi_window`` bars:

         ASVI = (current - rolling_median) / (rolling_std + 1e-9)

  3. Long BTC when ASVI <= spike_threshold (normal/low search).
  4. Flat when ASVI > spike_threshold (high search -> contrarian flat).
  5. Engine is long-only; no shorts.

Citations:
- Chemkha, R., Ben Jabeur, S., & Naifar, N. (2023). "Search-volume
  effects in cryptocurrency markets." RIBAF.
- Dastgir, S., et al. (2019). "Search volume index and Bitcoin
  returns." RIBAF.
- Salisu, A.A., Gupta, R., & Bouri, E. (2021). "Predicting Bitcoin
  with search-volume data." PLOS ONE.
"""

from __future__ import annotations

import math
from typing import Optional

import pandas as pd

from strategies.base import BaseStrategy


SEARCH_VOLUME_COLUMN = "search_volume"


class ContrarianSearchVolumeStrategy(BaseStrategy):
    """Long BTC except during search-volume spikes (ASVI > threshold)."""

    def __init__(
        self,
        symbol: str = "BTC/USDT",
        timeframe: str = "1d",
        search_volume_series: Optional[pd.Series] = None,
        # CITATION: contrarian-search-volume-literature
        # Chemkha/Ben Jabeur/Naifar (2023) compute the abnormal search
        # volume index (ASVI) over a rolling 4-week window.
        asvi_window: int = 4,
        # CITATION: contrarian-search-volume-literature
        # Dastgir et al. (2019) flag z>1 as a one-sigma SVI spike that
        # precedes negative subsequent returns.
        spike_threshold: float = 1.0,
    ):
        super().__init__(
            name="ContrarianSearchVolume",
            symbol=symbol,
            timeframe=timeframe,
        )
        if asvi_window < 2:
            raise ValueError("asvi_window must be >= 2")

        self.asvi_window = int(asvi_window)
        self.spike_threshold = float(spike_threshold)
        self._search_volume_series = search_volume_series

        # Long-only state -- resets to False on each fresh instance.
        self._position_open: bool = False

    def generate_signal(self, df: Optional[pd.DataFrame]):
        if df is None or df.empty:
            return self.hold(price=0.0, reason="empty df")

        price = float(df["close"].iloc[-1])

        if (
            self._search_volume_series is None
            or len(self._search_volume_series) == 0
        ):
            return self.hold(price=price, reason="no-search-volume-series")

        last_ts = df.index[-1]
        # Trailing window ending at last_ts (inclusive).
        window = self._search_volume_series.loc[: last_ts]
        if len(window) < self.asvi_window + 1:
            return self.hold(
                price=price,
                reason=(
                    f"warmup | sv_n={len(window)} < "
                    f"required={self.asvi_window + 1}"
                ),
            )

        recent = window.iloc[-(self.asvi_window + 1):].astype(float)
        if recent.isna().any():
            return self.hold(
                price=price, reason="search-volume-window-contains-nan",
            )

        rolling = recent.iloc[:-1]  # the asvi_window bars BEFORE current
        current = float(recent.iloc[-1])
        median = float(rolling.median())
        std = float(rolling.std(ddof=0))
        if not (math.isfinite(median) and math.isfinite(std)):
            return self.hold(
                price=price, reason="rolling-stats-non-finite",
            )

        # CITATION: standard numerical stability constant, not a tuned parameter
        asvi = (current - median) / (std + 1e-9)
        if not math.isfinite(asvi):
            return self.hold(
                price=price, reason="asvi-non-finite",
            )

        # Long-only: enter when ASVI <= spike_threshold (normal or low
        # search regime); exit when ASVI > spike_threshold (high search
        # -> contrarian flat).
        if asvi > self.spike_threshold:
            if self._position_open:
                self._position_open = False
                return self.sell(
                    price=price,
                    reason=(
                        f"asvi-spike-flat | asvi={asvi:+.3f} > "
                        f"{self.spike_threshold}"
                    ),
                    order_type="market",
                )
            return self.hold(
                price=price,
                reason=f"flat-on-spike | asvi={asvi:+.3f}",
            )

        if not self._position_open:
            self._position_open = True
            return self.buy(
                price=price,
                reason=(
                    f"asvi-normal-long | asvi={asvi:+.3f} <= "
                    f"{self.spike_threshold}"
                ),
                order_type="market",
            )
        return self.hold(
            price=price,
            reason=f"holding-long | asvi={asvi:+.3f}",
        )
