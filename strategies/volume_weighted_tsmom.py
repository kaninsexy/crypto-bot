"""strategies/volume_weighted_tsmom.py -- sq-012 single-symbol strategy.

Volume-weighted time-series momentum on BTC/USDT 1D. The TSMOM signal
is the volume-weighted average of trailing daily log-returns:

    signal_t = sum(log_ret_i * volume_i) / sum(volume_i)
             for i in [t - lookback, t - 1]

Long when signal > 0; flat when signal <= 0. Long-only by construction
(engine_multi is long-only without a dedicated short-side harness;
this strategy uses the same flat-when-negative convention as
DualMomentum). Single concurrent BTC long held by the `_position_open`
state attribute.

No look-ahead bias: the rolling window at bar t consumes only bars
[t - lookback, t - 1]; the bar t close is NOT included in its own
signal computation.

Citations:
- Huang, Z.-C., Sangiorgi, I., Urquhart, A. (2024). "Volume-weighted
  time-series momentum." SSRN. Reports a winner-minus-loser TSMOM
  portfolio achieving 0.94%/day with annualized Sharpe 2.17,
  outperforming simple TSMOM benchmarks. Provides the volume-
  weighting formulation used here.
- Shen, D., Urquhart, A., Wang, P. (2022). "High-volume periods and
  intraday return predictability in cryptocurrency markets."
  Financial Review. High-volume regime defines the trading day; the
  first-half-hour return positively predicts the last-half-hour
  return; trading the effect yields significant economic gains.
- Li, X., Zhang, X. (2023). "Time-series momentum on cryptocurrency
  daily returns." Proceedings of the 2nd International Conference
  on Business and Policy Studies (Springer). A daily TSMOM strategy
  with short lookback horizons generates significant positive
  profits and higher Sharpe ratios than passive holding even with
  0.1% transaction costs.

Output is ASCII-only.
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal


class VolumeWeightedTSMOMStrategy(BaseStrategy):
    """BTC/USDT 1D long-only volume-weighted time-series momentum."""

    def __init__(
        self,
        symbol: str = "BTC/USDT",
        timeframe: str = "1d",
        # CITATION: volume-weighted-tsmom-literature
        # Li & Zhang (2023) ICBPS use short-horizon daily TSMOM
        # lookbacks (10-30 bars) on cryptocurrency 1D. 20 sits in
        # the middle of that band and matches Huang/Sangiorgi/
        # Urquhart (2024) SSRN volume-weighted formulation
        # benchmarks.
        lookback_window: int = 20,
        # CITATION: volume-weighted-tsmom-literature
        # Engine default initial_balance for the Phase 4 backtest harness.
        notional_capital: float = 10_000.0,
    ):
        super().__init__(
            name="VolumeWeightedTSMOM",
            symbol=symbol,
            timeframe=timeframe,
        )
        if lookback_window < 2:
            raise ValueError("lookback_window must be >= 2")

        self.lookback_window = int(lookback_window)
        self.notional_capital = float(notional_capital)

        # Per-instance long-only state -- resets to False on every
        # fresh instantiation. CPCV correctness requires the trial
        # script's strategy_factory to construct a new instance per
        # block so this state resets at every block boundary.
        self._position_open: bool = False

    # ── Vector signal helper ─────────────────────────────────────────────────

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        """Return a pd.Series of position sizes per bar.

        The signal at bar t is the volume-weighted average of the
        trailing `lookback_window` log-returns, computed using ONLY
        bars strictly before t (no look-ahead). Position size is
        +1.0 when the signal is strictly positive, 0.0 otherwise
        (long-only flat-when-negative). The first lookback_window+1
        bars cannot have a valid signal -- they are 0.0.

        df must carry close + volume columns (full OHLCV is fine).
        Returned Series is indexed identically to df.
        """
        if df is None or df.empty:
            return pd.Series(dtype=float)
        if "close" not in df.columns or "volume" not in df.columns:
            raise ValueError(
                "VolumeWeightedTSMOM requires 'close' and 'volume' columns"
            )

        close = df["close"].astype(float)
        volume = df["volume"].astype(float)
        # Log returns: log(close_t / close_{t-1}); first row is NaN.
        log_ret = np.log(close / close.shift(1))

        # Rolling sums use the trailing lookback_window bars ending at
        # the current bar. We then SHIFT(1) so the signal at bar t
        # consumes only bars [t - lookback, t - 1] -- never includes
        # the bar t close in its own decision.
        weighted_sum = (log_ret * volume).rolling(
            window=self.lookback_window, min_periods=self.lookback_window,
        ).sum()
        volume_sum = volume.rolling(
            window=self.lookback_window, min_periods=self.lookback_window,
        ).sum()
        # Guard against zero-volume windows: NaN out the ratio.
        signal = (weighted_sum / volume_sum).where(volume_sum > 0)
        signal = signal.shift(1)  # no look-ahead

        positions = (signal > 0).astype(float).where(
            signal.notna(), other=0.0,
        )
        return positions.fillna(0.0)

    # ── Engine-facing single-bar interface ───────────────────────────────────

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        """BaseStrategy contract: emit one Signal for the latest bar.

        Wraps the vector `generate_signals` output: BUY when the latest
        position-size flips from 0 -> 1 with no position open, SELL
        when the latest position-size is 0 with a position open, HOLD
        otherwise. State (`_position_open`) is mutated in lockstep.
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

        if latest_pos > 0 and not self._position_open:
            self._position_open = True
            return self.buy(
                price=price,
                reason=(
                    f"vw-tsmom-long | lookback={self.lookback_window}"
                ),
                order_type="market",
            )
        if latest_pos <= 0 and self._position_open:
            self._position_open = False
            return self.sell(
                price=price,
                reason=(
                    f"vw-tsmom-flat | lookback={self.lookback_window}"
                ),
                order_type="market",
            )
        return self.hold(
            price=price,
            reason=(
                f"vw-tsmom-mid | latest_pos={latest_pos:.1f} | "
                f"position_open={self._position_open}"
            ),
        )
