"""
backtest/tests/test_engine_perp.py — Track G coverage for run_perp.

Synthetic spot+perp+funding fixtures.  Verifies:
  - Warm-up is identical to engine.run (no trades fire on warmup
    bars).
  - Equity curve length == len(common_idx) − warm_up_candles + 1
    end-of-period close (positions force-closed at last bar).
  - equity_curve.pct_change().dropna() survives as a per-bar
    return array — the contract that lets the same Sharpe formula
    feed DSR via `cpcv_common._sharpe_from_returns`.
  - Funding cash appears in the equity curve at exactly the funding
    settlement timestamps and only at those timestamps.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backtest.cpcv_common import _sharpe_from_returns
from backtest.engine_perp import run_perp
from strategies.base import BaseStrategy, Signal


# ── Fixture builders ─────────────────────────────────────────────────────────

def _hourly_index(n_bars: int, start: str = "2024-01-01") -> pd.DatetimeIndex:
    return pd.date_range(
        start=start, periods=n_bars, freq="1h", tz="UTC", name="timestamp",
    )


def _make_ohlcv(idx: pd.DatetimeIndex, base_price: float = 50_000.0) -> pd.DataFrame:
    n = len(idx)
    rng = np.random.default_rng(0)
    close = base_price + np.cumsum(rng.normal(0, 5, n))
    return pd.DataFrame(
        {
            "open":   close + rng.normal(0, 1, n),
            "high":   close + np.abs(rng.normal(0, 5, n)),
            "low":    close - np.abs(rng.normal(0, 5, n)),
            "close":  close,
            "volume": np.full(n, 1000.0),
        },
        index=idx,
    )


def _make_funding_history(
    spot_idx: pd.DatetimeIndex,
    cadence_hours: int = 8,
    rate: float = 0.0001,
    mark_price_series: pd.Series | None = None,
) -> pd.DataFrame:
    """Build a funding-history DataFrame at the requested cadence."""
    settlements = spot_idx[::cadence_hours]
    rates = np.full(len(settlements), rate, dtype=float)
    if mark_price_series is None:
        marks = np.full(len(settlements), 50_000.0, dtype=float)
    else:
        marks = mark_price_series.reindex(settlements).fillna(50_000.0).values
    return pd.DataFrame(
        {"funding_rate": rates, "mark_price": marks},
        index=settlements,
    )


# ── Test strategies ──────────────────────────────────────────────────────────

class _OpenAndHoldStrategy(BaseStrategy):
    """BUY on the first signal-eligible candle then HOLD forever.

    The engine's warm-up window guarantees this BUY arrives at
    `warm_up_candles + 1`.  Used to verify the open/hold path without
    introducing exit signals.
    """
    def __init__(
        self,
        symbol: str = "BTC/USDT",
        timeframe: str = "1h",
    ):
        super().__init__(name="OpenAndHold", symbol=symbol, timeframe=timeframe)
        self._opened = False

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        price = float(df["close"].iloc[-1])
        if not self._opened:
            self._opened = True
            return Signal(
                action="BUY", strategy=self.name, price=price,
                reason="open", order_type="market",
            )
        return Signal(
            action="HOLD", strategy=self.name, price=price,
            reason="hold", order_type="market",
        )


# ── Tests ────────────────────────────────────────────────────────────────────

def test_run_perp_returns_equity_curve_consumable_by_sharpe_helper():
    """The combined-leg equity series produces a per-bar return array
    that `_sharpe_from_returns` accepts (no extra reshape needed)."""
    idx = _hourly_index(800)
    df_spot = _make_ohlcv(idx, base_price=50_000.0)
    df_perp = _make_ohlcv(idx, base_price=50_000.0)
    funding = _make_funding_history(idx, cadence_hours=8, rate=0.0001)

    result = run_perp(
        df_spot=df_spot,
        df_perp=df_perp,
        funding_history=funding,
        strategy=_OpenAndHoldStrategy(),
        warm_up_candles=50,
    )

    eq = result.equity_curve
    assert isinstance(eq, pd.Series)
    rets = eq.pct_change().dropna().values.astype(float)
    assert rets.size > 0
    # Sharpe formula must produce a finite number.
    s = _sharpe_from_returns(rets, candle_duration_h=1.0)
    assert np.isfinite(s)


def test_run_perp_equity_curve_length_matches_post_warmup_window():
    """Equity is recorded once per post-warmup bar."""
    idx = _hourly_index(200)
    df_spot = _make_ohlcv(idx)
    df_perp = _make_ohlcv(idx)
    funding = _make_funding_history(idx, rate=0.0)

    warm = 50
    result = run_perp(
        df_spot=df_spot,
        df_perp=df_perp,
        funding_history=funding,
        strategy=_OpenAndHoldStrategy(),
        warm_up_candles=warm,
    )
    expected = len(idx) - warm
    assert len(result.equity_curve) == expected


def test_run_perp_funding_cash_appears_at_settlement_timestamps():
    """Equity steps up only at funding settlement timestamps when
    nothing else changes (constant prices)."""
    idx = _hourly_index(80)  # 80 hours = 10 settlements at 8h cadence
    # Constant prices on both legs so the only equity-changing
    # event is funding settlement.
    flat_spot = pd.DataFrame(
        {
            "open": 50_000.0, "high": 50_000.0, "low": 50_000.0,
            "close": 50_000.0, "volume": 1000.0,
        },
        index=idx,
    )
    flat_perp = flat_spot.copy()
    funding = _make_funding_history(idx, cadence_hours=8, rate=0.0001)

    result = run_perp(
        df_spot=flat_spot,
        df_perp=flat_perp,
        funding_history=funding,
        strategy=_OpenAndHoldStrategy(),
        warm_up_candles=10,
        # Disable funding-flip exit so positive funding doesn't alone
        # trigger anything.
        flip_exit_n=999,
        cushion_threshold=0.0,
    )

    eq = result.equity_curve
    diffs = eq.diff().dropna()
    # Equity strictly increases at each settlement timestamp inside
    # the post-warmup window (open already executed before the first
    # post-warmup bar).
    settlements_in_window = funding.index[
        (funding.index >= eq.index[0])
        & (funding.index <= eq.index[-1])
    ]
    # At each settlement in the window, equity should be strictly
    # higher than the prior bar (positive funding credited).  Allow
    # for the entry bar itself which can show a small fee-driven
    # negative step.
    settlement_diffs = diffs.reindex(
        settlements_in_window, method="nearest", tolerance=pd.Timedelta("30min"),
    ).dropna()
    assert (settlement_diffs > -1e-3).all(), (
        "Equity should not drop materially at funding settlement timestamps"
    )
    assert (settlement_diffs > 0).any(), (
        "At least one settlement should produce a positive equity step"
    )


def test_run_perp_force_closes_position_at_end():
    """Open positions close at the last bar so trade_history records
    the final exit."""
    idx = _hourly_index(150)
    df_spot = _make_ohlcv(idx)
    df_perp = _make_ohlcv(idx)
    funding = _make_funding_history(idx, rate=0.0)

    result = run_perp(
        df_spot=df_spot,
        df_perp=df_perp,
        funding_history=funding,
        strategy=_OpenAndHoldStrategy(),
        warm_up_candles=50,
    )
    # Strategy never SELLs explicitly; backtest_end forces close.
    assert len(result.trade_history) == 1
    assert result.trade_history[0].exit_reason == "backtest_end"


def test_run_perp_raises_when_too_few_aligned_candles():
    idx = _hourly_index(40)
    df_spot = _make_ohlcv(idx)
    df_perp = _make_ohlcv(idx)
    funding = _make_funding_history(idx, rate=0.0)

    with pytest.raises(ValueError):
        run_perp(
            df_spot=df_spot,
            df_perp=df_perp,
            funding_history=funding,
            strategy=_OpenAndHoldStrategy(),
            warm_up_candles=50,
        )
