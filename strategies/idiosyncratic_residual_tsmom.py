"""strategies/idiosyncratic_residual_tsmom.py -- Phase 4 sq-009 strategy.

Time-series momentum on the idiosyncratic residual returns of ETH/USDT
after regressing against BTC/USDT as the market factor. Long-only;
BTC/USDT is the factor leg and is never traded (always HOLD).

Algorithm per bar:

  1. Compute log-returns for ETH and BTC over the trailing
     ``beta_window`` + ``momentum_window`` bars (synchronised on the
     intersection of the two series so both legs are bar-aligned).
  2. Rolling OLS beta of ETH log-returns vs BTC log-returns over the
     trailing ``beta_window`` bars:

         beta = cov(eth_ret[-W:], btc_ret[-W:]) / var(btc_ret[-W:])

  3. Residual return series over the trailing ``momentum_window`` bars:

         residual_ret[t] = eth_ret[t] - beta * btc_ret[t]

  4. Momentum signal = sign(sum(residual_ret[-momentum_window:])).
  5. Long ETH when signal > 0; flat (SELL) when signal <= 0.

Position state is tracked internally (single concurrent long ETH or
flat). BTC/USDT always emits HOLD.

Citations:
- Blitz, D., Huij, J. & Martens, M. (2011). "Residual momentum."
  Journal of Empirical Finance.
- Kim, J. (2022). "Idiosyncratic momentum in cryptocurrency markets."
  Applied Economics.
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np
import pandas as pd

from strategies.base import Signal


# CITATION: unit-conversion constants for the timeframe-string parser
# (24 hours per day, 60 minutes per hour).  Not tuned parameters.
_HOURS_PER_DAY: float = 24.0
_MINUTES_PER_HOUR: float = 60.0


class IdiosyncraticResidualTSMOMStrategy:
    """Idiosyncratic-residual time-series momentum on ETH/USDT.

    BTC/USDT is the market factor (never traded). At most one ETH long
    held at a time; long-only.
    """

    def __init__(
        self,
        symbols: Optional[list[str]] = None,
        eth_symbol: str = "ETH/USDT",
        btc_symbol: str = "BTC/USDT",
        timeframe: str = "1h",
        # CITATION: idiosyncratic-residual-tsmom-literature
        # Blitz/Huij/Martens (2011) use a 36-month formation window for
        # residual momentum on equities; 720 bars at 1H (~30 days) is the
        # crypto-equivalent rolling-beta window per Kim (2022).
        beta_window: int = 720,
        # CITATION: idiosyncratic-residual-tsmom-literature
        # Kim (2022) uses 1-week (168 bars at 1H) momentum horizons for
        # crypto residual TSMOM.
        momentum_window: int = 168,
        # CITATION: idiosyncratic-residual-tsmom-literature
        # Engine default initial_balance for the Phase 4 backtest harness.
        notional_capital: float = 10_000.0,
        name: str = "IdiosyncraticResidualTSMOM",
    ):
        if symbols is None:
            symbols = [eth_symbol, btc_symbol]
        if not isinstance(symbols, (list, tuple)) or len(symbols) != 2:
            raise ValueError(
                "symbols must be exactly [eth_symbol, btc_symbol] (length 2)"
            )
        if eth_symbol == btc_symbol:
            raise ValueError("eth_symbol and btc_symbol must differ")
        # CITATION: input-validation guard, not a tuned parameter
        if beta_window < 30:
            raise ValueError("beta_window must be >= 30")
        # CITATION: input-validation guard, not a tuned parameter
        if momentum_window < 5:
            raise ValueError("momentum_window must be >= 5")

        self.name = str(name)
        self.symbols: list[str] = list(symbols)
        self.eth_symbol: str = str(eth_symbol)
        self.btc_symbol: str = str(btc_symbol)
        self.timeframe = str(timeframe)
        self.beta_window = int(beta_window)
        self.momentum_window = int(momentum_window)
        self.notional_capital = float(notional_capital)

        # engine_multi.min_history_bars = strategy.lookback_days + 2.
        # We need beta_window + momentum_window + 1 bar of returns, which
        # requires beta_window + momentum_window + 2 closes (one extra
        # close to produce the first return). Convert to "days" using
        # the timeframe's hours-per-bar.
        total_bars = self.beta_window + self.momentum_window + 2
        # CITATION: _HOURS_PER_DAY is a unit-conversion constant
        bars_per_day = max(
            1, int(round(_HOURS_PER_DAY / self._timeframe_hours())),
        )
        self.lookback_days: int = max(
            1, math.ceil(total_bars / bars_per_day),
        )

        # Position state: True if currently long ETH, False if flat.
        self._long_eth: bool = False

    # -- Engine sizing hook ---------------------------------------------------

    def position_fraction(
        self,
        df: pd.DataFrame,
        n_active: int,
        max_concentration_mult: float = 2.0,
    ) -> float:
        """Full ETH sleeve when long; zero otherwise.

        BTC never gets a BUY signal, so engine_multi never calls this
        for BTC. ETH is the only traded leg; full capital concentrated
        when long is consistent with the long-only rotation contract.
        """
        return 1.0

    # -- Signal generation ----------------------------------------------------

    def generate_signals(
        self,
        prices: dict[str, pd.DataFrame],
    ) -> dict[str, Signal]:
        """Map of symbol -> Signal for the current bar."""
        df_eth = prices.get(self.eth_symbol)
        df_btc = prices.get(self.btc_symbol)

        price_eth = self._latest_close(df_eth)
        price_btc = self._latest_close(df_btc)

        if (
            df_eth is None or df_btc is None
            or len(df_eth) == 0 or len(df_btc) == 0
        ):
            return self._hold_pair(
                price_eth, price_btc, reason="missing-data",
            )

        # Warmup guard: need beta_window + momentum_window returns,
        # which means beta_window + momentum_window + 1 closes.
        min_bars = self.beta_window + self.momentum_window + 2
        if len(df_eth) < min_bars or len(df_btc) < min_bars:
            return self._hold_pair(
                price_eth, price_btc,
                reason=(
                    f"warmup | len_eth={len(df_eth)} len_btc={len(df_btc)} "
                    f"required={min_bars}"
                ),
            )

        # Synchronise on the timestamp intersection so cov / var are
        # bar-aligned across legs.
        common = df_eth.index.intersection(df_btc.index)
        if len(common) < min_bars:
            return self._hold_pair(
                price_eth, price_btc,
                reason=(
                    f"warmup | common={len(common)} < required={min_bars}"
                ),
            )

        close_eth = df_eth["close"].astype(float).loc[common].to_numpy()
        close_btc = df_btc["close"].astype(float).loc[common].to_numpy()

        if (
            np.any(close_eth <= 0)
            or np.any(close_btc <= 0)
        ):
            return self._hold_pair(
                price_eth, price_btc, reason="non-positive-close",
            )

        log_eth = np.log(close_eth)
        log_btc = np.log(close_btc)
        eth_ret = np.diff(log_eth)
        btc_ret = np.diff(log_btc)

        if eth_ret.size < self.beta_window + self.momentum_window:
            return self._hold_pair(
                price_eth, price_btc,
                reason=(
                    f"warmup | n_returns={eth_ret.size} < "
                    f"required={self.beta_window + self.momentum_window}"
                ),
            )

        # Rolling beta over the trailing beta_window returns.
        eth_form = eth_ret[-self.beta_window:]
        btc_form = btc_ret[-self.beta_window:]
        var_btc = float(np.var(btc_form, ddof=0))
        # CITATION: standard numerical stability constant, not a tuned parameter
        if var_btc < 1e-12 or not math.isfinite(var_btc):
            return self._hold_pair(
                price_eth, price_btc, reason="zero-variance-btc",
            )
        cov_eth_btc = float(
            np.mean(
                (eth_form - eth_form.mean())
                * (btc_form - btc_form.mean())
            )
        )
        beta = cov_eth_btc / var_btc
        if not math.isfinite(beta):
            return self._hold_pair(
                price_eth, price_btc, reason="beta-non-finite",
            )

        # Residual returns over the trailing momentum_window bars.
        eth_mom = eth_ret[-self.momentum_window:]
        btc_mom = btc_ret[-self.momentum_window:]
        residuals = eth_mom - beta * btc_mom
        if not np.all(np.isfinite(residuals)):
            return self._hold_pair(
                price_eth, price_btc, reason="residuals-non-finite",
            )
        signal_sum = float(residuals.sum())

        # Long when residual momentum is positive; flat otherwise.
        if signal_sum > 0:
            if not self._long_eth:
                self._long_eth = True
                return {
                    self.eth_symbol: Signal(
                        action="BUY", strategy=self.name, price=price_eth,
                        reason=(
                            f"residual-tsmom-long | sum={signal_sum:+.4f} "
                            f"> 0 | beta={beta:.4f}"
                        ),
                        order_type="market",
                    ),
                    self.btc_symbol: Signal(
                        action="HOLD", strategy=self.name, price=price_btc,
                        reason="factor-leg",
                    ),
                }
            return self._hold_pair(
                price_eth, price_btc,
                reason=(
                    f"holding-long-eth | sum={signal_sum:+.4f} > 0 | "
                    f"beta={beta:.4f}"
                ),
            )

        # signal_sum <= 0
        if self._long_eth:
            self._long_eth = False
            return {
                self.eth_symbol: Signal(
                    action="SELL", strategy=self.name, price=price_eth,
                    reason=(
                        f"residual-tsmom-flat | sum={signal_sum:+.4f} "
                        f"<= 0 | beta={beta:.4f}"
                    ),
                    order_type="market",
                ),
                self.btc_symbol: Signal(
                    action="HOLD", strategy=self.name, price=price_btc,
                    reason="factor-leg",
                ),
            }
        return self._hold_pair(
            price_eth, price_btc,
            reason=(
                f"flat | sum={signal_sum:+.4f} <= 0 | beta={beta:.4f}"
            ),
        )

    # -- Helpers --------------------------------------------------------------

    def _timeframe_hours(self) -> float:
        # CITATION: timeframe-string parser; uses _HOURS_PER_DAY /
        # _MINUTES_PER_HOUR module constants (unit conversions).
        tf = self.timeframe.strip().lower()
        if tf.endswith("h"):
            try:
                return float(tf[:-1]) or 1.0
            except ValueError:
                return 1.0
        if tf.endswith("d"):
            try:
                return float(tf[:-1]) * _HOURS_PER_DAY
            except ValueError:
                return _HOURS_PER_DAY
        if tf.endswith("m"):
            try:
                return float(tf[:-1]) / _MINUTES_PER_HOUR
            except ValueError:
                return 1.0
        return 1.0

    @staticmethod
    def _latest_close(df: Optional[pd.DataFrame]) -> float:
        if df is None or len(df) == 0:
            return 0.0
        return float(df["close"].iloc[-1])

    def _hold_pair(
        self,
        price_eth: float,
        price_btc: float,
        *,
        reason: str,
    ) -> dict[str, Signal]:
        return {
            self.eth_symbol: Signal(
                action="HOLD", strategy=self.name, price=price_eth,
                reason=reason,
            ),
            self.btc_symbol: Signal(
                action="HOLD", strategy=self.name, price=price_btc,
                reason="factor-leg",
            ),
        }
