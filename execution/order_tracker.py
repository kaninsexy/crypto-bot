"""
execution/order_tracker.py — Persistent ledger of all live orders.

WHY THIS EXISTS
───────────────
When the bot crashes, restarts, or loses connection mid-trade, we need to
know exactly what orders are open on the exchange so we can reconcile state
before firing new ones.  This tracker maintains a local JSONL (JSON Lines)
log — one JSON object per line — so every order is durably recorded even if
the process dies between the write and the exchange confirmation.

It also gives us:
  • A live summary dashboard row per slot ("last trade at ...")
  • PnL reconciliation vs paper sim
  • Audit trail for taxes / manual review

JSONL FORMAT
────────────
Each line in the log file is a self-contained JSON object:
    {
      "ts":         "2026-04-02T09:15:00Z",   ISO 8601 UTC timestamp
      "slot":       "DCA",
      "action":     "BUY",
      "order_id":   "12345678",
      "fill_price": 84250.0,
      "filled_qty": 0.001184,
      "fee_usdt":   0.0998,
      "sl_id":      "12345679",
      "tp_id":      "12345680",
      "oco_id":     "",
      "is_short":   false,
      "error":      "",
      "reason":     "DCA base order: ..."
    }

USAGE
─────
    from execution.order_tracker import OrderTracker

    tracker = OrderTracker(log_path="logs/live_orders.jsonl")

    # After each execution:
    tracker.record_execution(result, signal)

    # On restart — check what was open when we died:
    tracker.load()
    for order in tracker.open_entries():
        print(order)

    # Get a summary table:
    print(tracker.summary())

    # Access all trades for PnL calculation:
    df = tracker.as_dataframe()
"""

import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional
import pandas as pd
from loguru import logger

from strategies.base import Signal
from execution.ccxt_executor import ExecutionResult


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class TrackedOrder:
    """
    A single execution event recorded to the order log.

    Each field maps directly to the JSONL columns described in the module
    docstring.  Paired BUY→SELL rows can be joined on slot_name to compute
    round-trip PnL.

    Attributes:
        ts:           UTC timestamp of when the order was recorded (not exchange time).
        slot:         Portfolio slot that triggered this signal (e.g. "DCA").
        action:       "BUY" or "SELL".
        order_id:     Primary exchange order ID.
        fill_price:   Average fill price in USDT.
        filled_qty:   Base-currency quantity filled.
        fee_usdt:     Estimated fee charged in USDT.
        sl_order_id:  Stop-loss order ID (if any).
        tp_order_id:  Take-profit order ID (if any).
        oco_order_id: OCO list order ID (if any; covers both SL+TP legs).
        is_short:     True if this was a futures short entry/exit.
        error:        Non-empty if the execution failed.
        reason:       The strategy's human-readable signal reason.
        amount_usdt:  USDT value of the position at entry (from Kelly sizing).
    """
    ts:           str
    slot:         str
    action:       str
    order_id:     str   = ""
    fill_price:   float = 0.0
    filled_qty:   float = 0.0
    fee_usdt:     float = 0.0
    sl_order_id:  str   = ""
    tp_order_id:  str   = ""
    oco_order_id: str   = ""
    is_short:     bool  = False
    error:        str   = ""
    reason:       str   = ""
    amount_usdt:  float = 0.0

    def is_failed(self) -> bool:
        return bool(self.error)

    def is_entry(self) -> bool:
        return self.action == "BUY"

    def is_exit(self) -> bool:
        return self.action == "SELL"

    def notional_usdt(self) -> float:
        """Entry/exit value in USDT = fill_price × filled_qty."""
        return self.fill_price * self.filled_qty

    def __str__(self) -> str:
        status = "FAIL" if self.error else "OK"
        return (
            f"[{self.ts[:19]}] [{status}] {self.slot:<15} {self.action:<4} "
            f"@ {self.fill_price:<12.4f} qty={self.filled_qty:.6f}  "
            f"id={self.order_id[:8] if self.order_id else 'n/a':<10}  "
            f"{'SHORT' if self.is_short else 'LONG'}"
        )


# ── Tracker ───────────────────────────────────────────────────────────────────

