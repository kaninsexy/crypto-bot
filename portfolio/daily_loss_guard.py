"""
portfolio/daily_loss_guard.py — Graduated daily loss cap (portfolio + per-strategy).

GRADUATED TIERS (portfolio level, measured from start-of-day equity)
─────────────────────────────────────────────────────────────────────
  1% loss → WARNING:    new position sizes → 75% of normal
  2% loss → CAUTIOUS:   new position sizes → 50% of normal
  3% loss → HALT:       no new trades; existing stops maintained
  4% loss → REDUCE:     close-only mode; stops tightened to 2% below current price
  5% loss → EMERGENCY:  all positions closed via market orders

TWO LAYERS
──────────
1. Portfolio-level (DailyLossGuard):
   Graduated tiers from TOTAL equity vs start-of-day (SOD) equity.
   SOD is captured on the first update() call each UTC day and reset at midnight.

2. Per-strategy layer (via update_slot / allows_slot_buy):
   Each strategy has its OWN daily loss threshold.  If a single strategy's allocated
   capital drops beyond its threshold → block that strategy's new buys only.
   Catches a single broken strategy while letting healthy ones keep trading.

BEHAVIOUR
─────────
  WARNING/CAUTIOUS : reduce new position sizes; existing positions untouched.
  HALT             : block new BUYs; existing positions run their own SL/TP.
  REDUCE           : block new BUYs; PortfolioManager tightens open stops once on
                     tier entry (2% trailing stop from current price).
  EMERGENCY        : PortfolioManager closes all open positions at market (one-shot).
  All tiers reset automatically at midnight UTC.

RESTART-SAFETY
──────────────
  Call save_state() / restore_state() via the portfolio checkpoint dict so that
  start-of-day equity survives bot restarts within the same UTC day.
  If the checkpoint is from a previous UTC day, the guard resets to NORMAL.

USAGE
─────
  guard = DailyLossGuard(portfolio_max_pct=5.0)

  # Before pm.run_candle() each candle:
  tier = guard.update(total_equity)          # sets tier, handles midnight reset

  # Inject into PortfolioManager so run_candle() can apply size_multiplier:
  pm._daily_guard = guard

  # Per-strategy slot check (called inside run_candle via _daily_guard):
  guard.update_slot("DCA", slot_equity)
  if guard.allows_slot_buy("DCA", slot_equity):
      ...
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone, date
from dataclasses import dataclass
from typing import Optional

from loguru import logger


# ── Tier enum ─────────────────────────────────────────────────────────────────

class DailyLossTier(enum.Enum):
    NORMAL    = "NORMAL"     # < 1% daily loss  — 100% size
    WARNING   = "WARNING"    # 1–2% daily loss  — 75% size
    CAUTIOUS  = "CAUTIOUS"   # 2–3% daily loss  — 50% size
    HALT      = "HALT"       # 3–4% daily loss  — no new trades
    REDUCE    = "REDUCE"     # 4–5% daily loss  — close-only; tighten stops
    EMERGENCY = "EMERGENCY"  # ≥ 5% daily loss  — close all positions


# (loss_pct threshold, tier, new-position size multiplier)
# Listed highest-first so we break on the first match.
_TIER_TABLE: list[tuple[float, DailyLossTier, float]] = [
    (5.0, DailyLossTier.EMERGENCY, 0.00),
    (4.0, DailyLossTier.REDUCE,    0.00),
    (3.0, DailyLossTier.HALT,      0.00),
    (2.0, DailyLossTier.CAUTIOUS,  0.50),
    (1.0, DailyLossTier.WARNING,   0.75),
]


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
    Graduated daily loss cap: portfolio-level tiers + per-strategy binary block.

    Complements CircuitBreaker (which tracks peak-to-trough drawdown) — these
    are orthogonal guards.  CircuitBreaker never resets until equity recovers;
    DailyLossGuard resets every midnight UTC so yesterday's bad day doesn't
    permanently cage the bot.

    Args:
        portfolio_max_pct:  EMERGENCY tier threshold (default 5.0%).
                            Lower tiers (1/2/3/4%) are automatically derived.
                            Kept for backward-compat with old single-threshold API.
        strategy_limits:    Override per-strategy thresholds.
        max_daily_loss_pct: Legacy alias for portfolio_max_pct.
    """

    def __init__(
        self,
        portfolio_max_pct:  float = 5.0,
        strategy_limits:    Optional[dict[str, float]] = None,
        max_daily_loss_pct: Optional[float] = None,   # legacy alias
    ):
        if max_daily_loss_pct is not None and portfolio_max_pct == 5.0:
            portfolio_max_pct = max_daily_loss_pct

        self.portfolio_max_pct = portfolio_max_pct
        self.strategy_limits   = strategy_limits or STRATEGY_DAILY_LIMITS.copy()

        # Portfolio-level state
        self._snapshot:            Optional[DailySnapshot] = None
        self._tier:                DailyLossTier           = DailyLossTier.NORMAL
        self._prev_tier:           DailyLossTier           = DailyLossTier.NORMAL
        self._emergency_triggered: bool = False
        self._notified_today:      bool = False  # used by main.py for Telegram dedup

        # Per-strategy state
        self._slot_snapshots: dict[str, DailySnapshot] = {}
        self._slot_tripped:   dict[str, bool]          = {}

    # ── Portfolio-level interface ──────────────────────────────────────────────

    def update(self, current_equity: float) -> DailyLossTier:
        """
        Call once per candle with TOTAL portfolio equity (before pm.run_candle).

        Handles midnight UTC reset automatically.

        Returns:
            Current DailyLossTier.
        """
        today = datetime.now(timezone.utc).date()

        # ── Midnight reset ───────────────────────────────────────────────────
        if self._snapshot is None or self._snapshot.day != today:
            old_eq  = self._snapshot.opening_equity if self._snapshot else current_equity
            old_day = str(self._snapshot.day)        if self._snapshot else str(today)
            self._snapshot            = DailySnapshot(day=today, opening_equity=current_equity)
            self._prev_tier           = self._tier
            self._tier                = DailyLossTier.NORMAL
            self._emergency_triggered = False
            self._notified_today      = False
            self._slot_tripped        = {k: False for k in self._slot_tripped}
            logger.info(
                f"🌅 DailyLossGuard RESET | New day: {today} UTC | "
                f"SOD equity: ${current_equity:,.2f} "
                f"(prev {old_day}: ${old_eq:,.2f} / tier was {self._prev_tier.value})"
            )

        # ── Compute loss % and map to tier ───────────────────────────────────
        loss_pct = max(0.0, self._loss_pct(current_equity, self._snapshot))
        new_tier = DailyLossTier.NORMAL
        for threshold, tier, _ in _TIER_TABLE:
            if loss_pct >= threshold:
                new_tier = tier
                break

        if new_tier != self._tier:
            self._transition(new_tier, current_equity, loss_pct)

        return self._tier

    def _transition(self, new_tier: DailyLossTier, equity: float, loss_pct: float) -> None:
        old_tier   = self._tier
        self._tier = new_tier
        sod_eq     = self._snapshot.opening_equity if self._snapshot else equity
        loss_usdt  = max(0.0, sod_eq - equity)

        _LOG_LEVELS = {
            DailyLossTier.WARNING:   (logger.warning,  f"⚠  DAILY LOSS WARNING   | -{loss_pct:.2f}% (${loss_usdt:,.2f}) | sizes → 75%  [was {old_tier.value}]"),
            DailyLossTier.CAUTIOUS:  (logger.warning,  f"🔶 DAILY LOSS CAUTIOUS  | -{loss_pct:.2f}% (${loss_usdt:,.2f}) | sizes → 50%  [was {old_tier.value}]"),
            DailyLossTier.HALT:      (logger.error,    f"🛑 DAILY LOSS HALT      | -{loss_pct:.2f}% (${loss_usdt:,.2f}) | NO new trades — existing stops only  [was {old_tier.value}]"),
            DailyLossTier.REDUCE:    (logger.error,    f"🔴 DAILY LOSS REDUCE    | -{loss_pct:.2f}% (${loss_usdt:,.2f}) | CLOSE-ONLY — tightening open stops  [was {old_tier.value}]"),
            DailyLossTier.EMERGENCY: (logger.critical, f"🚨 DAILY LOSS EMERGENCY | -{loss_pct:.2f}% (${loss_usdt:,.2f}) | CLOSING ALL POSITIONS  [was {old_tier.value}]"),
        }
        if new_tier in _LOG_LEVELS:
            log_fn, msg = _LOG_LEVELS[new_tier]
            log_fn(msg)

    def allows_new_buys(self, current_equity: float = 0.0) -> bool:
        """
        False at HALT, REDUCE, or EMERGENCY.

        current_equity arg retained for backward compatibility — not used
        (tier is already set by the most recent update() call).
        """
        return self._tier in (DailyLossTier.NORMAL, DailyLossTier.WARNING, DailyLossTier.CAUTIOUS)

    def size_multiplier(self) -> float:
        """
        Trade size multiplier to apply to all new BUY positions.

          NORMAL    → 1.00  (no change)
          WARNING   → 0.75  (-25%)
          CAUTIOUS  → 0.50  (-50%)
          HALT      → 0.00  (blocked)
          REDUCE    → 0.00  (blocked)
          EMERGENCY → 0.00  (blocked)

        Multiply this against the circuit-breaker size_multiplier() in run_candle().
        """
        for _, tier, mult in _TIER_TABLE:
            if self._tier == tier:
                return mult
        return 1.0  # NORMAL

    def is_close_only(self) -> bool:
        """True when only position exits are allowed (REDUCE or EMERGENCY)."""
        return self._tier in (DailyLossTier.REDUCE, DailyLossTier.EMERGENCY)

    def needs_emergency_close(self) -> bool:
        """
        Returns True exactly ONCE when EMERGENCY tier is first entered this day.

        PortfolioManager calls this each candle; the one-shot flag prevents
        repeat market-close attempts on subsequent candles at the same tier.
        """
        if self._tier == DailyLossTier.EMERGENCY and not self._emergency_triggered:
            self._emergency_triggered = True
            return True
        return False

    def current_loss_pct(self, current_equity: float) -> float:
        """Today's portfolio loss as a positive %. 0 if still profitable."""
        return max(0.0, self._loss_pct(current_equity, self._snapshot))

    def is_tripped(self) -> bool:
        """Backward-compat helper: True at HALT or worse (replaces old _tripped_today)."""
        return self._tier in (DailyLossTier.HALT, DailyLossTier.REDUCE, DailyLossTier.EMERGENCY)

    def day_open_equity(self) -> Optional[float]:
        """SOD equity (None until first update() call today)."""
        return self._snapshot.opening_equity if self._snapshot else None

    @property
    def tier(self) -> DailyLossTier:
        return self._tier

    # ── Checkpoint (restart-safety) ───────────────────────────────────────────

    def save_state(self) -> dict:
        """
        Return a serialisable dict to embed in the portfolio checkpoint JSON.

        Call from PortfolioManager.save_checkpoint():
            data["daily_loss_guard"] = self._daily_guard.save_state()
        """
        return {
            "sod_equity":           self._snapshot.opening_equity if self._snapshot else 0.0,
            "sod_date":             self._snapshot.day.isoformat() if self._snapshot else "",
            "tier":                 self._tier.value,
            "emergency_triggered":  self._emergency_triggered,
        }

    def restore_state(self, data: dict) -> None:
        """
        Restore from the embedded dict in the portfolio checkpoint.

        Safe to call with an empty dict — becomes a no-op.
        If the checkpoint is from a previous UTC day, resets to NORMAL
        (yesterday's losses don't carry forward).

        Call from PortfolioManager.load_checkpoint():
            dlg = data.get("daily_loss_guard")
            if dlg:
                self._daily_guard.restore_state(dlg)
        """
        saved_date_str = data.get("sod_date", "")
        today_str      = datetime.now(timezone.utc).date().isoformat()

        if not saved_date_str:
            return

        if saved_date_str == today_str:
            try:
                saved_date = date.fromisoformat(saved_date_str)
                sod_eq     = float(data.get("sod_equity", 0.0))
                if sod_eq > 0:
                    self._snapshot = DailySnapshot(day=saved_date, opening_equity=sod_eq)
                try:
                    self._tier = DailyLossTier(data.get("tier", "NORMAL"))
                except ValueError:
                    self._tier = DailyLossTier.NORMAL
                self._emergency_triggered = bool(data.get("emergency_triggered", False))
                logger.info(
                    f"[DailyLossGuard] Restored | SOD: ${sod_eq:,.2f} | "
                    f"Tier: {self._tier.value} | Date: {saved_date_str}"
                )
            except Exception as exc:
                logger.warning(f"[DailyLossGuard] Checkpoint restore failed ({exc}) — starting fresh.")
        else:
            logger.info(
                f"[DailyLossGuard] Checkpoint from {saved_date_str}, "
                f"today is {today_str} — resetting to NORMAL."
            )

    # ── Per-strategy interface (unchanged from v1) ────────────────────────────

    def update_slot(self, slot_name: str, slot_equity: float) -> None:
        """
        Register the current equity for a specific strategy slot.
        Sets the slot's day-open snapshot on the first call each UTC day.
        Call once per candle per active slot, before allows_slot_buy().
        """
        today = datetime.now(timezone.utc).date()
        snap  = self._slot_snapshots.get(slot_name)
        if snap is None or snap.day != today:
            self._slot_snapshots[slot_name] = DailySnapshot(day=today, opening_equity=slot_equity)
            self._slot_tripped[slot_name]   = False

    def allows_slot_buy(self, slot_name: str, slot_equity: float) -> bool:
        """Return True if this strategy slot is allowed to open a new buy."""
        if self._slot_tripped.get(slot_name, False):
            return False
        snap     = self._slot_snapshots.get(slot_name)
        max_loss = self.strategy_limits.get(slot_name)
        if snap is None or max_loss is None:
            return True
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
        loss  = self.current_loss_pct(current_equity)
        lines = [
            f"DailyLossGuard [{self._snapshot.day} UTC]  "
            f"SOD: ${self._snapshot.opening_equity:,.2f}  "
            f"Now: ${current_equity:,.2f}  "
            f"Loss: -{loss:.2f}%  "
            f"Tier: [{self._tier.value}]  "
            f"SizeMult: {self.size_multiplier():.0%}"
        ]
        for slot in self.tripped_slots():
            lines.append(f"  ⚠ {slot}: slot daily limit hit")
        return "\n".join(lines)

    # ── Internal ──────────────────────────────────────────────────────────────

    @staticmethod
    def _loss_pct(current_equity: float, snap: Optional[DailySnapshot]) -> float:
        """Loss % from opening equity. Positive = loss, negative = gain."""
        if snap is None or snap.opening_equity <= 0:
            return 0.0
        return (snap.opening_equity - current_equity) / snap.opening_equity * 100
