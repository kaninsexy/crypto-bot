"""End-to-end tests for short-position realized PnL.

Each test instantiates a fresh PaperTrading simulator with $100k, sends
synthetic Signals through the public execute_signal entry-point, and asserts
on the resulting trade record + simulator state. No mocks — these exercise
the real class surface.

Fee-schedule note (latent quirk in execute_signal SELL routing, NOT in
scope of the realized-pnl fix):
  - Opens via _handle_buy read signal.order_type → market signals pay
    FEE_MARKET (0.04%) on entry.
  - Partial closes (_handle_partial_sell) read signal.order_type → market
    signals pay FEE_MARKET on exit.
  - Full closes via SELL signal route through execute_signal:148 which
    calls _handle_full_sell(signal, price, reason) without forwarding
    signal.order_type, so the default "limit" wins → FEE_LIMIT (0.02%)
    on exit, regardless of the signal's order_type.
  This asymmetry is real in current main; the tests below model the
  actual fee behavior so they pin down post-refactor pnl exactly.

Setup math:
  Open short qty=0.1 @ 30_000 (market) → cost=3000, entry_fee=1.20,
  balance_after_open = 100_000 - 3001.20 = 96_998.80.

Full close short @ 27_000 (winning):  exit_fee = 2700 * 0.0002 = 0.54
  → pnl = (30_000 - 27_000) * 0.1 - 0.54 = 299.46
  → balance += cost_basis + pnl = 3000 + 299.46 = 3299.46
  → final balance = 100_298.26.

Full close short @ 33_000 (losing):   exit_fee = 33000 * 0.1 * 0.0002 = 0.66
  → pnl = (30_000 - 33_000) * 0.1 - 0.66 = -300.66
  → final balance = 96_998.80 + 2699.34 = 99_698.14.

Long control full close @ 33_000:     pnl = (33_000 - 30_000) * 0.1 - 0.66 = 299.34
  → final balance = 100_298.14.

Partial closes use FEE_MARKET as expected; values noted at each test.
"""

from __future__ import annotations

import pytest
from loguru import logger

from paper_trading.simulator import PaperTrading
from strategies.base import Signal


# Silence loguru once for the whole module — keeps pytest output clean.
logger.remove()


INIT_BALANCE = 100_000.0
ENTRY_PRICE  = 30_000.0
QTY          = 0.1
COST_USDT    = ENTRY_PRICE * QTY                    # 3_000.0
FEE_MARKET   = 0.0004                                # mirrors simulator.FEE_MARKET
ENTRY_FEE    = COST_USDT * FEE_MARKET                # 1.20
WIN_PRICE    = 27_000.0
LOSE_PRICE   = 33_000.0


def _open_signal(is_short: bool, price: float = ENTRY_PRICE) -> Signal:
    """BUY signal sized via metadata.amount_usdt so qty is deterministic."""
    return Signal(
        action="BUY",
        strategy="ShortPnLTest",
        price=price,
        reason="open (test fixture)",
        stop_loss=price * (1.05 if is_short else 0.95),
        take_profit=price * (0.95 if is_short else 1.05),
        is_short=is_short,
        leverage=1,
        order_type="market",
        quantity_pct=1.0,
        metadata={"amount_usdt": COST_USDT},
    )


def _close_signal(is_short: bool, price: float, quantity_pct: float = 1.0) -> Signal:
    return Signal(
        action="SELL",
        strategy="ShortPnLTest",
        price=price,
        reason="close (test fixture)",
        is_short=is_short,
        leverage=1,
        order_type="market",
        quantity_pct=quantity_pct,
    )


def _fresh_sim() -> PaperTrading:
    return PaperTrading(initial_balance=INIT_BALANCE, symbol="BTC/USDT")


# ── Full-close tests ─────────────────────────────────────────────────────────


def test_winning_short_full_close():
    """Short opened at 30k closed at 27k — short should win, ~+$299.46."""
    sim = _fresh_sim()
    sim.execute_signal(_open_signal(is_short=True), ENTRY_PRICE)
    assert sim.position is not None
    assert sim.position.is_short is True
    assert sim.position.quantity == pytest.approx(QTY)

    sim.execute_signal(_close_signal(is_short=True, price=WIN_PRICE), WIN_PRICE)

    assert sim.position is None
    assert len(sim.trade_history) == 1
    rec = sim.trade_history[-1]
    assert rec.side == "short"
    assert rec.pnl == pytest.approx(299.46, abs=0.05)
    assert rec.pnl > 0, "winning short must record positive pnl"
    assert sim.balance == pytest.approx(100_298.26, abs=0.05)
    assert sim.balance > INIT_BALANCE, "winning short must raise balance"


