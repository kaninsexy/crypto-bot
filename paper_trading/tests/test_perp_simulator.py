"""
paper_trading/tests/test_perp_simulator.py

Track F coverage for the two-leg PerpSimulator.  All tests are
fully synthetic — no OKX network calls.  Asserts the binding
behaviour from research/funding-rate-risk-model.md:

  - § 5: equal-notional construction at entry.
  - § 3: funding sign convention (positive funding → bot receives).
  - § 4.1: funding-flip exit after N consecutive negative
    settlements.
  - § 4.2: maintenance-margin cushion exit.
  - § 2.3: cross- vs isolated-margin liquidation distance.
  - § 5: combined-position sanity check at exit.
"""

from __future__ import annotations

import pytest

from paper_trading.base_simulator import BaseSimulator
from paper_trading.perp_simulator import PerpSimulator, PerpPosition
from strategies.base import Signal


def _buy(reason: str = "open", price: float = 50_000.0) -> Signal:
    return Signal(
        action="BUY",
        strategy="FundingRateHarvest",
        price=price,
        reason=reason,
        order_type="market",
    )


def _sell(reason: str = "close", price: float = 50_000.0) -> Signal:
    return Signal(
        action="SELL",
        strategy="FundingRateHarvest",
        price=price,
        reason=reason,
        order_type="market",
    )


def _make_sim(initial_balance: float = 10_000.0) -> PerpSimulator:
    return PerpSimulator(
        initial_balance=initial_balance,
        spot_symbol="BTC/USDT",
        perp_symbol="BTC/USDT",
        leverage=5.0,
        flip_exit_n=3,
    )


# ── Protocol satisfaction ────────────────────────────────────────────────────

def test_perp_simulator_satisfies_base_simulator_protocol():
    sim = _make_sim()
    assert isinstance(sim, BaseSimulator)


# ── Entry: equal-notional ────────────────────────────────────────────────────

def test_open_constructs_equal_notional_legs():
    """§ 5: at entry, spot notional == perp notional → zero net delta."""
    sim = _make_sim(initial_balance=10_000.0)
    sim.update_spot_close(50_000.0)
    sim.execute_signal(_buy(), current_price=50_000.0)

    pos = sim.position
    assert pos is not None

    spot_notional = pos.spot_quantity * pos.spot_entry_price
    perp_notional = pos.perp_quantity * pos.perp_entry_price
    assert spot_notional == pytest.approx(perp_notional, rel=1e-9)
    # Both equal the recorded entry notional.
    assert spot_notional == pytest.approx(pos.entry_notional, rel=1e-9)


def test_open_consumes_balance_to_capacity():
    """Per § 5 + capital-allocation note in the docstring: total
    capital consumed at entry should match the closed-form
    `notional + notional/leverage + 2*taker_fee*notional` formula
    given the balance, leaving balance ≈ 0 (or within float slop)."""
    sim = _make_sim(initial_balance=10_000.0)
    sim.update_spot_close(50_000.0)
    sim.execute_signal(_buy(), current_price=50_000.0)

    # After opening, balance should be close to zero — the simulator
    # sized legs to consume exactly `initial_balance`.
    assert abs(sim.balance) < 1.0  # within a USDT of zero


def test_open_without_spot_price_raises():
    """update_spot_close must be called before BUY."""
    sim = _make_sim()
    with pytest.raises(ValueError):
        sim.execute_signal(_buy(), current_price=50_000.0)


# ── Funding accrual & sign convention ────────────────────────────────────────

def test_positive_funding_credits_short_leg():
    """§ 3.1: funding > 0 → bot (short side) receives cash."""
    sim = _make_sim()
    sim.update_spot_close(50_000.0)
    sim.execute_signal(_buy(), current_price=50_000.0)
    perp_qty = sim.position.perp_quantity

    funding_rate = 0.0001  # +0.01 % per 8h settlement
    mark = 50_000.0
    sim.apply_funding_settlement(funding_rate=funding_rate, mark_price=mark)

    expected_cash = funding_rate * mark * perp_qty
    assert sim.position.accrued_funding == pytest.approx(expected_cash, rel=1e-9)


