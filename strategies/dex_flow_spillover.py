"""strategies/dex_flow_spillover.py -- sq-039 strategy.

DEX -> CEX order flow spillover on BTC/USDT 1H. Hypothesis-of-record:
statistically significant order flow imbalances on major decentralized
exchanges predict the direction of near-term price changes for the
same asset pair's perpetual / spot future on a centralized exchange.

Data-source design:

  The strategy is data-source agnostic. It consumes a pre-aligned
  ``ofi_series`` (one float per OHLCV bar, where positive = net buy
  flow, negative = net sell flow) and applies a rolling z-score
  normalization. The trial script supplies the series; in this repo
  the trial script computes a Lee-Ready style OHLCV-derived OFI
  proxy because no Uniswap V3 swap feed is wired up.

  Swapping in real on-chain DEX swap data (e.g. Uniswap V3
  WBTC/USDC pool aggregated to 1H bars per Makarov & Schoar 2023)
  requires changing only the helper that builds the series; the
  strategy class is unchanged.

Algorithm per bar (1H BTC/USDT):

  1. Look up the latest OFI proxy at df.index[-1] from the
     pre-aligned series.
  2. Compute a rolling z-score over a ``zscore_lookback`` (default
     60) trailing window of prior OFI values:
         z[t] = (ofi[t] - mean(ofi[t-60..t-1])) / std(ofi[t-60..t-1])
  3. Long signal (BUY) when z > entry_threshold (default +2.0):
     extreme positive DEX-style buy-flow regime -> directional
     spillover, ride the imbalance.
  4. Exit (SELL) after holding_period bars (default 5) since entry
     -- mid-range of the lead-lag horizon scaled from the 5-min
     Makarov & Schoar (2023) horizon to the 1H sampling frequency
     of this trial -- OR when z reverts to <= exit_threshold
     (default 0.0), whichever comes first.
  5. Long-only on spot per project conventions (matches sq-013 /
     sq-016 / sq-018 / sq-020 / sq-035 / sq-036): backtest.engine
     is structurally long-only on spot, and Han, Kang & Ryu (2024)
     report that crypto loser-shorts get punished by rebound moves.
     The literal hypothesis includes a symmetric short on extreme
     negative flow; that leg is dropped here.

Citations:

- Makarov, I. & Schoar, A. (2023). "Price Discovery in Decentralized
  Exchanges." SSRN. Order flow on Uniswap V3 significantly predicts
  CEX (Binance) prices: a 1-sigma DEX OFI predicts a 4.6 bps CEX
  price move over the next 5 minutes -- the directional-spillover
  effect this strategy targets.
- Lehar, A., St-Pierre, L. M., Moallemi, C. C., & Rizk, R. G.
  (2024). "The Role of Decentralized Exchanges in Crypto-Asset
  Price Discovery." SSRN. For ETH-USDC, Uniswap V3 contributes
  40-50% of price discovery, often leading CEX prices -- supports
  the lead-lag premise.
- Cong, L. W., Wang, Y., Tang, K., & Wang, J. (2022). "Arbitrage
  Opportunities in Decentralized Exchanges." SSRN. Persistent
  DEX/CEX dislocations average 0.29% daily profit potential --
  basis for treating extreme z-score flow as a tradeable signal.
- Han, C., Kang, B., & Ryu, J. (2024). "Time-Series and Cross-
  Sectional Momentum in the Cryptocurrency Market: A Comprehensive
  Analysis under Realistic Assumptions." SSRN. Crypto loser-shorts
  are punished by rebound moves -- precedent for the long-only
  adaptation here.
"""

from __future__ import annotations

import math
from typing import Optional

import pandas as pd

from strategies.base import BaseStrategy, Signal


