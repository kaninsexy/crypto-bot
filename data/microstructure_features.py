"""Phase 4.E microstructure feature builders.

Two distinct products, matching the NewsSentimentMomentum/AttentionMomentum
precedent (features precomputed by the trial script, injected into the
strategy via a constructor argument):

1. `build_signal_frame(klines_1m, timeframe)` — the SUBSTRATE the engine
   iterates: 1m resampled to the signal timeframe (taker split summed) plus
   the cheap per-bar order-flow delta columns (delta / delta_ratio /
   cum_delta).  This is what holdout.load_dev / load_holdout return.

2. `build_profile_features(klines_1m, signal_index, ...)` — the VOLUME
   PROFILE features (poc / vah / val + hvn_prices / lvn_prices) built FROM
   THE 1m DATA, aligned onto a given signal-bar index.  The three profile
   strategies (VolumeProfileAcceptance, LVNTraversal, HVNMeanReversion)
   receive the resulting frame via their `profile_features` constructor arg;
   the trial script computes it from the same 1m frame that produced the
   price substrate, truncated at the same boundary.

Look-ahead safety
-----------------
Both products are strictly backward-looking:

  - delta / delta_ratio come from a signal bar's own resampled taker split.
  - cum_delta accumulates WITHIN a bar's UTC day (reset 00:00 UTC).
  - The volume profile aligned to a signal bar dated D is built ONLY from
    the 1m bars of the prior `profile_days` COMPLETE UTC days [D-profile_days,
    D); day D itself is excluded, so no bar sees its own day's or any future
    volume-at-price.  Truncating the 1m input at any time T leaves the
    features for every bar at/onward-of-... — i.e. every bar whose day is
    <= T's day — unchanged (verified by the no-peek unit test).

The three profile strategies share their profile parameters
(profile_days=5, n_bins=100, smooth_bins=5, min_rel_prominence=0.25,
value_area_pct=0.70), so one builder with those defaults serves all three.
The four delta strategies read only the delta columns from the signal frame
and need no profile features.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from data.microstructure import (
    find_nodes,
    resample_ohlcv,
    value_area,
    volume_profile,
)

# Locked Variation #1 profile parameters, shared by the three
# volume-profile strategies (see their research/*-literature.md).
DEFAULT_PROFILE_DAYS: int = 5
DEFAULT_N_BINS: int = 100
DEFAULT_SMOOTH_BINS: int = 5
DEFAULT_MIN_REL_PROMINENCE: float = 0.25
DEFAULT_VALUE_AREA_PCT: float = 0.70

# Columns the signal frame carries beyond the resampled OHLCV+taker set.
DELTA_COLUMNS: tuple[str, ...] = ("delta", "delta_ratio", "cum_delta")
# Columns build_profile_features returns.
PROFILE_COLUMNS: tuple[str, ...] = (
    "poc", "vah", "val", "hvn_prices", "lvn_prices",
)

# Signal-timeframe → pandas offset alias.  Manifest/DSR timeframe strings
# ("15m", "1h") are not all valid pandas aliases ("15m" is not; "15min" is).
_TF_TO_PANDAS: dict[str, str] = {
    "1m": "1min", "5m": "5min", "15m": "15min", "30m": "30min",
    "1h": "1h", "2h": "2h", "4h": "4h", "8h": "8h", "12h": "12h",
    "1d": "1D",
}


def pandas_offset(timeframe: str) -> str:
    """Translate a manifest/DSR timeframe string to a pandas offset alias."""
    alias = _TF_TO_PANDAS.get(timeframe)
    if alias is None:
        raise ValueError(
            f"unsupported signal timeframe {timeframe!r}; "
            f"known: {sorted(_TF_TO_PANDAS)}"
        )
    return alias


def params_id(
    profile_days: int = DEFAULT_PROFILE_DAYS,
    n_bins: int = DEFAULT_N_BINS,
    smooth_bins: int = DEFAULT_SMOOTH_BINS,
    min_rel_prominence: float = DEFAULT_MIN_REL_PROMINENCE,
    value_area_pct: float = DEFAULT_VALUE_AREA_PCT,
) -> str:
    """Stable identifier for a profile-parameter set (cache keying)."""
    return (
        f"pd{profile_days}_nb{n_bins}_sb{smooth_bins}"
        f"_mp{min_rel_prominence:g}_va{value_area_pct:g}"
    )


# ── 1. Signal-frame substrate (resample + delta) ──────────────────────────────

def build_signal_frame(klines_1m: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Resample 1m klines to `timeframe` and attach order-flow delta columns.

    Returns a DataFrame indexed by signal-bar OPEN time with the resampled
    OHLCV + taker columns plus DELTA_COLUMNS.  No volume-profile columns
    (those are built separately via build_profile_features and injected into
    the strategy by the trial script).
    """
    if klines_1m.empty:
        raise ValueError("empty 1m klines")

    sig = resample_ohlcv(klines_1m, pandas_offset(timeframe))

    # delta = taker buys - taker sells = 2*taker_buy_base - volume (per bar).
    buys = sig["taker_buy_base"].to_numpy(dtype=float)
    vol = sig["volume"].to_numpy(dtype=float)
    delta = 2.0 * buys - vol
    sig["delta"] = delta
    with np.errstate(divide="ignore", invalid="ignore"):
        sig["delta_ratio"] = np.where(vol > 0, delta / vol, 0.0)
    # cum_delta resets at 00:00 UTC each day (within-session accumulation).
    sig["cum_delta"] = sig["delta"].groupby(sig.index.normalize()).cumsum()
    return sig


