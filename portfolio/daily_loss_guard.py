"""
portfolio/daily_loss_guard.py — Daily loss limit guard (portfolio + per-strategy).

TWO LAYERS
──────────
1. Portfolio-level (DailyLossGuard):
   If TOTAL equity drops > max_daily_loss_pct in one UTC day → block ALL new buys.
   Catches correlated meltdowns, flash crashes, and simultaneous multi-strategy losses.

2. Per-strategy layer (via update_slot / allows_slot_buy):
   Each strategy has its OWN daily loss threshold. If a single strategy's allocated
   capital drops beyond its threshold → block that strategy's new buys only.
   Catches a single broken strategy while letting healthy ones keep trading.

WHY DIFFERENT THRESHOLDS PER STRATEGY
──────────────────────────────────────
  DCA (8%):            It buys dips — a 5% BTC drop is its opportunity, not a signal to stop.
  MeanReversion (3%):  If MR is losing 3% in a day, the market is trending hard and the
                       mean-reversion assumption is broken. Stop.
  GridTrading (3%):    A 3% loss means price broke out of the grid range violently.
                       The grid's fundamental assumption no longer holds.
  Breakout (5%):       False breakouts happen; moderate tolerance.
  Supertrend (5%):     Can have whipsaw losses in ranging markets; moderate tolerance.
  TrendFollowing (6%): Designed to hold through pullbacks; looser limit.
  BearShort (5%):      Symmetric with Breakout.
  VWAP (4%):           Volume-based; tight because broken VWAP signals mean wrong regime.

BEHAVIOUR ON TRIP
─────────────────
  - Blocks new BUY signals only.
  - Does NOT force-close open positions (that would lock in losses at the worst price).
  - Open positions continue with their own SL/TP logic until they exit naturally.
  - Resets automatically at midnight UTC for the next trading day.

USAGE
─────
  guard = DailyLossGuard(portfolio_max_pct=5.0)

  # Each candle — portfolio level
  guard.update(total_equity=10_000.0)

  # Each candle — per slot (call for each active slot)
  guard.update_slot("DCA", slot_equity=2_500.0)
  guard.update_slot("GridTrading", slot_equity=1_800.0)

  # Check before opening any position
  if guard.allows_new_buys(total_equity=9_800.0):
      if guard.allows_slot_buy("DCA", slot_equity=2_450.0):
          ...open DCA trade...
"""

from __future__ import annotations

from datetime import datetime, timezone, date
from dataclasses import dataclass
from typing import Optional


# ── Per-strategy default daily loss thresholds ────────────────────────────────
STRATEGY_DAILY_LIMITS: dict[str, float] = {
    "DCA":            8.0,    # Looser — DCA buys dips intentionally
    "Supertrend":     5.0,
    "MeanReversion":  3.0,    # Tight — range-reversion assumption breaks fast
    "GridTrading":    3.0,    # Tight — grid range broken = stop
    "Breakout":       5.0,
    "TrendFollowing": 6.0,    # Can hold through pullbacks
    "BearShort":      5.0,
    "VWAP":           4.0,
}


@dataclass
class DailySnapshot:
    """Equity snapshot at start of UTC day."""
    day:            date
    opening_equity: float


