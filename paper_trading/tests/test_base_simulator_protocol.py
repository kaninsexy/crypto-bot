"""
paper_trading/tests/test_base_simulator_protocol.py

Verifies the Phase 4.B Track E contract:

  - BaseSimulator is a runtime-checkable structural protocol.
  - PaperTrading satisfies BaseSimulator without inheriting from it.
  - PerpSimulator (Phase 4.B Track F) satisfies BaseSimulator when
    importable; the test is skipped if the perp simulator module is
    absent in some out-of-tree configuration.

The structural-protocol contract is the substitution guarantee
StrategySlot.simulator depends on.  If a future simulator backend
fails to expose any of the listed attributes/methods, this test
fails before downstream call sites silently misbehave.
"""

from __future__ import annotations

import pytest

from paper_trading.base_simulator import BaseSimulator
from paper_trading.simulator import PaperTrading


# ── PaperTrading ─────────────────────────────────────────────────────────────

def test_papertrading_satisfies_base_simulator_protocol():
    sim = PaperTrading(initial_balance=10_000.0, symbol="BTC/USDT")
    assert isinstance(sim, BaseSimulator)


def test_papertrading_exposes_required_attributes():
    sim = PaperTrading(initial_balance=10_000.0, symbol="BTC/USDT")
    # Attributes that callers in engine/portfolio depend on.
    assert hasattr(sim, "balance")
    assert hasattr(sim, "initial_balance")
    assert hasattr(sim, "position")
    assert hasattr(sim, "trade_history")
    assert hasattr(sim, "symbol")
    assert hasattr(sim, "total_fees_paid")


def test_papertrading_exposes_required_methods():
    sim = PaperTrading(initial_balance=10_000.0, symbol="BTC/USDT")
    assert callable(getattr(sim, "execute_signal", None))
    assert callable(getattr(sim, "tick", None))
    assert callable(getattr(sim, "tick_ohlcv_candle", None))
    assert callable(getattr(sim, "get_balance", None))
    assert callable(getattr(sim, "get_equity", None))
    assert callable(getattr(sim, "get_checkpoint", None))
    assert callable(getattr(sim, "restore_checkpoint", None))


# ── PerpSimulator (Phase 4.B Track F) ────────────────────────────────────────

def test_perp_simulator_satisfies_base_simulator_protocol():
    """PerpSimulator must satisfy BaseSimulator structurally so it
    can be substituted into StrategySlot.simulator without changes
    to the portfolio layer."""
    perp_mod = pytest.importorskip("paper_trading.perp_simulator")
    sim = perp_mod.PerpSimulator(
        initial_balance=10_000.0,
        spot_symbol="BTC/USDT",
        perp_symbol="BTC/USDT",
    )
    assert isinstance(sim, BaseSimulator)
