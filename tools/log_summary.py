#!/usr/bin/env python3
"""
Parse logs/bot.log and produce a structured JSON daily summary.

Log format (loguru):
    {YYYY-MM-DD HH:mm:ss} | {LEVEL:<8} | {module}:{function} | {message}

Usage:
    python tools/log_summary.py           # last 24 hours (default)
    python tools/log_summary.py --days 7  # last 7 days
    python tools/log_summary.py --log-file /path/to/other.log
"""

import re
import json
import argparse
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import defaultdict

# ── Paths ─────────────────────────────────────────────────────────────────────

ROOT     = Path(__file__).parent.parent
LOG_FILE = ROOT / "logs" / "bot.log"
OUT_FILE = ROOT / "logs" / "daily_summary.json"

# ── Strategy → symbol mapping (mirrors config.py defaults) ───────────────────

STRATEGY_SYMBOLS: dict[str, str] = {
    "DCA":                "BTC/USDT",
    "Supertrend":         "ETH/USDT",
    "MeanReversion":      "ETH/USDT",
    "GridTrading":        "SOL/USDT",
    "Breakout":           "AVAX/USDT",
    "TrendFollowing":     "BTC/USDT",
    "BearShort":          "BTC/USDT",
    "VWAP":               "ETH/USDT",
    "VolatilityBreakout": "BTC/USDT",
    "DualMomentum":       "BTC/USDT",
}

ALL_STRATEGIES = list(STRATEGY_SYMBOLS.keys())

# ── Regex patterns ────────────────────────────────────────────────────────────
# Matches the loguru file format:
#   "2024-01-15 10:30:00 | INFO     | paper_trading.simulator:close_full | message"
RE_LINE = re.compile(
    r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s*\|\s*(\w+)\s*\|\s*[^|]+\|\s*(.+)$'
)

# Closed-trade summary emitted by main.py after flush_new_trades():
#   [TradeLog] 📝 DCA LONG closed | P&L: $+12.34 (+2.34%) | Reason: take_profit
RE_TRADELOG = re.compile(
    r'\[TradeLog\]\s+\S*\s*(\S+)\s+(\w+)\s+closed\s*\|\s*P&L:\s*\$([+-]?[\d.]+)\s*\(([+-]?[\d.]+)%\)\s*\|\s*Reason:\s*(.+)'
)

# [PAPER] ✅ FULL SELL 0.001234 @ 45000.0000 | PnL: $+12.34 (+2.34%) | Fee: $0.123 | Balance: $10012.34 | Reason: take_profit
RE_PAPER_FULL_SELL = re.compile(
    r'\[PAPER\]\s+[^\s]+\s+FULL SELL\s+([\d.]+)\s+@\s+([\d.]+)\s*\|'
    r'\s*PnL:\s*\$([+-]?[\d.]+)\s*\(([+-]?[\d.]+)%\)\s*\|'
    r'.*\|\s*(?:Reason|Balance[^|]*\|[^|]*Reason):\s*(.+)'
)

# [PAPER] ✅ PARTIAL SELL 0.001234 (50%) @ 45000.0000 | PnL: ...
RE_PAPER_PARTIAL_SELL = re.compile(
    r'\[PAPER\]\s+[^\s]+\s+PARTIAL SELL\s+([\d.]+)\s+\([\d.]+%\)\s+@\s+([\d.]+)\s*\|'
    r'\s*PnL:\s*\$([+-]?[\d.]+)\s*\(([+-]?[\d.]+)%\)\s*\|'
    r'.+\|\s*(.+)'
)

# [PAPER] ➕ BUY 0.001234 @ 45000.0000 | Cost: $55.50 | Fee: $0.055 | Balance: $944.50 | SL=... | TP=... |
RE_PAPER_BUY = re.compile(
    r'\[PAPER\]\s+\S+\s+(?:BUY|DCA ADD)\s+([\d.]+)\s+@\s+([\d.]+)\s*\|\s*Cost:\s*\$([\d.]+)'
)

# Regime change logged by portfolio/manager.py:
#   [Portfolio] Regime → BULL | Rebalancing target allocations.
RE_REGIME_CHANGE = re.compile(
    r'\[Portfolio\]\s+Regime\s+→\s+(\w+)\s*\|'
)

