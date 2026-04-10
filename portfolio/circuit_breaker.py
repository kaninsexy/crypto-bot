"""
portfolio/circuit_breaker.py — Portfolio Circuit Breaker (Phase E)

PURPOSE
───────
Protects against catastrophic losses by monitoring the total portfolio
equity across all strategy simulators and halting trading when a
configurable drawdown threshold is breached.

ANALOGY
───────
Think of this as the breaker in your electrical panel: when the load is
too high (a crash wipes 30%+ of your portfolio), the breaker trips and
cuts power to NEW entries. Existing positions can still close (SL/TP).
Once the load returns to a safe level (equity recovers to -15% from peak),
the breaker resets and trading resumes.

THRESHOLDS (configurable defaults)
────────────────────────────────────
  TRIP threshold  : -30% from portfolio peak equity (hard stop)
  WARN threshold  : -15% from portfolio peak equity (warning + reduce sizing)
  RESET threshold : -15% from peak (drawdown has recovered enough to resume)

STATES
──────
  NORMAL   → Trading allowed at full Kelly size
  WARNING  → Drawdown > WARN% — reduce all trade sizes by 50%
  TRIPPED  → Drawdown > TRIP% — block all NEW buys; only allow sells
  RESETTING → Post-trip, drawdown recovering; block buys until RESET threshold met

RECOVERY LOGIC
──────────────
  1. Circuit breaker trips at -30%.
  2. All NEW buy signals are blocked. Existing positions still SL/TP normally.
  3. Drawdown may deepen further while positions unwind — that's OK.
  4. Once drawdown improves to ≤ RESET% below peak, breaker resets automatically.
  5. Peak equity is updated only after a FULL RESET (not during drawdown).
  6. An optional LOCKOUT period (in candles) prevents premature resets on
     "dead cat bounces". Default: 24 candles (1 day on 1h data).

USAGE
─────
    from portfolio.circuit_breaker import CircuitBreaker, BreakerState

    cb = CircuitBreaker(trip_pct=30.0, warn_pct=15.0, reset_pct=15.0)

    # After each candle, call update() with current total portfolio equity
    state = cb.update(current_equity=9_200, candle_count=450)
    print(state)  # BreakerState.NORMAL / WARNING / TRIPPED / RESETTING

    # Before placing any new BUY:
    if cb.allows_new_buys():
        execute_buy()

    # Get sizing multiplier (1.0 = full, 0.5 = halved in WARNING, 0.0 = blocked)
    multiplier = cb.size_multiplier()
    trade_usdt = kelly_size * multiplier
"""

from __future__ import annotations
import enum
from dataclasses import dataclass
from datetime import datetime, timezone
from loguru import logger


# ── States ────────────────────────────────────────────────────────────────────

class BreakerState(enum.Enum):
    NORMAL     = "NORMAL"      # Trading full size
    WARNING    = "WARNING"     # Drawdown warning — reduce size 50%
    TRIPPED    = "TRIPPED"     # Hard stop — no new buys
    RESETTING  = "RESETTING"   # Post-trip recovery in progress


# ── Event log entry ───────────────────────────────────────────────────────────

@dataclass
class BreakerEvent:
    timestamp:      datetime
    event_type:     str         # "TRIP", "WARN", "RESET", "PEAK_UPDATE", "CANDLE"
    equity:         float
    peak_equity:    float
    drawdown_pct:   float
    state:          str
    message:        str = ""


# ── Circuit Breaker ───────────────────────────────────────────────────────────

