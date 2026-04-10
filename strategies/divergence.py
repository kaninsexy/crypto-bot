"""
strategies/divergence.py — RSI Divergence Detector

WHAT IS DIVERGENCE?
───────────────────
Divergence occurs when price and a momentum indicator (RSI) move in
opposite directions.  This signals that the trend is losing energy and
a reversal is approaching — before price actually reverses.

  BULLISH DIVERGENCE (BUY signal):
    Price makes a LOWER low     (still falling)
    RSI  makes a HIGHER low     (momentum improving)
    → Bears are losing strength. A bounce/reversal is likely.

  BEARISH DIVERGENCE (SELL / exit signal):
    Price makes a HIGHER high   (still rising)
    RSI  makes a LOWER high     (momentum weakening)
    → Bulls are losing strength. A pullback/reversal is likely.

WHY IT WORKS
────────────
RSI is a momentum oscillator.  When price revisits a previous extreme
but RSI can't match that extreme, it means fewer participants are
driving the move — a classic sign of exhaustion.

ALGORITHM
─────────
1. Compute RSI over the last `lookback` candles.
2. Find "pivot lows" in the close price (local minima).
3. Find the two most recent pivot lows within the lookback window.
4. Compare price direction vs RSI direction across those two pivots.
5. If price went DOWN but RSI went UP → bullish divergence.

The same logic applies in reverse for bearish divergence using pivot highs.

USAGE
─────
    from strategies.divergence import bullish_divergence, bearish_divergence

    # Returns True if bullish divergence detected in last 50 candles:
    if bullish_divergence(df, rsi_period=14, lookback=50, pivot_window=3):
        # High-quality DCA or MeanReversion entry

    # Returns a 0.0–1.0 strength score instead of just True/False:
    score = divergence_score(df, mode='bullish')
    # score > 0.6 = strong divergence

INTEGRATION
───────────
  DCA:           Use as optional base-order gate. Only open new cycle when
                 bullish divergence detected (avoids entering ongoing downtrends).

  MeanReversion: Use as entry confirmation. BB lower + StochRSI oversold +
                 bullish divergence = triple-confirmed entry.

  BearShort:     Use bearish divergence as additional entry filter.
"""

import numpy as np
import pandas as pd
import ta
from loguru import logger


# ── Pivot detection ──────────────────────────────────────────────────────────

def find_pivot_lows(
    series: pd.Series,
    window: int = 3,
) -> pd.Series:
    """
    Return a boolean Series that is True at each pivot low.

    A pivot low is a bar whose value is lower than the `window` bars
    on each side of it.  window=3 is standard for short-term pivots.

    Args:
        series: Price or indicator series (e.g. close, RSI).
        window: Number of bars on each side to compare.

    Returns:
        Boolean Series aligned with `series`.
    """
    is_pivot = pd.Series(False, index=series.index)
    arr = series.values
    n = len(arr)
    for i in range(window, n - window):
        left  = arr[i - window: i]
        right = arr[i + 1: i + window + 1]
        if arr[i] <= left.min() and arr[i] <= right.min():
            is_pivot.iloc[i] = True
    return is_pivot


def find_pivot_highs(
    series: pd.Series,
    window: int = 3,
) -> pd.Series:
    """
    Return a boolean Series that is True at each pivot high.

    A pivot high is a bar whose value is higher than the `window` bars
    on each side of it.
    """
    is_pivot = pd.Series(False, index=series.index)
    arr = series.values
    n = len(arr)
    for i in range(window, n - window):
        left  = arr[i - window: i]
        right = arr[i + 1: i + window + 1]
        if arr[i] >= left.max() and arr[i] >= right.max():
            is_pivot.iloc[i] = True
    return is_pivot


# ── Divergence detection ─────────────────────────────────────────────────────

