"""strategies/cross_sectional_skewness.py -- Phase 4 sq-016 strategy.

Cross-sectional expected-idiosyncratic-skewness strategy on a
10-symbol crypto basket at 1D.  Hypothesis: cryptocurrencies with the
LOWEST expected idiosyncratic skewness outperform those with the
highest, mirroring the negative cross-sectional skewness-return
relationship documented in Liu & Chen (2024) and the long-only
expected-skewness sort of Boyer/Mitton/Vorkink (2009).

Algorithm per rebalance bar (every `holding_period` bars):

  1. For each alt asset, compute daily log returns over the past
     `lookback_period + holding_period` bars.  Same window for the
     market factor (BTC).
  2. Window 1 (current period): residuals from regressing the asset's
     last `lookback_period` returns on BTC's last `lookback_period`
     returns.  Compute is_t = skew(resid_w1), iv_t = std(resid_w1).
  3. Window 2 (prior period, offset by `holding_period`): same OLS
     against BTC over the [-(lookback+holding):-holding] slice.
     Compute is_{t-1} = skew(resid_w2), iv_{t-1} = std(resid_w2).
  4. Cross-sectional OLS across alts at this bar:
         is_t = gamma0 + gamma1 * is_{t-1} + gamma2 * iv_{t-1} + e_i
  5. Predict next-period skewness for each alt:
         E[is_{t+1}] = gamma0 + gamma1 * is_t + gamma2 * iv_t
  6. Rank alts by E[is_{t+1}] ASCENDING (lowest first); long
     bottom-N equal-weight (the low-expected-skewness 'long-leg').

Long-only.  The full long-short specification in the sq-016
implementation notes is reduced to its long leg here because:
(a) engine_multi is long-only by construction; (b) Han, Kang & Ryu
(2024) document that crypto loser-shorts get punished by rebound
moves -- the same precedent applied to sq-013 (CrossSectionalReversal)
and sq-020 (CrossSectionalMomentum); (c) Boyer/Mitton/Vorkink (2009)
report Sharpe ~0.94 specifically on the long-only bottom-quintile
expected-idiosyncratic-skewness portfolio, which is the exact leg
this implementation tests.

BTC/USDT is the market factor used to extract idiosyncratic
residuals; it is NEVER traded by this strategy.  generate_signals
always emits HOLD for BTC; position_fraction would return 0 if the
engine ever asked (it will not, since BTC never receives a BUY).

Citations:
- Liu, Y.; Chen, Y. (2024). "Skewness risk and the cross-section of
  cryptocurrency returns." International Review of Financial
  Analysis. Negative cross-sectional relationship between asymmetry
  risk (skewness) and future returns in crypto, driven by
  idiosyncratic risk.
- Tekulova, P. (2022). "Skewness/Lottery Trading Strategy in
  Cryptocurrencies." SSRN. Skewness-sorted portfolios using a long
  (360-day) lookback exhibit positive performance through crisis
  periods.
- Boyer, B.; Mitton, T.; Vorkink, K. (2009). "Expected Idiosyncratic
  Skewness." Review of Financial Studies. Long-only bottom-quintile
  expected-idiosyncratic-skewness portfolio achieved Sharpe ~0.94
  in equities -- the methodology this implementation adapts to a
  crypto basket with BTC as the market factor.
- Han, C.; Kang, B.; Ryu, J. (2024). "Time-Series and Cross-Sectional
  Momentum in the Cryptocurrency Market: A Comprehensive Analysis
  under Realistic Assumptions." SSRN. Crypto loser-shorts get
  punished by rebound moves -- justifies long-only adaptation.
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np
import pandas as pd

from strategies.base import Signal


class CrossSectionalSkewnessStrategy:
    """Long-only bottom-N expected-idiosyncratic-skewness portfolio."""

    def __init__(
        self,
        symbols: list[str],
        market_symbol: str = "BTC/USDT",
        timeframe: str = "1d",
        # CITATION: cross-sectional-skewness-literature
        # Boyer/Mitton/Vorkink (2009) compute monthly idiosyncratic
        # skewness from daily residuals over a 60-day window; the same
        # 60-bar daily lookback is the natural cryptocurrency analogue.
        lookback_period: int = 60,
        # CITATION: cross-sectional-skewness-literature
        # Bottom quintile of a ~10-symbol universe (= 2 holdings).
        # Boyer/Mitton/Vorkink (2009) report the strongest premium on
        # the lowest-expected-skewness tail.
        top_n: int = 2,
        # CITATION: cross-sectional-skewness-literature
        # Boyer/Mitton/Vorkink (2009) rebalance monthly; the sq-016
        # implementation notes adopt the same monthly cadence, which
        # at 1D candles corresponds to a 30-bar holding period.
        holding_period: int = 30,
        # CITATION: cross-sectional-skewness-literature
        # Engine default initial_balance for the Phase 4 backtest
        # harness.
        notional_capital: float = 10_000.0,
        name: str = "CrossSectionalSkewness",
    ):
        if not symbols or len(symbols) < 3:
            raise ValueError(
                "symbols must contain at least 3 entries (market + >=2 alts)"
            )
        if market_symbol not in symbols:
            raise ValueError(
                f"market_symbol={market_symbol!r} must appear in "
                f"symbols={symbols!r}"
            )
        if lookback_period < 5:
            raise ValueError("lookback_period must be >= 5")
        if holding_period < 1:
            raise ValueError("holding_period must be >= 1")

        alt_symbols = [s for s in symbols if s != market_symbol]
        if top_n < 1 or top_n > len(alt_symbols):
            raise ValueError(
                f"top_n must satisfy 1 <= top_n <= n_alts="
                f"{len(alt_symbols)}; got {top_n}"
            )

        self.name = str(name)
        self.symbols: list[str] = list(symbols)
        self.market_symbol = str(market_symbol)
        self.alt_symbols: list[str] = alt_symbols
        self.timeframe = str(timeframe)
        self.lookback_period = int(lookback_period)
        self.top_n = int(top_n)
        self.holding_period = int(holding_period)
        self.notional_capital = float(notional_capital)

        # engine_multi.min_history_bars = strategy.lookback_days + 2.
        # We need lookback + holding + 1 closes (giving lookback+holding
        # daily returns spanning the two non-overlapping windows) before
        # the strategy can rank.
        self.lookback_days: int = self.lookback_period + self.holding_period + 1

        # Held set drives BUY / SELL / HOLD emission on rotation bars.
        self._held: set[str] = set()
        # Rebalance scheduler -- first call after engine warmup is the
        # initial rebalance; thereafter every holding_period bars.
        self._bars_since_rebalance: int = 0
        self._first_signal: bool = True

    # -- Engine sizing hook ---------------------------------------------------

    def position_fraction(
        self,
        df: pd.DataFrame,
        n_active: int,
        max_concentration_mult: float = 2.0,
    ) -> float:
        """Equal weight across the top_n held alts -- 1 / top_n.

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
        prices_now: dict[str, float] = {
            sym: self._latest_close(prices.get(sym)) for sym in self.symbols
        }

        # Decide whether this bar is a rebalance bar.
        if self._first_signal:
            self._first_signal = False
            self._bars_since_rebalance = 0
            is_rebalance = True
        else:
            self._bars_since_rebalance += 1
            if self._bars_since_rebalance >= self.holding_period:
                self._bars_since_rebalance = 0
                is_rebalance = True
            else:
                is_rebalance = False

        if not is_rebalance:
            # Mid-holding bar: HOLD every symbol so existing positions
            # ride through to the next rebalance.
            out: dict[str, Signal] = {}
            for sym in self.symbols:
                if sym == self.market_symbol:
                    reason = "market-factor | always-HOLD (never traded)"
                elif sym in self._held:
                    reason = (
                        f"holding | bars_since_rebalance="
                        f"{self._bars_since_rebalance}/{self.holding_period}"
                    )
                else:
                    reason = (
                        f"flat | bars_since_rebalance="
                        f"{self._bars_since_rebalance}/{self.holding_period}"
                    )
                out[sym] = Signal(
                    action="HOLD", strategy=self.name,
                    price=prices_now.get(sym, 0.0), reason=reason,
                )
            return out

        # Rebalance path -------------------------------------------------------
        out_rb: dict[str, Signal] = {}

        # 1. Compute BTC log-return series over the joint window.
        btc_df = prices.get(self.market_symbol)
        btc_returns = self._log_returns(btc_df)
        joint_len = self.lookback_period + self.holding_period

        if btc_returns is None or btc_returns.size < joint_len:
            # Market factor too short -- HOLD everything.
            for sym in self.symbols:
                out_rb[sym] = Signal(
                    action="HOLD", strategy=self.name,
                    price=prices_now.get(sym, 0.0),
                    reason=(
                        f"warmup | market-returns="
                        f"{0 if btc_returns is None else btc_returns.size}"
                        f" < {joint_len}"
                    ),
                )
            return out_rb

        btc_w1 = btc_returns[-self.lookback_period:]
        btc_w2 = btc_returns[
            -(self.lookback_period + self.holding_period):-self.holding_period
        ]

        # 2. Per-alt: residuals + (is_t, iv_t, is_{t-1}, iv_{t-1}).
        is_t_map: dict[str, float] = {}
        iv_t_map: dict[str, float] = {}
        is_t1_map: dict[str, float] = {}
        iv_t1_map: dict[str, float] = {}

        for sym in self.alt_symbols:
            alt_returns = self._log_returns(prices.get(sym))
            if alt_returns is None or alt_returns.size < joint_len:
                continue

            alt_w1 = alt_returns[-self.lookback_period:]
            alt_w2 = alt_returns[
                -(self.lookback_period + self.holding_period):
                -self.holding_period
            ]

            stats_t = self._residual_stats(alt_w1, btc_w1)
            stats_t1 = self._residual_stats(alt_w2, btc_w2)
            if stats_t is None or stats_t1 is None:
                continue

            is_t_map[sym] = stats_t[0]
            iv_t_map[sym] = stats_t[1]
            is_t1_map[sym] = stats_t1[0]
            iv_t1_map[sym] = stats_t1[1]

        # 3. Need >= 3 alts for a 3-coefficient cross-sectional fit.
        scored = [s for s in self.alt_symbols if s in is_t_map]
        if len(scored) < max(3, self.top_n):
            for sym in self.symbols:
                out_rb[sym] = Signal(
                    action="HOLD", strategy=self.name,
                    price=prices_now.get(sym, 0.0),
                    reason=(
                        f"warmup | scored={len(scored)} < "
                        f"required={max(3, self.top_n)}"
                    ),
                )
            return out_rb

        # 4. Cross-sectional OLS: is_t = g0 + g1*is_{t-1} + g2*iv_{t-1} + e.
        is_t_arr = np.array([is_t_map[s] for s in scored], dtype=np.float64)
        iv_t_arr = np.array([iv_t_map[s] for s in scored], dtype=np.float64)
        is_t1_arr = np.array(
            [is_t1_map[s] for s in scored], dtype=np.float64
        )
        iv_t1_arr = np.array(
            [iv_t1_map[s] for s in scored], dtype=np.float64
        )

        gammas = self._fit_3coef_ols(is_t1_arr, iv_t1_arr, is_t_arr)
        if gammas is None:
            # Singular regression matrix -- fall back to ranking by
            # current-period skewness alone (still long bottom-N).
            expected_skew = is_t_arr
            ranking_label = "fallback-is_t-rank"
        else:
            g0, g1, g2 = gammas
            expected_skew = g0 + g1 * is_t_arr + g2 * iv_t_arr
            ranking_label = (
                f"E[is_t+1]=g0={g0:+.3f}+g1={g1:+.3f}*is_t+g2={g2:+.3f}*iv_t"
            )

        # 5. Rank ASCENDING; pick bottom-N (lowest expected skewness).
        order = np.argsort(expected_skew)
        target_set: set[str] = {scored[idx] for idx in order[: self.top_n]}

        scores: dict[str, float] = {
            scored[i]: float(expected_skew[i]) for i in range(len(scored))
        }

        # 6. Emit signals.
        for sym in self.symbols:
            price = prices_now.get(sym, 0.0)

            if sym == self.market_symbol:
                out_rb[sym] = Signal(
                    action="HOLD", strategy=self.name, price=price,
                    reason=(
                        f"market-factor | {ranking_label}"
                    ),
                )
                continue

            score = scores.get(sym)
            score_str = (
                f"E_skew={score:+.4f}"
                if score is not None else "E_skew=NA"
            )

            in_target = sym in target_set
            in_held = sym in self._held

            if in_target and not in_held:
                self._held.add(sym)
                out_rb[sym] = Signal(
                    action="BUY", strategy=self.name, price=price,
                    reason=(
                        f"enter-low-skew-bottom-{self.top_n} | {score_str}"
                    ),
                    order_type="market",
                )
            elif (not in_target) and in_held:
                self._held.discard(sym)
                out_rb[sym] = Signal(
                    action="SELL", strategy=self.name, price=price,
                    reason=f"exit-rotation | {score_str}",
                    order_type="market",
                )
            else:
                if in_held:
                    reason = f"holding-low-skew | {score_str}"
                else:
                    reason = f"flat | {score_str}"
                out_rb[sym] = Signal(
                    action="HOLD", strategy=self.name, price=price,
                    reason=reason,
                )

        return out_rb

    # -- Helpers --------------------------------------------------------------

    @staticmethod
    def _latest_close(df: Optional[pd.DataFrame]) -> float:
        if df is None or len(df) == 0:
            return 0.0
        return float(df["close"].iloc[-1])

    @staticmethod
    def _log_returns(df: Optional[pd.DataFrame]) -> Optional[np.ndarray]:
        """Daily log returns from a per-symbol OHLCV slice.

        Returns None when the slice is missing or has < 2 closes;
        skips non-finite / non-positive closes safely.
        """
        if df is None or len(df) < 2:
            return None
        close = df["close"].astype(float).to_numpy()
        if not np.all(np.isfinite(close)) or np.any(close <= 0):
            return None
        rets = np.diff(np.log(close))
        if not np.all(np.isfinite(rets)):
            return None
        return rets

    @staticmethod
    def _residual_stats(
        alt_returns: np.ndarray,
        market_returns: np.ndarray,
    ) -> Optional[tuple[float, float]]:
        """OLS-residual (skewness, volatility) over a paired return window.

        Regresses alt = alpha + beta*market + eps via numpy lstsq;
        returns (skew(eps), std(eps)) or None on degenerate inputs.
        """
        if alt_returns.size != market_returns.size:
            return None
        if alt_returns.size < 5:
            return None
        if not (np.all(np.isfinite(alt_returns))
                and np.all(np.isfinite(market_returns))):
            return None
        var_m = float(np.var(market_returns))
        if var_m <= 1e-18:
            return None
        X = np.column_stack(
            [np.ones(market_returns.size, dtype=np.float64),
             market_returns.astype(np.float64)]
        )
        try:
            coefs, *_ = np.linalg.lstsq(X, alt_returns, rcond=None)
        except np.linalg.LinAlgError:
            return None
        alpha = float(coefs[0])
        beta = float(coefs[1])
        residuals = alt_returns - (alpha + beta * market_returns)
        if not np.all(np.isfinite(residuals)):
            return None
        skew = _safe_skew(residuals)
        vol = float(np.std(residuals))
        if not (math.isfinite(skew) and math.isfinite(vol)):
            return None
        return (skew, vol)

    @staticmethod
    def _fit_3coef_ols(
        x1: np.ndarray,
        x2: np.ndarray,
        y: np.ndarray,
    ) -> Optional[tuple[float, float, float]]:
        """Cross-sectional OLS y = g0 + g1*x1 + g2*x2 across alts.

        Returns (g0, g1, g2) or None when the design matrix is
        singular / underdetermined.
        """
        n = y.size
        if n < 3:
            return None
        if x1.size != n or x2.size != n:
            return None
        if not (np.all(np.isfinite(x1))
                and np.all(np.isfinite(x2))
                and np.all(np.isfinite(y))):
            return None
        X = np.column_stack(
            [np.ones(n, dtype=np.float64), x1.astype(np.float64),
             x2.astype(np.float64)]
        )
        try:
            coefs, _, rank, _ = np.linalg.lstsq(X, y, rcond=None)
        except np.linalg.LinAlgError:
            return None
        if rank < 3:
            return None
        if not np.all(np.isfinite(coefs)):
            return None
        return (float(coefs[0]), float(coefs[1]), float(coefs[2]))


def _safe_skew(x: np.ndarray) -> float:
    """Fisher-Pearson biased sample skewness.

    Equivalent to scipy.stats.skew(x, bias=True); coded inline to
    avoid pulling scipy as an additional runtime dependency.
    """
    arr = np.asarray(x, dtype=np.float64)
    n = arr.size
    if n < 3:
        return float("nan")
    mu = float(arr.mean())
    diff = arr - mu
    m2 = float(np.mean(diff * diff))
    if m2 <= 1e-18:
        return float("nan")
    m3 = float(np.mean(diff * diff * diff))
    return float(m3 / (m2 ** 1.5))