class CircuitBreaker:
    """
    Portfolio-level drawdown protection with automatic state machine management.

    Thread-safety: Not thread-safe. Call from a single event loop (main bot loop).
    """

    def __init__(
        self,
        initial_equity: float    = 10_000.0,
        trip_pct: float          = 30.0,      # Trip at -30% from peak
        warn_pct: float          = 15.0,      # Warn at -15% from peak
        reset_pct: float         = 15.0,      # Reset when drawdown ≤ reset_pct% below peak
        lockout_candles: int     = 24,        # Min candles after trip before reset is possible
        warning_size_mult: float = 0.50,      # Size multiplier in WARNING state
    ):
        """
        Args:
            initial_equity:      Portfolio starting equity (sets initial peak).
            trip_pct:            Drawdown % from peak that triggers the breaker (30 = -30%).
            warn_pct:            Drawdown % that triggers WARNING (reduces trade size).
            reset_pct:           Drawdown must recover to this % below peak for reset.
                                 E.g. 15 means equity must be within -15% of peak.
            lockout_candles:     Minimum candles before reset is possible after a trip.
            warning_size_mult:   Trade size multiplier when in WARNING state.
        """
        self.trip_pct          = trip_pct / 100
        self.warn_pct          = warn_pct / 100
        self.reset_pct         = reset_pct / 100
        self.lockout_candles   = lockout_candles
        self.warning_size_mult = warning_size_mult

        self._peak_equity:   float = initial_equity
        self._state:         BreakerState = BreakerState.NORMAL
        self._trip_candle:   int   = 0       # candle count when trip occurred
        self._candle_count:  int   = 0
        self._trip_equity:   float = 0.0    # equity at time of trip
        self._min_equity:    float = initial_equity  # lowest equity ever seen

        self.event_log: list[BreakerEvent] = []
        self.initial_equity = initial_equity

        logger.info(
            f"CircuitBreaker initialized | "
            f"Trip={trip_pct:.0f}% | Warn={warn_pct:.0f}% | "
            f"Reset={reset_pct:.0f}% | Lockout={lockout_candles}c"
        )

    # ── Core update ─────────────────────────────────────────────────────────

    def update(self, current_equity: float) -> BreakerState:
        """
        Update the circuit breaker with the latest portfolio equity.

        Call this every candle with the total portfolio equity
        (sum of all strategy simulators: cash + open position MTM).

        Args:
            current_equity: Total portfolio equity in USDT.

        Returns:
            Current BreakerState.
        """
        self._candle_count += 1

        # Track minimum equity (for reporting)
        if current_equity < self._min_equity:
            self._min_equity = current_equity

        # Update peak (only when not in TRIPPED or RESETTING state)
        if self._state in (BreakerState.NORMAL, BreakerState.WARNING):
            if current_equity > self._peak_equity:
                old_peak = self._peak_equity
                self._peak_equity = current_equity
                self._log_event("PEAK_UPDATE", current_equity,
                                f"Peak updated: ${old_peak:,.0f} → ${current_equity:,.0f}")

        # Current drawdown from peak (positive = below peak)
        dd_pct = (self._peak_equity - current_equity) / self._peak_equity

        # ── State transitions ─────────────────────────────────────────
        new_state = self._evaluate_state(current_equity, dd_pct)

        if new_state != self._state:
            self._transition(new_state, current_equity, dd_pct)

        return self._state

    def _evaluate_state(self, current_equity: float, dd_pct: float) -> BreakerState:
        """Determine what state the breaker SHOULD be in given current data."""

        if self._state == BreakerState.TRIPPED:
            candles_since_trip = self._candle_count - self._trip_candle
            lockout_done       = candles_since_trip >= self.lockout_candles

            if lockout_done and dd_pct <= self.reset_pct:
                # Recovery confirmed — begin resetting
                return BreakerState.RESETTING
            return BreakerState.TRIPPED

        if self._state == BreakerState.RESETTING:
            # Only fully reset when drawdown is below WARN level (not just RESET)
            if dd_pct <= self.warn_pct * 0.5:   # Full clear at half of warn threshold
                return BreakerState.NORMAL
            return BreakerState.RESETTING

        # From NORMAL or WARNING:
        if dd_pct >= self.trip_pct:
            return BreakerState.TRIPPED
        elif dd_pct >= self.warn_pct:
            return BreakerState.WARNING
        else:
            return BreakerState.NORMAL

    def _transition(self, new_state: BreakerState, equity: float, dd_pct: float):
        """Execute a state transition with logging."""
        old_state = self._state
        self._state = new_state

        if new_state == BreakerState.TRIPPED:
            self._trip_candle = self._candle_count
            self._trip_equity = equity
            msg = (
                f"🚨 CIRCUIT BREAKER TRIPPED | "
                f"Equity: ${equity:,.0f} | "
                f"Peak: ${self._peak_equity:,.0f} | "
                f"Drawdown: -{dd_pct*100:.1f}% (threshold: -{self.trip_pct*100:.0f}%) | "
                f"ALL NEW BUYS BLOCKED until recovery to -{self.reset_pct*100:.0f}%"
            )
            logger.critical(msg)
            self._log_event("TRIP", equity, msg)

        elif new_state == BreakerState.WARNING:
            msg = (
                f"⚠ CIRCUIT BREAKER WARNING | "
                f"Equity: ${equity:,.0f} | "
                f"Drawdown: -{dd_pct*100:.1f}% (warn: -{self.warn_pct*100:.0f}%) | "
                f"Trade sizes reduced to {self.warning_size_mult*100:.0f}%"
            )
            logger.warning(msg)
            self._log_event("WARN", equity, msg)

        elif new_state == BreakerState.RESETTING:
            candles_since = self._candle_count - self._trip_candle
            msg = (
                f"🔄 CIRCUIT BREAKER RESETTING | "
                f"Post-trip recovery in progress | "
                f"Candles since trip: {candles_since} | "
                f"Drawdown now: -{dd_pct*100:.1f}%"
            )
            logger.info(msg)
            self._log_event("RESETTING", equity, msg)

        elif new_state == BreakerState.NORMAL:
            msg = (
                f"✅ CIRCUIT BREAKER RESET | "
                f"Trading resumed at full size | "
                f"Equity: ${equity:,.0f} | "
                f"Drawdown: -{dd_pct*100:.1f}%"
            )
            logger.info(msg)
            # Update peak to current equity on full reset
            if self._state == BreakerState.NORMAL and old_state == BreakerState.RESETTING:
                self._peak_equity = equity
            self._log_event("RESET", equity, msg)

    # ── Query interface ──────────────────────────────────────────────────────

    def allows_new_buys(self) -> bool:
        """True if the breaker allows new BUY signals to be executed."""
        return self._state in (BreakerState.NORMAL, BreakerState.WARNING)

    def size_multiplier(self) -> float:
        """
        Return a trade size multiplier for the current state.

          NORMAL    → 1.0  (full Kelly size)
          WARNING   → 0.5  (half size)
          TRIPPED   → 0.0  (blocked)
          RESETTING → 0.0  (still blocked — wait for full reset)
        """
        return {
            BreakerState.NORMAL:    1.0,
            BreakerState.WARNING:   self.warning_size_mult,
            BreakerState.TRIPPED:   0.0,
            BreakerState.RESETTING: 0.0,
        }[self._state]

    def current_drawdown_pct(self, current_equity: float) -> float:
        """Return current drawdown from peak as a positive percentage."""
        return (self._peak_equity - current_equity) / self._peak_equity * 100

    @property
    def state(self) -> BreakerState:
        return self._state

    @property
    def peak_equity(self) -> float:
        return self._peak_equity

    @property
    def min_equity(self) -> float:
        return self._min_equity

    # ── Event log ────────────────────────────────────────────────────────────

    def _log_event(self, event_type: str, equity: float, message: str):
        dd = (self._peak_equity - equity) / self._peak_equity * 100
        self.event_log.append(BreakerEvent(
            timestamp=datetime.now(timezone.utc),
            event_type=event_type,
            equity=equity,
            peak_equity=self._peak_equity,
            drawdown_pct=dd,
            state=self._state.value,
            message=message,
        ))

    # ── Summary ─────────────────────────────────────────────────────────────

    def summary(self, current_equity: float) -> str:
        dd = self.current_drawdown_pct(current_equity)
        max_dd = self.current_drawdown_pct(self._min_equity)
        trips  = sum(1 for e in self.event_log if e.event_type == "TRIP")
        resets = sum(1 for e in self.event_log if e.event_type == "RESET")
        warns  = sum(1 for e in self.event_log if e.event_type == "WARN")

        state_str = self._state.value
        if self._state == BreakerState.TRIPPED:
            lockout_remaining = max(0, self.lockout_candles - (self._candle_count - self._trip_candle))
            state_str += f" (lockout: {lockout_remaining} candles remaining)"

        lines = [
            "─" * 56,
            "  CIRCUIT BREAKER STATUS",
            "─" * 56,
            f"  State          : {state_str}",
            f"  Size Multiplier: {self.size_multiplier():.1%}",
            f"  Current Equity : ${current_equity:>10,.2f}",
            f"  Peak Equity    : ${self._peak_equity:>10,.2f}",
            f"  Current DD     : -{dd:>8.2f}%  (trip @ -{self.trip_pct*100:.0f}%)",
            f"  Max Drawdown   : -{max_dd:>8.2f}%",
            f"  Events: {trips} trips | {resets} resets | {warns} warnings",
            f"  Candles tracked: {self._candle_count}",
            "─" * 56,
        ]
        return "\n".join(lines)
