"""strategies/volume_profile_acceptance.py -- Phase 4.E VolumeProfileAcceptance.

Locked Variation #1 mechanical spec: see
research/volume-profile-acceptance-literature.md.

Price accepted above a prior value area -- two consecutive signal-bar closes
above the prior N-day value-area high (VAH) on above-median buy-side delta
-- exhibits initiative buying and continues up.  Rejection back into value
is the invalidation.  Long-only (backtest.engine is long-only spot).

VAH comes from the volume profile built FROM THE 1m DATA
(data.microstructure_features.build_profile_features); the trial script
precomputes it, truncated at the same boundary as the price frame, and
injects it via the `profile_features` constructor arg (NewsSentimentMomentum
precedent).  Required column: 'vah'.  Delta comes from the signal frame.
"""

from __future__ import annotations

import math
from typing import Optional

import pandas as pd

from strategies._microstructure_util import feature_row
from strategies.base import BaseStrategy, Signal

# Locked Variation #1 parameters.
_DELTA_MEDIAN_LOOKBACK: int = 30    # bars for the per-bar delta median
_TIME_STOP_BARS: int = 24


class VolumeProfileAcceptanceStrategy(BaseStrategy):
    def __init__(
        self,
        profile_features: Optional[pd.DataFrame] = None,
        symbol: str = "BTCUSDT",
        timeframe: str = "1h",
    ):
        super().__init__(
            name="VolumeProfileAcceptance", symbol=symbol, timeframe=timeframe,
        )
        self._features = profile_features
        self._position_open: bool = False
        self._bars_since_entry: int = 0

    def generate_signal(self, df: Optional[pd.DataFrame]) -> Signal:
        if df is None or df.empty:
            return self.hold(price=0.0, reason="empty df")
        last = df.iloc[-1]
        price = float(last["close"])
        ts = df.index[-1]
        row = feature_row(self._features, ts)
        vah_t = float(row["vah"]) if row is not None else float("nan")

        # ── Exit branch (long-only guard) ────────────────────────────────
        if self._position_open:
            self._bars_since_entry += 1
            # Acceptance failed: close back below the (live) VAH.
            if math.isfinite(vah_t) and price < vah_t:
                return self._exit(price, f"reject <vah {vah_t:.2f}")
            if self._bars_since_entry >= _TIME_STOP_BARS:
                return self._exit(price, f"time-stop {_TIME_STOP_BARS}")
            return self.hold(price=price, reason="hold position")

        # ── Entry branch ─────────────────────────────────────────────────
        if len(df) < _DELTA_MEDIAN_LOOKBACK + 1:
            return self.hold(price=price, reason="delta warmup")
        if not math.isfinite(vah_t):
            return self.hold(price=price, reason="vah warmup")
        prev_ts = df.index[-2]
        prev = feature_row(self._features, prev_ts)
        if prev is None or not math.isfinite(float(prev.get("vah", float("nan")))):
            return self.hold(price=price, reason="prev vah warmup")
        vah_prev = float(prev["vah"])

        close_prev = float(df["close"].iloc[-2])
        delta_t = float(last["delta"])
        median_delta = float(
            df["delta"].iloc[-(_DELTA_MEDIAN_LOOKBACK + 1):-1].median()
        )

        two_closes_above = price > vah_t and close_prev > vah_prev
        if two_closes_above and delta_t > median_delta and delta_t > 0:
            self._position_open = True
            self._bars_since_entry = 0
            return self.buy(
                price=price,
                reason=(
                    f"value-area acceptance long | close {price:.2f}>vah "
                    f"{vah_t:.2f} (2 bars) | delta {delta_t:.1f}>med "
                    f"{median_delta:.1f}"
                ),
                order_type="market",
            )
        return self.hold(price=price, reason="no acceptance")

    def _exit(self, price: float, why: str) -> Signal:
        self._position_open = False
        return self.sell(price=price, reason=f"exit | {why}", order_type="market")