class DailyLossGuard:
    """
    Two-layer daily loss protection: portfolio-level and per-strategy.

    Args:
        portfolio_max_pct:    Max portfolio loss % per day (default 5.0).
        strategy_limits:      Override per-strategy thresholds. Defaults to
                              STRATEGY_DAILY_LIMITS if not provided.
    """

    def __init__(
        self,
        portfolio_max_pct: float = 5.0,
        strategy_limits:   Optional[dict[str, float]] = None,
        # Legacy parameter name — kept for backward compatibility
        max_daily_loss_pct: Optional[float] = None,
    ):
        # Accept old kwarg name transparently
        if max_daily_loss_pct is not None and portfolio_max_pct == 5.0:
            portfolio_max_pct = max_daily_loss_pct

        self.portfolio_max_pct  = portfolio_max_pct
        self.strategy_limits    = strategy_limits or STRATEGY_DAILY_LIMITS.copy()

        # Portfolio-level state
        self._snapshot:         Optional[DailySnapshot] = None
        self._tripped_today:    bool = False
        self._notified_today:   bool = False   # Used by main.py to avoid repeat Telegram alerts

        # Per-strategy state: {slot_name: (DailySnapshot, tripped_bool)}
        self._slot_snapshots:   dict[str, DailySnapshot] = {}
        self._slot_tripped:     dict[str, bool] = {}

    # ── Portfolio-level interface ─────────────────────────────────────────────

    def update(self, current_equity: float) -> None:
        """
        Call once per candle with TOTAL portfolio equity.
        Sets the day-open snapshot and resets flags at midnight UTC.
        """
        today = datetime.now(timezone.utc).date()
        if self._snapshot is None or self._snapshot.day != today:
            self._snapshot      = DailySnapshot(day=today, opening_equity=current_equity)
            self._tripped_today = False
            self._notified_today = False
            # Also reset all per-slot trip flags on new day
            self._slot_tripped = {k: False for k in self._slot_tripped}

    def allows_new_buys(self, current_equity: float) -> bool:
        """Return True if new buys are allowed at the portfolio level."""
        if self._snapshot is None:
            return True
        if self._tripped_today:
            return False
        if self._loss_pct(current_equity, self._snapshot) >= self.portfolio_max_pct:
            self._tripped_today = True
            return False
        return True

    def current_loss_pct(self, current_equity: float) -> float:
        """Today's portfolio loss as a positive %. 0 if still profitable."""
        return max(0.0, self._loss_pct(current_equity, self._snapshot))

    def is_tripped(self) -> bool:
        return self._tripped_today

    def day_open_equity(self) -> Optional[float]:
        return self._snapshot.opening_equity if self._snapshot else None

    # ── Per-strategy interface ────────────────────────────────────────────────

    def update_slot(self, slot_name: str, slot_equity: float) -> None:
        """
        Register the current equity for a specific strategy slot.
        Sets the slot's day-open snapshot on the first call each UTC day.

        Call this once per candle per active slot before allows_slot_buy().
        """
        today = datetime.now(timezone.utc).date()
        snap  = self._slot_snapshots.get(slot_name)

        if snap is None or snap.day != today:
            self._slot_snapshots[slot_name] = DailySnapshot(day=today, opening_equity=slot_equity)
            self._slot_tripped[slot_name]   = False

    def allows_slot_buy(self, slot_name: str, slot_equity: float) -> bool:
        """
        Return True if this specific strategy slot is allowed to open a new buy.

        False if the slot has lost more than its configured daily limit.
        If no threshold is configured for this slot, returns True.
        """
        if self._slot_tripped.get(slot_name, False):
            return False

        snap      = self._slot_snapshots.get(slot_name)
        max_loss  = self.strategy_limits.get(slot_name)

        if snap is None or max_loss is None:
            return True   # No threshold configured — allow

        loss = self._loss_pct(slot_equity, snap)
        if loss >= max_loss:
            self._slot_tripped[slot_name] = True
            return False

        return True

    def slot_loss_pct(self, slot_name: str, slot_equity: float) -> float:
        """Today's loss for a strategy slot as a positive %. 0 if profitable."""
        snap = self._slot_snapshots.get(slot_name)
        return max(0.0, self._loss_pct(slot_equity, snap)) if snap else 0.0

    def tripped_slots(self) -> list[str]:
        """Return names of all strategy slots currently blocked by their daily limit."""
        return [k for k, v in self._slot_tripped.items() if v]

    # ── Summary ───────────────────────────────────────────────────────────────

    def summary(self, current_equity: float) -> str:
        if self._snapshot is None:
            return "DailyLossGuard: not yet initialized"
        loss   = self._loss_pct(current_equity, self._snapshot)
        status = "TRIPPED" if self._tripped_today else "OK"
        lines  = [
            f"DailyLossGuard [{self._snapshot.day}]  "
            f"Open: ${self._snapshot.opening_equity:,.2f}  "
            f"Now: ${current_equity:,.2f}  "
            f"Loss: {loss:.2f}%/{self.portfolio_max_pct:.1f}%  [{status}]"
        ]
        for slot, tripped in self._slot_tripped.items():
            if tripped:
                lines.append(f"  ⚠ {slot}: daily limit hit")
        return "\n".join(lines)

    # ── Internal ──────────────────────────────────────────────────────────────

    @staticmethod
    def _loss_pct(current_equity: float, snap: Optional[DailySnapshot]) -> float:
        """Loss % relative to opening equity. Positive = loss, negative = gain."""
        if snap is None or snap.opening_equity <= 0:
            return 0.0
        return (snap.opening_equity - current_equity) / snap.opening_equity * 100
