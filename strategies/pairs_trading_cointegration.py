"""
strategies/pairs_trading_cointegration.py — Phase 4.C sq-005 Variation #1.

REFRAME NOTE: Classical pairs trading requires short positions (long the
undervalued leg, short the overvalued leg simultaneously). engine_multi is
long-only. This implementation reframes as a two-asset rotation: hold
BTC/USDT when the spread is abnormally low (BTC has underperformed relative
to ETH), hold ETH/USDT when the spread is abnormally high (ETH has
underperformed relative to BTC). At most one asset is held at a time; no
short positions. The cointegration signal is used for timing/selection,
not delta-neutral hedging.

This tests whether the cointegration spread has directional predictive power
in crypto -- a necessary (not sufficient) condition for the full pairs trade.
If this passes the verdict tree, a proper delta-neutral variation (Option A)
becomes worth the engine investment.

Citations:
  Park, T. (2026). Statistical Arbitrage Strategies Using Cointegration
    Analysis in Cryptocurrency Markets. IJSRA.
  Carvalho, D.S. (2021). Pairs trading: cointegration-based methods applied
    to the cryptocurrency market. Master Dissertation, UCP.
  Tadi, M. & Witzany, J. (2023). Copula-Based Trading of Cointegrated
    Cryptocurrency Pairs. arXiv.
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np
import pandas as pd

from strategies.base import Signal


class PairsTradingCointegrationStrategy:
    """Two-asset cointegration rotation between symbol_a and symbol_b.

    Long-only by construction: at most one asset is held at any time.
    The cointegration spread z-score selects which asset to rotate into
    when the relationship is statistically significant (ADF p < threshold).
    """

    def __init__(
        self,
        symbols: Optional[list[str]] = None,
        symbol_a: str = "BTC/USDT",
        symbol_b: str = "ETH/USDT",
        timeframe: str = "1h",
        # CITATION: pairs-trading-cointegration-literature
        # Carvalho (2021): longer formation periods (~6 months at daily,
        # equivalent to 720 bars at 1H) outperform shorter ones for
        # Engle-Granger cointegration on crypto pairs.
        hedge_ratio_window: int = 720,
        # CITATION: pairs-trading-cointegration-literature
        # Park (2026) calibrates the spread z-score window in the
        # 100-200 bar range; 168 bars (= 1 week at 1H) sits in that
        # band and aligns with weekly mean-reversion horizons.
        zscore_window: int = 168,
        # CITATION: pairs-trading-cointegration-literature
        # Park (2026) sec.3 entry threshold |z| > 2.0; matches the
        # 2-sigma convention in Tadi & Witzany (2023) sec.4.
        entry_z_threshold: float = 2.0,
        # CITATION: pairs-trading-cointegration-literature
        # Carvalho (2021) sec.5.2 exit at |z| <= 0.5 for the rotation
        # variant -- earlier than z=0 to capture partial reversion and
        # reduce round-trips during noisy mean re-touches.
        exit_z_threshold: float = 0.5,
        # CITATION: pairs-trading-cointegration-literature
        # Standard Engle-Granger ADF significance level for
        # cointegration acceptance per Park (2026) sec.3.
        cointegration_pvalue_threshold: float = 0.05,
        # CITATION: pairs-trading-cointegration-literature
        # Engine default initial_balance for the Phase 4 backtest harness.
        notional_capital: float = 10_000.0,
        name: str = "PairsTradingCointegration",
    ):
        if symbols is None:
            symbols = [symbol_a, symbol_b]
        if not isinstance(symbols, (list, tuple)) or len(symbols) != 2:
            raise ValueError(
                "symbols must be exactly [symbol_a, symbol_b] (length 2)"
            )
        if symbols[0] == symbols[1]:
            raise ValueError("symbol_a and symbol_b must differ")
        if hedge_ratio_window < 30:
            raise ValueError("hedge_ratio_window must be >= 30")
        if zscore_window < 10:
            raise ValueError("zscore_window must be >= 10")
        if entry_z_threshold <= 0:
            raise ValueError("entry_z_threshold must be > 0")
        if exit_z_threshold < 0:
            raise ValueError("exit_z_threshold must be >= 0")
        if exit_z_threshold >= entry_z_threshold:
            raise ValueError(
                "exit_z_threshold must be strictly less than entry_z_threshold"
            )
        if not (0.0 < cointegration_pvalue_threshold < 1.0):
            raise ValueError(
                "cointegration_pvalue_threshold must lie in (0, 1)"
            )

        self.name = str(name)
        self.symbols: list[str] = list(symbols)
        self.symbol_a: str = self.symbols[0]
        self.symbol_b: str = self.symbols[1]
        self.timeframe = str(timeframe)
        self.hedge_ratio_window = int(hedge_ratio_window)
        self.zscore_window = int(zscore_window)
        self.entry_z_threshold = float(entry_z_threshold)
        self.exit_z_threshold = float(exit_z_threshold)
        self.cointegration_pvalue_threshold = float(
            cointegration_pvalue_threshold
        )
        self.notional_capital = float(notional_capital)

        # engine_multi reads `lookback_days` for the per-bar history
        # gate: ceil((hedge_ratio_window + zscore_window) / 24) + 5.
        # The +5 buffer absorbs index-alignment slack between the two
        # legs without delaying the warmup beyond what the strategy
        # itself enforces internally.
        total_bars = self.hedge_ratio_window + self.zscore_window
        self.lookback_days: int = math.ceil(total_bars / 24) + 5

        # Position state: None | "A" | "B" -- exactly one asset held.
        self._current_position: Optional[str] = None

    # -- Engine sizing hook ---------------------------------------------------

    def position_fraction(
        self,
        df: pd.DataFrame,
        n_active: int,
        max_concentration_mult: float = 2.0,
    ) -> float:
        """Full capital in whichever asset is held.

        At most one asset is held at a time by construction, so 1.0 here
        never causes over-allocation. engine_multi clamps to available
        cash, so this is a soft cap.
        """
        return 1.0

    # -- Signal generation ----------------------------------------------------

    def generate_signals(
        self,
        prices: dict[str, pd.DataFrame],
    ) -> dict[str, Signal]:
        """Map of symbol -> Signal for the current bar."""
        df_a = prices.get(self.symbol_a)
        df_b = prices.get(self.symbol_b)

        price_a = self._latest_close(df_a)
        price_b = self._latest_close(df_b)

        if (
            df_a is None or df_b is None
            or len(df_a) == 0 or len(df_b) == 0
        ):
            return self._both_hold(price_a, price_b, reason="missing-data")

        # 1. Warmup guard.
        min_bars = self.hedge_ratio_window + self.zscore_window + 2
        if len(df_a) < min_bars or len(df_b) < min_bars:
            return self._both_hold(
                price_a, price_b,
                reason=(
                    f"warmup | len_a={len(df_a)} len_b={len(df_b)} "
                    f"required={min_bars}"
                ),
            )

        # Synchronise on intersection of timestamps so the two series
        # are bar-aligned for cov / var.
        common = df_a.index.intersection(df_b.index)
        if len(common) < min_bars:
            return self._both_hold(
                price_a, price_b,
                reason=(
                    f"warmup | common={len(common)} < required={min_bars}"
                ),
            )

        close_a = df_a["close"].astype(float).loc[common].to_numpy()
        close_b = df_b["close"].astype(float).loc[common].to_numpy()

        # 2. Spread computation in log space.
        total = self.hedge_ratio_window + self.zscore_window
        if np.any(close_a[-total:] <= 0) or np.any(close_b[-total:] <= 0):
            return self._both_hold(
                price_a, price_b, reason="non-positive-close",
            )
        log_a = np.log(close_a[-total:])
        log_b = np.log(close_b[-total:])

        log_a_form = log_a[-self.hedge_ratio_window:]
        log_b_form = log_b[-self.hedge_ratio_window:]
        var_b = float(np.var(log_b_form, ddof=0))
        # CITATION: standard numerical stability constant, not a tuned parameter
        if var_b < 1e-12 or not math.isfinite(var_b):
            return self._both_hold(
                price_a, price_b, reason="zero-variance-log-b",
            )
        cov_ab = float(
            np.mean(
                (log_a_form - log_a_form.mean())
                * (log_b_form - log_b_form.mean())
            )
        )
        hedge_ratio = cov_ab / var_b
        if not math.isfinite(hedge_ratio):
            return self._both_hold(
                price_a, price_b, reason="hedge-ratio-non-finite",
            )

        # Spread series over the trailing zscore_window bars.
        spread = (
            log_a[-self.zscore_window:]
            - hedge_ratio * log_b[-self.zscore_window:]
        )

        # 3. Cointegration filter via ADF on the spread series.
        try:
            from statsmodels.tsa.stattools import adfuller
            adf_res = adfuller(spread, autolag="AIC")
            p_value = float(adf_res[1])
        except Exception as exc:
            return self._both_hold(
                price_a, price_b,
                reason=f"adf-failure: {exc.__class__.__name__}",
            )

        if (
            not math.isfinite(p_value)
            or p_value >= self.cointegration_pvalue_threshold
        ):
            # If the pair has decointegrated and we are holding, close
            # out so we don't ride a divergent spread. Otherwise sit out.
            if self._current_position is not None:
                return self._close_current(
                    price_a, price_b,
                    reason=f"decointegrated p={p_value:.3f}",
                )
            return self._both_hold(
                price_a, price_b,
                reason=f"not-cointegrated p={p_value:.3f}",
            )

        # 4. Z-score of the spread.
        spread_mean = float(spread.mean())
        spread_std = float(spread.std(ddof=0))
        # CITATION: standard numerical stability constant, not a tuned parameter
        if spread_std < 1e-12 or not math.isfinite(spread_std):
            return self._both_hold(
                price_a, price_b, reason="zero-spread-std",
            )
        z = (float(spread[-1]) - spread_mean) / spread_std

        # 6. Signal logic -- closes before opens.
        # Exit rules first.
        if self._current_position == "A" and z >= -self.exit_z_threshold:
            self._current_position = None
            return {
                self.symbol_a: Signal(
                    action="SELL", strategy=self.name, price=price_a,
                    reason=(
                        f"exit-A | z={z:+.3f} >= "
                        f"-{self.exit_z_threshold:.2f} | p={p_value:.3f}"
                    ),
                    order_type="market",
                ),
                self.symbol_b: Signal(
                    action="HOLD", strategy=self.name, price=price_b,
                    reason=f"flat-after-exit-A | z={z:+.3f}",
                ),
            }
        if self._current_position == "B" and z <= self.exit_z_threshold:
            self._current_position = None
            return {
                self.symbol_b: Signal(
                    action="SELL", strategy=self.name, price=price_b,
                    reason=(
                        f"exit-B | z={z:+.3f} <= "
                        f"+{self.exit_z_threshold:.2f} | p={p_value:.3f}"
                    ),
                    order_type="market",
                ),
                self.symbol_a: Signal(
                    action="HOLD", strategy=self.name, price=price_a,
                    reason=f"flat-after-exit-B | z={z:+.3f}",
                ),
            }

        # Entry rules (only when flat).
        if self._current_position is None:
            if z < -self.entry_z_threshold:
                self._current_position = "A"
                return {
                    self.symbol_a: Signal(
                        action="BUY", strategy=self.name, price=price_a,
                        reason=(
                            f"entry-A | z={z:+.3f} < "
                            f"-{self.entry_z_threshold:.2f} | "
                            f"hedge={hedge_ratio:.4f} | p={p_value:.3f}"
                        ),
                        order_type="market",
                    ),
                    self.symbol_b: Signal(
                        action="HOLD", strategy=self.name, price=price_b,
                        reason=f"flat-leg | z={z:+.3f}",
                    ),
                }
            if z > self.entry_z_threshold:
                self._current_position = "B"
                return {
                    self.symbol_b: Signal(
                        action="BUY", strategy=self.name, price=price_b,
                        reason=(
                            f"entry-B | z={z:+.3f} > "
                            f"+{self.entry_z_threshold:.2f} | "
                            f"hedge={hedge_ratio:.4f} | p={p_value:.3f}"
                        ),
                        order_type="market",
                    ),
                    self.symbol_a: Signal(
                        action="HOLD", strategy=self.name, price=price_a,
                        reason=f"flat-leg | z={z:+.3f}",
                    ),
                }

        # Default: HOLD both with informative reason.
        if self._current_position == "A":
            holding_reason = (
                f"holding-A | z={z:+.3f} | exit_at z>=-"
                f"{self.exit_z_threshold:.2f}"
            )
        elif self._current_position == "B":
            holding_reason = (
                f"holding-B | z={z:+.3f} | exit_at z<=+"
                f"{self.exit_z_threshold:.2f}"
            )
        else:
            holding_reason = (
                f"flat | z={z:+.3f} within "
                f"+/-{self.entry_z_threshold:.2f} | p={p_value:.3f}"
            )
        return self._both_hold(price_a, price_b, reason=holding_reason)

    # -- Helpers --------------------------------------------------------------

    @staticmethod
    def _latest_close(df: Optional[pd.DataFrame]) -> float:
        if df is None or len(df) == 0:
            return 0.0
        return float(df["close"].iloc[-1])

    def _both_hold(
        self,
        price_a: float,
        price_b: float,
        *,
        reason: str,
    ) -> dict[str, Signal]:
        return {
            self.symbol_a: Signal(
                action="HOLD", strategy=self.name, price=price_a,
                reason=reason,
            ),
            self.symbol_b: Signal(
                action="HOLD", strategy=self.name, price=price_b,
                reason=reason,
            ),
        }

    def _close_current(
        self,
        price_a: float,
        price_b: float,
        *,
        reason: str,
    ) -> dict[str, Signal]:
        prior = self._current_position
        self._current_position = None
        if prior == "A":
            return {
                self.symbol_a: Signal(
                    action="SELL", strategy=self.name, price=price_a,
                    reason=f"force-close-A | {reason}",
                    order_type="market",
                ),
                self.symbol_b: Signal(
                    action="HOLD", strategy=self.name, price=price_b,
                    reason=f"flat-after-force-close-A | {reason}",
                ),
            }
        if prior == "B":
            return {
                self.symbol_b: Signal(
                    action="SELL", strategy=self.name, price=price_b,
                    reason=f"force-close-B | {reason}",
                    order_type="market",
                ),
                self.symbol_a: Signal(
                    action="HOLD", strategy=self.name, price=price_a,
                    reason=f"flat-after-force-close-B | {reason}",
                ),
            }
        return self._both_hold(price_a, price_b, reason=reason)