# Initial regime on startup:
#   Initial regime: RANGE (confidence 72%)
RE_REGIME_INIT = re.compile(
    r'Initial regime:\s+(\w+)\s*\(confidence'
)

# Circuit breaker patterns (emitted by portfolio/circuit_breaker.py):
#   🚨 CIRCUIT BREAKER TRIPPED | Equity: $9,500 | Peak: $10,000 | Drawdown: -5.0% ...
RE_CB_TRIP = re.compile(
    r'CIRCUIT BREAKER TRIPPED\s*\|\s*Equity:\s*\$([\d,]+)\s*\|\s*Peak:\s*\$([\d,]+)\s*\|\s*Drawdown:\s*-([\d.]+)%'
)
#   ⚠ CIRCUIT BREAKER WARNING | Equity: $9,800 | Drawdown: -2.0% ...
RE_CB_WARN = re.compile(
    r'CIRCUIT BREAKER WARNING\s*\|\s*Equity:\s*\$([\d,]+)\s*\|\s*Drawdown:\s*-([\d.]+)%'
)
#   ✅ CIRCUIT BREAKER RESET | Trading resumed at full size | Equity: $10,100 | Drawdown: -0.5%
RE_CB_RESET = re.compile(
    r'CIRCUIT BREAKER RESET\s*\|\s*Trading resumed.+\|\s*Equity:\s*\$([\d,]+)\s*\|\s*Drawdown:\s*-([\d.]+)%'
)
#   🔄 CIRCUIT BREAKER RESETTING | Post-trip recovery in progress | Candles since trip: 3 | Drawdown now: -3.5%
RE_CB_RESETTING = re.compile(
    r'CIRCUIT BREAKER RESETTING\s*\|.+Drawdown now:\s*-([\d.]+)%'
)

# Portfolio actions per candle:
#   [Portfolio] Actions → DCA:HOLD | Supertrend:BUY | MeanReversion:HOLD | ...
RE_ACTIONS = re.compile(r'\[Portfolio\]\s+Actions\s+→\s+(.+)')

# Real candle tick — marks the end of the startup / replay phase:
#   ▶ New 1h candle: 2026-04-10 03:00:00 | Close: 83240.5000
RE_REAL_CANDLE = re.compile(r'▶ New \S+ candle:')

# Replay-related messages that should always be tagged as startup events
# even if they appear after the first candle (e.g. on subsequent restarts):
RE_REPLAY_MSG = re.compile(r'replay|replayed|missed candle', re.IGNORECASE)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_ts(ts_str: str) -> datetime:
    """Parse 'YYYY-MM-DD HH:mm:ss' into an aware UTC datetime."""
    return datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def _fmt_ts(dt: datetime) -> str:
    return dt.isoformat()


def _strip_commas(s: str) -> float:
    return float(s.replace(",", ""))


# ── Core parser ───────────────────────────────────────────────────────────────