def bullish_divergence(
    df: pd.DataFrame,
    rsi_period: int = 14,
    lookback: int = 50,
    pivot_window: int = 3,
    min_price_drop: float = 0.005,  # Price second low must be ≥ 0.5% lower
    min_rsi_rise: float = 1.0,      # RSI second low must be ≥ 1 point higher
) -> bool:
    """
    Detect regular bullish divergence over the last `lookback` candles.

    Conditions:
      1. Find the two most recent pivot lows in price.
      2. Second low is LOWER than first low (price still falling).
      3. RSI at second low is HIGHER than RSI at first low (momentum improving).
      4. The magnitude thresholds prevent noise from triggering false positives.

    Args:
        df:              OHLCV DataFrame with "close" column.
        rsi_period:      RSI lookback (default 14).
        lookback:        How many recent candles to scan for pivots.
        pivot_window:    How many bars on each side define a pivot (default 3).
        min_price_drop:  Minimum fractional price drop between lows (e.g. 0.005 = 0.5%).
        min_rsi_rise:    Minimum RSI points rise between lows (e.g. 1.0 points).

    Returns:
        True if bullish divergence detected, False otherwise.
    """
    needed = rsi_period + lookback + pivot_window * 2 + 5
    if len(df) < needed:
        return False

    close = df["close"].copy()
    rsi_series = ta.momentum.RSIIndicator(close, window=rsi_period).rsi()

    # Work on the last `lookback` candles only
    close_recent = close.iloc[-lookback:]
    rsi_recent   = rsi_series.iloc[-lookback:]

    # Find pivot lows in price
    price_pivots = find_pivot_lows(close_recent, window=pivot_window)
    pivot_indices = close_recent[price_pivots].index.tolist()

    if len(pivot_indices) < 2:
        return False  # Not enough pivot lows to compare

    # Take the two most recent pivot lows
    p1_idx = pivot_indices[-2]
    p2_idx = pivot_indices[-1]

    price_p1 = float(close_recent[p1_idx])
    price_p2 = float(close_recent[p2_idx])
    rsi_p1   = float(rsi_recent[p1_idx])
    rsi_p2   = float(rsi_recent[p2_idx])

    # Skip if RSI is NaN at pivot points (insufficient warmup data)
    if pd.isna(rsi_p1) or pd.isna(rsi_p2):
        return False

    # Condition 1: Price made a lower low
    price_dropped = (price_p1 - price_p2) / price_p1 >= min_price_drop

    # Condition 2: RSI made a higher low (momentum improving)
    rsi_rose = (rsi_p2 - rsi_p1) >= min_rsi_rise

    divergence = price_dropped and rsi_rose

    if divergence:
        logger.debug(
            f"[Divergence] BULLISH: price {price_p1:.4f}→{price_p2:.4f} "
            f"(drop {(price_p1-price_p2)/price_p1*100:.2f}%) | "
            f"RSI {rsi_p1:.1f}→{rsi_p2:.1f} (+{rsi_p2-rsi_p1:.1f})"
        )

    return divergence


def bearish_divergence(
    df: pd.DataFrame,
    rsi_period: int = 14,
    lookback: int = 50,
    pivot_window: int = 3,
    min_price_rise: float = 0.005,  # Price second high must be ≥ 0.5% higher
    min_rsi_drop: float = 1.0,      # RSI second high must be ≥ 1 point lower
) -> bool:
    """
    Detect regular bearish divergence over the last `lookback` candles.

    Conditions:
      1. Find the two most recent pivot highs in price.
      2. Second high is HIGHER than first high (price still rising).
      3. RSI at second high is LOWER than RSI at first high (momentum fading).

    Args:
        df:              OHLCV DataFrame with "close" column.
        rsi_period:      RSI lookback (default 14).
        lookback:        How many recent candles to scan for pivots.
        pivot_window:    How many bars on each side define a pivot (default 3).
        min_price_rise:  Minimum fractional price rise between highs.
        min_rsi_drop:    Minimum RSI points drop between highs.

    Returns:
        True if bearish divergence detected, False otherwise.
    """
    needed = rsi_period + lookback + pivot_window * 2 + 5
    if len(df) < needed:
        return False

    close = df["close"].copy()
    rsi_series = ta.momentum.RSIIndicator(close, window=rsi_period).rsi()

    close_recent = close.iloc[-lookback:]
    rsi_recent   = rsi_series.iloc[-lookback:]

    price_pivots = find_pivot_highs(close_recent, window=pivot_window)
    pivot_indices = close_recent[price_pivots].index.tolist()

    if len(pivot_indices) < 2:
        return False

    p1_idx = pivot_indices[-2]
    p2_idx = pivot_indices[-1]

    price_p1 = float(close_recent[p1_idx])
    price_p2 = float(close_recent[p2_idx])
    rsi_p1   = float(rsi_recent[p1_idx])
    rsi_p2   = float(rsi_recent[p2_idx])

    if pd.isna(rsi_p1) or pd.isna(rsi_p2):
        return False

    # Condition 1: Price made a higher high
    price_rose = (price_p2 - price_p1) / price_p1 >= min_price_rise

    # Condition 2: RSI made a lower high (momentum weakening)
    rsi_dropped = (rsi_p1 - rsi_p2) >= min_rsi_drop

    divergence = price_rose and rsi_dropped

    if divergence:
        logger.debug(
            f"[Divergence] BEARISH: price {price_p1:.4f}→{price_p2:.4f} "
            f"(rise {(price_p2-price_p1)/price_p1*100:.2f}%) | "
            f"RSI {rsi_p1:.1f}→{rsi_p2:.1f} (-{rsi_p1-rsi_p2:.1f})"
        )

    return divergence


