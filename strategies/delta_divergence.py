"""strategies/delta_divergence.py -- Phase 4.E DeltaDivergence strategy.

Locked Variation #1 mechanical spec: see
research/delta-divergence-literature.md.

Bullish order-flow divergence, long-only: price makes a new low versus a
prior same-session pivot low, but cumulative taker delta is HIGHER (less
net selling) than at that pivot -- selling is exhausting -- and the bar
closes as a bullish reversal candle.  Long the exhaustion; the bearish
new-high mirror is a distinct hypothesis and is NOT traded (backtest.engine
is long-only spot; peer precedent sq-013/016/018/019/020).

Substrate: Binance spot 1m resampled to 15m, enriched with per-bar delta
and daily-anchored cum_delta (data.microstructure_features).  Gate 7:
cum_delta resets 00:00 UTC daily and divergences are measured WITHIN a
single UTC session, so the pivot low is searched only among same-day prior
bars.

CPCV note: `_position_open` and the frozen entry state reset on every fresh
instantiation; the trial factory must build a new instance per block.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

from strategies._microstructure_util import wilder_atr
from strategies.base import BaseStrategy, Signal

# Locked Variation #1 parameters (research/delta-divergence-literature.md).
_PIVOT_LOOKBACK: int = 20          # P: same-session prior bars to search
_MIN_SESSION_BARS: int = 4         # need this many same-day prior bars
_ATR_PERIOD: int = 14
_TARGET_ATR: float = 2.0           # d: take-profit distance
_STOP_ATR: float = 0.5             # stop below the entry bar's low
_TIME_STOP_BARS: int = 16


class DeltaDivergenceStrategy(BaseStrategy):
    def __init__(self, symbol: str = "BTCUSDT", timeframe: str = "15m"):
        super().__init__(
            name="DeltaDivergence", symbol=symbol, timeframe=timeframe,
        )
        self._position_open: bool = False
        self._entry_price: Optional[float] = None
        self._atr_entry: Optional[float] = None
        self._entry_low: Optional[float] = None
        self._bars_since_entry: int = 0

    def generate_signal(self, df: Optional[pd.DataFrame]) -> Signal:
        if df is None or df.empty:
            return self.hold(price=0.0, reason="empty df")

        last = df.iloc[-1]
        price = float(last["close"])

        # ── Exit branch (long-only guard: only when in position) ──────────
        if self._position_open:
            self._bars_since_entry += 1
            target = self._entry_price + _TARGET_ATR * self._atr_entry
            stop = self._entry_low - _STOP_ATR * self._atr_entry
            if price >= target:
                return self._exit(price, f"target {target:.2f}")
            if price < stop:
                return self._exit(price, f"stop {stop:.2f}")
            if self._bars_since_entry >= _TIME_STOP_BARS:
                return self._exit(price, f"time-stop {_TIME_STOP_BARS}")
            return self.hold(price=price, reason="hold position")

        # ── Entry branch ─────────────────────────────────────────────────
        atr = wilder_atr(df, _ATR_PERIOD)
        if atr is None:
            return self.hold(price=price, reason="atr warmup")

        last_ts = df.index[-1]
        day = last_ts.normalize()
        # Same-day prior bars only (Gate 7). searchsorted on the sorted
        # index is O(log n) vs an O(n) boolean mask per bar.
        day_start = df.index.searchsorted(day)
        prior = df.iloc[day_start:-1]                # same-day, before t
        if len(prior) < _MIN_SESSION_BARS:
            return self.hold(price=price, reason="session warmup")

        window = prior.iloc[-_PIVOT_LOOKBACK:]      # cap at P bars
        piv_i = window["low"].idxmin()
        piv_low = float(window.loc[piv_i, "low"])
        piv_cd = float(window.loc[piv_i, "cum_delta"])

        cum_delta_t = float(last["cum_delta"])
        low_t = float(last["low"])
        open_t = float(last["open"])

        new_low = low_t < piv_low
        bullish_divergence = cum_delta_t > piv_cd
        reversal_candle = price > open_t
        if new_low and bullish_divergence and reversal_candle:
            self._position_open = True
            self._entry_price = price
            self._atr_entry = atr
            self._entry_low = low_t
            self._bars_since_entry = 0
            return self.buy(
                price=price,
                reason=(
                    f"delta-divergence long | new_low {low_t:.2f}<{piv_low:.2f} "
                    f"| cd {cum_delta_t:.1f}>{piv_cd:.1f}"
                ),
                order_type="market",
            )
        return self.hold(price=price, reason="no divergence")

    def _exit(self, price: float, why: str) -> Signal:
        self._position_open = False
        self._entry_price = None
        self._atr_entry = None
        self._entry_low = None
        return self.sell(price=price, reason=f"exit | {why}", order_type="market")
