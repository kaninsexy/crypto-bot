"""tests/strategies/test_intraday_seasonality.py — sq-003 unit tests.

Five behavioural tests for `IntradaySeasonalityEffects`:

  (a) BUY fires only at entry_hour when not in position
  (b) SELL fires only at exit_hour when in position
  (c) SELL never fires at exit_hour when _position_open is False
  (d) HOLD on all other hours
  (e) Fresh instance resets state (CPCV gate #7)
"""

from __future__ import annotations

import pandas as pd
import pytest

from strategies.intraday_seasonality import IntradaySeasonalityEffects


def _frame_at(hour: int, day: str = "2025-01-15") -> pd.DataFrame:
    """Minimal one-row OHLCV DataFrame whose last index is `day` at `hour`:00 UTC."""
    ts = pd.Timestamp(f"{day} {hour:02d}:00:00", tz="UTC")
    return pd.DataFrame(
        {
            "open": [100.0],
            "high": [101.0],
            "low": [99.0],
            "close": [100.5],
            "volume": [1.0],
        },
        index=pd.DatetimeIndex([ts], name="timestamp"),
    )


# ── (a) BUY fires only at entry_hour when not in position ──────────────────

def test_buy_fires_at_entry_hour_when_flat():
    s = IntradaySeasonalityEffects(entry_hour=21, exit_hour=23)
    sig = s.generate_signal(_frame_at(21))
    assert sig.action == "BUY"
    assert s._position_open is True


def test_buy_does_not_fire_when_already_open():
    s = IntradaySeasonalityEffects(entry_hour=21, exit_hour=23)
    s._position_open = True  # simulate prior fill
    sig = s.generate_signal(_frame_at(21))
    # Already long: re-entry suppressed; HOLD is the expected response.
    assert sig.action == "HOLD"


# ── (b) SELL fires only at exit_hour when in position ──────────────────────

def test_sell_fires_at_exit_hour_when_open():
    s = IntradaySeasonalityEffects(entry_hour=21, exit_hour=23)
    s._position_open = True
    sig = s.generate_signal(_frame_at(23))
    assert sig.action == "SELL"
    assert s._position_open is False


# ── (c) SELL NEVER fires at exit_hour when _position_open is False ─────────
# Gate #2 (long-only): the highest-risk gate.  This test is the regression
# guard for it.

def test_sell_does_not_fire_at_exit_hour_when_flat():
    s = IntradaySeasonalityEffects(entry_hour=21, exit_hour=23)
    assert s._position_open is False  # baseline
    sig = s.generate_signal(_frame_at(23))
    assert sig.action == "HOLD", (
        "gate #2 violation: SELL must not fire when _position_open is False"
    )
    assert sig.action != "SELL"
    assert s._position_open is False  # still flat


# ── (d) HOLD on all other hours ────────────────────────────────────────────

@pytest.mark.parametrize(
    "hour",
    [h for h in range(24) if h not in (21, 23)],
)
def test_hold_on_non_window_hours(hour):
    s = IntradaySeasonalityEffects(entry_hour=21, exit_hour=23)
    sig = s.generate_signal(_frame_at(hour))
    assert sig.action == "HOLD", (
        f"hour {hour} should HOLD (entry=21, exit=23)"
    )
    assert s._position_open is False


# ── (e) Fresh instance resets state ────────────────────────────────────────

def test_fresh_instance_resets_position_state():
    s1 = IntradaySeasonalityEffects(entry_hour=21, exit_hour=23)
    s1.generate_signal(_frame_at(21))
    assert s1._position_open is True

    # New instance — state must NOT carry across.  This is the CPCV
    # gate #7 contract: strategy_factory creates fresh instances per
    # block; block-boundary state must not leak.
    s2 = IntradaySeasonalityEffects(entry_hour=21, exit_hour=23)
    assert s2._position_open is False
    sig = s2.generate_signal(_frame_at(23))
    assert sig.action == "HOLD"
    assert s2._position_open is False
