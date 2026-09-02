"""
backtest/tests/test_engine_cs.py — cross-sectional perp book engine.

Synthetic 3-symbol panels only; no cache, no network, no trials.log.
Covers the seven behaviours the engine contract promises:

  (a) funding sign      — long pays positive funding, short receives
  (b) fee accounting    — exact taker fee on a known rebalance
  (c) beta hedge        — engine-computed BTC hedge neutralises book beta
  (d) delisting         — mask False force-closes the leg
  (e) liquidation       — per-leg maintenance-margin breach
  (f) fee stress        — fee_multiplier=2 doubles the fee drag
  (g) return series     — length/index match the panel and reconcile
                          with the equity curve

Plus: funding-timestamp millisecond-jitter flooring, and the
CPCV-adapter contract (`CrossSectionalResult` is a `BacktestResult`
whose `equity_curve.pct_change().dropna()` feeds
`cpcv_common._sharpe_from_returns` unchanged).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backtest.cpcv_common import _sharpe_from_returns
from backtest.engine import BacktestResult
from backtest.engine_cs import (
    CrossSectionalResult,
    align_funding_to_bars,
    liquidation_price,
    run_engine_cs,
)

SYMBOLS = ["BTCUSDT", "AAAUSDT", "BBBUSDT"]


# ── Fixtures / helpers ───────────────────────────────────────────────────────

def _index(n: int, freq: str = "1D") -> pd.DatetimeIndex:
    return pd.date_range("2020-01-01", periods=n, freq=freq, tz="UTC")


def _flat_panel(n: int = 6, price: float = 100.0) -> dict:
    idx = _index(n)
    return {
        sym: pd.DataFrame({"close": np.full(n, price)}, index=idx)
        for sym in SYMBOLS
    }


def _jittered_funding(idx: pd.DatetimeIndex, rate: float) -> pd.Series:
    """One settlement per bar, stamped with the millisecond jitter the
    Binance UM archive carries (`00:00:00.002`)."""
    ts = idx + pd.Timedelta(milliseconds=2)
    return pd.Series(np.full(len(idx), rate), index=ts)


class FixedWeights:
    """Strategy stub: returns the same target book every rebalance."""

    name = "FixedWeightsStub"

    def __init__(self, weights: dict, beta_hedge: bool = False,
                 hedge_symbol: str = "BTCUSDT"):
        self.weights = dict(weights)
        self.beta_hedge = beta_hedge
        self.hedge_symbol = hedge_symbol
        self.calls: list = []

    def target_weights(self, t, panel_upto_t):
        self.calls.append((t, len(panel_upto_t)))
        return dict(self.weights)


# ── (a) funding sign ─────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "weight,expected_sign",
    [(0.5, -1.0), (-0.5, +1.0)],
    ids=["long-pays", "short-receives"],
)
def test_funding_sign_long_pays_short_receives(weight, expected_sign):
    n = 5
    data = _flat_panel(n)
    idx = data["AAAUSDT"].index
    rate = 0.01
    funding = {"AAAUSDT": _jittered_funding(idx, rate)}

    res = run_engine_cs(
        data,
        FixedWeights({"AAAUSDT": weight}),
        funding=funding,
        initial_balance=10_000.0,
        fee_taker=0.0,
        slippage=0.0,
        rebalance_every=1000,   # rebalance at bar 0 only
        close_at_end=False,
    )

    # qty = 10_000 * |w| / 100 = 50 units; |funding| = 50 * 100 * 0.01 = 50
    per_bar = expected_sign * 50.0
    assert res.funding_pnl_series.iloc[0] == pytest.approx(0.0)
    for i in range(1, n):
        assert res.funding_pnl_series.iloc[i] == pytest.approx(per_bar)
    assert res.funding_pnl == pytest.approx(per_bar * (n - 1))
    # Price is flat, fees are zero → equity moves on funding alone.
    assert res.price_pnl == pytest.approx(0.0)
    assert res.equity_curve.iloc[-1] == pytest.approx(
        10_000.0 + per_bar * (n - 1))


def test_funding_timestamp_jitter_is_floored_to_the_hour():
    """`00:00:00.002` must accrue in the 00:00 bar, not the next one."""
    idx = _index(3)
    funding = {"AAAUSDT": pd.Series(
        [0.01, 0.02, 0.03],
        index=pd.DatetimeIndex([
            idx[0] + pd.Timedelta(milliseconds=2),
            idx[1] + pd.Timedelta(milliseconds=999),
            idx[1] + pd.Timedelta(hours=8),
        ]),
    )}
    aligned = align_funding_to_bars(funding, idx, ["AAAUSDT"])
    assert aligned["AAAUSDT"].tolist() == pytest.approx([0.01, 0.02, 0.03])

    # Without flooring, the 2 ms stamp leaks out of bar 0 into bar 1.
    unfloored = align_funding_to_bars(
        funding, idx, ["AAAUSDT"], floor_freq="ns")
    assert unfloored["AAAUSDT"].iloc[0] == pytest.approx(0.0)
    assert unfloored["AAAUSDT"].iloc[1] == pytest.approx(0.01)
    assert unfloored["AAAUSDT"].iloc[2] == pytest.approx(0.05)


# ── (b) fee accounting on a known rebalance ──────────────────────────────────

def test_fee_accounting_on_a_known_rebalance():
    data = _flat_panel(4)
    res = run_engine_cs(
        data,
        FixedWeights({"AAAUSDT": 0.5, "BBBUSDT": -0.5}),
        initial_balance=10_000.0,
        fee_taker=0.0005,
        slippage=0.0005,
        rebalance_every=1000,
        close_at_end=False,
    )
    # long  : qty +50 @ fill 100*(1+5e-4)=100.05 → notional 5002.50
    # short : qty -50 @ fill 100*(1-5e-4)= 99.95 → notional 4997.50
    # fee   : 0.0005 * (5002.50 + 4997.50) = 5.00 exactly
    assert res.fee_series.iloc[0] == pytest.approx(5.0, abs=1e-9)
    assert res.turnover.iloc[0] == pytest.approx(10_000.0, abs=1e-9)
    assert res.equity_curve.iloc[0] == pytest.approx(9_995.0, abs=1e-9)
    assert res.total_fees == pytest.approx(5.0, abs=1e-9)
    # No further trading and flat prices → equity is unchanged after.
    assert res.equity_curve.iloc[-1] == pytest.approx(9_995.0, abs=1e-9)
    assert res.n_trades == 2   # two leg opens


# ── (c) beta-hedge neutrality ────────────────────────────────────────────────

def test_beta_hedge_neutralises_constructed_book_beta():
    """AAA = 2× BTC returns, BBB = 0.5× BTC returns, exactly.

    Book (+0.25 AAA, −0.25 BBB) has β = 0.25·2 − 0.25·0.5 = 0.375, so
    the engine must add −0.375 in BTCUSDT.
    """
    n = 60
    idx = _index(n)
    rng = np.random.default_rng(20260902)
    r_btc = rng.normal(0.0, 0.01, size=n)
    r_btc[0] = 0.0

    def _prices(mult: float) -> np.ndarray:
        return 100.0 * np.cumprod(1.0 + mult * r_btc)

    data = {
        "BTCUSDT": pd.DataFrame({"close": _prices(1.0)}, index=idx),
        "AAAUSDT": pd.DataFrame({"close": _prices(2.0)}, index=idx),
        "BBBUSDT": pd.DataFrame({"close": _prices(0.5)}, index=idx),
    }
    strat = FixedWeights(
        {"AAAUSDT": 0.25, "BBBUSDT": -0.25}, beta_hedge=True)
    res = run_engine_cs(
        data, strat,
        initial_balance=10_000.0,
        fee_taker=0.0, slippage=0.0,
        rebalance_every=1,
        beta_lookback=30,
        close_at_end=False,
    )
    late = [ts for ts in res.weights_history if ts >= idx[35]]
    assert late, "expected rebalances after the beta lookback warms up"
    for ts in late:
        w = res.weights_history[ts]
        assert w["BTCUSDT"] == pytest.approx(-0.375, abs=1e-9)
        assert w["AAAUSDT"] == pytest.approx(0.25)
        assert w["BBBUSDT"] == pytest.approx(-0.25)

    # Sanity: with the hedge off, no BTC leg is taken at all.
    strat_nohedge = FixedWeights({"AAAUSDT": 0.25, "BBBUSDT": -0.25})
    res_nohedge = run_engine_cs(
        data, strat_nohedge, initial_balance=10_000.0,
        fee_taker=0.0, slippage=0.0, rebalance_every=1, close_at_end=False,
    )
    assert all(
        "BTCUSDT" not in w for w in res_nohedge.weights_history.values())


def test_gross_weight_cap_is_enforced_on_strategy_weights():
    data = _flat_panel(4)
    with pytest.raises(ValueError, match="gross cap"):
        run_engine_cs(
            data,
            FixedWeights({"AAAUSDT": 0.8, "BBBUSDT": -0.8}),
            initial_balance=10_000.0,
        )


# ── (d) forced close on delisting ────────────────────────────────────────────

def test_forced_close_when_symbol_leaves_the_universe():
    n = 6
    data = _flat_panel(n)
    idx = data["AAAUSDT"].index
    mask = pd.DataFrame(True, index=idx, columns=SYMBOLS)
    mask.loc[idx[3]:, "AAAUSDT"] = False   # delisted from bar 3

    res = run_engine_cs(
        data,
        FixedWeights({"AAAUSDT": 0.5}),
        universe_mask=mask,
        initial_balance=10_000.0,
        fee_taker=0.0005,
        slippage=0.0,
        rebalance_every=1,
        close_at_end=False,
    )
    assert len(res.forced_close_events) == 1
    ev = res.forced_close_events[0]
    assert ev.symbol == "AAAUSDT"
    assert ev.timestamp == idx[3]
    assert ev.reason == "ineligible"
    assert ev.exit_price == pytest.approx(100.0)
    # After the force-close the name is never re-entered.
    for ts in [t for t in res.weights_history if t >= idx[3]]:
        assert "AAAUSDT" not in res.weights_history[ts]
    assert res.gross_exposure.iloc[-1] == pytest.approx(0.0)
    # Exactly one leg open (bar 0); no re-opens after the delisting.
    assert res.n_trades == 1
    # The final realised leg is the forced close, and nothing trades after.
    assert res.trade_history[-1].reason == "ineligible"
    assert res.trade_history[-1].exit_time == idx[3]
    assert all(t.exit_time <= idx[3] for t in res.trade_history)
    assert res.turnover.iloc[4:].sum() == pytest.approx(0.0)


def test_forced_close_uses_last_available_close_when_the_bar_is_missing():
    """A symbol whose bars simply stop (no row at all) is closed at its
    last available close, not silently carried."""
    n = 6
    idx = _index(n)
    data = {
        sym: pd.DataFrame({"close": np.full(n, 100.0)}, index=idx)
        for sym in SYMBOLS
    }
    # AAA's archive ends after bar 2 at a price of 120.
    data["AAAUSDT"] = pd.DataFrame(
        {"close": [100.0, 110.0, 120.0]}, index=idx[:3])

    res = run_engine_cs(
        data,
        FixedWeights({"AAAUSDT": 0.5}),
        initial_balance=10_000.0,
        fee_taker=0.0, slippage=0.0,
        rebalance_every=1000,
        close_at_end=False,
    )
    assert len(res.forced_close_events) == 1
    ev = res.forced_close_events[0]
    assert ev.symbol == "AAAUSDT"
    assert ev.timestamp == idx[3]
    assert ev.exit_price == pytest.approx(120.0)


# ── (e) liquidation trigger ──────────────────────────────────────────────────

def test_liquidation_price_formula_matches_the_risk_model():
    # research/funding-rate-risk-model.md §2.2: S_liq = S0(1+1/L)/(1+mr)
    assert liquidation_price(100.0, False, 5.0, 0.005) == pytest.approx(
        100.0 * 1.2 / 1.005)
    # Long side is the symmetric derivation.
    assert liquidation_price(100.0, True, 5.0, 0.005) == pytest.approx(
        100.0 * 0.8 / 0.995)


def test_short_leg_liquidation_is_triggered_and_penalised():
    idx = _index(4)
    prices = np.array([100.0, 101.0, 140.0, 141.0])
    data = {
        "BTCUSDT": pd.DataFrame({"close": np.full(4, 100.0)}, index=idx),
        "AAAUSDT": pd.DataFrame({"close": prices}, index=idx),
        "BBBUSDT": pd.DataFrame({"close": np.full(4, 100.0)}, index=idx),
    }
    res = run_engine_cs(
        data,
        FixedWeights({"AAAUSDT": -0.5}),
        initial_balance=10_000.0,
        fee_taker=0.0005,
        slippage=0.0,
        leverage=3.0,
        maintenance_margin_ratio=0.01,
        liquidation_penalty=0.005,
        rebalance_every=1000,
        close_at_end=False,
    )
    assert len(res.liquidation_events) == 1
    ev = res.liquidation_events[0]
    assert ev.symbol == "AAAUSDT"
    assert ev.side == "short"
    assert ev.timestamp == idx[2]
    # entry 100 (zero slippage), L=3, mr=0.01 → S_liq = 100·(4/3)/1.01
    assert ev.liquidation_price == pytest.approx(100.0 * (4 / 3) / 1.01)
    assert ev.exit_price == pytest.approx(140.0)
    assert ev.notional == pytest.approx(50.0 * 140.0)
    assert ev.penalty == pytest.approx(0.005 * 50.0 * 140.0)
    assert res.total_penalties == pytest.approx(0.005 * 50.0 * 140.0)
    # Book is flat afterwards and the leg is never re-opened.
    assert res.gross_exposure.iloc[-1] == pytest.approx(0.0)
    assert [t.reason for t in res.trade_history] == ["liquidation"]

    # A tighter maintenance margin cannot make the breach disappear, and
    # a leg that never breaches records no event.
    calm = run_engine_cs(
        {k: v.iloc[:2] for k, v in data.items()},
        FixedWeights({"AAAUSDT": -0.5}),
        initial_balance=10_000.0,
        fee_taker=0.0005, slippage=0.0,
        leverage=3.0, maintenance_margin_ratio=0.01,
        rebalance_every=1000, close_at_end=False,
    )
    assert calm.liquidation_events == []


# ── (f) fee multiplier ───────────────────────────────────────────────────────

def test_fee_multiplier_doubles_the_fee_drag():
    data = _flat_panel(6)
    kwargs = dict(
        initial_balance=10_000.0,
        fee_taker=0.0005,
        slippage=0.0005,
        rebalance_every=1,
    )
    base = run_engine_cs(
        data, FixedWeights({"AAAUSDT": 0.5, "BBBUSDT": -0.5}), **kwargs)
    stressed = run_engine_cs(
        data, FixedWeights({"AAAUSDT": 0.5, "BBBUSDT": -0.5}),
        fee_multiplier=2.0, **kwargs)

    # First bar starts from identical equity → exactly 2×.
    assert stressed.fee_series.iloc[0] == pytest.approx(
        2.0 * base.fee_series.iloc[0], abs=1e-9)
    # Over the whole run the ratio is 2× up to the compounding of the
    # smaller equity base the stressed run trades from.
    assert stressed.total_fees / base.total_fees == pytest.approx(
        2.0, rel=0.01)
    assert stressed.equity_curve.iloc[-1] < base.equity_curve.iloc[-1]


# ── (g) return-series shape and equity reconciliation ────────────────────────

def test_return_series_matches_panel_index_and_reconciles_with_equity():
    n = 40
    idx = _index(n)
    rng = np.random.default_rng(7)
    data = {
        sym: pd.DataFrame(
            {"close": 100.0 * np.cumprod(
                1.0 + rng.normal(0.0, 0.02, size=n))},
            index=idx,
        )
        for sym in SYMBOLS
    }
    funding = {
        sym: _jittered_funding(idx, 0.0001) for sym in SYMBOLS
    }
    res = run_engine_cs(
        data,
        FixedWeights({"AAAUSDT": 0.4, "BBBUSDT": -0.4}),
        funding=funding,
        initial_balance=10_000.0,
        rebalance_every=2,
    )

    assert len(res.returns) == n
    assert res.returns.index.equals(idx)
    assert res.equity_curve.index.equals(idx)
    assert len(res.turnover) == n
    assert len(res.funding_pnl_series) == n
    assert len(res.price_pnl_series) == n
    assert len(res.fee_series) == n

    rebuilt = 10_000.0 * (1.0 + res.returns).cumprod()
    pd.testing.assert_series_equal(
        rebuilt, res.equity_curve, check_names=False, rtol=1e-12,
    )
    # PnL decomposition adds up to the equity change.
    assert (
        res.price_pnl + res.funding_pnl - res.total_fees
    ) == pytest.approx(res.equity_curve.iloc[-1] - 10_000.0, abs=1e-6)
    assert res.total_turnover > 0.0


# ── CPCV / per-bar-store adapter contract ────────────────────────────────────

def test_result_satisfies_the_backtest_result_contract():
    n = 40
    idx = _index(n)
    rng = np.random.default_rng(11)
    data = {
        sym: pd.DataFrame(
            {"close": 100.0 * np.cumprod(
                1.0 + rng.normal(0.0005, 0.02, size=n))},
            index=idx,
        )
        for sym in SYMBOLS
    }
    res = run_engine_cs(
        data,
        FixedWeights({"AAAUSDT": 0.4, "BBBUSDT": -0.4}),
        period_label="cpcv-cs-block-0",
    )
    assert isinstance(res, CrossSectionalResult)
    assert isinstance(res, BacktestResult)
    assert res.period_label == "cpcv-cs-block-0"
    assert res.metrics.total_trades == res.n_trades > 0

    # The exact call cpcv_multi makes per block.
    rets = res.equity_curve.pct_change().dropna().values.astype(float)
    assert rets.size == n - 1
    sharpe = _sharpe_from_returns(rets, 24.0)
    assert np.isfinite(sharpe)


def test_wide_close_panel_input_is_accepted():
    n = 12
    idx = _index(n)
    wide = pd.DataFrame(
        {sym: np.full(n, 100.0) for sym in SYMBOLS}, index=idx)
    res = run_engine_cs(
        wide,
        FixedWeights({"AAAUSDT": 0.5}),
        initial_balance=10_000.0,
        fee_taker=0.0, slippage=0.0,
        rebalance_every=1000, close_at_end=False,
    )
    assert res.symbols == SYMBOLS
    assert res.equity_curve.iloc[-1] == pytest.approx(10_000.0)


def test_funding_and_eligibility_may_ride_on_the_frames():
    """Column form — what a block-slicing CPCV runner needs."""
    n = 5
    idx = _index(n)
    data = {}
    for sym in SYMBOLS:
        data[sym] = pd.DataFrame(
            {
                "close": np.full(n, 100.0),
                "funding_rate": np.full(n, 0.01),
                "eligible": np.full(n, True),
            },
            index=idx,
        )
    data["AAAUSDT"].loc[idx[3]:, "eligible"] = False

    res = run_engine_cs(
        data,
        FixedWeights({"AAAUSDT": 0.5}),
        initial_balance=10_000.0,
        fee_taker=0.0, slippage=0.0,
        rebalance_every=1000, close_at_end=False,
    )
    # Bars 1-3 accrue funding on the long leg (−50 each), then it closes.
    assert res.funding_pnl_series.iloc[1] == pytest.approx(-50.0)
    assert len(res.forced_close_events) == 1
    assert res.forced_close_events[0].timestamp == idx[3]
