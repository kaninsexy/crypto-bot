"""Enriched signal-timeframe frame builder (Phase 4.E substrate glue).

The Phase 4.E microstructure strategies trade on coarse *signal-timeframe*
bars (15m / 1h) but their volume-profile features must be built from the
underlying 1m data (locked gate in the per-strategy literature files:
"volume profile built from Binance spot 1m bars over the prior N days").

This module bridges that: given the 1m klines (see data/binance_vision.py)
and a signal timeframe, it returns ONE DataFrame indexed by signal-bar
open-time carrying

  - resampled OHLCV + taker split       (open/high/low/close/volume,
                                          quote_volume, n_trades,
                                          taker_buy_base, taker_buy_quote)
  - order-flow delta features           (delta, delta_ratio, cum_delta)
  - daily volume-profile features        (poc, vah, val,
                                          hvn_prices, lvn_prices)

so the engine can iterate at signal cadence while the strategies read
profile features that were computed at 1m granularity.

Look-ahead safety
-----------------
Every feature at signal bar t uses only information available at or
before t:

  - delta / delta_ratio come from bar t's own resampled taker split.
  - cum_delta is the running delta sum WITHIN bar t's UTC day (reset at
    00:00 UTC) — a backward-looking within-session accumulation.
  - The volume profile for a signal bar dated D is built from the 1m
    bars of the prior `profile_days` COMPLETE UTC days [D-profile_days,
    D) — the current forming day D is EXCLUDED, so no bar sees its own
    day's (or any future) volume-at-price. This matches the locked
    "prior N completed UTC days, EXCLUDING the current forming day"
    gate shared by VolumeProfileAcceptance, LVNTraversal, and
    HVNMeanReversion.

The three profile strategies share their profile construction
parameters (profile_days=5, n_bins=100, smooth_bins=5,
min_rel_prominence=0.25, value_area_pct=0.70), so a single builder with
those defaults serves all three. The four delta strategies
(LiquiditySweepReversal, DeltaDivergence, VWAPInstitutionalBand,
BreakoutDeltaConfirmed) ignore the profile columns and read only the
delta columns; they are unaffected by the profile parameters.
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

# Signal-timeframe → pandas offset alias.  The manifest/DSR timeframe
# strings ("15m", "1h") are NOT all valid pandas offset aliases
# ("15m" is not; "15min" is), so translate at the resample boundary.
_TF_TO_PANDAS: dict[str, str] = {
    "1m": "1min", "5m": "5min", "15m": "15min", "30m": "30min",
    "1h": "1h", "2h": "2h", "4h": "4h", "8h": "8h", "12h": "12h",
    "1d": "1D",
}

# Columns the builder adds on top of the resampled OHLCV frame.
FEATURE_COLUMNS: tuple[str, ...] = (
    "delta", "delta_ratio", "cum_delta",
    "poc", "vah", "val", "hvn_prices", "lvn_prices",
)


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
    timeframe: str,
    profile_days: int,
    n_bins: int,
    smooth_bins: int,
    min_rel_prominence: float,
    value_area_pct: float,
) -> str:
    """Stable identifier for a feature-parameter set (cache keying)."""
    return (
        f"{timeframe}_pd{profile_days}_nb{n_bins}_sb{smooth_bins}"
        f"_mp{min_rel_prominence:g}_va{value_area_pct:g}"
    )


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
    `profile_days` complete days [D-profile_days, D) — D itself is
    excluded.  Days without a full complement of prior data still emit
    a row (whatever prior days exist); days with NO prior data get
    NaN scalars and empty node lists.

    Returns a DataFrame indexed by normalised UTC day (Timestamp at
    00:00) with columns poc, vah, val, hvn_prices, lvn_prices.
    """
    # Partition the 1m frame into per-day slices once.
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

    out = pd.DataFrame(rows).set_index("day")
    return out


def build_signal_frame(
    klines_1m: pd.DataFrame,
    timeframe: str,
    profile_days: int = DEFAULT_PROFILE_DAYS,
    n_bins: int = DEFAULT_N_BINS,
    smooth_bins: int = DEFAULT_SMOOTH_BINS,
    min_rel_prominence: float = DEFAULT_MIN_REL_PROMINENCE,
    value_area_pct: float = DEFAULT_VALUE_AREA_PCT,
) -> pd.DataFrame:
    """Resample 1m klines to `timeframe` and attach microstructure features.

    Returns a DataFrame indexed by signal-bar OPEN time with the
    resampled OHLCV + taker columns plus the FEATURE_COLUMNS.  Every
    feature is backward-looking (see module docstring).

    `klines_1m` must be the UTC-indexed 1m frame from
    data.binance_vision.load_klines (columns include volume and
    taker_buy_base).
    """
    if klines_1m.empty:
        raise ValueError("empty 1m klines")

    sig = resample_ohlcv(klines_1m, pandas_offset(timeframe))

    # ── Order-flow delta (from the resampled taker sums) ──────────────
    # delta = taker buys - taker sells = taker_buy_base - (volume -
    # taker_buy_base) = 2*taker_buy_base - volume, summed over the bar.
    buys = sig["taker_buy_base"].to_numpy(dtype=float)
    vol = sig["volume"].to_numpy(dtype=float)
    delta = 2.0 * buys - vol
    sig["delta"] = delta
    with np.errstate(divide="ignore", invalid="ignore"):
        sig["delta_ratio"] = np.where(vol > 0, delta / vol, 0.0)
    # cum_delta resets at 00:00 UTC each day (within-session accumulation).
    sig["cum_delta"] = (
        sig["delta"].groupby(sig.index.normalize()).cumsum()
    )

    # ── Daily volume-profile features (from the 1m data) ──────────────
    profiles = _daily_profiles(
        klines_1m, profile_days, n_bins, smooth_bins,
        min_rel_prominence, value_area_pct,
    )
    sig_day = sig.index.normalize()
    for col in ("poc", "vah", "val"):
        sig[col] = profiles[col].reindex(sig_day).to_numpy()
    # Object (list) columns: reindex by day, then broadcast to signal bars.
    for col in ("hvn_prices", "lvn_prices"):
        day_map = profiles[col]
        sig[col] = [
            day_map.get(d, []) if d in day_map.index else []
            for d in sig_day
        ]

    return sig
