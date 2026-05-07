"""strategies/intraday_jump_reversal.py -- IntradayJumpReversal strategy.

Fade large, statistically significant intraday price moves (jumps)
on BTC/USDT 1H.

Hypothesis: a 1H return whose magnitude exceeds a z-score threshold
relative to recent rolling volatility represents a transient
overreaction / liquidity imbalance that mean-reverts.  Long-only
on spot: when a sufficiently large negative 1H return prints, fade
it by going long; hold until either the next opposite jump or a
fixed bar cap.  Sources: Wen et al. (2022) NAJEF, Zaremba et al.
(2021) IRFA, Alexzap (2026) Medium/DEV (BTC-USD JMR backtest).

Algorithm per 1H candle:

  1. Compute the current 1H simple return:

         r_t = (close_t - close_{t-1}) / close_{t-1}

  2. Compute the rolling standard deviation of 1H returns over a
     trailing window (default 24 hours, exclusive of the current
     candle so the threshold is observable before the bar's
     return is realized for sizing-purposes intuition; in practice
     we include the prior n bars):

         sigma_t = std(r_{t-vol_window} ... r_{t-1})

  3. Form the z-score:

         z_t = r_t / sigma_t

  4. Apply the long-only fade rule:

         z_t <= -z_threshold AND not _position_open  -> BUY (fade down jump)
         _position_open AND
           (z_t >= +z_threshold OR
            bars_held >= max_hold_bars)             -> SELL
         otherwise                                  -> HOLD

  5. Long-only by construction: never emit Signal.SELL when
     `_position_open` is False.  Han et al. (2024) document that
     crypto loser-shorts are punished by rebound moves; the same
     long-only-leg precedent applied to sq-013, sq-016, sq-018,
     sq-019, sq-020 carries over.

CPCV note: `_position_open` and `_bars_held` are per-instance state.
The trial script's `make_strategy` factory must create a FRESH
instance for each CPCV block so no boundary state leaks across
blocks.  The default 50-candle engine warmup plus the strategy's
own internal HOLD-on-insufficient-history guard (returns HOLD until
vol_window + 2 candles accumulate) keep entries deterministic
block-to-block.
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal


# CITATION: intraday-jump-reversal-literature
# Alexzap (2026) BTC-USD JMR backtest uses a 24-hour rolling window
# for the volatility reference on 1H candles; this matches the
# "recent rolling volatility" language in Wen et al. (2022) and
# Zaremba et al. (2021) for daily/intraday reversal sorts.
_DEFAULT_VOL_WINDOW: int = 24
# CITATION: intraday-jump-reversal-literature
# Alexzap (2026) and the broader jump-detection literature (e.g.
# Lee & Mykland 2008) use z thresholds in the 2.5-4.0 range; 3.0
# is the canonical "large jump" cutoff and what Alexzap reports
# positive Sharpes on for BTC-USD 2022-2024.
_DEFAULT_Z_THRESHOLD: float = 3.0
# CITATION: intraday-jump-reversal-literature
# Wen et al. (2022) and Shen et al. (2022) report that intraday
# reversals in crypto perpetuals decay within ~24 hours; capping
# the hold at one day prevents the position from drifting beyond
# the documented half-life of the reversal signal.
_DEFAULT_MAX_HOLD_BARS: int = 24


class IntradayJumpReversalStrategy(BaseStrategy):
    """Long-only fade of statistically significant negative 1H jumps.

    Long-only by construction: the long-only-leg precedent that
    applied to sq-013, sq-016, sq-018, sq-019, sq-020 carries over
    here because (a) backtest.engine is structurally long-only on
    spot, and (b) Han et al. (2024) document that crypto
    loser-shorts are punished by rebound moves.

    CPCV note: `_position_open` and `_bars_held` reset to False/0
    on each fresh instantiation.  The strategy_factory in the trial
    script must create a new instance per CPCV block so block-
    boundary state does not leak.
    """

    def __init__(
        self,
        symbol: str = "BTC/USDT",
        timeframe: str = "1h",
        vol_window: int = _DEFAULT_VOL_WINDOW,
        z_threshold: float = _DEFAULT_Z_THRESHOLD,
        max_hold_bars: int = _DEFAULT_MAX_HOLD_BARS,
    ):
        super().__init__(
            name="IntradayJumpReversal",
            symbol=symbol,
            timeframe=timeframe,
        )
        if vol_window < 4:
            raise ValueError("vol_window must be >= 4")
        if z_threshold <= 0:
            raise ValueError("z_threshold must be > 0")
        if max_hold_bars < 1:
            raise ValueError("max_hold_bars must be >= 1")

        self.vol_window = int(vol_window)
        self.z_threshold = float(z_threshold)
        self.max_hold_bars = int(max_hold_bars)

        # Per-instance long-only state.  False/0 on every fresh
        # instance for CPCV block-boundary correctness.
        self._position_open: bool = False
        self._bars_held: int = 0

    def _compute_zscore(self, df: pd.DataFrame) -> Optional[tuple[float, float]]:
        """Return (current_return, z_score) or None if insufficient history.

        The rolling sigma uses the prior `vol_window` 1H returns,
        excluding the current bar's return (no look-ahead: at the
        moment of decision, sigma is computed from already-completed
        bars).
        """
        if len(df) < self.vol_window + 2:
            return None
        close = df["close"].astype(float)
        returns = close.pct_change()
        # Current return = the last completed return (close_t / close_{t-1} - 1)
        r_t = float(returns.iloc[-1])
        if not math.isfinite(r_t):
            return None
        # Sigma over the prior vol_window returns (exclusive of r_t)
        prior_returns = returns.iloc[-(self.vol_window + 1):-1]
        if len(prior_returns) < self.vol_window:
            return None
        sigma = float(prior_returns.std(ddof=0))
        if not math.isfinite(sigma) or sigma <= 0:
            return None
        z = r_t / sigma
        if not math.isfinite(z):
            return None
        return r_t, z

    def generate_signal(self, df: Optional[pd.DataFrame]) -> Signal:
        if df is None or df.empty:
            return self.hold(price=0.0, reason="empty df")

        price = float(df["close"].iloc[-1])

        # Tick the bars-held counter when a position is open.  This
        # increments on every generate_signal call after entry.
        if self._position_open:
            self._bars_held += 1

        zres = self._compute_zscore(df)
        if zres is None:
            return self.hold(
                price=price,
                reason=(
                    f"vol warmup | need vol_window+2={self.vol_window + 2} "
                    f"candles"
                ),
            )
        r_t, z = zres

        # Exit branch.  Long-only guard: SELL only fires when a prior
        # BUY in this instance flipped _position_open to True.
        if self._position_open:
            # Opposite-direction jump: large positive return (mean-
            # reversion completed / overshoot in our favor).
            if z >= self.z_threshold:
                self._position_open = False
                self._bars_held = 0
                return self.sell(
                    price=price,
                    reason=(
                        f"jump-exit-up | z={z:+.3f} >= "
                        f"+{self.z_threshold:.2f} | r={r_t:+.5f}"
                    ),
                    order_type="market",
                )
            # Time stop: cap the hold at max_hold_bars per the
            # documented intraday-reversal half-life.
            if self._bars_held >= self.max_hold_bars:
                self._position_open = False
                self._bars_held = 0
                return self.sell(
                    price=price,
                    reason=(
                        f"jump-exit-time | bars_held={self.max_hold_bars} "
                        f"| z={z:+.3f}"
                    ),
                    order_type="market",
                )
            return self.hold(
                price=price,
                reason=(
                    f"jump-hold | bars_held={self._bars_held} | "
                    f"z={z:+.3f} | r={r_t:+.5f}"
                ),
            )

        # Entry branch.  Fade large negative jumps only (long-only).
        if z <= -self.z_threshold:
            self._position_open = True
            self._bars_held = 0
            return self.buy(
                price=price,
                reason=(
                    f"jump-fade-long | z={z:+.3f} <= "
                    f"-{self.z_threshold:.2f} | r={r_t:+.5f}"
                ),
                order_type="market",
            )

        return self.hold(
            price=price,
            reason=(
                f"no-jump | z={z:+.3f} | r={r_t:+.5f} | "
                f"threshold=+/-{self.z_threshold:.2f}"
            ),
        )
