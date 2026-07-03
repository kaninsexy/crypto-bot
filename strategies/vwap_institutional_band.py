"""strategies/vwap_institutional_band.py -- Phase 4.E VWAPInstitutionalBand.

Locked Variation #1 mechanical spec: see
research/vwap-institutional-band-literature.md.

Variation #1 tests ONLY the mean-reversion-long side: a stretched move to
the lower 2-sigma session-VWAP band, confirmed by buy-side aggressor delta,
reverts toward VWAP.  Long-only (backtest.engine is long-only spot).  The
continuation-beyond-upper-band side is a distinct hypothesis and is NOT
enabled here.

VWAP + bands are a signal-timeframe running feature (session VWAP resets
00:00 UTC; bands = rolling std of close-vwap over `window` bars).  Per the
NewsSentimentMomentum precedent they are precomputed by the trial script
(data.microstructure.session_vwap / vwap_bands) from the SAME signal frame
the engine iterates, truncated at the same boundary, and injected via the
`vwap_features` constructor arg -- keeping VWAP out of the O(n^2) growing-
slice path.  Required columns: 'vwap', 'lower_2'.
"""

from __future__ import annotations

from typing import Optional

import math
import pandas as pd

from strategies._microstructure_util import feature_row, wilder_atr
from strategies.base import BaseStrategy, Signal

# Locked Variation #1 parameters.
_ATR_PERIOD: int = 14
_STOP_ATR: float = 0.5         # stop below the -2 sigma band
_TIME_STOP_BARS: int = 12


class VWAPInstitutionalBandStrategy(BaseStrategy):
    def __init__(
        self,
        vwap_features: Optional[pd.DataFrame] = None,
        symbol: str = "BTCUSDT",
        timeframe: str = "15m",
    ):
        super().__init__(
            name="VWAPInstitutionalBand", symbol=symbol, timeframe=timeframe,
        )
        # Precomputed VWAP + bands aligned to the signal-bar index (trial
        # script owns the computation; backward-only running VWAP).
        self._features = vwap_features
        self._position_open: bool = False
        self._atr_entry: Optional[float] = None
        self._bars_since_entry: int = 0

    def generate_signal(self, df: Optional[pd.DataFrame]) -> Signal:
        if df is None or df.empty:
            return self.hold(price=0.0, reason="empty df")
        last = df.iloc[-1]
        price = float(last["close"])
        ts = df.index[-1]
        row = feature_row(self._features, ts)
        if row is None or not math.isfinite(float(row.get("vwap", float("nan")))):
            return self.hold(price=price, reason="vwap warmup")
        vwap_t = float(row["vwap"])
        lower2_t = float(row["lower_2"])

        # ── Exit branch (long-only guard) ────────────────────────────────
        if self._position_open:
            self._bars_since_entry += 1
            if not math.isfinite(lower2_t):
                stop = -math.inf
            else:
                stop = lower2_t - _STOP_ATR * self._atr_entry
            if price >= vwap_t:
                return self._exit(price, f"reverted to vwap {vwap_t:.2f}")
            if price < stop:
                return self._exit(price, f"stop {stop:.2f}")
            if self._bars_since_entry >= _TIME_STOP_BARS:
                return self._exit(price, f"time-stop {_TIME_STOP_BARS}")
            return self.hold(price=price, reason="hold position")

        # ── Entry branch ─────────────────────────────────────────────────
        if len(df) < 2 or not math.isfinite(lower2_t):
            return self.hold(price=price, reason="band warmup")
        prev_ts = df.index[-2]
        prev = feature_row(self._features, prev_ts)
        if prev is None or not math.isfinite(float(prev.get("lower_2", float("nan")))):
            return self.hold(price=price, reason="prev band warmup")
        atr = wilder_atr(df, _ATR_PERIOD)
        if atr is None:
            return self.hold(price=price, reason="atr warmup")

        close_prev = float(df["close"].iloc[-2])
        lower2_prev = float(prev["lower_2"])
        delta_t = float(last["delta"])

        fresh_touch = price <= lower2_t and close_prev > lower2_prev
        if fresh_touch and delta_t > 0:
            self._position_open = True
            self._atr_entry = atr
            self._bars_since_entry = 0
            return self.buy(
                price=price,
                reason=(
                    f"vwap lower-band reversion long | close {price:.2f}<=L2 "
                    f"{lower2_t:.2f} | vwap {vwap_t:.2f}"
                ),
                order_type="market",
            )
        return self.hold(price=price, reason="no fresh band touch")

    def _exit(self, price: float, why: str) -> Signal:
        self._position_open = False
        self._atr_entry = None
        return self.sell(price=price, reason=f"exit | {why}", order_type="market")
