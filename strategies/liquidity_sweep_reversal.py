"""strategies/liquidity_sweep_reversal.py -- Phase 4.E LiquiditySweepReversal.

Locked Variation #1 mechanical spec: see
research/liquidity-sweep-reversal-literature.md.

A bar sweeps a prior swing low by a shallow margin (a stop-hunt below
resting liquidity) then closes back inside the range on buy-side aggressor
delta -> a failed breakdown; long the reversion.  Long-only: only the
swing-LOW sweep is traded (backtest.engine is long-only spot; the
swing-high mirror is a distinct hypothesis).

Substrate: Binance spot 1m resampled to 15m with per-bar delta
(data.microstructure_features.build_signal_frame).  All rolling windows are
computed from the current df tail (backward-only).
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

from strategies._microstructure_util import wilder_atr
from strategies.base import BaseStrategy, Signal

# Locked Variation #1 parameters.
_SWING_LOOKBACK: int = 20     # L: prior bars for the swing low (excl. t)
_ATR_PERIOD: int = 14
_SWEEP_ATR: float = 0.5       # k: max shallow-sweep depth
_TARGET_ATR: float = 2.0      # m: take-profit distance
_STOP_ATR: float = 0.10       # stop below the sweep low
_TIME_STOP_BARS: int = 16


class LiquiditySweepReversalStrategy(BaseStrategy):
    def __init__(self, symbol: str = "BTCUSDT", timeframe: str = "15m"):
        super().__init__(
            name="LiquiditySweepReversal", symbol=symbol, timeframe=timeframe,
        )
        self._position_open: bool = False
        self._entry_price: Optional[float] = None
        self._atr_entry: Optional[float] = None
        self._sweep_low: Optional[float] = None
        self._bars_since_entry: int = 0

    def generate_signal(self, df: Optional[pd.DataFrame]) -> Signal:
        if df is None or df.empty:
            return self.hold(price=0.0, reason="empty df")
        last = df.iloc[-1]
        price = float(last["close"])

        # ── Exit branch (long-only guard) ────────────────────────────────
        if self._position_open:
            self._bars_since_entry += 1
            target = self._entry_price + _TARGET_ATR * self._atr_entry
            stop = self._sweep_low - _STOP_ATR * self._atr_entry
            if price >= target:
                return self._exit(price, f"target {target:.2f}")
            if price < stop:
                return self._exit(price, f"stop {stop:.2f}")
            if self._bars_since_entry >= _TIME_STOP_BARS:
                return self._exit(price, f"time-stop {_TIME_STOP_BARS}")
            return self.hold(price=price, reason="hold position")

        # ── Entry branch ─────────────────────────────────────────────────
        if len(df) < _SWING_LOOKBACK + 1:
            return self.hold(price=price, reason="swing warmup")
        atr = wilder_atr(df, _ATR_PERIOD)
        if atr is None:
            return self.hold(price=price, reason="atr warmup")

        swing_low = float(df["low"].iloc[-(_SWING_LOOKBACK + 1):-1].min())
        low_t = float(last["low"])
        delta_t = float(last["delta"])

        swept = low_t < swing_low
        shallow = (swing_low - low_t) <= _SWEEP_ATR * atr
        closed_inside = price > swing_low
        if swept and shallow and closed_inside and delta_t > 0:
            self._position_open = True
            self._entry_price = price
            self._atr_entry = atr
            self._sweep_low = low_t
            self._bars_since_entry = 0
            return self.buy(
                price=price,
                reason=(
                    f"sweep-reversal long | low {low_t:.2f}<swing {swing_low:.2f} "
                    f"| depth {swing_low - low_t:.2f}<= {_SWEEP_ATR}*atr"
                ),
                order_type="market",
            )
        return self.hold(price=price, reason="no sweep")

    def _exit(self, price: float, why: str) -> Signal:
        self._position_open = False
        self._entry_price = None
        self._atr_entry = None
        self._sweep_low = None
        return self.sell(price=price, reason=f"exit | {why}", order_type="market")