# ── 2. Volume-profile features (from 1m, aligned to signal bars) ──────────────

def _daily_profiles(
    klines_1m: pd.DataFrame,
    profile_days: int,
    n_bins: int,
    smooth_bins: int,
    min_rel_prominence: float,
    value_area_pct: float,
) -> pd.DataFrame:
    """One row per UTC day with that day's backward-looking profile.

    The profile for day D is built from the 1m bars of the prior
    `profile_days` complete days [D-profile_days, D) — D itself is excluded.
    Days with no prior data get NaN scalars and empty node lists.  Computed
    once per day (NOT per signal bar), so the cost is
    O(days * profile_days_of_1m), not quadratic in signal bars.

    Returns a DataFrame indexed by normalised UTC day with columns
    poc, vah, val, hvn_prices, lvn_prices.
    """
    day_key = klines_1m.index.normalize()
    per_day: dict[pd.Timestamp, pd.DataFrame] = {
        day: grp for day, grp in klines_1m.groupby(day_key)
    }
    days = sorted(per_day.keys())

    rows = []
    for i, day in enumerate(days):
        prior = days[max(0, i - profile_days):i]
        if not prior:
            rows.append({
                "day": day, "poc": np.nan, "vah": np.nan, "val": np.nan,
                "hvn_prices": [], "lvn_prices": [],
            })
            continue
        window = pd.concat([per_day[d] for d in prior])
        prof = volume_profile(window, n_bins=n_bins)
        try:
            va = value_area(prof, pct=value_area_pct)
        except ValueError:
            va = {"poc": np.nan, "vah": np.nan, "val": np.nan}
        hvn = find_nodes(
            prof, "hvn", smooth_bins=smooth_bins,
            min_rel_prominence=min_rel_prominence,
        )
        lvn = find_nodes(
            prof, "lvn", smooth_bins=smooth_bins,
            min_rel_prominence=min_rel_prominence,
        )
        rows.append({
            "day": day,
            "poc": float(va["poc"]),
            "vah": float(va["vah"]),
            "val": float(va["val"]),
            "hvn_prices": [float(x) for x in hvn["price"].tolist()],
            "lvn_prices": [float(x) for x in lvn["price"].tolist()],
        })
    return pd.DataFrame(rows).set_index("day")


def build_profile_features(
    klines_1m: pd.DataFrame,
    signal_index: pd.DatetimeIndex,
    profile_days: int = DEFAULT_PROFILE_DAYS,
    n_bins: int = DEFAULT_N_BINS,
    smooth_bins: int = DEFAULT_SMOOTH_BINS,
    min_rel_prominence: float = DEFAULT_MIN_REL_PROMINENCE,
    value_area_pct: float = DEFAULT_VALUE_AREA_PCT,
) -> pd.DataFrame:
    """Volume-profile features from 1m data, aligned to `signal_index`.

    Returns a DataFrame indexed by `signal_index` with PROFILE_COLUMNS.  The
    row for signal bar t carries the profile of the prior `profile_days`
    complete UTC days before t's day — strictly backward-looking.  Pass a 1m
    frame truncated at the same boundary as the price substrate (dev: 1m <
    holdout_start) so features never see across the boundary.
    """
    if klines_1m.empty:
        raise ValueError("empty 1m klines")
    profiles = _daily_profiles(
        klines_1m, profile_days, n_bins, smooth_bins,
        min_rel_prominence, value_area_pct,
    )
    days = pd.DatetimeIndex(signal_index).normalize()
    out = pd.DataFrame(index=pd.DatetimeIndex(signal_index))
    for col in ("poc", "vah", "val"):
        out[col] = profiles[col].reindex(days).to_numpy()
    for col in ("hvn_prices", "lvn_prices"):
        day_map = profiles[col]
        out[col] = [
            day_map.get(d, []) if d in day_map.index else []
            for d in days
        ]
    return out