class DEXFlowSpilloverStrategy(BaseStrategy):
    """Long-only directional spillover on extreme positive OFI z-score."""

    def __init__(
        self,
        symbol: str = "BTC/USDT",
        timeframe: str = "1h",
        ofi_series: Optional[pd.Series] = None,
        # CITATION: dex-flow-spillover-literature
        # Makarov & Schoar (2023) form their predictive regression
        # over a 5-minute horizon; on 1H bars a 60-bar rolling
        # window (~2.5 days) is the conventional intraday-stats
        # normalization window used in jump / OFI z-score studies
        # (matches IntradayJumpReversal vol_window scaling and the
        # 60-day window used in Chen 2023 for PCR).
        zscore_lookback: int = 60,
        # CITATION: dex-flow-spillover-literature
        # Makarov & Schoar (2023) document a 1-sigma DEX OFI -> 4.6
        # bps CEX move; the +2.0 threshold targets the cleaner
        # extreme tail (top ~2.3% of the standard normal) for a
        # higher signal-to-noise spillover trade. Same convention
        # used in sq-036 (PCR contrarian) for tail entries.
        entry_threshold: float = 2.0,
        # CITATION: dex-flow-spillover-literature
        # Hysteresis exit at z=0.0 (mean reversion of the flow
        # imbalance) avoids round-tripping when z hovers near
        # the entry threshold.
        exit_threshold: float = 0.0,
        # CITATION: dex-flow-spillover-literature
        # Makarov & Schoar (2023) document the spillover effect
        # decays within minutes on 5-min sampling; on 1H sampling
        # the equivalent horizon is short, but the cumulative
        # imbalance can persist over multiple 1H bars per Cong et
        # al. (2022) DEX/CEX arbitrage persistence findings. A
        # 5-bar (5-hour) cap captures the directional move while
        # bounding hold-time risk.
        holding_period: int = 5,
    ):
        super().__init__(
            name="DEXFlowSpillover",
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
        self._ofi_series = ofi_series

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

        if self._ofi_series is None or len(self._ofi_series) == 0:
            return self.hold(price=price, reason="no-ofi-series")

        last_ts = df.index[-1]
        # Trailing window ending at last_ts (inclusive).
        window_end = self._ofi_series.loc[: last_ts]
        if len(window_end) < self.zscore_lookback + 1:
            return self.hold(
                price=price,
                reason=(
                    f"warmup | ofi_n={len(window_end)} < "
                    f"required={self.zscore_lookback + 1}"
                ),
            )

        recent = window_end.iloc[-(self.zscore_lookback + 1):].astype(float)
        if recent.isna().any():
            return self.hold(
                price=price, reason="ofi-window-contains-nan",
            )

        prior = recent.iloc[:-1]
        current = float(recent.iloc[-1])
        mean = float(prior.mean())
        std = float(prior.std(ddof=0))
        if not (math.isfinite(mean) and math.isfinite(std)):
            return self.hold(
                price=price, reason="rolling-stats-non-finite",
            )

        # Numerical-stability floor; not a tuned parameter.
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

        # Position management
        if self._position_open:
            self._bars_in_position += 1
            # Time-based exit (lead-lag horizon, default 5 bars)
            if self._bars_in_position >= self.holding_period:
                self._position_open = False
                bars_held = self._bars_in_position
                self._bars_in_position = 0
                return self.sell(
                    price=price,
                    reason=(
                        f"ofi-time-exit | bars_held={bars_held} >= "
                        f"holding_period={self.holding_period} | z={z:+.3f}"
                    ),
                    order_type="market",
                )
            # Mean-reversion exit (flow imbalance reverts to neutral)
            if z <= self.exit_threshold:
                self._position_open = False
                bars_held = self._bars_in_position
                self._bars_in_position = 0
                return self.sell(
                    price=price,
                    reason=(
                        f"ofi-zreverted-exit | z={z:+.3f} <= "
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

        # Directional long entry on extreme positive DEX-style flow.
        if z > self.entry_threshold:
            self._position_open = True
            self._bars_in_position = 0
            return self.buy(
                price=price,
                reason=(
                    f"ofi-spillover-long | z={z:+.3f} > "
                    f"{self.entry_threshold} | hold={self.holding_period}h"
                ),
                order_type="market",
            )

        return self.hold(
            price=price,
            reason=f"ofi-neutral | z={z:+.3f}",
        )