class OrderTracker:
    """
    Maintains a JSONL log of every live execution and exposes query helpers.

    Thread-safe at the file-write level (each write is a single line append).
    Not designed for concurrent multi-process access to the same log file.

    Args:
        log_path:  Path to the JSONL log file.
                   Directories are created automatically if they don't exist.
    """

    def __init__(self, log_path: str = "logs/live_orders.jsonl"):
        self.log_path = log_path
        self._orders: list[TrackedOrder] = []
        self._ensure_log_dir()
        self.load()

    # ── Public API ────────────────────────────────────────────────────────────

    def record_execution(
        self,
        result: ExecutionResult,
        signal: Signal,
    ) -> TrackedOrder:
        """
        Record a CCXTExecutor result to both the in-memory list and the JSONL file.

        Always call this — even for failed executions — so failures are logged.
        Returns the TrackedOrder that was recorded.
        """
        order = TrackedOrder(
            ts           = _utcnow(),
            slot         = result.slot_name,
            action       = result.action,
            order_id     = result.order_id,
            fill_price   = result.fill_price,
            filled_qty   = result.filled_qty,
            fee_usdt     = result.fee_usdt,
            sl_order_id  = result.sl_order_id,
            tp_order_id  = result.tp_order_id,
            oco_order_id = result.oco_order_id,
            is_short     = result.is_short,
            error        = result.error,
            reason       = signal.reason[:200],   # Truncate for log brevity
            amount_usdt  = signal.metadata.get("amount_usdt", 0.0),
        )

        self._orders.append(order)
        self._append_to_file(order)

        if order.is_failed():
            logger.warning(f"[OrderTracker] Failed order recorded: {order}")
        else:
            logger.debug(f"[OrderTracker] Recorded: {order}")

        return order

    def load(self) -> int:
        """
        Load all orders from the JSONL log file into memory.

        Called automatically on construction so state survives restarts.
        Returns the number of orders loaded.
        """
        if not os.path.exists(self.log_path):
            logger.debug(f"[OrderTracker] No existing log at {self.log_path} — starting fresh.")
            return 0

        loaded = 0
        errors = 0
        with open(self.log_path, "r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    order = TrackedOrder(**data)
                    self._orders.append(order)
                    loaded += 1
                except Exception as e:
                    errors += 1
                    logger.warning(
                        f"[OrderTracker] Could not parse line {line_no} "
                        f"in {self.log_path}: {e}"
                    )

        if loaded:
            logger.info(
                f"[OrderTracker] Loaded {loaded} orders from {self.log_path}"
                + (f" ({errors} parse errors)" if errors else "")
            )
        return loaded

    def all_orders(self) -> list[TrackedOrder]:
        """Return all orders (entry + exit) in chronological order."""
        return list(self._orders)

    def successful_orders(self) -> list[TrackedOrder]:
        """Return only successfully executed orders (no error)."""
        return [o for o in self._orders if not o.is_failed()]

    def failed_orders(self) -> list[TrackedOrder]:
        """Return only failed executions."""
        return [o for o in self._orders if o.is_failed()]

    def open_entries(self) -> list[TrackedOrder]:
        """
        Return BUY orders that have not yet been matched by a SELL.

        Uses a simple greedy match: for each slot, count how many buys
        vs sells exist — the excess buys are "open".

        This is an approximation for cases with partial fills or tranche
        exits.  For precise position tracking, reconcile against the
        exchange's open positions via CCXTExecutor.sync_balance().
        """
        from collections import Counter
        buys   = Counter(o.slot for o in self._orders if o.action == "BUY"  and not o.is_failed())
        sells  = Counter(o.slot for o in self._orders if o.action == "SELL" and not o.is_failed())

        open_slots = {slot for slot, count in buys.items() if count > sells.get(slot, 0)}

        # Return the most recent BUY for each open slot
        result = []
        for slot in open_slots:
            slot_buys = [o for o in self._orders if o.slot == slot and o.action == "BUY" and not o.is_failed()]
            if slot_buys:
                result.append(slot_buys[-1])
        return result

    def orders_for_slot(self, slot: str) -> list[TrackedOrder]:
        """Return all orders for a specific strategy slot, newest first."""
        return [o for o in reversed(self._orders) if o.slot == slot]

    def last_order_for_slot(self, slot: str) -> Optional[TrackedOrder]:
        """Return the most recent order for a slot, or None."""
        for o in reversed(self._orders):
            if o.slot == slot:
                return o
        return None

    def as_dataframe(self) -> pd.DataFrame:
        """
        Return all orders as a pandas DataFrame for analysis.

        Useful for computing live PnL, win rate, fee totals, etc.
        """
        if not self._orders:
            return pd.DataFrame(columns=[
                "ts", "slot", "action", "order_id", "fill_price",
                "filled_qty", "fee_usdt", "is_short", "error",
                "reason", "amount_usdt",
            ])
        rows = [asdict(o) for o in self._orders]
        df = pd.DataFrame(rows)
        df["ts"] = pd.to_datetime(df["ts"], utc=True)
        return df

    def pnl_summary(self) -> dict:
        """
        Compute a rough PnL summary across all matched BUY→SELL pairs.

        Returns a dict with keys: total_fees, gross_pnl, net_pnl, n_trades.
        Only counts complete round-trips (one BUY + one SELL per slot).
        This is approximate — partial fills are not precisely handled.
        """
        from collections import defaultdict
        entries: dict = defaultdict(list)
        exits:   dict = defaultdict(list)

        for o in self.successful_orders():
            if o.action == "BUY":
                entries[o.slot].append(o)
            else:
                exits[o.slot].append(o)

        gross_pnl  = 0.0
        total_fees = 0.0
        n_trades   = 0

        for slot in entries:
            slot_entries = entries[slot]
            slot_exits   = exits.get(slot, [])

            for entry, exit_ in zip(slot_entries, slot_exits):
                if entry.is_short:
                    # Short profit: sell high, buy low
                    trade_pnl = (entry.fill_price - exit_.fill_price) * exit_.filled_qty
                else:
                    # Long profit: buy low, sell high
                    trade_pnl = (exit_.fill_price - entry.fill_price) * exit_.filled_qty

                fees = entry.fee_usdt + exit_.fee_usdt
                gross_pnl  += trade_pnl
                total_fees += fees
                n_trades   += 1

        return {
            "gross_pnl":  round(gross_pnl, 4),
            "total_fees": round(total_fees, 4),
            "net_pnl":    round(gross_pnl - total_fees, 4),
            "n_trades":   n_trades,
        }

    def summary(self, n_recent: int = 10) -> str:
        """
        Human-readable summary of recent orders and overall statistics.
        """
        lines = [
            "",
            "=" * 72,
            "  ORDER TRACKER SUMMARY",
            "=" * 72,
            f"  Log file  : {self.log_path}",
            f"  Total     : {len(self._orders)} orders  "
            f"({len(self.failed_orders())} failed)",
            f"  Open      : {len(self.open_entries())} open entries",
        ]

        pnl = self.pnl_summary()
        if pnl["n_trades"] > 0:
            lines.extend([
                f"  Closed    : {pnl['n_trades']} round-trips",
                f"  Gross PnL : ${pnl['gross_pnl']:+.2f}  "
                f"Net PnL: ${pnl['net_pnl']:+.2f}  "
                f"Fees: ${pnl['total_fees']:.2f}",
            ])

        if self._orders:
            recent = self._orders[-n_recent:]
            lines.append("")
            lines.append(f"  LAST {min(n_recent, len(self._orders))} ORDERS:")
            lines.append("  " + "-" * 68)
            for o in reversed(recent):
                lines.append(f"  {o}")

        lines.extend(["", "=" * 72])
        return "\n".join(lines)

    # ── Private helpers ───────────────────────────────────────────────────────

    def _ensure_log_dir(self) -> None:
        """Create the directory for log_path if it doesn't exist."""
        log_dir = os.path.dirname(self.log_path)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)

    def _append_to_file(self, order: TrackedOrder) -> None:
        """Append a single TrackedOrder as a JSON line to the log file."""
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(order), ensure_ascii=False) + "\n")
        except Exception as e:
            logger.error(
                f"[OrderTracker] Failed to write to {self.log_path}: {e}. "
                f"Order is in memory but NOT persisted to disk."
            )


# ── Utility ───────────────────────────────────────────────────────────────────

def _utcnow() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