def parse_log(log_path: Path, since: datetime) -> dict:
    """
    Read *log_path* line by line and extract all events after *since*.

    Returns a dict with keys:
        closed_trades, open_positions_raw, regime_changes,
        errors, circuit_breaker_events, actions_timeline
    """
    closed_trades:          list[dict] = []
    paper_sells:            list[dict] = []  # [PAPER] SELL lines (no strategy name)
    paper_buys:             list[dict] = []  # [PAPER] BUY lines  (no strategy name)
    regime_changes:         list[dict] = []
    errors:                 list[dict] = []
    startup_events:         list[dict] = []  # warnings/errors from startup & replay
    circuit_breaker_events: list[dict] = []
    actions_timeline:       list[dict] = []  # [{line_num, ts, actions: {strat: action}}]

    current_regime          = None
    prev_regime             = None
    first_real_candle_seen  = False  # becomes True after the first "▶ New … candle:" line
    line_num                = 0      # file-order counter used for positional BUY matching

    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
            for raw_line in fh:
                line_num += 1
                line = raw_line.rstrip("\n")
                m = RE_LINE.match(line)
                if not m:
                    continue  # skip malformed / continuation lines

                ts_str, level, message = m.group(1), m.group(2).strip(), m.group(3).strip()

                try:
                    ts = _parse_ts(ts_str)
                except ValueError:
                    continue  # unparseable timestamp

                if ts < since:
                    continue  # outside our window

                # Track startup/replay boundary
                if RE_REAL_CANDLE.search(message):
                    first_real_candle_seen = True

                # ── [TradeLog] closed trade ────────────────────────────────
                if "[TradeLog]" in message:
                    tm = RE_TRADELOG.match(message)
                    if tm:
                        strategy, side, pnl, pnl_pct, reason = tm.groups()
                        closed_trades.append({
                            "strategy":    strategy,
                            "symbol":      STRATEGY_SYMBOLS.get(strategy),
                            "side":        side.lower(),
                            "pnl":         float(pnl),
                            "pnl_pct":     float(pnl_pct),
                            "exit_reason": reason.strip(),
                            "exit_time":   _fmt_ts(ts),
                            # entry_price / exit_price correlated below
                            "entry_price": None,
                            "exit_price":  None,
                        })

                # ── [PAPER] FULL / PARTIAL SELL ────────────────────────────
                elif "[PAPER]" in message and "SELL" in message:
                    sell_m = RE_PAPER_FULL_SELL.match(message) or RE_PAPER_PARTIAL_SELL.match(message)
                    if sell_m:
                        paper_sells.append({
                            "ts":        ts,
                            "qty":       float(sell_m.group(1)),
                            "exit_price": float(sell_m.group(2)),
                            "pnl":       float(sell_m.group(3)),
                            "pnl_pct":   float(sell_m.group(4)),
                            "reason":    sell_m.group(5).strip(),
                            "is_partial": "PARTIAL" in message,
                        })

                # ── [PAPER] BUY ────────────────────────────────────────────
                elif "[PAPER]" in message and ("BUY" in message or "DCA ADD" in message):
                    buy_m = RE_PAPER_BUY.search(message)
                    if buy_m:
                        paper_buys.append({
                            "line_num":    line_num,   # file order for positional matching
                            "ts":          ts,
                            "qty":         float(buy_m.group(1)),
                            "entry_price": float(buy_m.group(2)),
                            "cost":        float(buy_m.group(3)),
                        })

                # ── Regime change ──────────────────────────────────────────
                elif "Regime" in message or "regime" in message:
                    rc_m = RE_REGIME_CHANGE.search(message)
                    ri_m = RE_REGIME_INIT.search(message)
                    if rc_m:
                        new_regime = rc_m.group(1)
                        # Only record genuine transitions (bot guarantees this,
                        # but parser may see repeated regimes across restarts)
                        if new_regime != current_regime:
                            regime_changes.append({
                                "timestamp":   _fmt_ts(ts),
                                "from_regime": current_regime or "UNKNOWN",
                                "to_regime":   new_regime,
                            })
                        prev_regime    = current_regime
                        current_regime = new_regime
                    elif ri_m:
                        current_regime = ri_m.group(1)

                # ── Circuit breaker ────────────────────────────────────────
                if "CIRCUIT BREAKER" in message:
                    cb_trip = RE_CB_TRIP.search(message)
                    cb_warn = RE_CB_WARN.search(message)
                    cb_reset = RE_CB_RESET.search(message)
                    cb_reset2 = RE_CB_RESETTING.search(message)

                    if cb_trip:
                        circuit_breaker_events.append({
                            "timestamp":   _fmt_ts(ts),
                            "state":       "TRIPPED",
                            "equity":      _strip_commas(cb_trip.group(1)),
                            "peak":        _strip_commas(cb_trip.group(2)),
                            "drawdown_pct": float(cb_trip.group(3)),
                        })
                    elif cb_reset:
                        circuit_breaker_events.append({
                            "timestamp":   _fmt_ts(ts),
                            "state":       "RESET",
                            "equity":      _strip_commas(cb_reset.group(1)),
                            "peak":        None,
                            "drawdown_pct": float(cb_reset.group(2)),
                        })
                    elif cb_reset2:
                        circuit_breaker_events.append({
                            "timestamp":   _fmt_ts(ts),
                            "state":       "RESETTING",
                            "equity":      None,
                            "peak":        None,
                            "drawdown_pct": float(cb_reset2.group(1)),
                        })
                    elif cb_warn:
                        circuit_breaker_events.append({
                            "timestamp":   _fmt_ts(ts),
                            "state":       "WARNING",
                            "equity":      _strip_commas(cb_warn.group(1)),
                            "peak":        None,
                            "drawdown_pct": float(cb_warn.group(2)),
                        })

                # ── Errors and warnings ────────────────────────────────────
                if level in ("ERROR", "WARNING", "CRITICAL"):
                    event = {
                        "timestamp": _fmt_ts(ts),
                        "level":     level,
                        "message":   message,
                    }
                    # Route to startup_events if we haven't seen a real candle yet
                    # (i.e. we're still in the startup / missed-candle-replay phase),
                    # or if the message itself is replay-related (handles restarts
                    # mid-session where replay fires after earlier candles).
                    is_startup = (
                        not first_real_candle_seen
                        or RE_REPLAY_MSG.search(message) is not None
                    )
                    if is_startup:
                        startup_events.append(event)
                    else:
                        errors.append(event)

                # ── Portfolio actions timeline ─────────────────────────────
                act_m = RE_ACTIONS.search(message)
                if act_m:
                    parts = act_m.group(1).split("|")
                    actions: dict[str, str] = {}
                    for part in parts:
                        part = part.strip()
                        if ":" in part:
                            strat, action = part.split(":", 1)
                            actions[strat.strip()] = action.strip()
                    if actions:
                        actions_timeline.append({
                            "line_num": line_num,   # file order for BUY matching
                            "ts":       ts,
                            "actions":  actions,
                        })

    except FileNotFoundError:
        raise
    except Exception as exc:
        print(f"[warn] Unexpected error reading log: {exc}", file=sys.stderr)

    return {
        "closed_trades":          closed_trades,
        "paper_sells":            paper_sells,
        "paper_buys":             paper_buys,
        "regime_changes":         regime_changes,
        "errors":                 errors,
        "startup_events":         startup_events,
        "circuit_breaker_events": circuit_breaker_events,
        "actions_timeline":       actions_timeline,
        "current_regime":         current_regime,
    }


