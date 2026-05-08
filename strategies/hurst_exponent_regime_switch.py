"""strategies/hurst_exponent_regime_switch.py -- sq-027 single-symbol strategy.

Hurst-exponent regime-switch strategy on BTC/USDT 4H. The rolling
Hurst exponent classifies the current regime as trending/persistent
(H > h_upper), mean-reverting/anti-persistent (H < h_lower), or
random-walk (h_lower <= H <= h_upper). Only one sub-strategy is
active at any bar:

  - H > h_upper          -> trend-following: long when trailing
                            momentum_lookback log-return sum > 0;
                            flat otherwise.
  - H < h_lower          -> mean-reversion: long when rolling z-score
                            of close vs zscore_window mean is < entry_z;
                            exit when z >= exit_z (or stop hit).
  - h_lower <= H <= h_upper -> neutral (no new entries; hold existing
                            position until its sub-strategy emits an
                            exit).

Long-only by construction (engine_multi has no short-side harness;
single concurrent BTC long; same flat-when-negative convention as
DualMomentum / VolumeWeightedTSMOM / VolatilityScaledTSMOM). The
strategy tracks _active_mode so the exit logic that closes a position
matches the sub-strategy that opened it.

No look-ahead: the rolling Hurst window at bar t consumes only bars
strictly before t (returns are diff'd then SHIFTed by 1 inside the
helpers); the trend-following momentum and mean-reversion z-score
windows also consume only bars [t - window, t - 1].

Hurst estimator: rescaled-range (R/S) on the trailing
hurst_window log-returns. Single-window R/S, computed in closed form
each bar:

    Y_i        = sum(returns[0..i] - mean(returns))
    R          = max(Y) - min(Y)
    S          = std(returns, ddof=1)
    H_hat      = log(R / S) / log(N)

This is the classical Hurst (1951) / Mandelbrot (1968) estimator
referenced as the standard rolling-window estimator by Calef &
Kucinic (2021) and Begusic et al. (2022). A single-window
estimator is preferred over multi-scale R/S here because the rolling
window is itself a single contiguous slice and the multi-scale
approach would require nested sub-windows that the rolling cadence
recomputes on every bar.

Citations:
- Calef, A., Kucinic, M. (2021). "Switching approach for
  Cryptocurrency trading using the Hurst exponent." SSRN.
  Switching between trend-following (H>0.5) and mean-reversion
  (H<0.5) on Bitcoin yields Sharpe 0.69, outperforming both
  standalone strategies and buy-and-hold. The paper's H>0.5 vs
  H<0.5 cut is widened here to a 0.45 / 0.55 neutral band per
  the implementation_notes "neutral zone between the thresholds
  can be used to avoid trading in random-walk conditions."
- Begusic, S., Velickovic, P., Lio, P. (2022). "Deep
  Reinforcement Learning in Cryptocurrency Trading: A Review and
  Case Study on the Hurst Exponent." Applied Sciences. Adding the
  time-varying Hurst exponent as a state feature increased a DRL
  agent's BTC/USD Sharpe from 1.05 to 1.77, confirming that
  rolling-Hurst regime classification carries directional
  information on Bitcoin daily-bar data.
- Kyriazis, N. A. (2020). "The adaptive market hypothesis in the
  cryptocurrency markets." Eurasian Economic Review. 15 major
  cryptocurrencies exhibit time-varying Hurst exponents, supporting
  the AMH and the regime-based switching rationale.

Output is ASCII-only.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal


def _hurst_rs(returns: np.ndarray) -> float:
    """Rescaled-range Hurst exponent on a single contiguous window.

    Returns NaN when the window is too short (< 20 returns) or the
    standard deviation is zero (degenerate flat returns). The output
    is bounded to (0, 2) by the R/S construction; values outside
    [0.0, 1.0] are extrapolation artefacts of the small-sample
    estimator and the caller treats them with the same regime-band
    test as in-band values.
    """
    arr = np.asarray(returns, dtype=np.float64)
    arr = arr[np.isfinite(arr)]
    n = arr.size
    if n < 20:
        return float("nan")
    mean = float(arr.mean())
    centered = arr - mean
    Y = np.cumsum(centered)
    R = float(Y.max() - Y.min())
    S = float(arr.std(ddof=1))
    if S <= 0.0 or not math.isfinite(S):
        return float("nan")
    if R <= 0.0 or not math.isfinite(R):
        return float("nan")
    return float(np.log(R / S) / np.log(n))


class HurstExponentRegimeSwitchStrategy(BaseStrategy):
    """BTC/USDT 4H long-only Hurst-exponent regime-switch."""

    def __init__(
        self,
        symbol: str = "BTC/USDT",
        timeframe: str = "4h",
        # CITATION: hurst-exponent-regime-switch-literature
        # Calef & Kucinic (2021) and the implementation_notes
        # specify a "rolling window of returns (e.g., 100-250
        # periods)". 100 sits at the lower bound of that range and
        # is the smallest window that gives the R/S estimator a
        # statistically meaningful sample on the 4H substrate
        # (~16.7 days of regime evidence, well above the typical
        # 1-week crypto regime persistence reported by Kyriazis
        # (2020)).
        hurst_window: int = 100,
        # CITATION: hurst-exponent-regime-switch-literature
        # Implementation_notes: "If H > a threshold (e.g., 0.55),
        # deploy trend-following. If H < a threshold (e.g., 0.45),
        # deploy mean-reversion. A neutral zone between the
        # thresholds can be used to avoid trading in random-walk
        # conditions." 0.45 / 0.55 are the cited example
        # thresholds.
        h_upper: float = 0.55,
        h_lower: float = 0.45,
        # CITATION: hurst-exponent-regime-switch-literature
        # Trend-following sub-strategy momentum window. 30 4H bars
        # = ~5 days, the shortest momentum horizon documented as
        # productive on BTC daily-equivalent data by Calef &
        # Kucinic (2021) and aligned with the
        # VolatilityScaledTSMOM (sq-021) BTC/USDT 1D
        # momentum_lookback=30 default. The same lookback is used
        # so the trend leg is directly comparable to the
        # standalone TSMOM trial.
        momentum_lookback: int = 30,
        # CITATION: hurst-exponent-regime-switch-literature
        # Mean-reversion z-score window. 30 4H bars (~5 days).
        # Matches the trend window so both sub-strategies see the
        # same trailing horizon, which keeps the regime-switch
        # decision boundary statistically clean (the only thing
        # that flips is the SIGN of the rule, not the lookback).
        zscore_window: int = 30,
        # CITATION: hurst-exponent-regime-switch-literature
        # Entry z threshold for the MR sub-strategy. -1.5 matches
        # the MeanReversion_BTC_Residual (Phase 4.A v1) entry_z
        # threshold derived from Fil & Kristoufek (2020); a 1.5
        # standard-deviation oversold cut is the standard
        # crypto-MR entry trigger.
        entry_z: float = -1.5,
        # CITATION: hurst-exponent-regime-switch-literature
        # Exit z threshold. Returning to the rolling mean (z=0) is
        # the canonical mean-reversion exit per the same Fil &
        # Kristoufek convention used in MeanReversion_BTC_Residual.
        exit_z: float = 0.0,
        # CITATION: hurst-exponent-regime-switch-literature
        # Hard stop on the MR leg. -8% matches the
        # MeanReversion_BTC_Residual stop_loss_pct=0.08 sized to
        # crypto's 4H realized vol so the worst-case loss is
        # bounded when the residual fails to revert. Trend leg
        # uses no fixed stop -- it exits on momentum-flat.
        mr_stop_loss_pct: float = 0.08,
        # CITATION: hurst-exponent-regime-switch-literature
        # Engine default initial_balance for the Phase 4 backtest
        # harness; the simulator clamps amount_usdt to available
        # balance so this is the reference notional only.
        notional_capital: float = 10_000.0,
    ):
        super().__init__(
            name="HurstExponentRegimeSwitch",
            symbol=symbol,
            timeframe=timeframe,
        )
        if hurst_window < 20:
            raise ValueError("hurst_window must be >= 20")
        if not (0.0 < h_lower < h_upper < 1.0):
            raise ValueError(
                "thresholds must satisfy 0 < h_lower < h_upper < 1"
            )
        if momentum_lookback < 2:
            raise ValueError("momentum_lookback must be >= 2")
        if zscore_window < 2:
            raise ValueError("zscore_window must be >= 2")
        if entry_z >= 0.0:
            raise ValueError("entry_z must be < 0 (oversold cut)")
        if exit_z < entry_z:
            raise ValueError("exit_z must be >= entry_z")
        if not (0.0 < mr_stop_loss_pct < 1.0):
            raise ValueError("mr_stop_loss_pct must be in (0, 1)")

        self.hurst_window = int(hurst_window)
        self.h_upper = float(h_upper)
        self.h_lower = float(h_lower)
        self.momentum_lookback = int(momentum_lookback)
        self.zscore_window = int(zscore_window)
        self.entry_z = float(entry_z)
        self.exit_z = float(exit_z)
        self.mr_stop_loss_pct = float(mr_stop_loss_pct)
        self.notional_capital = float(notional_capital)

        # Per-instance long-only state -- resets on every fresh
        # instantiation. CPCV correctness requires the trial script's
        # strategy_factory to construct a new instance per block so
        # this state resets at every block boundary.
        self._position_open: bool = False
        # Sub-strategy that opened the current position. None when
        # flat. "trend" or "mr" when long.
        self._active_mode: str | None = None

    # -- Internal helpers --------------------------------------------------

    def _rolling_hurst(self, returns: pd.Series) -> float:
        """Hurst on the trailing self.hurst_window returns ending at
        bar t-1 (no look-ahead).

        Returns NaN when fewer than self.hurst_window finite returns
        are available or the R/S estimator degenerates.
        """
        ret = returns.dropna()
        if len(ret) < self.hurst_window:
            return float("nan")
        window = ret.iloc[-self.hurst_window:].to_numpy()
        return _hurst_rs(window)

    def _trend_signal_long(self, log_ret: pd.Series) -> bool:
        """Trend leg: long when trailing momentum_lookback log-return
        sum > 0 (computed on returns ending at bar t-1)."""
        ret = log_ret.dropna()
        if len(ret) < self.momentum_lookback:
            return False
        recent = ret.iloc[-self.momentum_lookback:]
        return float(recent.sum()) > 0.0

    def _mr_z(self, close: pd.Series) -> float:
        """Mean-reversion z-score = (close[t] - mean) / std on the
        trailing zscore_window of close prices ending at bar t-1.
        Returns NaN if not enough samples or std is zero.
        """
        if len(close) < self.zscore_window + 1:
            return float("nan")
        # Window of z-score = closes [t - W, t - 1]; current price
        # tested against that mean/std.
        window = close.iloc[-(self.zscore_window + 1):-1]
        mean = float(window.mean())
        std = float(window.std(ddof=1))
        if std <= 0.0 or not math.isfinite(std):
            return float("nan")
        current = float(close.iloc[-1])
        return (current - mean) / std

    # -- Engine-facing single-bar interface --------------------------------

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        """BaseStrategy contract: emit one Signal for the latest bar.

        Logic:
          1. Compute rolling Hurst on the last hurst_window log
             returns. NaN -> HOLD (warmup).
          2. Classify regime: trend (H > h_upper), mr (H < h_lower),
             neutral (otherwise).
          3. If flat: open a long if the active sub-strategy's entry
             rule fires AND the regime matches that sub-strategy.
             Neutral regime never opens a new position.
          4. If long: close on the active sub-strategy's exit rule.
             Trend leg exits on momentum-flat; MR leg exits on
             z >= exit_z OR a -mr_stop_loss_pct drawdown from entry.
             Regime classification does NOT force-close an open
             position -- the sub-strategy that opened it owns the
             exit decision.
        """
        if df is None or df.empty:
            return self.hold(price=0.0, reason="empty df")
        if "close" not in df.columns:
            return self.hold(price=0.0, reason="missing-close")

        close = df["close"].astype(float)
        price = float(close.iloc[-1])

        log_ret = np.log(close / close.shift(1))

        # 1. Hurst regime classification (using returns up to t-1).
        # SHIFT(1) so the rolling window at bar t consumes only bars
        # strictly before t.
        ret_for_hurst = log_ret.shift(1)
        h = self._rolling_hurst(ret_for_hurst)
        if not math.isfinite(h):
            return self.hold(
                price=price,
                reason=(
                    f"hurst-warmup | n_returns="
                    f"{int(log_ret.dropna().size)} need "
                    f"{self.hurst_window + 1}"
                ),
            )

        if h > self.h_upper:
            regime = "trend"
        elif h < self.h_lower:
            regime = "mr"
        else:
            regime = "neutral"

        # 2. Compute both sub-strategy signals (cheap; needed for exits
        # regardless of current regime).
        ret_for_signal = log_ret.shift(1)
        trend_long = self._trend_signal_long(ret_for_signal)
        z = self._mr_z(close)

        # 3. Position-management branch: handle exits first when long.
        if self._position_open:
            if self._active_mode == "trend":
                # Exit when momentum flips non-positive.
                if not trend_long:
                    self._position_open = False
                    self._active_mode = None
                    return self.sell(
                        price=price,
                        reason=(
                            f"hurst-trend-exit | H={h:.3f} | "
                            f"momentum_flat (mom_lookback="
                            f"{self.momentum_lookback})"
                        ),
                        order_type="market",
                    )
                return self.hold(
                    price=price,
                    reason=(
                        f"hurst-trend-hold | H={h:.3f} | "
                        f"regime={regime} | momentum_long"
                    ),
                )
            # _active_mode == "mr"
            if math.isfinite(z) and z >= self.exit_z:
                self._position_open = False
                self._active_mode = None
                return self.sell(
                    price=price,
                    reason=(
                        f"hurst-mr-exit | H={h:.3f} | z={z:.3f} | "
                        f"reverted-to-mean"
                    ),
                    order_type="market",
                )
            return self.hold(
                price=price,
                reason=(
                    f"hurst-mr-hold | H={h:.3f} | regime={regime} | "
                    f"z={(z if math.isfinite(z) else float('nan')):.3f}"
                ),
            )

        # 4. Flat: regime gate decides whether to open.
        if regime == "trend":
            if trend_long:
                self._position_open = True
                self._active_mode = "trend"
                amount_usdt = float(self.notional_capital)
                return self.buy(
                    price=price,
                    reason=(
                        f"hurst-trend-entry | H={h:.3f} (>"
                        f"{self.h_upper:.2f}) | momentum_lookback="
                        f"{self.momentum_lookback} | "
                        f"amount_usdt={amount_usdt:,.0f}"
                    ),
                    order_type="market",
                    metadata={
                        "amount_usdt": amount_usdt,
                        "hurst": h,
                        "regime": regime,
                        "active_mode": "trend",
                    },
                )
            return self.hold(
                price=price,
                reason=(
                    f"hurst-trend-wait | H={h:.3f} | regime={regime} | "
                    f"momentum_flat"
                ),
            )
        if regime == "mr":
            if math.isfinite(z) and z <= self.entry_z:
                self._position_open = True
                self._active_mode = "mr"
                amount_usdt = float(self.notional_capital)
                stop_loss = price * (1.0 - self.mr_stop_loss_pct)
                return self.buy(
                    price=price,
                    reason=(
                        f"hurst-mr-entry | H={h:.3f} (<"
                        f"{self.h_lower:.2f}) | z={z:.3f} (<="
                        f"{self.entry_z:.2f}) | "
                        f"SL={stop_loss:.4f} | "
                        f"amount_usdt={amount_usdt:,.0f}"
                    ),
                    stop_loss=stop_loss,
                    order_type="market",
                    metadata={
                        "amount_usdt": amount_usdt,
                        "hurst": h,
                        "regime": regime,
                        "active_mode": "mr",
                        "z_score": z,
                    },
                )
            return self.hold(
                price=price,
                reason=(
                    f"hurst-mr-wait | H={h:.3f} | regime={regime} | "
                    f"z={(z if math.isfinite(z) else float('nan')):.3f}"
                ),
            )
        return self.hold(
            price=price,
            reason=(
                f"hurst-neutral | H={h:.3f} | "
                f"random-walk-band [{self.h_lower:.2f}, "
                f"{self.h_upper:.2f}]"
            ),
        )
