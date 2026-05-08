"""strategies/put_call_ratio_contrarian.py -- sq-036 strategy.

Crypto Put/Call Ratio (PCR) Contrarian on BTC/USDT 1D. Hypothesis-of-
record: extreme one-sided options sentiment, captured by a high
Put/Call Ratio, precedes a contrarian price reversal that can be
profitably traded long.

Data-source design:

  The strategy is data-source agnostic. It consumes a pre-aligned
  ``pcr_series`` (one float per OHLCV bar) and applies a rolling
  z-score normalization. The trial script supplies the series; in
  this repo the trial script computes it from OHLCV up-vs-down flow
  as a proxy because no Deribit options feed is wired up.

  Swapping in a real Deribit PCR (e.g. open-interest based) requires
  changing only the trial-script helper that builds the series; the
  strategy class is unchanged.

Algorithm per bar (daily):

  1. Look up the latest PCR value at df.index[-1] from the pre-
     aligned series.
  2. Compute a rolling z-score over a ``zscore_lookback`` (default
     60) trailing window of prior PCR values:
         z[t] = (pcr[t] - mean(pcr[t-60..t-1])) / std(pcr[t-60..t-1])
  3. Long signal (BUY) when z > entry_threshold (default +2.0):
     crowded bearish-sentiment regime -> contrarian long.
  4. Exit (SELL) after holding_period bars (default 3) since entry,
     OR when z reverts to <= exit_threshold (default 0.0), whichever
     comes first.
  5. Long-only on spot per project conventions. The literal
     hypothesis includes a short leg on extreme negative z-score; it
     is dropped here for the same reason as sq-013/sq-016/sq-018/
     sq-020/sq-035: backtest.engine is long-only on spot, and Han,
     Kang & Ryu (2024) report that crypto loser-shorts are punished
     by rebound moves.

Citations:

- Kyriazis, N. A.; Papakyriakou, P.; Rozas, G. (2022). "Investor
  sentiment in the Bitcoin market: An analysis of the put/call
  ratio." Global Finance Journal. A one-standard-deviation increase
  in the Bitcoin PCR is associated with a +1.69% next-day return.
- Akyildirim, E. et al. (2024). "Forecasting Bitcoin prices: The
  role of the options market." Finance Research Letters. The open-
  interest put-call ratio has significant contrarian predictive
  power for Bitcoin returns.
- Chen, Z. et al. (2023). "The information content of the Bitcoin
  options market." Journal of International Financial Markets,
  Institutions and Money. The OI-based PCR is a strong contrarian
  predictor of Bitcoin spot returns over 1-5 day horizons (basis
  for the holding_period default of 3 days).
- Han, C.; Kang, B.; Ryu, J. (2024). "Time-Series and Cross-
  Sectional Momentum in the Cryptocurrency Market: A Comprehensive
  Analysis under Realistic Assumptions." SSRN. Crypto loser-shorts
  are punished by rebound moves -- justifies dropping the short
  leg in the long-only adaptation.
"""

from __future__ import annotations

import math
from typing import Optional

import pandas as pd

from strategies.base import BaseStrategy, Signal


PCR_COLUMN = "pcr_value"


