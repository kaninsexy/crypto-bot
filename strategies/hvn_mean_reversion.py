"""strategies/hvn_mean_reversion.py -- Phase 4.E HVNMeanReversion.

Locked Variation #1 mechanical spec: see
research/hvn-mean-reversion-literature.md.

A high-volume node (HVN) is a price where large size transacted; positions
are defended there.  Price falling INTO an HVN from above tends to
decelerate and revert up (the node acts as support).  Long-only: only the
buy-at-HVN-support case is traded (backtest.engine is long-only spot).

HVN node list comes from the volume profile built FROM THE 1m DATA
(build_profile_features), precomputed by the trial script and injected via
the `profile_features` constructor arg.  Required column: 'hvn_prices'.
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from strategies._microstructure_util import feature_row, node_prices, wilder_atr
from strategies.base import BaseStrategy, Signal

# Locked Variation #1 parameters.
_ATR_PERIOD: int = 14
_TOUCH_TOL: float = 0.001      # bar low within 0.1% touches the HVN
_TARGET_ATR: float = 1.5       # r: reversion target
_STOP_ATR: float = 0.75        # s: stop below the node (HVN broke)
_TIME_STOP_BARS: int = 24


class HVNMeanReversionStrategy(BaseStrategy):
    def __init__(
        self,
        profile_features: Optional[pd.DataFrame] = None,
        symbol: str = "BTCUSDT",
        timeframe: str = "1h",
    ):
        super().__init__(
            name="HVNMeanReversion", symbol=symbol, timeframe=timeframe,
        )
        self._features = profile_features
        self._position_open: bool = False
        self._atr_entry: Optional[float] = None
        self._hvn_s: Optional[float] = None
        self._bars_since_entry: int = 0

    def generate_signal(self, df: Optional[pd.DataFrame]) -> Signal:
        if df is None or df.empty:
            return self.hold(price=0.0, reason="empty df")
        last = df.iloc[-1]
        price = float(last["close"])

        # ── Exit branch (long-only guard) ────────────────────────────────
        if self._position_open:
            self._bars_since_entry += 1
            target = self._hvn_s + _TARGET_ATR * self._atr_entry
            stop = self._hvn_s - _STOP_ATR * self._atr_entry
            if price >= target:
                return self._exit(price, f"target {target:.2f}")
            if price < stop:
                return self._exit(price, f"stop <hvn {stop:.2f}")
            if self._bars_since_entry >= _TIME_STOP_BARS:
                return self._exit(price, f"time-stop {_TIME_STOP_BARS}")
            return self.hold(price=price, reason="hold position")

        # ── Entry branch ─────────────────────────────────────────────────
        if len(df) < 2:
            return self.hold(price=price, reason="warmup")
        ts = df.index[-1]
        row = feature_row(self._features, ts)
        if row is None:
            return self.hold(price=price, reason="profile warmup")
        atr = wilder_atr(df, _ATR_PERIOD)
        if atr is None:
            return self.hold(price=price, reason="atr warmup")

        hvns = node_prices(row.get("hvn_prices"))
        close_prev = float(df["close"].iloc[-2])
        # Nearest HVN at or below the prior close (support from above).
        at_or_below = hvns[hvns <= close_prev]
        if at_or_below.size == 0:
            return self.hold(price=price, reason="no hvn below")
        hvn_s = float(at_or_below.max())

        low_t = float(last["low"])
        delta_t = float(last["delta"])
        touches = low_t <= hvn_s * (1.0 + _TOUCH_TOL)
        holds_above = price > hvn_s
        if touches and holds_above and delta_t > 0:
            self._position_open = True
            self._atr_entry = atr
            self._hvn_s = hvn_s
            self._bars_since_entry = 0
            return self.buy(
                price=price,
                reason=(
                    f"hvn support long | low {low_t:.2f} touch hvn {hvn_s:.2f} "
                    f"| close {price:.2f}>hvn | delta {delta_t:.1f}>0"
                ),
                order_type="market",
            )
        return self.hold(price=price, reason="no hvn touch")

    def _exit(self, price: float, why: str) -> Signal:
        self._position_open = False
        self._atr_entry = None
        self._hvn_s = None
        return self.sell(price=price, reason=f"exit | {why}", order_type="market")
