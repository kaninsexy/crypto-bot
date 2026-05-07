"""strategies/volatility_scaled_tsmom.py -- sq-021 single-symbol strategy.

Volatility-scaled time-series momentum on BTC/USDT 1D. Combines a
classical TSMOM signal (sign of trailing N-day log return) with the
Barroso & Santa-Clara (2015) volatility-scaling overlay so that
position size targets a constant level of annualised risk.

Signal at bar t (no look-ahead):

    momentum_t = sum(log_ret_i)  for i in [t - lookback, t - 1]
    long when momentum_t > 0; flat when momentum_t <= 0

Position size at entry:

    realized_vol_annual = std(log_ret_i for i in [t - vol_window, t - 1])
                          * sqrt(365)
    vol_factor          = min(target_vol_annual / realized_vol_annual,
                              vol_factor_cap)
    amount_usdt         = notional_capital * vol_factor

Long-only by construction (engine_multi has no short-side harness in
this codebase; this strategy uses the same flat-when-negative
convention as DualMomentum and VolumeWeightedTSMOM). Single
concurrent BTC long; daily rebalance.

Citations:
- Grobys, K., Kolari, J. W., Sandretto, D., Shahzad, S. J. H., Aijoe, J.
  (2025). "Cryptocurrency momentum has (not) its moments."
  Financial Markets and Portfolio Management. Volatility-management
  techniques applied to cryptocurrency momentum portfolios
  substantially increase payoffs and produce statistically
  significant alphas by mitigating severe crashes.
- Kumar, M. P., Jenefer, B. M. (2026). "Time-series momentum in
  cryptocurrency markets: a pre and post spot Bitcoin ETF analysis."
  Zenodo. A volatility-scaled TSMOM portfolio generated a Sharpe
  ratio of 0.82 pre-ETF and 1.22 post-ETF, outperforming
  Buy-and-Hold in regime-switching conditions.
- Catalin (2026). "BTC volatility-aligned momentum engine."
  finaur.com. Practitioner framework aligning daily Bitcoin
  momentum signals with volatility regimes -- ATR-based risk
  scaling, defined entry/exit conditions.
- Barroso, P., Santa-Clara, P. (2015). "Momentum has its moments."
  Journal of Financial Economics. Original equity-market formulation
  of constant-volatility-target scaling that the crypto adaptations
  above import.

Output is ASCII-only.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal


class VolatilityScaledTSMOMStrategy(BaseStrategy):
    """BTC/USDT 1D long-only volatility-scaled time-series momentum."""

    def __init__(
        self,
        symbol: str = "BTC/USDT",
        timeframe: str = "1d",
        # CITATION: volatility-scaled-tsmom-literature
        # Kumar & Jenefer (2026) and Li & Zhang (2023) ICBPS report
        # short-horizon daily TSMOM lookbacks (10-30 bars) as the
        # productive band for cryptocurrency 1D. 30 sits at the upper
        # edge of that band and matches the "past N-month return"
        # convention discussed in Grobys et al. (2025).
        momentum_lookback: int = 30,
        # CITATION: volatility-scaled-tsmom-literature
        # Barroso & Santa-Clara (2015) use a 6-month rolling realized
        # vol on monthly returns; the equivalent on daily crypto data
        # cited by Grobys et al. (2025) and the implementation_notes
        # ("standard deviation of daily returns over the past 30-60
        # days") use 30-60 daily returns. 30 is the lower bound of
        # that range and aligns with the momentum_lookback so the two
        # rolling windows reach validity at the same bar.
        vol_window: int = 30,
        # CITATION: volatility-scaled-tsmom-literature
        # Barroso & Santa-Clara (2015) target 12% annualised vol;
        # Grobys et al. (2025) and Kumar & Jenefer (2026) replicate
        # the construction with 12-15% annualised. 15% is the
        # conventional crypto-equity portfolio default and matches
        # the Supertrend Phase 4.A daily-resurrection variation.
        target_vol_annual: float = 0.15,
        # CITATION: volatility-scaled-tsmom-literature
        # Spot BTC has no leverage in this codebase; the
        # implementation_notes are explicit about constant-risk
        # scaling and Barroso-Santa-Clara's original construction
        # caps at 1.5x for equities. On spot crypto with no margin
        # the cap collapses to 1.0 (no leverage). Held-flat as a
        # locked pre-trial gate.
        vol_factor_cap: float = 1.0,
        # CITATION: volatility-scaled-tsmom-literature
        # Engine default initial_balance for the Phase 4 backtest
        # harness; the simulator clamps amount_usdt to available
        # balance so this is the reference notional only.
        notional_capital: float = 10_000.0,
    ):
        super().__init__(
            name="VolatilityScaledTSMOM",
            symbol=symbol,
            timeframe=timeframe,
        )
        if momentum_lookback < 2:
            raise ValueError("momentum_lookback must be >= 2")
        if vol_window < 2:
            raise ValueError("vol_window must be >= 2")
        if target_vol_annual <= 0.0:
            raise ValueError("target_vol_annual must be > 0")
        if vol_factor_cap <= 0.0:
            raise ValueError("vol_factor_cap must be > 0")

        self.momentum_lookback = int(momentum_lookback)
        self.vol_window = int(vol_window)
        self.target_vol_annual = float(target_vol_annual)
        self.vol_factor_cap = float(vol_factor_cap)
        self.notional_capital = float(notional_capital)

        # Per-instance long-only state -- resets to False on every
        # fresh instantiation. CPCV correctness requires the trial
        # script's strategy_factory to construct a new instance per
        # block so this state resets at every block boundary.
        self._position_open: bool = False

    # -- Vector signal helper ----------------------------------------------

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        """Return a pd.Series of position sizes per bar (0.0 or 1.0).

        The signal at bar t is the sign of the trailing
        `momentum_lookback` log-return sum, computed using ONLY bars
        strictly before t (no look-ahead). Position size is 1.0 when
        the trailing sum is strictly positive, 0.0 otherwise
        (long-only flat-when-negative).
        """
        if df is None or df.empty:
            return pd.Series(dtype=float)
        if "close" not in df.columns:
            raise ValueError("VolatilityScaledTSMOM requires 'close' column")

        close = df["close"].astype(float)
        log_ret = np.log(close / close.shift(1))

        # Rolling sum of trailing momentum_lookback log returns ending
        # at the current bar; SHIFT(1) so the signal at bar t consumes
        # only bars [t - momentum_lookback, t - 1].
        momentum = log_ret.rolling(
            window=self.momentum_lookback,
            min_periods=self.momentum_lookback,
        ).sum().shift(1)

        positions = (momentum > 0).astype(float).where(
            momentum.notna(), other=0.0,
        )
        return positions.fillna(0.0)

    # -- Engine-facing single-bar interface --------------------------------

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        """BaseStrategy contract: emit one Signal for the latest bar.

        BUY when the latest position-size flips from 0 -> 1 with no
        position open (size scaled by Barroso-Santa-Clara vol factor),
        SELL when the latest position-size is 0 with a position open,
        HOLD otherwise. State (`_position_open`) is mutated in
        lockstep.
        """
        if df is None or df.empty:
            return self.hold(price=0.0, reason="empty df")
        price = float(df["close"].iloc[-1])

        positions = self.generate_signals(df)
        if positions.empty:
            return self.hold(price=price, reason="empty positions")
        latest_pos = float(positions.iloc[-1])
        if not math.isfinite(latest_pos):
            return self.hold(price=price, reason="non-finite-position")

        # Compute realized vol over the trailing vol_window of daily
        # log returns ending at bar t-1 (no look-ahead).
        if latest_pos > 0 and not self._position_open:
            close = df["close"].astype(float)
            log_ret = np.log(close / close.shift(1)).dropna()
            if len(log_ret) < self.vol_window + 1:
                return self.hold(
                    price=price,
                    reason=(
                        f"vs-tsmom-warmup | "
                        f"n_returns={len(log_ret)} need "
                        f"{self.vol_window + 1}"
                    ),
                )
            recent = log_ret.iloc[-(self.vol_window + 1):-1]
            realized_vol_daily = float(recent.std(ddof=1))
            realized_vol_annual = realized_vol_daily * float(np.sqrt(365.0))

            if (realized_vol_annual < 1e-8 or
                    not np.isfinite(realized_vol_annual)):
                vol_factor = self.vol_factor_cap
            else:
                vol_factor = min(
                    self.target_vol_annual / realized_vol_annual,
                    self.vol_factor_cap,
                )

            amount_usdt = float(self.notional_capital * vol_factor)
            self._position_open = True
            return self.buy(
                price=price,
                reason=(
                    f"vs-tsmom-long | mom_lookback="
                    f"{self.momentum_lookback} | vol_window="
                    f"{self.vol_window} | "
                    f"realized_vol_annual={realized_vol_annual:.4f} | "
                    f"vol_factor={vol_factor:.3f} | "
                    f"amount_usdt={amount_usdt:,.0f}"
                ),
                order_type="market",
                metadata={
                    "amount_usdt": amount_usdt,
                    "vol_factor": vol_factor,
                    "realized_vol_annual": realized_vol_annual,
                    "target_vol_annual": self.target_vol_annual,
                    "momentum_lookback": self.momentum_lookback,
                    "vol_window": self.vol_window,
                },
            )
        if latest_pos <= 0 and self._position_open:
            self._position_open = False
            return self.sell(
                price=price,
                reason=(
                    f"vs-tsmom-flat | mom_lookback="
                    f"{self.momentum_lookback}"
                ),
                order_type="market",
            )
        return self.hold(
            price=price,
            reason=(
                f"vs-tsmom-mid | latest_pos={latest_pos:.1f} | "
                f"position_open={self._position_open}"
            ),
        )
