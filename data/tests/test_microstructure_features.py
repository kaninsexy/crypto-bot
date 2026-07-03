"""Unit tests for data/microstructure_features.py (synthetic 1m, no cache)."""

import numpy as np
import pandas as pd
import pytest

from data.microstructure_features import (
    DELTA_COLUMNS,
    PROFILE_COLUMNS,
    build_profile_features,
    build_signal_frame,
    pandas_offset,
)


def _synth_1m(days: int = 8, start: str = "2024-01-01") -> pd.DataFrame:
    """Deterministic 1m klines: gentle upward ramp so daily profiles differ."""
    n = days * 24 * 60
    idx = pd.date_range(start, periods=n, freq="1min", tz="UTC")
    close = 100.0 + np.linspace(0, 20, n)
    vol = np.full(n, 10.0)
    return pd.DataFrame(
        {
            "open": close, "high": close + 0.5, "low": close - 0.5,
            "close": close, "volume": vol, "quote_volume": vol * close,
            "n_trades": np.full(n, 5),
            "taker_buy_base": np.full(n, 6.0),   # 6 buy / 4 sell per 1m
            "taker_buy_quote": np.full(n, 6.0) * close,
        },
        index=idx,
    )


# ── build_signal_frame (substrate: resample + delta) ──────────────────────────

def test_offset_alias():
    assert pandas_offset("15m") == "15min"
    assert pandas_offset("1h") == "1h"
    with pytest.raises(ValueError):
        pandas_offset("13m")


def test_signal_frame_has_delta_not_profile():
    sig = build_signal_frame(_synth_1m(), "1h")
    for c in DELTA_COLUMNS:
        assert c in sig.columns
    for c in PROFILE_COLUMNS:
        assert c not in sig.columns          # profile is NOT on the frame


def test_signal_frame_resample_conserves():
    df = _synth_1m(days=2)
    sig = build_signal_frame(df, "1h")
    assert sig["volume"].sum() == pytest.approx(df["volume"].sum())
    assert sig["taker_buy_base"].sum() == pytest.approx(df["taker_buy_base"].sum())


def test_delta_matches_taker_split_and_resets_daily():
    sig = build_signal_frame(_synth_1m(days=3), "1h")
    row = sig.iloc[10]
    assert row["delta"] == pytest.approx(2 * row["taker_buy_base"] - row["volume"])
    for _, day in sig.groupby(sig.index.normalize()):
        assert day["cum_delta"].iloc[0] == pytest.approx(day["delta"].iloc[0])
        assert day["cum_delta"].iloc[-1] == pytest.approx(day["delta"].sum())


def test_signal_frame_empty_raises():
    with pytest.raises(ValueError):
        build_signal_frame(_synth_1m().iloc[0:0], "1h")


# ── build_profile_features (from 1m, aligned to signal bars) ──────────────────

def _sig_index(df_1m, tf="1h"):
    return build_signal_frame(df_1m, tf).index


def test_profile_features_columns_and_alignment():
    df = _synth_1m()
    idx = _sig_index(df)
    feats = build_profile_features(df, idx, profile_days=3)
    assert list(feats.columns) == list(PROFILE_COLUMNS)
    assert feats.index.equals(pd.DatetimeIndex(idx))


def test_profile_features_backward_looking():
    df = _synth_1m(days=6)
    idx = _sig_index(df)
    feats = build_profile_features(df, idx, profile_days=2)
    # Day 0 has no prior day -> NaN poc.
    day0 = feats[feats.index.normalize() == pd.Timestamp("2024-01-01", tz="UTC")]
    assert day0["poc"].isna().all()
    # A later day's POC lies within its prior 2 days' price range and
    # EXCLUDES its own (higher) prices.
    day4 = pd.Timestamp("2024-01-05", tz="UTC")
    prior = df.loc["2024-01-03":"2024-01-04 23:59"]
    poc = feats[feats.index.normalize() == day4]["poc"].iloc[0]
    assert prior["low"].min() <= poc <= prior["high"].max()
    assert poc < df.loc["2024-01-05"]["low"].min()


def test_profile_features_no_peek_on_truncation():
    """Truncating the 1m input at T must not change features for bars whose
    day is <= T's day (the resolution's no-peek requirement)."""
    df = _synth_1m(days=8)
    idx = _sig_index(df)
    full = build_profile_features(df, idx, profile_days=3)

    # Truncate 1m at end of day 4; recompute on the same (full) signal index.
    cut = pd.Timestamp("2024-01-05 23:59:00", tz="UTC")
    trunc = build_profile_features(df.loc[:cut], idx, profile_days=3)

    # Bars up to and including day 5 must be identical (their profiles use
    # only days <= 4, all present in the truncated input).
    upto = idx[idx <= pd.Timestamp("2024-01-05 23:00", tz="UTC")]
    a = full.loc[upto, ["poc", "vah", "val"]]
    b = trunc.loc[upto, ["poc", "vah", "val"]]
    pd.testing.assert_frame_equal(a, b)
