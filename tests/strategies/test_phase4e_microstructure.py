"""Unit tests for the Phase 4.E microstructure strategies.

Each strategy is exercised for: its ENTRY trigger, each EXIT branch, and
the long-only never-sell-when-flat gate.  Synthetic bars are hand-built to
hit each mechanical rule from the locked literature specs.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from strategies.breakout_delta_confirmed import BreakoutDeltaConfirmedStrategy
from strategies.delta_divergence import DeltaDivergenceStrategy
from strategies.hvn_mean_reversion import HVNMeanReversionStrategy
from strategies.liquidity_sweep_reversal import LiquiditySweepReversalStrategy
from strategies.lvn_traversal import LVNTraversalStrategy
from strategies.volume_profile_acceptance import VolumeProfileAcceptanceStrategy
from strategies.vwap_institutional_band import VWAPInstitutionalBandStrategy


def _idx(n, freq="15min", start="2024-03-04"):
    return pd.date_range(start, periods=n, freq=freq, tz="UTC")


def _base(n, freq="15min", start="2024-03-04", price=100.0):
    """Flat benign frame with all columns the strategies read."""
    idx = _idx(n, freq, start)
    c = np.full(n, price)
    return pd.DataFrame(
        {
            "open": c.copy(), "high": c + 1.0, "low": c - 1.0, "close": c.copy(),
            "volume": np.full(n, 10.0), "taker_buy_base": np.full(n, 5.0),
            "delta": np.zeros(n), "delta_ratio": np.zeros(n),
            "cum_delta": np.zeros(n),
        },
        index=idx,
    )


# ── DeltaDivergence ───────────────────────────────────────────────────────────

def _delta_div_entry_frame():
    df = _base(22)  # all same UTC day (22 * 15min < 24h)
    # pivot low bar (index 5): lowest low, most-negative cum_delta
    df.iloc[5, df.columns.get_loc("low")] = 95.0
    df.iloc[5, df.columns.get_loc("cum_delta")] = -50.0
    # current bar (last): new low, higher cum_delta, bullish candle
    li = len(df) - 1
    df.iloc[li, df.columns.get_loc("low")] = 94.0
    df.iloc[li, df.columns.get_loc("cum_delta")] = -40.0
    df.iloc[li, df.columns.get_loc("open")] = 100.0
    df.iloc[li, df.columns.get_loc("close")] = 100.5
    return df


def test_delta_divergence_entry_and_time_stop():
    s = DeltaDivergenceStrategy()
    sig = s.generate_signal(_delta_div_entry_frame())
    assert sig.action == "BUY" and s._position_open is True
    # time-stop: 16 benign bars later -> SELL
    df = _base(22 + 16)
    last = None
    for i in range(22, len(df)):
        last = s.generate_signal(df.iloc[:i + 1])
    assert last.action == "SELL" and s._position_open is False


def test_delta_divergence_target_exit():
    s = DeltaDivergenceStrategy()
    s.generate_signal(_delta_div_entry_frame())
    entry = s._entry_price
    atr = s._atr_entry
    df = _base(24, price=100.0)
    df.iloc[-1, df.columns.get_loc("close")] = entry + 3.0 * atr  # >= target
    sig = s.generate_signal(df)
    assert sig.action == "SELL" and "target" in sig.reason


def test_delta_divergence_never_sells_when_flat():
    s = DeltaDivergenceStrategy()
    df = _base(40)
    assert all(
        s.generate_signal(df.iloc[:i + 1]).action != "SELL"
        for i in range(20, len(df))
    )


# ── LiquiditySweepReversal ────────────────────────────────────────────────────

def _sweep_entry_frame():
    df = _base(25)
    # swing low over the prior-20-bar window (indices 4..23) = 96
    df.iloc[10, df.columns.get_loc("low")] = 96.0
    li = len(df) - 1
    # shallow sweep just below 96, close back inside, buy delta
    df.iloc[li, df.columns.get_loc("low")] = 95.7
    df.iloc[li, df.columns.get_loc("close")] = 100.0
    df.iloc[li, df.columns.get_loc("delta")] = 40.0
    return df


def test_liquidity_sweep_entry_and_stop():
    s = LiquiditySweepReversalStrategy()
    sig = s.generate_signal(_sweep_entry_frame())
    assert sig.action == "BUY" and s._position_open is True
    sweep_low = s._sweep_low
    atr = s._atr_entry
    df = _base(26)
    df.iloc[-1, df.columns.get_loc("close")] = sweep_low - 0.5 * atr  # below stop
    sig = s.generate_signal(df)
    assert sig.action == "SELL" and "stop" in sig.reason


def test_liquidity_sweep_rejects_deep_sweep():
    s = LiquiditySweepReversalStrategy()
    df = _sweep_entry_frame()
    df.iloc[-1, df.columns.get_loc("low")] = 80.0   # deep sweep (> 0.5*ATR)
    assert s.generate_signal(df).action == "HOLD"


def test_liquidity_sweep_never_sells_when_flat():
    s = LiquiditySweepReversalStrategy()
    df = _base(40)
    assert all(
        s.generate_signal(df.iloc[:i + 1]).action != "SELL"
        for i in range(21, len(df))
    )


# ── BreakoutDeltaConfirmed ────────────────────────────────────────────────────

def _breakout_entry_frame():
    df = _base(120, freq="1h")
    df["delta"] = 10.0                       # modest positive delta baseline
    li = len(df) - 1
    df.iloc[li, df.columns.get_loc("close")] = 105.0   # > range high (101)
    df.iloc[li, df.columns.get_loc("high")] = 105.5
    df.iloc[li, df.columns.get_loc("delta")] = 500.0   # top-quartile
    return df


def test_breakout_entry_and_fakeout_exit():
    s = BreakoutDeltaConfirmedStrategy()
    sig = s.generate_signal(_breakout_entry_frame())
    assert sig.action == "BUY" and s._position_open is True
    rng = s._range_high
    df = _base(122, freq="1h")
    df.iloc[-1, df.columns.get_loc("close")] = rng - 1.0   # back inside range
    sig = s.generate_signal(df)
    assert sig.action == "SELL" and "fakeout" in sig.reason


def test_breakout_requires_top_quartile_delta():
    s = BreakoutDeltaConfirmedStrategy()
    df = _breakout_entry_frame()
    df.iloc[-1, df.columns.get_loc("delta")] = 1.0   # below q75 -> no entry
    assert s.generate_signal(df).action == "HOLD"


def test_breakout_never_sells_when_flat():
    s = BreakoutDeltaConfirmedStrategy()
    df = _base(120, freq="1h")
    assert all(
        s.generate_signal(df.iloc[:i + 1]).action != "SELL"
        for i in range(101, len(df))
    )


# ── VWAPInstitutionalBand (vwap features injected) ────────────────────────────

def _vwap_feats(idx, vwap=100.0, lower_2=97.0):
    return pd.DataFrame(
        {"vwap": np.full(len(idx), vwap), "lower_2": np.full(len(idx), lower_2)},
        index=idx,
    )


def test_vwap_entry_and_revert_exit():
    df = _base(20)
    feats = _vwap_feats(df.index)
    # previous bar above band, current bar touches band with buy delta
    df.iloc[-2, df.columns.get_loc("close")] = 99.0     # > lower_2 (97)
    df.iloc[-1, df.columns.get_loc("close")] = 96.5     # <= lower_2
    df.iloc[-1, df.columns.get_loc("delta")] = 20.0
    s = VWAPInstitutionalBandStrategy(vwap_features=feats)
    sig = s.generate_signal(df)
    assert sig.action == "BUY" and s._position_open is True
    # revert to vwap -> SELL (fresh in-position instance on a new index)
    df2 = _base(21)
    df2.iloc[-1, df2.columns.get_loc("close")] = 100.5   # >= vwap
    s2 = VWAPInstitutionalBandStrategy(vwap_features=_vwap_feats(df2.index))
    s2._position_open = True
    s2._atr_entry = 1.0
    sig = s2.generate_signal(df2)
    assert sig.action == "SELL" and "vwap" in sig.reason


def test_vwap_never_sells_when_flat():
    df = _base(40)
    feats = _vwap_feats(df.index)
    s = VWAPInstitutionalBandStrategy(vwap_features=feats)
    assert all(
        s.generate_signal(df.iloc[:i + 1]).action != "SELL"
        for i in range(16, len(df))
    )


# ── VolumeProfileAcceptance (profile features injected) ───────────────────────

def _vah_feats(idx, vah=100.0):
    return pd.DataFrame(
        {"vah": np.full(len(idx), vah), "val": np.full(len(idx), 90.0),
         "poc": np.full(len(idx), 95.0)},
        index=idx,
    )


def test_vpa_entry_and_reject_exit():
    df = _base(40, freq="1h")
    feats = _vah_feats(df.index, vah=100.0)
    df.iloc[-2, df.columns.get_loc("close")] = 101.0   # above VAH
    df.iloc[-1, df.columns.get_loc("close")] = 102.0   # 2nd close above VAH
    df.iloc[-1, df.columns.get_loc("delta")] = 50.0    # > median (0)
    s = VolumeProfileAcceptanceStrategy(profile_features=feats)
    assert s.generate_signal(df).action == "BUY" and s._position_open
    # reject back below VAH -> SELL
    df2 = _base(41, freq="1h")
    df2.iloc[-1, df2.columns.get_loc("close")] = 98.0
    s2 = VolumeProfileAcceptanceStrategy(profile_features=_vah_feats(df2.index))
    s2._position_open = True
    sig = s2.generate_signal(df2)
    assert sig.action == "SELL" and "reject" in sig.reason


def test_vpa_needs_two_consecutive_closes():
    df = _base(40, freq="1h")
    feats = _vah_feats(df.index, vah=100.0)
    df.iloc[-2, df.columns.get_loc("close")] = 99.0    # prev NOT above VAH
    df.iloc[-1, df.columns.get_loc("close")] = 102.0
    df.iloc[-1, df.columns.get_loc("delta")] = 50.0
    s = VolumeProfileAcceptanceStrategy(profile_features=feats)
    assert s.generate_signal(df).action == "HOLD"


def test_vpa_never_sells_when_flat():
    df = _base(40, freq="1h")
    s = VolumeProfileAcceptanceStrategy(profile_features=_vah_feats(df.index))
    assert all(
        s.generate_signal(df.iloc[:i + 1]).action != "SELL"
        for i in range(31, len(df))
    )


# ── LVNTraversal (profile features injected) ──────────────────────────────────

def _node_feats(idx, hvn, lvn):
    return pd.DataFrame(
        {"hvn_prices": [list(hvn)] * len(idx), "lvn_prices": [list(lvn)] * len(idx)},
        index=idx,
    )


def test_lvn_entry_and_reach_hvn_exit():
    df = _base(20)
    feats = _node_feats(df.index, hvn=[95.0, 110.0], lvn=[102.0])
    df.iloc[-2, df.columns.get_loc("close")] = 101.0   # below LVN 102
    df.iloc[-1, df.columns.get_loc("close")] = 102.5   # enters LVN
    df.iloc[-1, df.columns.get_loc("delta")] = 30.0
    s = LVNTraversalStrategy(profile_features=feats)
    assert s.generate_signal(df).action == "BUY" and s._position_open
    # reach upper HVN -> SELL
    df2 = _base(21)
    feats2 = _node_feats(df2.index, hvn=[95.0, 110.0], lvn=[102.0])
    df2.iloc[-1, df2.columns.get_loc("close")] = 110.5
    s2 = LVNTraversalStrategy(profile_features=feats2)
    s2._position_open = True
    s2._atr_entry = 1.0
    s2._lvn_p = 102.0
    s2._hvn_up = 110.0
    sig = s2.generate_signal(df2)
    assert sig.action == "SELL" and "hvn" in sig.reason


def test_lvn_requires_flanking_hvns():
    df = _base(20)
    # LVN above price but NO HVN above it -> not tradeable
    feats = _node_feats(df.index, hvn=[95.0], lvn=[102.0])
    df.iloc[-2, df.columns.get_loc("close")] = 101.0
    df.iloc[-1, df.columns.get_loc("close")] = 102.5
    df.iloc[-1, df.columns.get_loc("delta")] = 30.0
    s = LVNTraversalStrategy(profile_features=feats)
    assert s.generate_signal(df).action == "HOLD"


def test_lvn_never_sells_when_flat():
    df = _base(30)
    feats = _node_feats(df.index, hvn=[95.0, 110.0], lvn=[102.0])
    s = LVNTraversalStrategy(profile_features=feats)
    assert all(
        s.generate_signal(df.iloc[:i + 1]).action != "SELL"
        for i in range(16, len(df))
    )


# ── HVNMeanReversion (profile features injected) ──────────────────────────────

def test_hvn_entry_and_target_exit():
    df = _base(20, freq="1h")
    feats = _node_feats(df.index, hvn=[99.0], lvn=[])
    df.iloc[-2, df.columns.get_loc("close")] = 100.0   # prior close above HVN 99
    df.iloc[-1, df.columns.get_loc("low")] = 99.0      # touches HVN from above
    df.iloc[-1, df.columns.get_loc("close")] = 99.5    # holds above
    df.iloc[-1, df.columns.get_loc("delta")] = 25.0
    s = HVNMeanReversionStrategy(profile_features=feats)
    assert s.generate_signal(df).action == "BUY" and s._position_open
    hvn_s = s._hvn_s
    atr = s._atr_entry
    df2 = _base(21, freq="1h")
    df2.iloc[-1, df2.columns.get_loc("close")] = hvn_s + 2.0 * atr  # >= target
    s2 = HVNMeanReversionStrategy(profile_features=_node_feats(df2.index, [99.0], []))
    s2._position_open = True
    s2._atr_entry = atr
    s2._hvn_s = hvn_s
    sig = s2.generate_signal(df2)
    assert sig.action == "SELL" and "target" in sig.reason


def test_hvn_never_sells_when_flat():
    df = _base(30, freq="1h")
    feats = _node_feats(df.index, hvn=[99.0], lvn=[])
    s = HVNMeanReversionStrategy(profile_features=feats)
    assert all(
        s.generate_signal(df.iloc[:i + 1]).action != "SELL"
        for i in range(16, len(df))
    )