def test_negative_funding_debits_short_leg():
    """§ 3.1: funding < 0 → bot pays out."""
    sim = _make_sim()
    sim.update_spot_close(50_000.0)
    sim.execute_signal(_buy(), current_price=50_000.0)
    perp_qty = sim.position.perp_quantity

    funding_rate = -0.00005
    mark = 50_000.0
    # First settlement only; threshold N=3 has not been hit yet.
    sim.apply_funding_settlement(funding_rate=funding_rate, mark_price=mark)
    assert sim.position is not None
    assert sim.position.accrued_funding == pytest.approx(
        funding_rate * mark * perp_qty, rel=1e-9,
    )


# ── Funding-flip exit ────────────────────────────────────────────────────────

def test_funding_flip_exits_after_n_consecutive_negative_settlements():
    """§ 4.1: N=3 default; three negative settlements close the leg."""
    sim = _make_sim()
    sim.update_spot_close(50_000.0)
    sim.execute_signal(_buy(), current_price=50_000.0)
    assert sim.position is not None

    sim.apply_funding_settlement(funding_rate=-0.0001, mark_price=50_000.0)
    assert sim.position is not None  # streak = 1
    sim.apply_funding_settlement(funding_rate=-0.0001, mark_price=50_000.0)
    assert sim.position is not None  # streak = 2
    sim.apply_funding_settlement(funding_rate=-0.0001, mark_price=50_000.0)
    # streak = 3 → exit fires.
    assert sim.position is None
    # Trade record records the funding_flip exit reason.
    assert len(sim.trade_history) == 1
    assert sim.trade_history[0].exit_reason == "funding_flip"


def test_streak_resets_after_a_positive_settlement():
    """A non-negative settlement clears the funding-flip counter."""
    sim = _make_sim()
    sim.update_spot_close(50_000.0)
    sim.execute_signal(_buy(), current_price=50_000.0)

    sim.apply_funding_settlement(funding_rate=-0.0001, mark_price=50_000.0)
    sim.apply_funding_settlement(funding_rate=-0.0001, mark_price=50_000.0)
    sim.apply_funding_settlement(funding_rate=+0.0001, mark_price=50_000.0)
    # Streak reset.
    sim.apply_funding_settlement(funding_rate=-0.0001, mark_price=50_000.0)
    sim.apply_funding_settlement(funding_rate=-0.0001, mark_price=50_000.0)
    assert sim.position is not None  # only 2 in current streak


# ── Maintenance-margin breach exit ───────────────────────────────────────────

def test_maintenance_margin_breach_closes_position_isolated():
    """§ 4.2: cushion below threshold triggers voluntary close.

    Tested in isolated-margin mode because cross-margin's
    spot-leg offset cancels the perp-leg loss exactly under a
    pure mark move (no basis dislocation), so a delta-neutral
    cross-margin position can't breach cushion via spot+perp
    alone — that's the cross-margin protection working.  The
    isolated mode isolates the perp-leg cushion from the spot
    leg, mirroring what a cross-margin cushion breach would
    look like in a basis-blowout regime.
    """
    sim = PerpSimulator(
        initial_balance=10_000.0,
        spot_symbol="BTC/USDT",
        perp_symbol="BTC/USDT",
        leverage=5.0,
        cushion_threshold=100.0,  # extreme threshold so a small adverse move trips
        margin_mode="isolated",
    )
    sim.update_spot_close(50_000.0)
    sim.execute_signal(_buy(), current_price=50_000.0)
    assert sim.position is not None

    # Tick a small adverse move — cushion drops below the inflated threshold.
    sim.tick_ohlcv_candle(high=55_000.0, low=50_000.0, close=52_000.0)
    assert sim.position is None
    assert sim.trade_history[-1].exit_reason == "margin_breach"


def test_cross_margin_absorbs_pure_mark_move():
    """§ 1: under cross margin the spot leg's MTM gain offsets the
    perp leg's MTM loss when price rises — no margin breach should
    fire on a pure-mark adverse move."""
    sim = _make_sim(initial_balance=10_000.0)  # default margin_mode=cross
    sim.update_spot_close(50_000.0)
    sim.execute_signal(_buy(), current_price=50_000.0)
    # Spot tracks perp 1:1 (no basis blowout) → cushion stays high.
    sim.update_spot_close(55_000.0)
    sim.tick_ohlcv_candle(high=55_000.0, low=50_000.0, close=55_000.0)
    assert sim.position is not None  # absorbed by cross-margin offset


# ── Liquidation distance: cross vs isolated ──────────────────────────────────

