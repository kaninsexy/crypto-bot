"""Microstructure feature builders (Phase 4.E substrate).

Derived features consumed by the microstructure-orderflow strategy
family. Inputs are 1m klines carrying the taker buy/sell aggressor
split (see data/binance_vision.py). Everything here is deterministic
and backward-looking: every feature at time t uses bars with
open-time strictly <= t. No feature peeks forward.

Feature groups
--------------
1. Order-flow delta:   taker_delta(), cumulative delta
2. Volume profile:     volume_profile(), value_area(), find_nodes()
3. VWAP:               session_vwap(), anchored_vwap(), vwap_bands()
4. Resampling helper:  resample_ohlcv() (1m -> signal timeframe,
                       preserving the taker split)

Design notes
------------
- Volume profile spreads each 1m bar's volume UNIFORMLY across its
  [low, high] range (standard OHLCV approximation of volume-at-price;
  exact allocation needs tick data, which the batch defers).
- Value area is the classic 70% expansion around the point of
  control (POC).
- HVN/LVN detection is parameter-explicit local-extrema logic, per
  the pre-registration discipline (strategy literature files lock
  the parameters).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# ---------------------------------------------------------------- #
# 1. Order-flow delta                                               #
# ---------------------------------------------------------------- #


def taker_delta(df: pd.DataFrame) -> pd.DataFrame:
    """Per-bar aggressor imbalance from the taker buy split.

    delta        = taker buys - taker sells   (base units)
    delta_ratio  = delta / volume             (in [-1, +1], 0 volume -> 0)
    cum_delta    = running sum of delta

    Requires columns: volume, taker_buy_base.
    """
    if "taker_buy_base" not in df.columns:
        raise ValueError("taker_buy_base column required (Binance klines)")
    out = pd.DataFrame(index=df.index)
    sells = df["volume"] - df["taker_buy_base"]
    out["delta"] = df["taker_buy_base"] - sells  # == 2*buys - volume
    vol = df["volume"].to_numpy(dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.where(vol > 0, out["delta"].to_numpy() / vol, 0.0)
    out["delta_ratio"] = ratio
    out["cum_delta"] = out["delta"].cumsum()
    return out


# ---------------------------------------------------------------- #
# 2. Volume profile                                                 #
# ---------------------------------------------------------------- #


def volume_profile(
    df: pd.DataFrame,
    n_bins: int = 100,
    price_min: float | None = None,
    price_max: float | None = None,
) -> pd.DataFrame:
    """Volume-at-price histogram over the given window of 1m bars.

    Each bar's volume is spread uniformly across the price bins its
    [low, high] range overlaps (partial overlap = proportional
    share). Returns a DataFrame with columns: price (bin midpoint),
    volume. Sum of volume equals df.volume.sum() (up to float error).
    """
    if df.empty:
        raise ValueError("empty window")
    lo = float(df["low"].min()) if price_min is None else float(price_min)
    hi = float(df["high"].max()) if price_max is None else float(price_max)
    if hi <= lo:  # degenerate flat window
        return pd.DataFrame({"price": [lo], "volume": [float(df["volume"].sum())]})

    edges = np.linspace(lo, hi, n_bins + 1)
    vols = np.zeros(n_bins)

    bar_lo = df["low"].to_numpy(dtype=float)
    bar_hi = df["high"].to_numpy(dtype=float)
    bar_vol = df["volume"].to_numpy(dtype=float)

    for blo, bhi, bv in zip(bar_lo, bar_hi, bar_vol):
        if bv <= 0:
            continue
        if bhi <= blo:  # zero-range bar: all volume in one bin
            idx = min(int((blo - lo) / (hi - lo) * n_bins), n_bins - 1)
            vols[max(idx, 0)] += bv
            continue
        # proportional overlap of [blo, bhi] with each bin
        left = np.clip(edges[:-1], blo, bhi)
        right = np.clip(edges[1:], blo, bhi)
        overlap = np.maximum(right - left, 0.0)
        vols += bv * overlap / (bhi - blo)

    mids = (edges[:-1] + edges[1:]) / 2
    return pd.DataFrame({"price": mids, "volume": vols})


def value_area(profile: pd.DataFrame, pct: float = 0.70) -> dict:
    """POC + value area bounds from a volume_profile() result.

    Classic expansion: start at the POC bin, greedily add the larger
    neighbouring bin until `pct` of total volume is covered.
    Returns {"poc": price, "vah": price, "val": price}.
    """
    v = profile["volume"].to_numpy(dtype=float)
    p = profile["price"].to_numpy(dtype=float)
    total = v.sum()
    if total <= 0:
        raise ValueError("profile has no volume")
    poc = int(np.argmax(v))
    lo = hi = poc
    covered = v[poc]
    target = pct * total
    while covered < target and (lo > 0 or hi < len(v) - 1):
        below = v[lo - 1] if lo > 0 else -1.0
        above = v[hi + 1] if hi < len(v) - 1 else -1.0
        if above >= below:
            hi += 1
            covered += v[hi]
        else:
            lo -= 1
            covered += v[lo]
    return {"poc": float(p[poc]), "vah": float(p[hi]), "val": float(p[lo])}


def find_nodes(
    profile: pd.DataFrame,
    kind: str = "hvn",
    smooth_bins: int = 5,
    min_rel_prominence: float = 0.25,
) -> pd.DataFrame:
    """High/low-volume nodes from the smoothed profile.

    HVN ("high-volume node", order-block-adjacent zone): a local
    maximum whose volume is at least `min_rel_prominence` of the
    profile's max bin volume.

    LVN ("low-volume node", FVG-adjacent thin zone): the minimum-
    volume bin BETWEEN two consecutive HVNs, kept only if its volume
    is at most (1 - min_rel_prominence) of the smaller flanking HVN —
    i.e. a genuine valley, not a plateau. This matches the trading
    usage: an LVN only means something as thin ground between two
    accepted price areas.

    Returns DataFrame with columns: price, volume.
    """
    if kind not in ("hvn", "lvn"):
        raise ValueError("kind must be 'hvn' or 'lvn'")
    v = (
        profile["volume"]
        .rolling(smooth_bins, center=True, min_periods=1)
        .mean()
        .to_numpy(dtype=float)
    )
    p = profile["price"].to_numpy(dtype=float)
    vmax = v.max()
    if vmax <= 0:
        return pd.DataFrame(columns=["price", "volume"])

    # HVNs: thresholded local maxima (ties allowed for plateaus).
    peak_idx = [
        i for i in range(1, len(v) - 1)
        if v[i] >= v[i - 1] and v[i] >= v[i + 1]
        and v[i] >= min_rel_prominence * vmax
    ]
    if kind == "hvn":
        rows = [{"price": float(p[i]), "volume": float(v[i])} for i in peak_idx]
        return pd.DataFrame(rows, columns=["price", "volume"])

    # LVNs: deepest valley between each pair of consecutive HVNs.
    rows = []
    for a, b in zip(peak_idx, peak_idx[1:]):
        if b - a < 2:
            continue
        seg = v[a + 1:b]
        j = a + 1 + int(np.argmin(seg))
        if v[j] <= (1.0 - min_rel_prominence) * min(v[a], v[b]):
            rows.append({"price": float(p[j]), "volume": float(v[j])})
    return pd.DataFrame(rows, columns=["price", "volume"])


# ---------------------------------------------------------------- #
# 3. VWAP                                                           #
# ---------------------------------------------------------------- #


def _typical(df: pd.DataFrame) -> pd.Series:
    return (df["high"] + df["low"] + df["close"]) / 3.0


def session_vwap(df: pd.DataFrame, session: str = "1D") -> pd.Series:
    """Running VWAP that resets each session (UTC calendar buckets)."""
    tp = _typical(df)
    pv = tp * df["volume"]
    grp = df.index.floor(session)
    cum_pv = pv.groupby(grp).cumsum()
    cum_v = df["volume"].groupby(grp).cumsum()
    return (cum_pv / cum_v.replace(0, np.nan)).rename("vwap")


def anchored_vwap(df: pd.DataFrame, anchor_ts) -> pd.Series:
    """VWAP anchored at a specific timestamp (NaN before the anchor)."""
    anchor = pd.Timestamp(anchor_ts)
    if anchor.tzinfo is None:
        anchor = anchor.tz_localize("UTC")
    mask = df.index >= anchor
    tp = _typical(df)
    pv = (tp * df["volume"]).where(mask, 0.0)
    v = df["volume"].where(mask, 0.0)
    out = pv.cumsum() / v.cumsum().replace(0, np.nan)
    return out.where(mask).rename("avwap")


def vwap_bands(df: pd.DataFrame, vwap: pd.Series, window: int = 60) -> pd.DataFrame:
    """VWAP +/- k*sigma bands; sigma = rolling std of (close - vwap)."""
    dev = (df["close"] - vwap).rolling(window, min_periods=window // 2).std()
    return pd.DataFrame({
        "vwap": vwap,
        "upper_1": vwap + dev, "lower_1": vwap - dev,
        "upper_2": vwap + 2 * dev, "lower_2": vwap - 2 * dev,
    })


# ---------------------------------------------------------------- #
# 4. Resampling                                                     #
# ---------------------------------------------------------------- #


def resample_ohlcv(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """1m -> coarser signal bars, preserving the taker split.

    timeframe: pandas offset alias ("15min", "1h", "4h", "1D", ...).
    Bars are labelled by OPEN time (left edge), matching the engine's
    convention. Empty buckets are dropped.
    """
    agg = {
        "open": "first", "high": "max", "low": "min", "close": "last",
        "volume": "sum",
    }
    for col in ("quote_volume", "n_trades", "taker_buy_base", "taker_buy_quote"):
        if col in df.columns:
            agg[col] = "sum"
    out = df.resample(timeframe, label="left", closed="left").agg(agg)
    return out.dropna(subset=["open"])
