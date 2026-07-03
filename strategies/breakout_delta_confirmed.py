"""strategies/breakout_delta_confirmed.py -- Phase 4.E BreakoutDeltaConfirmed.

Locked Variation #1 mechanical spec: see
research/breakout-delta-confirmed-literature.md.

A range breakout is traded ONLY when it is accepted: the breakout bar
closes beyond the prior range high AND shows top-quartile buy-side aggressor
delta (genuine initiative, not a thin-liquidity wick).  The delta-gated
redesign of the retired unfiltered Breakout.  Long-only upside breakouts.

Substrate: Binance spot 1m resampled to 1h with per-bar delta.  All rolling
windows computed from the current df tail (backward-only).
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from strategies._microstructure_util import wilder_atr
from strategies.base import BaseStrategy, Signal

# Locked Variation #1 parameters.
_RANGE_LOOKBACK: int = 24      # R: prior bars for the range high (excl. t)
_DELTA_WINDOW: int = 100       # W: prior bars for the delta 75th percentile
_ATR_PERIOD: int = 14
_TARGET_ATR: float = 3.0       # b: take-profit distance
_TRAIL_ATR: float = 2.0        # a: chandelier trail distance
_TIME_STOP_BARS: int = 48


class BreakoutDeltaConfirmedStrategy(BaseStrategy):
    def __init__(self, symbol: str = "BTCUSDT", timeframe: str = "1h"):
        super().__init__(
            name="BreakoutDeltaConfirmed", symbol=symbol, timeframe=timeframe,
        )
        self._position_open: bool = False
        self._entry_price: Optional[float] = None
        self._atr_entry: Optional[float] = None
        self._range_high: Optional[float] = None
        self._max_close: Optional[float] = None
        self._bars_since_entry: int = 0

    def generate_signal(self, df: Optional[pd.DataFrame]) -> Signal:
        if df is None or df.empty:
            return self.hold(price=0.0, reason="empty df")
        last = df.iloc[-1]
        price = float(last["close"])

        # ── Exit branch (long-only guard) ────────────────────────────────
        if self._position_open:
            self._bars_since_entry += 1
            if price > self._max_close:
                self._max_close = price
            target = self._entry_price + _TARGET_ATR * self._atr_entry
            trail = self._max_close - _TRAIL_ATR * self._atr_entry
            if price < self._range_high:
                return self._exit(price, f"fakeout <range {self._range_high:.2f}")
            if price >= target:
                return self._exit(price, f"target {target:.2f}")
            if price < trail:
                return self._exit(price, f"trail {trail:.2f}")
            if self._bars_since_entry >= _TIME_STOP_BARS:
                return self._exit(price, f"time-stop {_TIME_STOP_BARS}")
            return self.hold(price=price, reason="hold position")

        # ── Entry branch ─────────────────────────────────────────────────
        if len(df) < _DELTA_WINDOW + 1:
            return self.hold(price=price, reason="window warmup")
        atr = wilder_atr(df, _ATR_PERIOD)
        if atr is None:
            return self.hold(price=price, reason="atr warmup")

        range_high = float(df["high"].iloc[-(_RANGE_LOOKBACK + 1):-1].max())
        prior_delta = df["delta"].iloc[-(_DELTA_WINDOW + 1):-1].to_numpy(dtype=float)
        delta_q75 = float(np.percentile(prior_delta, 75))
        delta_t = float(last["delta"])

        if price > range_high and delta_t >= delta_q75 and delta_t > 0:
            self._position_open = True
            self._entry_price = price
            self._atr_entry = atr
            self._range_high = range_high
            self._max_close = price
            self._bars_since_entry = 0
            return self.buy(
                price=price,
                reason=(
                    f"delta-confirmed breakout | close {price:.2f}>range "
                    f"{range_high:.2f} | delta {delta_t:.1f}>=q75 {delta_q75:.1f}"
                ),
                order_type="market",
            )
        return self.hold(price=price, reason="no breakout")

    def _exit(self, price: float, why: str) -> Signal:
        self._position_open = False
        self._entry_price = None
        self._atr_entry = None
        self._range_high = None
        self._max_close = None
        return self.sell(price=price, reason=f"exit | {why}", order_type="market")