def test_cross_margin_doubles_liquidation_distance_vs_isolated():
    """§ 2.3: cross-margin S_liq ≈ S0 × (1 + 2/L) / (1 + mr) ;
    isolated S_liq ≈ S0 × (1 + 1/L) / (1 + mr).
    Cross should be roughly twice the upward move."""
    s0 = 50_000.0
    leverage = 5.0
    mr = 0.005

    sim_cross = PerpSimulator(
        initial_balance=10_000.0,
        spot_symbol="BTC/USDT", perp_symbol="BTC/USDT",
        leverage=leverage, maintenance_margin_ratio=mr,
        margin_mode="cross",
    )
    sim_iso = PerpSimulator(
        initial_balance=10_000.0,
        spot_symbol="BTC/USDT", perp_symbol="BTC/USDT",
        leverage=leverage, maintenance_margin_ratio=mr,
        margin_mode="isolated",
    )
    for s in (sim_cross, sim_iso):
        s.update_spot_close(s0)
        s.execute_signal(_buy(), current_price=s0)

    cross_liq = sim_cross.liquidation_price()
    iso_liq = sim_iso.liquidation_price()

    expected_cross = s0 * (1.0 + 2.0 / leverage) / (1.0 + mr)
    expected_iso = s0 * (1.0 + 1.0 / leverage) / (1.0 + mr)
    assert cross_liq == pytest.approx(expected_cross, rel=1e-9)
    assert iso_liq == pytest.approx(expected_iso, rel=1e-9)
    assert cross_liq > iso_liq


# ── Combined-position sanity at exit ─────────────────────────────────────────

def test_combined_position_sanity_within_tolerance_records_no_violation():
    sim = _make_sim()
    sim.update_spot_close(50_000.0)
    sim.execute_signal(_buy(), current_price=50_000.0)
    # Close at same prices — combined notional should match expected.
    sim.update_spot_close(50_000.0)
    sim.execute_signal(_sell(), current_price=50_000.0)
    assert sim.combined_position_sanity_violations == []


def test_close_returns_funding_cash_to_balance():
    """Accrued funding should sweep into balance on close."""
    sim = _make_sim()
    sim.update_spot_close(50_000.0)
    sim.execute_signal(_buy(), current_price=50_000.0)
    # Apply one settlement to credit funding.
    sim.apply_funding_settlement(funding_rate=0.0005, mark_price=50_000.0)
    funding_credited = sim.position.accrued_funding
    assert funding_credited > 0

    balance_before_close = sim.balance
    sim.update_spot_close(50_000.0)
    sim.execute_signal(_sell(), current_price=50_000.0)
    # Position closed.
    assert sim.position is None
    # Balance grew by ~ (spot+perp returned + funding − exit fees).
    # Funding portion alone should be present in the post-close balance.
    assert sim.balance > balance_before_close + funding_credited * 0.99


# ── Checkpoint round-trip ────────────────────────────────────────────────────

def test_get_and_restore_checkpoint_round_trips_open_position():
    sim = _make_sim()
    sim.update_spot_close(50_000.0)
    sim.execute_signal(_buy(), current_price=50_000.0)
    sim.apply_funding_settlement(funding_rate=0.0003, mark_price=50_000.0)

    snap = sim.get_checkpoint()

    sim2 = _make_sim()
    sim2.restore_checkpoint(snap)

    assert sim2.balance == sim.balance
    assert sim2.total_fees_paid == sim.total_fees_paid
    assert sim2.position is not None
    assert sim2.position.spot_quantity == sim.position.spot_quantity
    assert sim2.position.perp_quantity == sim.position.perp_quantity
    assert sim2.position.accrued_funding == sim.position.accrued_funding


def test_get_and_restore_checkpoint_round_trips_flat_state():
    sim = _make_sim()
    snap = sim.get_checkpoint()

    sim2 = _make_sim()
    sim2.restore_checkpoint(snap)
    assert sim2.position is None
    assert sim2.balance == sim.balance


# ── Path (a) — exit_mr_ratio_threshold (chat 2026-05-02 fix) ─────────────────


