"""
portfolio/reconciler.py — Exchange reconciliation on startup (live mode only).

Compares the bot's internal position state (restored from checkpoint) against
actual open positions on the exchange. Called once after load_checkpoint().

WHY THIS EXISTS
───────────────
Between bot restarts, three bad things can happen without this guard:

  1. GHOST position — someone opened a trade manually on Binance, or a
     previous bot crash placed an order the checkpoint never recorded.
     The bot has no idea this exists, so it will never close it.

  2. ZOMBIE position — the bot's checkpoint says there's an open position,
     but the exchange already closed it (TP hit, liquidation, manual close).
     Without detection, the bot will never place a new trade on that slot.

  3. QUANTITY MISMATCH — partial fills, manual reduces, or rounding errors
     mean the bot's tracked quantity no longer matches reality. Future P&L
     calculations will be wrong.

DISCREPANCY HANDLING
────────────────────
  GHOST    — position exists on exchange but not in bot checkpoint
             → logs CRITICAL, optionally sends a reduce-only market close
               (controlled by RECONCILE_AUTO_CLOSE_GHOST in .env)

  ZOMBIE   — position exists in bot checkpoint but not on exchange
             → clears bot internal state so the strategy slot can resume
               trading; logs WARNING with details

  MISMATCH — position exists on both sides but quantities differ by > 1%
             → logs WARNING; exchange quantity is used as source of truth
               and bot quantities are scaled proportionally

FAULT TOLERANCE
───────────────
  - Paper mode → immediate no-op; zero impact on behavior.
  - Exchange unreachable (network error, timeout) → startup continues
    safely with a warning. The timeout is configurable via
    RECONCILE_TIMEOUT_SECONDS (default 15s).
  - Any exception inside reconciliation → logged, bot starts normally.
    The guard never crashes startup.

CONFIGURATION (.env)
────────────────────
  RECONCILE_AUTO_CLOSE_GHOST=false   # true → auto-close unknowns
  RECONCILE_TIMEOUT_SECONDS=15       # API fetch timeout
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from loguru import logger

import config

if TYPE_CHECKING:
    import ccxt
    from portfolio.manager import PortfolioManager


# ── Thresholds ────────────────────────────────────────────────────────────────

# Warn if abs(exchange_qty - bot_qty) / exchange_qty exceeds this fraction.
MISMATCH_THRESHOLD: float = 0.01   # 1%


# ── Public entry point ────────────────────────────────────────────────────────

def reconcile_on_startup(
    pm: "PortfolioManager",
    exchange: "ccxt.binance",
) -> dict:
    """
    Main entry point. Call this once after pm.load_checkpoint() returns.

    Args:
        pm:       PortfolioManager with slots already initialized + checkpoint loaded.
        exchange: Live ccxt exchange instance (created by create_exchange()).

    Returns:
        A report dict. Keys:
          "mode"        → "paper" or "live"
          "skipped"     → True if paper mode or exchange unreachable
          "error"       → error message string (if skipped due to exception)
          "ghosts"      → list of ghost position dicts
          "zombies"     → list of zombie position dicts
          "mismatches"  → list of mismatch position dicts
          "matched"     → list of clean-match position dicts
    """
    # ── Paper mode: hard no-op ────────────────────────────────────────────────
    if config.TRADING_MODE != "live":
        logger.debug("[Reconciler] Paper mode — skipping exchange reconciliation.")
        return {"mode": "paper", "skipped": True}

    logger.info(
        "[Reconciler] 🔍 Starting exchange reconciliation "
        "(checkpoint vs live Binance USDT-M Futures positions)..."
    )

    # ── Fetch exchange positions with timeout guard ───────────────────────────
    exchange_positions: dict = {}
    try:
        exchange_positions = _fetch_exchange_positions(exchange)
    except Exception as exc:
        logger.warning(
            f"[Reconciler] ⚠️  Could not fetch positions from Binance "
            f"({type(exc).__name__}: {exc}). "
            f"Startup will continue WITHOUT reconciliation. "
            f"Verify positions manually before trading goes live."
        )
        report = {"mode": "live", "skipped": True, "error": str(exc)}
        _log_report(report)
        return report

    # ── Collect bot's internal positions from checkpoint ──────────────────────
    bot_positions: dict = _collect_bot_positions(pm)

    # ── Compare and handle discrepancies ──────────────────────────────────────
    report = _compare_and_reconcile(pm, exchange, exchange_positions, bot_positions)

    # ── Log final summary ─────────────────────────────────────────────────────
    _log_report(report)

    return report


# ── Exchange position fetch ───────────────────────────────────────────────────

def _fetch_exchange_positions(exchange: "ccxt.binance") -> dict:
    """
    Fetch all open positions from Binance USDT-M Futures.

    Returns: {"{SYMBOL}:{side}" → {"symbol", "side", "qty", "entry"}}
    Only includes positions where quantity > 0 (actually open).

    Temporarily switches the exchange to "future" context and always
    restores it to "spot" on exit. Applies RECONCILE_TIMEOUT_SECONDS
    to cap the API call duration.
    """
    original_type    = exchange.options.get("defaultType", "spot")
    original_timeout = getattr(exchange, "timeout", 30_000)
    timeout_ms       = config.RECONCILE_TIMEOUT_SECONDS * 1_000

    try:
        exchange.options["defaultType"] = "future"
        exchange.timeout = timeout_ms

        raw: list = exchange.fetch_positions()

        result: dict = {}
        for pos in raw:
            qty = float(pos.get("contracts", 0) or 0)
            if abs(qty) < 1e-9:
                continue   # zero-size / closed position

            symbol = pos.get("symbol", "")
            norm   = _normalize_symbol(symbol)
            side   = (pos.get("side") or "long").lower()
            entry  = float(
                pos.get("entryPrice")
                or pos.get("averageEntryPrice")
                or 0
            )

            # Key by symbol+side so hedge-mode positions are kept separate
            key = f"{norm}:{side}"
            if key in result:
                result[key]["qty"] += qty   # aggregate if duplicate (hedge mode)
            else:
                result[key] = {
                    "symbol": norm,
                    "side":   side,
                    "qty":    qty,
                    "entry":  entry,
                }

        logger.debug(
            f"[Reconciler] Exchange positions fetched: "
            f"{len(result)} open ({len(raw)} total returned by API)"
        )
        return result

    finally:
        # Always restore original state even on exception
        exchange.options["defaultType"] = original_type
        exchange.timeout = original_timeout


def _normalize_symbol(symbol: str) -> str:
    """
    Convert ccxt futures symbols to spot-style notation.

    Examples:
      "BTC/USDT:USDT"  →  "BTC/USDT"
      "ETH/USDT:USDT"  →  "ETH/USDT"
      "BTC/USDT"       →  "BTC/USDT"   (already clean)
    """
    if ":" in symbol:
        return symbol.split(":")[0]
    return symbol


# ── Bot position collector ────────────────────────────────────────────────────

def _collect_bot_positions(pm: "PortfolioManager") -> dict:
    """
    Enumerate all open positions tracked in the bot's strategy slots.

    Returns: {strategy_name → {"symbol", "side", "qty", "entry", "slot", "pos_obj"}}

    "slot" and "pos_obj" are live references — mutating them directly
    updates the bot's internal state (used by the zombie handler).
    """
    result: dict = {}
    for sname, slot in pm._slots.items():
        pos = slot.simulator.position
        if pos is None:
            continue
        result[sname] = {
            "symbol":  pos.symbol,
            "side":    pos.side.lower(),
            "qty":     float(pos.quantity),
            "entry":   float(pos.avg_entry_price),
            "slot":    slot,
            "pos_obj": pos,
        }
    return result


# ── Core comparison logic ─────────────────────────────────────────────────────

def _compare_and_reconcile(
    pm: "PortfolioManager",
    exchange: "ccxt.binance",
    exchange_positions: dict,   # key = "SYMBOL:side"
    bot_positions: dict,        # key = strategy_name
) -> dict:
    """
    Three-way comparison between exchange and bot state.

    Iterates exchange positions first (finds ghosts and mismatches),
    then iterates remaining bot positions (finds zombies).

    Mutates bot state directly for zombies (position cleared) and
    mismatches (quantity scaled). Ghost auto-close is optional.
    """
    report = {
        "mode":       "live",
        "skipped":    False,
        "ghosts":     [],
        "zombies":    [],
        "mismatches": [],
        "matched":    [],
    }

    # Build reverse index: "{SYMBOL}:{side}" → [strategy_name, ...]
    # Needed to find which bot slots correspond to each exchange position.
    bot_by_key: dict[str, list[str]] = {}
    for sname, bpos in bot_positions.items():
        key = f"{bpos['symbol']}:{bpos['side']}"
        bot_by_key.setdefault(key, []).append(sname)

    # Track which bot strategies we've accounted for
    accounted_strategies: set[str] = set()

    # ── Pass 1: for each exchange position, find matching bot position(s) ──
    for ex_key, ex_pos in exchange_positions.items():
        sym    = ex_pos["symbol"]
        side   = ex_pos["side"]
        ex_qty = ex_pos["qty"]

        if ex_key in bot_by_key:
            # ── Match found ───────────────────────────────────────────────────
            snames = bot_by_key[ex_key]
            accounted_strategies.update(snames)

            # Sum quantities across all slots trading this (symbol, side)
            bot_total_qty = sum(bot_positions[sn]["qty"] for sn in snames)
            mismatch_frac = abs(ex_qty - bot_total_qty) / max(ex_qty, 1e-9)

            if mismatch_frac > MISMATCH_THRESHOLD:
                # ── Quantity mismatch ─────────────────────────────────────────
                logger.warning(
                    f"[Reconciler] ⚠️  QUANTITY MISMATCH | "
                    f"{sym} {side.upper()} | "
                    f"Exchange: {ex_qty:.6f} | "
                    f"Bot total: {bot_total_qty:.6f} | "
                    f"Drift: {mismatch_frac * 100:.2f}% | "
                    f"Slots: {snames} | "
                    f"Using exchange as source of truth."
                )
                _adjust_bot_quantities(snames, bot_positions, ex_qty)
                report["mismatches"].append({
                    "symbol":       sym,
                    "side":         side,
                    "exchange_qty": ex_qty,
                    "bot_qty":      bot_total_qty,
                    "mismatch_pct": mismatch_frac * 100,
                    "strategies":   snames,
                })
            else:
                # ── Clean match ───────────────────────────────────────────────
                logger.info(
                    f"[Reconciler] ✅ OK | "
                    f"{sym} {side.upper()} | "
                    f"Exchange: {ex_qty:.6f} | "
                    f"Bot: {bot_total_qty:.6f} | "
                    f"Slots: {snames}"
                )
                report["matched"].append({
                    "symbol":     sym,
                    "side":       side,
                    "qty":        ex_qty,
                    "strategies": snames,
                })

        else:
            # ── GHOST: exchange position with no matching bot slot ────────────
            logger.critical(
                f"[Reconciler] 🚨 GHOST POSITION | "
                f"{sym} {side.upper()} | "
                f"Qty: {ex_qty:.6f} | "
                f"Entry: ${ex_pos['entry']:,.4f} | "
                f"Not tracked by any strategy slot — possible manual trade "
                f"or crash during order placement."
            )
            report["ghosts"].append({
                "symbol": sym,
                "side":   side,
                "qty":    ex_qty,
                "entry":  ex_pos["entry"],
            })

            if config.RECONCILE_AUTO_CLOSE_GHOST:
                _auto_close_ghost(exchange, sym, side, ex_qty)
            else:
                logger.critical(
                    f"[Reconciler] RECONCILE_AUTO_CLOSE_GHOST=False — ghost left open. "
                    f"Close {sym} {side.upper()} manually in Binance or set "
                    f"RECONCILE_AUTO_CLOSE_GHOST=true in .env."
                )

    # ── Pass 2: bot positions not found on exchange = ZOMBIE ─────────────────
    for sname, bpos in bot_positions.items():
        if sname in accounted_strategies:
            continue   # Already verified in pass 1

        sym  = bpos["symbol"]
        side = bpos["side"]
        qty  = bpos["qty"]

        logger.warning(
            f"[Reconciler] ⚠️  ZOMBIE POSITION | "
            f"[{sname}] {sym} {side.upper()} | "
            f"Qty: {qty:.6f} | Entry: ${bpos['entry']:,.4f} | "
            f"Present in checkpoint but NOT on exchange "
            f"(externally closed, liquidated, or filled during crash). "
            f"Clearing bot state so slot can resume."
        )
        _mark_externally_closed(sname, bpos["slot"])

        report["zombies"].append({
            "symbol":   sym,
            "side":     side,
            "qty":      qty,
            "entry":    bpos["entry"],
            "strategy": sname,
        })

    return report


# ── State mutation helpers ────────────────────────────────────────────────────

def _adjust_bot_quantities(
    snames: list[str],
    bot_positions: dict,
    exchange_total_qty: float,
) -> None:
    """
    Proportionally scale all bot slot quantities so they sum to
    exchange_total_qty. The average entry price is untouched (we
    don't know which lots were partially closed on the exchange).

    Example:
      Exchange: 0.020 BTC (total)
      DCA slot:           qty=0.012
      TrendFollowing slot: qty=0.010
      Bot total = 0.022 → scale = 0.020/0.022 = 0.909
      DCA adjusted:    0.012 * 0.909 = 0.01090...
      Trend adjusted:  0.010 * 0.909 = 0.00909...
      New total = 0.020 ✓
    """
    bot_total = sum(bot_positions[sn]["qty"] for sn in snames)
    if bot_total <= 0:
        return

    scale = exchange_total_qty / bot_total

    for sn in snames:
        pos = bot_positions[sn]["pos_obj"]
        old_qty = pos.quantity
        pos.quantity   = old_qty * scale
        pos.total_cost = pos.total_cost * scale   # keep avg_entry consistent
        logger.info(
            f"[Reconciler]   Adjusted [{sn}] qty: "
            f"{old_qty:.6f} → {pos.quantity:.6f}  (×{scale:.4f})"
        )


def _mark_externally_closed(sname: str, slot) -> None:
    """
    Clear a zombie position so the strategy slot starts fresh.

    We can't generate a real P&L record (no exit price available),
    so we just zero the internal state. The position never ended up
    in our trade history — it's effectively a "lost" trade.

    Strategy-level flags (e.g. _in_position, _entries) are reset via
    sync_state() so the strategy immediately starts looking for new
    entries on the next candle.
    """
    # Clear simulator position
    slot.simulator.position = None

    # Sync the strategy's own state flags
    strategy = slot.strategy
    try:
        if hasattr(strategy, "sync_state"):
            strategy.sync_state(simulator_has_position=False)
        elif hasattr(strategy, "_in_position"):
            strategy._in_position = False
    except Exception as exc:
        logger.debug(
            f"[Reconciler] sync_state() failed for {sname} "
            f"(non-fatal): {exc}"
        )


def _auto_close_ghost(
    exchange: "ccxt.binance",
    symbol: str,
    side: str,
    qty: float,
) -> None:
    """
    Send a reduce-only market order to close a ghost position on Binance.

    Only called when RECONCILE_AUTO_CLOSE_GHOST=True.

    A reduce-only order can never open or increase a position — it only
    reduces an existing one — so this is safe even if our qty estimate
    is slightly off (Binance will close the remainder).

    Failures are logged at CRITICAL but do NOT crash startup.
    """
    close_side = "sell" if side == "long" else "buy"
    original_type = exchange.options.get("defaultType", "spot")

    try:
        exchange.options["defaultType"] = "future"
        order = exchange.create_market_order(
            symbol=symbol,
            side=close_side,
            amount=qty,
            params={"reduceOnly": True},
        )
        exchange.options["defaultType"] = original_type
        logger.critical(
            f"[Reconciler] 🔒 Ghost closed via auto-close | "
            f"{symbol} {side.upper()} | "
            f"Qty: {qty:.6f} | "
            f"Order ID: {order.get('id')} | "
            f"Status: {order.get('status')}"
        )
    except Exception as exc:
        exchange.options["defaultType"] = original_type
        logger.critical(
            f"[Reconciler] 💀 Auto-close FAILED for ghost {symbol} "
            f"{side.upper()} qty={qty:.6f} — {type(exc).__name__}: {exc}. "
            f"CLOSE THIS POSITION MANUALLY IN BINANCE NOW."
        )


# ── Report formatter ──────────────────────────────────────────────────────────

def _log_report(report: dict) -> None:
    """
    Emit a structured reconciliation summary to the log at INFO level.
    Called once at the end of reconcile_on_startup().
    """
    n_ghost    = len(report.get("ghosts",     []))
    n_zombie   = len(report.get("zombies",    []))
    n_mismatch = len(report.get("mismatches", []))
    n_matched  = len(report.get("matched",    []))
    skipped    = report.get("skipped", False)
    mode       = report.get("mode", "?")

    sep   = "─" * 62
    lines = ["", sep, "  STARTUP RECONCILIATION REPORT", sep]

    if mode == "paper":
        lines.append("  MODE   : PAPER — reconciliation skipped (correct)")
        lines.append(sep)
        logger.info("\n".join(lines))
        return

    if skipped:
        err = report.get("error", "unknown error")
        lines += [
            f"  STATUS : ⚠️  SKIPPED — exchange unreachable",
            f"  ERROR  : {err}",
            f"  ACTION : Verify open positions manually before trading.",
        ]
        lines.append(sep)
        logger.warning("\n".join(lines))
        return

    clean  = (n_ghost + n_zombie + n_mismatch) == 0
    status = "✅ CLEAN" if clean else "⚠️  DISCREPANCIES FOUND"

    lines += [
        f"  STATUS     : {status}",
        f"  Matched    : {n_matched}  (exchange ↔ bot agree)",
        f"  Ghosts     : {n_ghost}  (on exchange, unknown to bot)",
        f"  Zombies    : {n_zombie}  (in bot, gone from exchange)",
        f"  Mismatches : {n_mismatch}  (qty drift > 1%)",
    ]

    if n_ghost > 0:
        lines.append("")
        lines.append("  🚨 GHOST POSITIONS:")
        for g in report["ghosts"]:
            auto   = "→ auto-closed" if config.RECONCILE_AUTO_CLOSE_GHOST else "→ left open (RECONCILE_AUTO_CLOSE_GHOST=false)"
            lines.append(
                f"     {g['symbol']} {g['side'].upper()} "
                f"qty={g['qty']:.6f} @ ${g['entry']:,.4f}  {auto}"
            )

    if n_zombie > 0:
        lines.append("")
        lines.append("  ℹ️  ZOMBIE POSITIONS (cleared from bot state):")
        for z in report["zombies"]:
            lines.append(
                f"     [{z['strategy']}] {z['symbol']} {z['side'].upper()} "
                f"qty={z['qty']:.6f} @ ${z['entry']:,.4f}"
            )

    if n_mismatch > 0:
        lines.append("")
        lines.append("  ℹ️  QUANTITY MISMATCHES (bot adjusted to match exchange):")
        for m in report["mismatches"]:
            lines.append(
                f"     {m['symbol']} {m['side'].upper()} | "
                f"exchange={m['exchange_qty']:.6f}  "
                f"bot_was={m['bot_qty']:.6f}  "
                f"({m['mismatch_pct']:.2f}% drift) | "
                f"slots: {m['strategies']}"
            )

    lines.append(sep)
    lines.append("")

    if clean:
        logger.info("\n".join(lines))
    else:
        logger.warning("\n".join(lines))