class PutCallRatioContrarianStrategy(BaseStrategy):
    """Long-only contrarian on extreme high PCR z-score."""

    def __init__(
        self,
        symbol: str = "BTC/USDT",
        timeframe: str = "1d",
        pcr_series: Optional[pd.Series] = None,
        # CITATION: put-call-ratio-contrarian-literature
        # Chen et al. (2023) and Akyildirim et al. (2024) both use a
        # ~60-day rolling normalization window for the PCR signal.
        # Default 60 daily bars (~3 calendar months).
        zscore_lookback: int = 60,
        # CITATION: put-call-ratio-contrarian-literature
        # Kyriazis et al. (2022) report a 1-sigma PCR move drives a
        # +1.69% next-day return; the +2.0 threshold targets the
        # extreme tail (top ~2.3% of the standard normal) for a
        # cleaner contrarian signal.
        entry_threshold: float = 2.0,
        # CITATION: put-call-ratio-contrarian-literature
        # Hysteresis exit at z=0.0 (mean reversion) avoids round-
        # tripping when z hovers near the entry threshold.
        exit_threshold: float = 0.0,
        # CITATION: put-call-ratio-contrarian-literature
        # Chen et al. (2023) document predictive power over 1-5 day
        # horizons; default 3 sits in the middle of that range.
        holding_period: int = 3,
    ):
        super().__init__(
            name="PutCallRatioContrarian",
            symbol=symbol,
            timeframe=timeframe,
        )
        if zscore_lookback < 5:
            raise ValueError("zscore_lookback must be >= 5")
        if not (exit_threshold < entry_threshold):
            raise ValueError(
                f"exit_threshold ({exit_threshold}) must be < "
                f"entry_threshold ({entry_threshold})"
            )
        if holding_period < 1:
            raise ValueError("holding_period must be >= 1")

        self.zscore_lookback = int(zscore_lookback)
        self.entry_threshold = float(entry_threshold)
        self.exit_threshold = float(exit_threshold)
        self.holding_period = int(holding_period)
        self._pcr_series = pcr_series

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

        if self._pcr_series is None or len(self._pcr_series) == 0:
            return self.hold(price=price, reason="no-pcr-series")

        last_ts = df.index[-1]
        # Trailing window ending at last_ts (inclusive).
        window_end = self._pcr_series.loc[: last_ts]
        if len(window_end) < self.zscore_lookback + 1:
            return self.hold(
                price=price,
                reason=(
                    f"warmup | pcr_n={len(window_end)} < "
                    f"required={self.zscore_lookback + 1}"
                ),
            )

        recent = window_end.iloc[-(self.zscore_lookback + 1):].astype(float)
        if recent.isna().any():
            return self.hold(
                price=price, reason="pcr-window-contains-nan",
            )

        prior = recent.iloc[:-1]
        current = float(recent.iloc[-1])
        mean = float(prior.mean())
        std = float(prior.std(ddof=0))
        if not (math.isfinite(mean) and math.isfinite(std)):
            return self.hold(
                price=price, reason="rolling-stats-non-finite",
            )

        # CITATION: standard numerical-stability constant, not a tuned parameter
        if std < 1e-12:
            return self.hold(
                price=price,
                reason=f"std-too-small | std={std:.2e}",
            )

        z = (current - mean) / std
        if not math.isfinite(z):
            return self.hold(
                price=price, reason="zscore-non-finite",
            )

        # ── Position management ──────────────────────────────────────
        if self._position_open:
            self._bars_in_position += 1
            # Time-based exit (literature 1-5 day horizon, default 3)
            if self._bars_in_position >= self.holding_period:
                self._position_open = False
                bars_held = self._bars_in_position
                self._bars_in_position = 0
                return self.sell(
                    price=price,
                    reason=(
                        f"pcr-time-exit | bars_held={bars_held} >= "
                        f"holding_period={self.holding_period} | z={z:+.3f}"
                    ),
                    order_type="market",
                )
            # Mean-reversion exit (z reverts to neutral)
            if z <= self.exit_threshold:
                self._position_open = False
                bars_held = self._bars_in_position
                self._bars_in_position = 0
                return self.sell(
                    price=price,
                    reason=(
                        f"pcr-zreverted-exit | z={z:+.3f} <= "
                        f"{self.exit_threshold} | bars_held={bars_held}"
                    ),
                    order_type="market",
                )
            return self.hold(
                price=price,
                reason=(
                    f"holding-long | z={z:+.3f} | "
                    f"bars={self._bars_in_position}/{self.holding_period}"
                ),
            )

        # Contrarian long entry on extreme high PCR.
        if z > self.entry_threshold:
            self._position_open = True
            self._bars_in_position = 0
            return self.buy(
                price=price,
                reason=(
                    f"pcr-contrarian-long | z={z:+.3f} > "
                    f"{self.entry_threshold} | hold={self.holding_period}d"
                ),
                order_type="market",
            )

        return self.hold(
            price=price,
            reason=f"pcr-neutral | z={z:+.3f}",
        )