def test_exit_mr_ratio_threshold_fires_at_correct_equity():
    """Path (a): exit_mr_ratio_threshold takes the literature value
    verbatim (account margin ratio, not the (equity-MM)/MM
    multiplier).  In isolated mode, an adverse perp move that drops
    equity / notional below the threshold fires `margin_breach`."""
    sim = PerpSimulator(
        initial_balance=10_000.0,
        spot_symbol="BTC/USDT",
        perp_symbol="BTC/USDT",
        leverage=5.0,
        exit_mr_ratio_threshold=0.01,
        margin_mode="isolated",
    )
    sim.update_spot_close(50_000.0)
    sim.execute_signal(_buy(), current_price=50_000.0)
    assert sim.position is not None

    # In isolated mode at L=5x, MR drops below 0.01 around mark ≈ 59_400.
    # Tick a high above that to trigger the exit.
    sim.tick_ohlcv_candle(high=60_000.0, low=50_000.0, close=58_000.0)
    assert sim.position is None
    assert sim.trade_history[-1].exit_reason == "margin_breach"


def test_exit_mr_ratio_threshold_does_not_fire_at_safe_equity():
    """Small adverse move keeps MR well above 0.01 even in
    isolated mode."""
    sim = PerpSimulator(
        initial_balance=10_000.0,
        spot_symbol="BTC/USDT",
        perp_symbol="BTC/USDT",
        leverage=5.0,
        exit_mr_ratio_threshold=0.01,
        margin_mode="isolated",
    )
    sim.update_spot_close(50_000.0)
    sim.execute_signal(_buy(), current_price=50_000.0)

    sim.tick_ohlcv_candle(high=51_000.0, low=50_000.0, close=50_500.0)
    assert sim.position is not None  # safe; no exit


def test_both_thresholds_set_raises():
    """cushion_threshold and exit_mr_ratio_threshold are mutually
    exclusive — set exactly one (or neither for the default)."""
    with pytest.raises(ValueError, match="mutually exclusive"):
        PerpSimulator(
            initial_balance=10_000.0,
            spot_symbol="BTC/USDT",
            perp_symbol="BTC/USDT",
            cushion_threshold=0.5,
            exit_mr_ratio_threshold=0.01,
        )


def test_default_cushion_threshold_path_unchanged():
    """Regression guard: explicit cushion_threshold=100.0 still fires
    via the legacy cushion path in isolated mode.  (Mirrors the
    pre-Path-(a) test
    `test_maintenance_margin_breach_closes_position_isolated`.)"""
    sim = PerpSimulator(
        initial_balance=10_000.0,
        spot_symbol="BTC/USDT",
        perp_symbol="BTC/USDT",
        leverage=5.0,
        cushion_threshold=100.0,
        margin_mode="isolated",
    )
    sim.update_spot_close(50_000.0)
    sim.execute_signal(_buy(), current_price=50_000.0)
    sim.tick_ohlcv_candle(high=55_000.0, low=50_000.0, close=52_000.0)
    assert sim.position is None
    assert sim.trade_history[-1].exit_reason == "margin_breach"


def test_neither_threshold_kwarg_uses_default_cushion_path():
    """Regression guard: omitting both kwargs falls back to
    DEFAULT_CUSHION_THRESHOLD on the cushion path (legacy default).
    """
    sim = PerpSimulator(
        initial_balance=10_000.0,
        spot_symbol="BTC/USDT",
        perp_symbol="BTC/USDT",
    )
    assert sim.cushion_threshold is not None
    assert sim.exit_mr_ratio_threshold is None


# ── exit_forensics ledger ────────────────────────────────────────────────────


def test_exit_forensics_populated_on_close():
    """Every close appends exactly one row to exit_forensics with
    the audit-script-required schema."""
    sim = _make_sim()
    sim.update_spot_close(50_000.0)
    sim.execute_signal(_buy(), current_price=50_000.0)
    sim.update_spot_close(50_500.0)
    sim.execute_signal(_sell(), current_price=50_500.0)

    assert len(sim.exit_forensics) == 1
    rec = sim.exit_forensics[0]
    expected_keys = {
        "exit_time", "exit_reason", "spot_qty", "spot_exit_price",
        "perp_qty", "perp_exit_price", "entry_notional",
        "spot_notional_at_exit", "perp_notional_at_exit",
        "basis_at_exit_abs_pct", "spot_pnl", "perp_pnl",
        "funding_cash", "total_pnl",
    }
    assert expected_keys.issubset(set(rec.keys()))
    # Sign convention: perp_qty is signed (negative for short).
    assert rec["perp_qty"] < 0
    assert rec["spot_qty"] > 0
    # basis_at_exit_abs_pct is non-negative (it's an absolute fraction).
    assert rec["basis_at_exit_abs_pct"] >= 0
