"""Unit tests for data/microstructure.py (synthetic data, no network)."""

import numpy as np
import pandas as pd
import pytest

from data.microstructure import (
    anchored_vwap,
    find_nodes,
    resample_ohlcv,
    session_vwap,
    taker_delta,
    value_area,
    volume_profile,
    vwap_bands,
)


def _bars(n=120, start="2024-01-01", price=100.0, vol=10.0, spread=1.0):
    idx = pd.date_range(start, periods=n, freq="1min", tz="UTC")
    close = np.full(n, price, dtype=float)
    return pd.DataFrame(
        {
            "open": close,
            "high": close + spread,
            "low": close - spread,
            "close": close,
            "volume": np.full(n, vol),
            "taker_buy_base": np.full(n, vol / 2),
        },
        index=idx,
    )


# ------------------------------------------------------------- delta

def test_delta_balanced_is_zero():
    d = taker_delta(_bars())
    assert np.allclose(d["delta"], 0.0)
    assert np.allclose(d["delta_ratio"], 0.0)
    assert np.allclose(d["cum_delta"], 0.0)


def test_delta_all_buyers():
    df = _bars()
    df["taker_buy_base"] = df["volume"]  # every trade taker-buy
    d = taker_delta(df)
    assert np.allclose(d["delta"], df["volume"])
    assert np.allclose(d["delta_ratio"], 1.0)
    assert d["cum_delta"].iloc[-1] == pytest.approx(df["volume"].sum())


def test_delta_zero_volume_bar_ratio_is_zero():
    df = _bars(n=3)
    df.loc[df.index[1], ["volume", "taker_buy_base"]] = 0.0
    d = taker_delta(df)
    assert d["delta_ratio"].iloc[1] == 0.0


def test_delta_requires_taker_column():
    df = _bars().drop(columns=["taker_buy_base"])
    with pytest.raises(ValueError):
        taker_delta(df)


# ----------------------------------------------------- volume profile

def test_profile_conserves_volume():
    df = _bars(n=200)
    # vary prices so the profile spans multiple bins
    ramp = np.linspace(0, 10, 200)
    for col in ("open", "high", "low", "close"):
        df[col] = df[col] + ramp
    prof = volume_profile(df, n_bins=50)
    assert prof["volume"].sum() == pytest.approx(df["volume"].sum(), rel=1e-9)


def test_profile_poc_at_heavy_price():
    df = _bars(n=100)
    # bars 40-60 trade 10x the volume at price ~120
    heavy = slice(40, 60)
    for col in ("open", "high", "low", "close"):
        df.iloc[heavy, df.columns.get_loc(col)] = 120.0
    df.iloc[heavy, df.columns.get_loc("high")] = 121.0
    df.iloc[heavy, df.columns.get_loc("low")] = 119.0
    df.iloc[heavy, df.columns.get_loc("volume")] = 100.0
    prof = volume_profile(df, n_bins=60)
    va = value_area(prof)
    assert 118.0 <= va["poc"] <= 122.0
    assert va["val"] <= va["poc"] <= va["vah"]


def test_value_area_covers_target_share():
    df = _bars(n=300)
    rng = np.random.default_rng(7)
    walk = np.cumsum(rng.normal(0, 0.5, 300))
    for col in ("open", "close"):
        df[col] = 100 + walk
    df["high"] = df[["open", "close"]].max(axis=1) + 0.5
    df["low"] = df[["open", "close"]].min(axis=1) - 0.5
    prof = volume_profile(df, n_bins=80)
    va = value_area(prof, pct=0.70)
    inside = prof[(prof["price"] >= va["val"]) & (prof["price"] <= va["vah"])]
    assert inside["volume"].sum() >= 0.70 * prof["volume"].sum()


def test_flat_window_degenerates_to_single_bin():
    df = _bars(n=10, spread=0.0)
    prof = volume_profile(df)
    assert len(prof) == 1
    assert prof["volume"].iloc[0] == pytest.approx(df["volume"].sum())


def test_hvn_lvn_detection():
    # two volume humps with a valley between them
    price = np.linspace(90, 110, 100)
    vol = (
        np.exp(-((price - 95) ** 2)) * 100
        + np.exp(-((price - 105) ** 2)) * 80
        + 1.0
    )
    prof = pd.DataFrame({"price": price, "volume": vol})
    hvns = find_nodes(prof, "hvn", smooth_bins=3, min_rel_prominence=0.05)
    lvns = find_nodes(prof, "lvn", smooth_bins=3, min_rel_prominence=0.05)
    assert any(abs(p - 95) < 2 for p in hvns["price"])
    assert any(abs(p - 105) < 2 for p in hvns["price"])
    assert any(94 < p < 106 for p in lvns["price"])  # the valley


# ---------------------------------------------------------------- vwap

def test_session_vwap_equals_weighted_mean_within_day():
    df = _bars(n=60)
    df["close"] = np.linspace(100, 110, 60)
    df["high"] = df["close"] + 1
    df["low"] = df["close"] - 1
    df["volume"] = np.linspace(1, 5, 60)
    v = session_vwap(df, "1D")
    tp = (df["high"] + df["low"] + df["close"]) / 3
    expected = (tp * df["volume"]).sum() / df["volume"].sum()
    assert v.iloc[-1] == pytest.approx(expected)


def test_session_vwap_resets_across_days():
    a = _bars(n=30, start="2024-01-01 23:30", price=100.0)
    b = _bars(n=30, start="2024-01-02 00:00", price=200.0)
    df = pd.concat([a, b])
    v = session_vwap(df, "1D")
    assert v.iloc[29] == pytest.approx(100.0)  # day 1 all at 100
    assert v.iloc[30] == pytest.approx(200.0)  # day 2 starts fresh


def test_anchored_vwap_nan_before_anchor():
    df = _bars(n=50)
    anchor = df.index[20]
    av = anchored_vwap(df, anchor)
    assert av.iloc[:20].isna().all()
    assert av.iloc[20:].notna().all()


def test_vwap_bands_symmetric():
    df = _bars(n=200)
    rng = np.random.default_rng(1)
    df["close"] = 100 + rng.normal(0, 1, 200)
    v = session_vwap(df, "1D")
    bands = vwap_bands(df, v, window=50)
    tail = bands.dropna()
    assert ((tail["upper_1"] - tail["vwap"])
            - (tail["vwap"] - tail["lower_1"])).abs().max() < 1e-9


# ------------------------------------------------------------ resample

def test_resample_preserves_totals_and_ohlc():
    df = _bars(n=120)
    df["close"] = np.arange(120, dtype=float) + 100
    df["open"] = df["close"] - 0.5
    df["high"] = df["close"] + 1
    df["low"] = df["open"] - 1
    out = resample_ohlcv(df, "1h")
    assert len(out) == 2
    assert out["volume"].sum() == pytest.approx(df["volume"].sum())
    assert out["taker_buy_base"].sum() == pytest.approx(df["taker_buy_base"].sum())
    assert out["open"].iloc[0] == df["open"].iloc[0]
    assert out["close"].iloc[-1] == df["close"].iloc[-1]
    assert out["high"].iloc[0] == df["high"].iloc[:60].max()


def test_resample_labels_by_open_time():
    df = _bars(n=120)
    out = resample_ohlcv(df, "1h")
    assert out.index[0] == df.index[0].floor("1h")
