"""strategies/exchange_netflow_reversal.py -- sq-034 strategy.

Cross-sectional exchange-netflow reversal strategy on a 10-symbol
crypto basket at 1D. Hypothesis: cryptocurrencies that experience
the most negative exchange netflow z-score (largest net outflows
from centralised exchanges) outperform on the subsequent day; the
long bottom-quintile basket captures positive risk-adjusted returns.

The published hypothesis is long-short. The Phase 4 backtest
harness is long-only, so we test the long leg of the spread in
isolation. The bottom-N (most-negative netflow z-score) names are
held equal-weight and rotated daily.

Algorithm per bar (daily):

  1. Read each symbol's `netflow_zscore` column (pre-injected by
     the trial script from `data/onchain/<sym>_netflow.parquet`).
  2. If the column is absent or non-finite for any symbol, that
     symbol is excluded from this bar's ranking.
  3. Rank scored symbols by netflow z-score ASCENDING (most-
     negative first); pick the bottom-N (default N=2 of 10 =
     bottom quintile).
  4. BUY for symbols entering the loser portfolio with no
     position.
  5. SELL for symbols leaving the loser portfolio (rotation).
  6. Otherwise HOLD.

Long-only; equal weight across the held basket via
`position_fraction = 1 / top_n`.

Citations:
- Fantazzini, D.; Li, S. (2024). "On-Chain Data and Cryptocurrency
  Market Predictability." SSRN.
- Kim, T.-m.; Ahn, J.-w. (2023). "Unveiling the Predictive Power
  of On-Chain Data on Cryptocurrency Returns." Finance Research
  Letters.
- Chen, Y.; Li, Z.; Li, L. (2023). "What Factors Drive
  Cryptocurrency Returns? A Comprehensive Analysis Using On-Chain
  and Off-Chain Data." SSRN.
"""

from __future__ import annotations

import math
from typing import Optional

import pandas as pd

from strategies.base import Signal


NETFLOW_ZSCORE_COLUMN = "netflow_zscore"


class ExchangeNetflowReversalStrategy:
    """Long-only bottom-N cross-sectional exchange-netflow reversal."""

    def __init__(
        self,
        symbols: list[str],
        timeframe: str = "1d",
        # CITATION: exchange-netflow-reversal-literature
        # Fantazzini/Li (2024) and Chen et al. (2023) normalise daily
        # netflow with a ~30-day rolling window before signal extraction.
        zscore_window: int = 30,
        # CITATION: exchange-netflow-reversal-literature
        # Bottom quintile of a 10-symbol universe = 2 holdings, the
        # "accumulation" tail Kim/Ahn (2023) report generates the
        # strongest contrarian premium.
        top_n: int = 2,
        # CITATION: exchange-netflow-reversal-literature
        # Engine default initial_balance for the Phase 4 backtest harness.
        notional_capital: float = 10_000.0,
        name: str = "ExchangeNetflowReversal",
    ):
        if not symbols or len(symbols) < 2:
            raise ValueError("symbols must contain at least 2 entries")
        if zscore_window < 2:
            raise ValueError("zscore_window must be >= 2")
        if top_n < 1 or top_n > len(symbols):
            raise ValueError(
                f"top_n must satisfy 1 <= top_n <= len(symbols)={len(symbols)}; "
                f"got {top_n}"
            )

        self.name = str(name)
        self.symbols: list[str] = list(symbols)
        self.timeframe = str(timeframe)
        self.zscore_window = int(zscore_window)
        self.top_n = int(top_n)
        self.notional_capital = float(notional_capital)

        # engine_multi.min_history_bars = strategy.lookback_days + 2.
        # The injected netflow_zscore column already encodes the
        # rolling window; we still pad lookback_days to zscore_window
        # so the engine waits for one full warmup window of data
        # before trading.
        self.lookback_days: int = self.zscore_window + 1

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

        # 1. Pull the latest netflow z-score per symbol.
        zscores: dict[str, float] = {}
        prices_now: dict[str, float] = {}
        for sym in self.symbols:
            df = prices.get(sym)
            price = self._latest_close(df)
            prices_now[sym] = price
            if df is None or len(df) == 0:
                continue
            if NETFLOW_ZSCORE_COLUMN not in df.columns:
                continue
            z = float(df[NETFLOW_ZSCORE_COLUMN].iloc[-1])
            if math.isfinite(z):
                zscores[sym] = z

        # 2. If we cannot rank yet, HOLD everything and exit.
        if len(zscores) < self.top_n:
            for sym in self.symbols:
                out[sym] = Signal(
                    action="HOLD", strategy=self.name,
                    price=prices_now.get(sym, 0.0),
                    reason=(
                        f"warmup | scored={len(zscores)} < "
                        f"top_n={self.top_n}"
                    ),
                )
            return out

        # 3. Rank ASCENDING (most-negative z-score = biggest outflow);
        #    pick the bottom-N "accumulation" tail.
        ranked = sorted(zscores.items(), key=lambda kv: kv[1])
        target_set: set[str] = {sym for sym, _ in ranked[: self.top_n]}

        # 4. Emit signals.
        for sym in self.symbols:
            price = prices_now.get(sym, 0.0)
            score = zscores.get(sym)
            score_str = (
                f"netflow_z={score:+.3f}" if score is not None
                else "netflow_z=NA"
            )

            in_target = sym in target_set
            in_held = sym in self._held

            if in_target and not in_held:
                self._held.add(sym)
                out[sym] = Signal(
                    action="BUY", strategy=self.name, price=price,
                    reason=(
                        f"enter-outflow-bottom-{self.top_n} | {score_str}"
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