# ── Open-position inference ───────────────────────────────────────────────────

def _build_strategy_entry_prices(
    actions_timeline: list[dict],
    paper_buys: list[dict],
) -> dict[str, float]:
    """
    Map each strategy's most recent BUY action to the price from the
    corresponding [PAPER] BUY log entry, using file-line order.

    Why this works: when the portfolio manager iterates _slots and executes
    a BUY for each strategy, each simulator immediately logs its own
    "[PAPER] ➕ BUY ... @ {price}" line in _slots iteration order — the
    same order that strategies appear in the "Actions →" line.  So the Nth
    strategy with BUY in the Actions line corresponds to the Nth [PAPER] BUY
    line that follows it in the file.  File-line order (not timestamp) is
    the correct tie-breaker because multiple simulators log within the same
    second.
    """
    strategy_entry_price: dict[str, float] = {}
    buy_idx = 0  # cursor into paper_buys; advances monotonically

    for act_entry in actions_timeline:
        act_line_num = act_entry["line_num"]
        act_ts       = act_entry["ts"]

        buying_strats = [
            s for s, a in act_entry["actions"].items() if a == "BUY"
        ]
        if not buying_strats:
            continue

        # Advance cursor past any [PAPER] BUY entries that appeared before
        # this Actions line — they belong to an earlier candle.
        while buy_idx < len(paper_buys) and paper_buys[buy_idx]["line_num"] <= act_line_num:
            buy_idx += 1

        # Collect [PAPER] BUY entries that appear after this Actions line
        # and within a 3-second window (one per simulator, in file order).
        window_buys: list[dict] = []
        j = buy_idx
        while j < len(paper_buys) and len(window_buys) < len(buying_strats):
            b     = paper_buys[j]
            delta = (b["ts"] - act_ts).total_seconds()
            if delta > 3:
                break
            window_buys.append(b)
            j += 1

        # Positional match: 1st buying strategy → 1st [PAPER] BUY, etc.
        for strat, buy_data in zip(buying_strats, window_buys):
            strategy_entry_price[strat] = buy_data["entry_price"]

    return strategy_entry_price