def test_losing_short_full_close():
    """Short opened at 30k closed at 33k — short should lose, ~-$300.66."""
    sim = _fresh_sim()
    sim.execute_signal(_open_signal(is_short=True), ENTRY_PRICE)
    sim.execute_signal(_close_signal(is_short=True, price=LOSE_PRICE), LOSE_PRICE)

    assert sim.position is None
    rec = sim.trade_history[-1]
    assert rec.side == "short"
    assert rec.pnl == pytest.approx(-300.66, abs=0.05)
    assert rec.pnl < 0, "losing short must record negative pnl"
    assert sim.balance == pytest.approx(99_698.14, abs=0.05)
    assert sim.balance < INIT_BALANCE, "losing short must lower balance"


def test_long_control_full_close():
    """Regression guard: long @ 30k → 33k must still earn ~+$299.34."""
    sim = _fresh_sim()
    sim.execute_signal(_open_signal(is_short=False), ENTRY_PRICE)
    sim.execute_signal(_close_signal(is_short=False, price=LOSE_PRICE), LOSE_PRICE)

    assert sim.position is None
    rec = sim.trade_history[-1]
    assert rec.side == "long"
    assert rec.pnl == pytest.approx(299.34, abs=0.05)
    assert rec.pnl > 0
    assert sim.balance == pytest.approx(100_298.14, abs=0.05)
    assert sim.balance > INIT_BALANCE


# ── Partial-close tests ──────────────────────────────────────────────────────


def test_winning_short_partial_close():
    """Close 50% of a winning short — partial pnl ≈ +$149.46, half qty remains.

    Partial-close path uses signal.order_type → market fees here.
    pnl = (30_000 - 27_000) * 0.05 - (27000 * 0.05 * 0.0004) = 150 - 0.54 = 149.46.
    """
    sim = _fresh_sim()
    sim.execute_signal(_open_signal(is_short=True), ENTRY_PRICE)

    sim.execute_signal(
        _close_signal(is_short=True, price=WIN_PRICE, quantity_pct=0.5),
        WIN_PRICE,
    )

    assert sim.position is not None, "partial close must leave the position open"
    assert sim.position.quantity == pytest.approx(QTY * 0.5)
    assert sim.position.total_cost == pytest.approx(COST_USDT * 0.5)

    rec = sim.trade_history[-1]
    assert rec.is_partial is True
    assert rec.side == "short"
    assert rec.pnl == pytest.approx(149.46, abs=0.05)
    assert rec.pnl > 0


def test_losing_short_partial_close():
    """Close 50% of a losing short — partial pnl ≈ -$150.66, half qty remains.

    pnl = (30_000 - 33_000) * 0.05 - (33000 * 0.05 * 0.0004) = -150 - 0.66 = -150.66.
    """
    sim = _fresh_sim()
    sim.execute_signal(_open_signal(is_short=True), ENTRY_PRICE)

    sim.execute_signal(
        _close_signal(is_short=True, price=LOSE_PRICE, quantity_pct=0.5),
        LOSE_PRICE,
    )

    assert sim.position is not None
    assert sim.position.quantity == pytest.approx(QTY * 0.5)

    rec = sim.trade_history[-1]
    assert rec.is_partial is True
    assert rec.side == "short"
    assert rec.pnl == pytest.approx(-150.66, abs=0.05)
    assert rec.pnl < 0


def test_long_partial_close():
    """Regression guard: 50% partial close on a winning long — half qty, half pnl.

    pnl = (33_000 - 30_000) * 0.05 - (33000 * 0.05 * 0.0004) = 150 - 0.66 = 149.34.
    """
    sim = _fresh_sim()
    sim.execute_signal(_open_signal(is_short=False), ENTRY_PRICE)

    sim.execute_signal(
        _close_signal(is_short=False, price=LOSE_PRICE, quantity_pct=0.5),
        LOSE_PRICE,
    )

    assert sim.position is not None
    assert sim.position.quantity == pytest.approx(QTY * 0.5)

    rec = sim.trade_history[-1]
    assert rec.is_partial is True
    assert rec.side == "long"
    assert rec.pnl == pytest.approx(149.34, abs=0.05)
    assert rec.pnl > 0
