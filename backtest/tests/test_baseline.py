"""
backtest/tests/test_baseline.py — Tests for the buy-and-hold baseline.
"""

import math

import numpy as np
import pandas as pd
import pytest

from backtest.baseline import (
    BaselineError,
    BaselineResult,
    beats_baseline,
    buy_and_hold_sharpe,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_df(close: np.ndarray, freq: str = "1h", start: str = "2024-01-01") -> pd.DataFrame:
    idx = pd.date_range(start=start, periods=len(close), freq=freq, tz="UTC")
    idx.name = "timestamp"
    return pd.DataFrame({"close": close.astype(float)}, index=idx)


# ── 1. Uptrend → strong positive Sharpe + positive total return ──────────────

def test_uptrend_positive_sharpe():
    rng = np.random.default_rng(1)
    n = 1000
    t = np.arange(n)
    close = 100.0 * np.exp(t / 2000.0) + rng.normal(0, 0.05, n)
    df = _make_df(close)
    out = buy_and_hold_sharpe(df)
    assert out.sharpe > 1.0, f"uptrend Sharpe {out.sharpe} should be > 1.0"
    assert out.total_return > 0, (
        f"uptrend total_return {out.total_return} should be > 0"
    )


# ── 2. Downtrend → strong negative Sharpe + negative total return ────────────

def test_downtrend_negative_sharpe():
    rng = np.random.default_rng(2)
    n = 1000
    t = np.arange(n)
    close = 100.0 * np.exp(-t / 2000.0) + rng.normal(0, 0.05, n)
    df = _make_df(close)
    out = buy_and_hold_sharpe(df)
    assert out.sharpe < -1.0, (
        f"downtrend Sharpe {out.sharpe} should be < -1.0"
    )
    assert out.total_return < 0, (
        f"downtrend total_return {out.total_return} should be < 0"
    )


# ── 3. Flat random walk → Sharpe near zero ──────────────────────────────────

def test_flat_near_zero_sharpe():
    """Detrended random walk → realised mean exactly 0 → Sharpe near 0.

    A raw `rng.normal(0, σ, n)` series at hourly cadence has realised
    drift std ≈ σ·√n over n bars.  At σ=0.005, n=1000 that's a
    realised drift of order 0.16 — annualised it can swing well past
    ±1 by chance, so an undetrended random walk does NOT reliably
    test "no signal".  Detrending (subtract the sample mean) fixes
    realised drift to 0 by construction; the only residual Sharpe
    comes from per-bar covariance with the linear cumulative term,
    which is small.
    """
    rng = np.random.default_rng(3)
    n = 1000
    rets = rng.normal(0.0, 0.005, n)
    rets = rets - rets.mean()  # detrend: realised mean = 0 exactly
    close = 100.0 * np.cumprod(1.0 + rets)
    df = _make_df(close)
    out = buy_and_hold_sharpe(df)
    assert abs(out.sharpe) < 1.0, (
        f"flat (detrended) random walk Sharpe {out.sharpe} should be |·| < 1.0"
    )


# ── 4. n_bars equals len(df) - 1 (one bar lost to pct_change) ────────────────

def test_n_bars_equals_len_minus_one():
    n = 250
    close = np.linspace(100.0, 110.0, n)
    df = _make_df(close)
    out = buy_and_hold_sharpe(df)
    assert out.n_bars == n - 1


# ── 5. candle_hours inferred from index spacing ─────────────────────────────

def test_candle_hours_inferred():
    close = np.linspace(100.0, 110.0, 100)

    df_1h = _make_df(close, freq="1h")
    out_1h = buy_and_hold_sharpe(df_1h)
    assert out_1h.candle_hours == pytest.approx(1.0)

    df_4h = _make_df(close, freq="4h")
    out_4h = buy_and_hold_sharpe(df_4h)
    assert out_4h.candle_hours == pytest.approx(4.0)


# ── 6. Sharpe matches cpcv.py's helper exactly (formula-equivalence anchor) ─

def test_sharpe_matches_cpcv_helper():
    """Formula-equivalence anchor against the sacred module.  If
    `backtest.cpcv._sharpe_from_returns` ever changes its formula,
    this test breaks and forces a corresponding update here."""
    from backtest.cpcv import _sharpe_from_returns as cpcv_sharpe

    rng = np.random.default_rng(99)
    rets = rng.normal(0.0005, 0.01, 500)

    # Construct close path so that close.pct_change() reproduces `rets`
    # exactly: close[i+1] = close[i] · (1 + rets[i]).
    close = np.empty(rets.size + 1, dtype=float)
    close[0] = 100.0
    for i, r in enumerate(rets):
        close[i + 1] = close[i] * (1.0 + r)

    df = _make_df(close, freq="1h")
    out = buy_and_hold_sharpe(df)
    expected = cpcv_sharpe(rets, candle_duration_h=out.candle_hours)
    assert out.sharpe == pytest.approx(expected, abs=1e-9, rel=1e-9), (
        f"baseline Sharpe {out.sharpe} != cpcv Sharpe {expected}"
    )


# ── 7. Empty frame raises ────────────────────────────────────────────────────

def test_empty_frame_raises():
    with pytest.raises(BaselineError, match="empty"):
        buy_and_hold_sharpe(pd.DataFrame())


# ── 8. Missing close column raises with column name in message ──────────────

def test_missing_close_column_raises():
    df = pd.DataFrame(
        {"open": [1.0, 2.0]},
        index=pd.date_range("2024-01-01", periods=2, freq="1h", tz="UTC"),
    )
    with pytest.raises(BaselineError, match="close"):
        buy_and_hold_sharpe(df)


# ── 9. Single-bar frame raises ──────────────────────────────────────────────

def test_single_bar_raises():
    df = _make_df(np.array([100.0]))
    with pytest.raises(BaselineError, match="2 bars"):
        buy_and_hold_sharpe(df)


# ── 10. Non-finite close prices raise ───────────────────────────────────────

def test_non_finite_close_raises():
    df_inf = _make_df(np.array([100.0, np.inf, 102.0, 103.0]))
    with pytest.raises(BaselineError, match="non-finite"):
        buy_and_hold_sharpe(df_inf)

    df_nan = _make_df(np.array([100.0, np.nan, 102.0, 103.0]))
    with pytest.raises(BaselineError, match="non-finite"):
        buy_and_hold_sharpe(df_nan)


# ── 11. Custom close column name ────────────────────────────────────────────

def test_custom_close_col():
    close = np.linspace(100.0, 110.0, 50)
    idx = pd.date_range("2024-01-01", periods=50, freq="1h", tz="UTC")
    df = pd.DataFrame({"Close": close}, index=idx)
    out = buy_and_hold_sharpe(df, close_col="Close")
    assert isinstance(out, BaselineResult)
    assert out.n_bars == 49
    assert out.total_return == pytest.approx(0.10, abs=1e-9)


# ── 12. beats_baseline is strict (>, not ≥) ─────────────────────────────────

def test_beats_baseline_strict():
    assert beats_baseline(1.01, 1.0) is True
    assert beats_baseline(1.0, 1.0) is False
    assert beats_baseline(0.99, 1.0) is False


# ── 13. beats_baseline returns False on non-finite Sharpes ──────────────────

def test_beats_baseline_handles_non_finite():
    assert beats_baseline(float("nan"), 0.5) is False
    assert beats_baseline(0.5, float("nan")) is False
    assert beats_baseline(float("inf"), 1.0) is False
    assert beats_baseline(1.0, float("-inf")) is False


# ── 14. Total return compounds correctly ────────────────────────────────────

def test_total_return_compounds_correctly():
    """Close path [100, 110, 121] → returns [0.10, 0.10] →
    total = 1.10 · 1.10 - 1 = 0.21."""
    df = _make_df(np.array([100.0, 110.0, 121.0]))
    out = buy_and_hold_sharpe(df)
    assert out.total_return == pytest.approx(0.21, abs=1e-12)