def infer_open_positions(parsed: dict) -> list[dict]:
    """
    Infer which strategies currently hold open positions by tracking the last
    BUY/SELL action per strategy from the [Portfolio] Actions timeline.

    A strategy is considered to have an open position when its most recent
    non-HOLD, non-BLOCKED action was BUY and no [TradeLog] close was logged
    *after* that BUY timestamp.

    Returns a list of dicts with fields the log can provide:
        strategy, side, entry_price (best-effort), unrealized_pnl (null),
        unrealized_pnl_pct (null)
    """
    # Build strategy → entry_price map using positional file-order matching
    strategy_entry_price = _build_strategy_entry_prices(
        parsed["actions_timeline"], parsed["paper_buys"]
    )

    # Last BUY / SELL action timestamp per strategy
    last_buy_ts:  dict[str, datetime] = {}
    last_sell_ts: dict[str, datetime] = {}

    for entry in parsed["actions_timeline"]:
        ts      = entry["ts"]
        actions = entry["actions"]
        for strat, action in actions.items():
            if action == "BUY":
                last_buy_ts[strat] = ts
            elif action in ("SELL", "CLOSE"):
                last_sell_ts[strat] = ts

    # Timestamp of most recent [TradeLog] close per strategy
    last_close_ts: dict[str, datetime] = {}
    for trade in parsed["closed_trades"]:
        strat = trade["strategy"]
        ts    = datetime.fromisoformat(trade["exit_time"])
        if strat not in last_close_ts or ts > last_close_ts[strat]:
            last_close_ts[strat] = ts

    open_positions: list[dict] = []
    for strat, buy_ts in last_buy_ts.items():
        # Skip if the position was closed after the last BUY
        if last_close_ts.get(strat, datetime.min.replace(tzinfo=timezone.utc)) >= buy_ts:
            continue
        if last_sell_ts.get(strat, datetime.min.replace(tzinfo=timezone.utc)) >= buy_ts:
            continue

        open_positions.append({
            "strategy":           strat,
            "symbol":             STRATEGY_SYMBOLS.get(strat),
            "side":               "long",   # BearShort would be "short"; not distinguishable from log
            "entry_price":        strategy_entry_price.get(strat),
            "unrealized_pnl":     None,     # runtime value; not available in log files
            "unrealized_pnl_pct": None,
            "open_since":         buy_ts.isoformat(),
        })

    return open_positions


# ── Entry-price enrichment ────────────────────────────────────────────────────

def enrich_exit_prices(closed_trades: list[dict], paper_sells: list[dict]) -> None:
    """
    Attempt to fill in exit_price on closed_trades by matching [TradeLog] entries
    to [PAPER] SELL entries at the same timestamp (±2 s) and same pnl.
    Modifies closed_trades in place.
    """
    used: set[int] = set()
    for trade in closed_trades:
        trade_ts  = datetime.fromisoformat(trade["exit_time"])
        trade_pnl = trade["pnl"]
        best_idx  = None
        best_delta = timedelta(seconds=2)
        for i, sell in enumerate(paper_sells):
            if i in used:
                continue
            delta = abs(sell["ts"] - trade_ts)
            if delta <= best_delta and abs(sell["pnl"] - trade_pnl) < 0.02:
                best_delta = delta
                best_idx   = i
        if best_idx is not None:
            trade["exit_price"] = paper_sells[best_idx]["exit_price"]
            used.add(best_idx)


# ── Summary stats ─────────────────────────────────────────────────────────────

