"""Shared helpers for the Phase 4.E microstructure strategies.

Kept deliberately small: Wilder ATR on the signal-timeframe bars and
array-safe access to the volume-profile node columns (which arrive as a
Python list from a fresh build_signal_frame and as a numpy array from the
parquet cache — both must be handled).
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np
import pandas as pd


# Wilder RMA converges geometrically: the seed's weight after k bars is
# ((period-1)/period)^k, so a bounded tail reproduces the full-history ATR to
# far below signal precision (for period=14, a 250-bar tail leaves the seed at
# ~(13/14)^236 ~= 4e-8).  Bounding the window keeps per-bar ATR O(1) instead
# of O(n) -- without it, the engine's growing-slice loop is O(n^2) and a
# 150k-bar dev window would trip the 4-hour compute circuit breaker.
_ATR_MAX_TAIL: int = 250


def wilder_atr(df: pd.DataFrame, period: int = 14) -> Optional[float]:
    """Latest Wilder ATR value over `period` bars, or None if insufficient.

    True range = max(high-low, |high-prev_close|, |low-prev_close|);
    ATR is the Wilder (RMA / alpha=1/period) moving average of TR.  Computed
    from at most the last `_ATR_MAX_TAIL` bars (see note above) so the value
    is stable and the cost is bounded per call.
    """
    if len(df) < period + 1:
        return None
    if len(df) > _ATR_MAX_TAIL:
        df = df.iloc[-_ATR_MAX_TAIL:]
    high = df["high"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    close = df["close"].to_numpy(dtype=float)
    prev_close = close[:-1]
    tr = np.maximum.reduce([
        high[1:] - low[1:],
        np.abs(high[1:] - prev_close),
        np.abs(low[1:] - prev_close),
    ])
    if tr.size < period:
        return None
    # Wilder RMA: seed with the simple mean of the first `period` TRs,
    # then recursively smooth.
    atr = float(tr[:period].mean())
    for x in tr[period:]:
        atr = (atr * (period - 1) + float(x)) / period
    if not math.isfinite(atr) or atr <= 0:
        return None
    return atr


def node_prices(cell) -> np.ndarray:
    """Coerce a profile node cell (list or ndarray, maybe empty/NaN) to a
    clean 1-D float array of finite prices."""
    if cell is None:
        return np.empty(0, dtype=float)
    arr = np.asarray(cell, dtype=float).ravel()
    if arr.size == 0:
        return arr
    return arr[np.isfinite(arr)]


def nearest_below(prices: np.ndarray, ref: float) -> Optional[float]:
    """Highest node price strictly below `ref`, or None."""
    below = prices[prices < ref]
    return float(below.max()) if below.size else None


def nearest_above(prices: np.ndarray, ref: float) -> Optional[float]:
    """Lowest node price strictly above `ref`, or None."""
    above = prices[prices > ref]
    return float(above.min()) if above.size else None


def feature_row(features: Optional[pd.DataFrame], ts) -> Optional[pd.Series]:
    """Row of a precomputed feature frame at exactly `ts`, or None.

    Used by the constructor-injected-feature strategies (profile / VWAP) to
    look up the feature values aligned to the current signal bar.  Returns
    None when `features` is missing or has no row at `ts` — the strategy
    then holds.  Alignment is by exact timestamp, so a feature frame built
    from a differently-truncated input never silently shifts.
    """
    if features is None or ts not in features.index:
        return None
    return features.loc[ts]
