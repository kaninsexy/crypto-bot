"""Unit tests for data/microstructure_features.py (synthetic 1m, no cache)."""

import numpy as np
import pandas as pd
import pytest

from data.microstructure_features import (
    FEATURE_COLUMNS,
    build_signal_frame,
    pandas_offset,
    params_id,
)


def _synth_1m(days: int = 8, start: str = "2024-01-01") -> pd.DataFrame:
    """Deterministic 1m klines: a gentle price ramp per day, constant vol."""
    n = days * 24 * 60
    idx = pd.date_range(start, periods=n, freq="1min", tz="UTC")
    # price drifts up across the whole window so daily profiles differ
    close = 100.0 + np.linspace(0, 20, n)
    vol = np.full(n, 10.0)
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": vol,
            "quote_volume": vol * close,
            "n_trades": np.full(n, 5),
            "taker_buy_base": np.full(n, 6.0),   # 6 buy / 4 sell -> delta +2/bar
            "taker_buy_quote": np.full(n, 6.0) * close,
        },
        index=idx,
    )


def test_offset_and_params_id():
    assert pandas_offset("15m") == "15min"
    assert pandas_offset("1h") == "1h"
    with pytest.raises(ValueError):
        pandas_offset("13m")
    assert "pd5" in params_id("15m", 5, 100, 5, 0.25, 0.70)


def test_all_feature_columns_present():
    sig = build_signal_frame(_synth_1m(), "1h", profile_days=3)
    for c in FEATURE_COLUMNS:
        assert c in sig.columns


def test_resample_preserves_volume_and_taker():
    df = _synth_1m(days=2)
    sig = build_signal_frame(df, "1h", profile_days=1)
    assert sig["volume"].sum() == pytest.approx(df["volume"].sum())
    assert sig["taker_buy_base"].sum() == pytest.approx(df["taker_buy_base"].sum())


def test_delta_matches_taker_split():
    # 6 taker-buy of 10 volume per 1m -> per 1h bar: buys=360, vol=600,
    # delta = 2*360 - 600 = 120.
    sig = build_signal_frame(_synth_1m(days=2), "1h", profile_days=1)
    row = sig.iloc[10]
    assert row["delta"] == pytest.approx(2 * row["taker_buy_base"] - row["volume"])
    assert row["delta"] == pytest.approx(120.0)


def test_cum_delta_resets_at_utc_midnight():
    sig = build_signal_frame(_synth_1m(days=3), "1h", profile_days=1)
    by_day = sig.groupby(sig.index.normalize())
    for _, day in by_day:
        # first bar of the day: cum_delta == that bar's delta
        assert day["cum_delta"].iloc[0] == pytest.approx(day["delta"].iloc[0])
        # cumulative within the day
        assert day["cum_delta"].iloc[-1] == pytest.approx(day["delta"].sum())


def test_profile_is_backward_looking():
    # profile_days=2: day 0 and day 1 have < 2 prior days -> some NaN;
    # a later day's profile must be built ONLY from strictly-prior days.
    df = _synth_1m(days=6)
    sig = build_signal_frame(df, "1h", profile_days=2)

    # Day 0 has no prior day -> POC NaN for all its bars.
    day0 = sig[sig.index.normalize() == pd.Timestamp("2024-01-01", tz="UTC")]
    assert day0["poc"].isna().all()

    # A mid-window day's POC must lie within the price range of its prior
    # 2 days, and NOT include its own (higher, later) prices.
    day4 = pd.Timestamp("2024-01-05", tz="UTC")
    prior = df.loc["2024-01-03":"2024-01-04 23:59"]
    bar = sig[sig.index.normalize() == day4].iloc[0]
    assert prior["low"].min() <= bar["poc"] <= prior["high"].max()
    # own-day prices are strictly higher than the prior-window max -> excluded
    own = df.loc["2024-01-05"]
    assert bar["poc"] < own["low"].min()


def test_empty_input_raises():
    with pytest.raises(ValueError):
        build_signal_frame(_synth_1m().iloc[0:0], "1h")