def compute_summary(closed_trades: list[dict], open_positions: list[dict],
                    current_regime: str | None) -> dict:
    total   = len(closed_trades)
    wins    = [t for t in closed_trades if t["pnl"] > 0]
    total_pnl = round(sum(t["pnl"] for t in closed_trades), 2)
    win_rate  = round(len(wins) / total * 100, 1) if total else 0.0

    # Most active strategy
    trade_counts: dict[str, int] = defaultdict(int)
    for t in closed_trades:
        trade_counts[t["strategy"]] += 1
    most_active = max(trade_counts, key=trade_counts.get) if trade_counts else None

    # Strategies with zero closed trades (potential concern)
    active_strategies = set(trade_counts.keys())
    inactive = [s for s in ALL_STRATEGIES if s not in active_strategies]

    return {
        "total_closed_trades": total,
        "total_closed_pnl":    total_pnl,
        "win_rate_pct":        win_rate,
        "most_active_strategy": most_active,
        "current_regime":      current_regime or "UNKNOWN",
        "inactive_strategies": inactive,
    }


# ── Human-readable stdout report ──────────────────────────────────────────────

def print_human_summary(summary: dict, closed_trades: list[dict],
                        open_positions: list[dict], regime_changes: list[dict],
                        errors: list[dict], startup_events: list[dict],
                        cb_events: list[dict], period_label: str) -> None:
    sep = "─" * 60

    print(f"\n{'═' * 60}")
    print(f"  BOT LOG SUMMARY  ({period_label})")
    print(f"{'═' * 60}")

    # ── Summary stats ──
    print(f"\n{sep}")
    print("  DAILY STATS")
    print(sep)
    pnl_sign = "+" if summary["total_closed_pnl"] >= 0 else ""
    print(f"  Closed trades    : {summary['total_closed_trades']}")
    print(f"  Total PnL        : {pnl_sign}${summary['total_closed_pnl']:,.2f}")
    print(f"  Win rate         : {summary['win_rate_pct']:.1f}%")
    print(f"  Most active      : {summary['most_active_strategy'] or 'N/A'}")
    print(f"  Current regime   : {summary['current_regime']}")
    if summary["inactive_strategies"]:
        print(f"  ⚠ Inactive strats: {', '.join(summary['inactive_strategies'])}")

    # ── Closed trades ──
    print(f"\n{sep}")
    print(f"  CLOSED TRADES  ({len(closed_trades)})")
    print(sep)
    if closed_trades:
        for t in closed_trades:
            sign   = "✅" if t["pnl"] >= 0 else "❌"
            ep_str = f" | entry ${t['entry_price']:,.4f}" if t.get("entry_price") else ""
            xp_str = f" → ${t['exit_price']:,.4f}" if t.get("exit_price") else ""
            print(
                f"  {sign} [{t['exit_time'][:19]}]  {t['strategy']:<18} {t['side'].upper():<6}"
                f"  PnL: {'+' if t['pnl'] >= 0 else ''}${t['pnl']:,.2f} ({'+' if t['pnl_pct'] >= 0 else ''}{t['pnl_pct']:.2f}%)"
                f"{ep_str}{xp_str}  [{t['exit_reason']}]"
            )
    else:
        print("  (none)")

    # ── Open positions ──
    print(f"\n{sep}")
    print(f"  OPEN POSITIONS  ({len(open_positions)})")
    print(sep)
    if open_positions:
        for p in open_positions:
            ep_str = f"@ ${p['entry_price']:,.4f}" if p.get("entry_price") else "@ unknown"
            upnl   = f"  uPnL: ${p['unrealized_pnl']:+,.2f}" if p.get("unrealized_pnl") is not None else ""
            print(f"  • {p['strategy']:<18} {p['side'].upper():<6}  {ep_str}  since {p['open_since'][:19]}{upnl}")
    else:
        print("  (none)")

    # ── Regime changes ──
    print(f"\n{sep}")
    print(f"  REGIME CHANGES  ({len(regime_changes)})")
    print(sep)
    if regime_changes:
        for r in regime_changes:
            print(f"  [{r['timestamp'][:19]}]  {r['from_regime']} → {r['to_regime']}")
    else:
        print("  (none)")

    # ── Circuit breaker events ──
    print(f"\n{sep}")
    print(f"  CIRCUIT BREAKER EVENTS  ({len(cb_events)})")
    print(sep)
    if cb_events:
        for e in cb_events:
            eq_str = f"  equity=${e['equity']:,.0f}" if e.get("equity") else ""
            print(f"  [{e['timestamp'][:19]}]  {e['state']:<12}  dd=-{e['drawdown_pct']:.1f}%{eq_str}")
    else:
        print("  (none)")

    # ── Errors & warnings ──
    print(f"\n{sep}")
    print(f"  ERRORS & WARNINGS  ({len(errors)})")
    print(sep)
    if errors:
        for e in errors[:20]:   # cap at 20 to avoid flooding stdout
            print(f"  [{e['timestamp'][:19]}]  {e['level']:<8}  {e['message'][:120]}")
        if len(errors) > 20:
            print(f"  … and {len(errors) - 20} more (see JSON output for full list)")
    else:
        print("  (none)")

    # ── Startup / replay events ──
    print(f"\n{sep}")
    print(f"  STARTUP & REPLAY EVENTS  ({len(startup_events)})")
    print(sep)
    if startup_events:
        for e in startup_events[:10]:
            print(f"  [{e['timestamp'][:19]}]  {e['level']:<8}  {e['message'][:120]}")
        if len(startup_events) > 10:
            print(f"  … and {len(startup_events) - 10} more (see JSON output)")
    else:
        print("  (none)")

    print(f"\n{'═' * 60}\n")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Parse logs/bot.log and output a structured JSON summary.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument(
        "--days",
        type=float,
        default=1.0,
        metavar="N",
        help="How many days of history to include (default: 1 = last 24 hours). "
             "Accepts decimals, e.g. --days 0.5 for last 12 hours.",
    )
    ap.add_argument(
        "--log-file",
        default=str(LOG_FILE),
        metavar="PATH",
        help=f"Path to the log file (default: {LOG_FILE})",
    )
    ap.add_argument(
        "--out-file",
        default=str(OUT_FILE),
        metavar="PATH",
        help=f"Path for JSON output (default: {OUT_FILE})",
    )
    ap.add_argument(
        "--json-only",
        action="store_true",
        help="Suppress human-readable stdout output; only write JSON.",
    )
    args = ap.parse_args()

    log_path = Path(args.log_file)
    out_path = Path(args.out_file)

    if not log_path.exists():
        print(f"[error] Log file not found: {log_path}", file=sys.stderr)
        sys.exit(1)

    since = datetime.now(timezone.utc) - timedelta(days=args.days)
    period_label = (
        "last 24 hours" if args.days == 1.0
        else f"last {args.days:.4g} days"
    )

    # ── Parse ──────────────────────────────────────────────────────────────
    parsed = parse_log(log_path, since)

    # ── Enrich: fill exit_price on closed trades via [PAPER] SELL matching ─
    enrich_exit_prices(parsed["closed_trades"], parsed["paper_sells"])

    # ── Infer open positions ───────────────────────────────────────────────
    open_positions = infer_open_positions(parsed)

    # ── Compute summary stats ──────────────────────────────────────────────
    summary = compute_summary(
        parsed["closed_trades"],
        open_positions,
        parsed["current_regime"],
    )

    # ── Build output document ──────────────────────────────────────────────
    output = {
        "generated_at":          datetime.now(timezone.utc).isoformat(),
        "period":                period_label,
        "since":                 since.isoformat(),
        "closed_trades":         parsed["closed_trades"],
        "open_positions":        open_positions,
        "regime_changes":        parsed["regime_changes"],
        "errors":                parsed["errors"],
        "startup_events":        parsed["startup_events"],
        "circuit_breaker_events": parsed["circuit_breaker_events"],
        "summary":               summary,
    }

    # ── Write JSON ─────────────────────────────────────────────────────────
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output, indent=2, default=str))
    print(f"[✓] JSON summary written to: {out_path}", file=sys.stderr)

    # ── Human-readable summary ─────────────────────────────────────────────
    if not args.json_only:
        print_human_summary(
            summary,
            parsed["closed_trades"],
            open_positions,
            parsed["regime_changes"],
            parsed["errors"],
            parsed["startup_events"],
            parsed["circuit_breaker_events"],
            period_label,
        )


if __name__ == "__main__":
    main()
