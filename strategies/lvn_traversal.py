"""strategies/lvn_traversal.py -- Phase 4.E LVNTraversal.

Locked Variation #1 mechanical spec: see
research/lvn-traversal-literature.md.

A low-volume node (LVN) is a thin price zone between two high-volume nodes
(HVNs): little size traded there, so price traverses it fast rather than
settling.  When price enters an LVN from below on positive aggressor delta,
it tends to travel to the next HVN above.  Long-only (backtest.engine is
long-only spot); only upward traversal is traded.

HVN/LVN node lists come from the volume profile built FROM THE 1m DATA
(build_profile_features), precomputed by the trial script and injected via
the `profile_features` constructor arg.  Required columns: 'hvn_prices',
'lvn_prices'.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

from strategies._microstructure_util import (
    feature_row,
    nearest_above,
    nearest_below,
    node_prices,
    wilder_atr,
)
from strategies.base import BaseStrategy, Signal

# Locked Variation #1 parameters.
_ATR_PERIOD: int = 14
_STOP_ATR: float = 0.25        # rejected back below the LVN
_TIME_STOP_BARS: int = 8       # thin zones traverse fast


class LVNTraversalStrategy(BaseStrategy):
    def __init__(
        self,
        profile_features: Optional[pd.DataFrame] = None,
        symbol: str = "BTCUSDT",
        timeframe: str = "15m",
    ):
        super().__init__(
            name="LVNTraversal", symbol=symbol, timeframe=timeframe,
        )
        self._features = profile_features
        self._position_open: bool = False
        self._atr_entry: Optional[float] = None
        self._lvn_p: Optional[float] = None
        self._hvn_up: Optional[float] = None
        self._bars_since_entry: int = 0

    def generate_signal(self, df: Optional[pd.DataFrame]) -> Signal:
        if df is None or df.empty:
            return self.hold(price=0.0, reason="empty df")
        last = df.iloc[-1]
        price = float(last["close"])

        # ── Exit branch (long-only guard) ────────────────────────────────
        if self._position_open:
            self._bars_since_entry += 1
            stop = self._lvn_p - _STOP_ATR * self._atr_entry
            if price >= self._hvn_up:
                return self._exit(price, f"reached hvn {self._hvn_up:.2f}")
            if price < stop:
                return self._exit(price, f"rejected <lvn-stop {stop:.2f}")
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
        lvns = node_prices(row.get("lvn_prices"))
        close_prev = float(df["close"].iloc[-2])

        lvn_p = nearest_above(lvns, close_prev)          # target LVN above
        if lvn_p is None:
            return self.hold(price=price, reason="no lvn above")
        hvn_up = nearest_above(hvns, lvn_p)              # acceptance target
        hvn_dn = nearest_below(hvns, lvn_p)              # flanking HVN below
        if hvn_up is None or hvn_dn is None:
            return self.hold(price=price, reason="lvn not flanked by hvns")

        delta_t = float(last["delta"])
        enters_lvn = close_prev < lvn_p and price >= lvn_p
        below_target = price < hvn_up
        if enters_lvn and below_target and delta_t > 0:
            self._position_open = True
            self._atr_entry = atr
            self._lvn_p = lvn_p
            self._hvn_up = hvn_up
            self._bars_since_entry = 0
            return self.buy(
                price=price,
                reason=(
                    f"lvn traversal long | enter lvn {lvn_p:.2f} -> hvn "
                    f"{hvn_up:.2f} | delta {delta_t:.1f}>0"
                ),
                order_type="market",
            )
        return self.hold(price=price, reason="no lvn entry")

    def _exit(self, price: float, why: str) -> Signal:
        self._position_open = False
        self._atr_entry = None
        self._lvn_p = None
        self._hvn_up = None
        return self.sell(price=price, reason=f"exit | {why}", order_type="market")