def divergence_score(
    df: pd.DataFrame,
    mode: str = "bullish",
    rsi_period: int = 14,
    lookback: int = 50,
    pivot_window: int = 3,
) -> float:
    """
    Return a 0.0–1.0 confidence score for divergence strength.

    0.0 = no divergence detected
    0.5 = weak divergence (minimum thresholds barely met)
    1.0 = strong divergence (large price drop + large RSI rise)

    Useful for weighting signals rather than treating divergence as binary.

    Args:
        df:          OHLCV DataFrame.
        mode:        "bullish" or "bearish".
        rsi_period:  RSI lookback window.
        lookback:    Candles to scan.
        pivot_window: Pivot detection window.

    Returns:
        Float in [0.0, 1.0].
    """
    needed = rsi_period + lookback + pivot_window * 2 + 5
    if len(df) < needed:
        return 0.0

    close = df["close"].copy()
    rsi_series = ta.momentum.RSIIndicator(close, window=rsi_period).rsi()
    close_recent = close.iloc[-lookback:]
    rsi_recent   = rsi_series.iloc[-lookback:]

    if mode == "bullish":
        price_pivots = find_pivot_lows(close_recent, window=pivot_window)
    else:
        price_pivots = find_pivot_highs(close_recent, window=pivot_window)

    pivot_indices = close_recent[price_pivots].index.tolist()
    if len(pivot_indices) < 2:
        return 0.0

    p1_idx = pivot_indices[-2]
    p2_idx = pivot_indices[-1]
    price_p1 = float(close_recent[p1_idx])
    price_p2 = float(close_recent[p2_idx])
    rsi_p1   = float(rsi_recent[p1_idx])
    rsi_p2   = float(rsi_recent[p2_idx])

    if pd.isna(rsi_p1) or pd.isna(rsi_p2) or price_p1 <= 0:
        return 0.0

    if mode == "bullish":
        price_chg = (price_p1 - price_p2) / price_p1   # positive = lower low
        rsi_chg   = (rsi_p2 - rsi_p1)                  # positive = higher low
        if price_chg < 0.002 or rsi_chg < 0.5:          # below minimum thresholds
            return 0.0
        # Score: normalise price drop and RSI rise to [0, 1]
        # Full score = 3% price drop + 10 RSI points
        price_score = min(1.0, price_chg / 0.03)
        rsi_score   = min(1.0, rsi_chg   / 10.0)
        return float((price_score + rsi_score) / 2)

    else:  # bearish
        price_chg = (price_p2 - price_p1) / price_p1   # positive = higher high
        rsi_chg   = (rsi_p1 - rsi_p2)                  # positive = lower high
        if price_chg < 0.002 or rsi_chg < 0.5:
            return 0.0
        price_score = min(1.0, price_chg / 0.03)
        rsi_score   = min(1.0, rsi_chg   / 10.0)
        return float((price_score + rsi_score) / 2)
