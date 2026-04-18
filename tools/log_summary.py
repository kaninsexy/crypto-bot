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

import numpy as np

# ── Paths ─────────────────────────────────────────────────────────────────────

ROOT             = Path(__file__).parent.parent
LOG_FILE         = ROOT / "logs" / "bot.log"
OUT_FILE         = ROOT / "logs" / "daily_summary.json"
PAPER_STATE_FILE = ROOT / "dashboard" / "data" / "paper_state.json"

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

# Inverse map: symbol → [strategy names] — used by price-based trade attribution.
# Built at module load so _attribute_sell_to_strategy() doesn't recompute it per call.
_SYMBOL_TO_STRATEGIES: dict[str, list[str]] = {}
for _s, _sym in STRATEGY_SYMBOLS.items():
    _SYMBOL_TO_STRATEGIES.setdefault(_sym, []).append(_s)

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
RE_REPLAY_MSG   = re.compile(r'replay|replayed|missed candle', re.IGNORECASE)
RE_GRID_REENTRY = re.compile(r'SL triggered.*missed candle replay', re.IGNORECASE)
STARTUP_REPLAY_WINDOW = timedelta(minutes=5)

# Checkpoint position restore logged by PortfolioManager on restart:
#   [Portfolio] ↩ VolatilityBreakout: restored OPEN long | Entry $71,457.12
RE_CHECKPOINT_RESTORE = re.compile(
    r'\[Portfolio\]\s+\S*\s*(\w+):\s+restored OPEN \w+\s*\|\s*Entry\s*\$([\d,]+(?:\.\d+)?)'
)


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
    first_real_candle_ts    = None   # timestamp of that first candle
    line_num                = 0      # file-order counter used for positional BUY matching
    restored_positions:     dict[str, float] = {}  # strategy → entry_price from checkpoint restore logs

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
                    if not first_real_candle_seen:
                        first_real_candle_ts = ts
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
                    # Route to startup_events when:
                    #   (a) no real candle seen yet (pure startup warnings), OR
                    #   (b) message matches replay pattern AND is within 5 min of
                    #       the first real candle (genuine missed-candle catch-up).
                    #
                    # GridTrading re-entry after an SL exit logs
                    # "SL triggered (missed candle replay)" during normal trading —
                    # it matches RE_REPLAY_MSG but must go to errors, tagged
                    # as "grid_reentry_sl", once we're past the startup window.
                    within_startup_window = (
                        first_real_candle_ts is not None
                        and (ts - first_real_candle_ts) <= STARTUP_REPLAY_WINDOW
                    )
                    is_replay_msg = RE_REPLAY_MSG.search(message) is not None
                    is_startup = (
                        not first_real_candle_seen
                        or (is_replay_msg and within_startup_window)
                    )
                    if not is_startup and is_replay_msg and RE_GRID_REENTRY.search(message):
                        event["tag"] = "grid_reentry_sl"
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

                # ── Checkpoint position restore (Bug 3: entry_price fallback) ─
                if "[Portfolio]" in message and "restored OPEN" in message:
                    cr_m = RE_CHECKPOINT_RESTORE.search(message)
                    if cr_m:
                        strat_name  = cr_m.group(1)
                        entry_price = float(cr_m.group(2).replace(",", ""))
                        restored_positions[strat_name] = entry_price

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
        "restored_positions":     restored_positions,
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


# ── Open-position enrichment from paper_state.json ───────────────────────────

def enrich_open_positions_from_paper_state(
    open_positions:    list[dict],
    paper_state_path:  Path = PAPER_STATE_FILE,
    restored_positions: dict[str, float] | None = None,
    paper_buys:        list[dict] | None = None,
) -> None:
    """
    Enrich open positions inferred from the log with live data from
    dashboard/data/paper_state.json.

    This is necessary for positions restored from a checkpoint on restart:
    no [PAPER] BUY line exists in the current log session, so entry_price,
    stop_loss, and unrealized_pnl are all None after log-only inference.

    Enrichment rules (modifies open_positions in place):
      - entry_price:        filled only when None (log-inferred value takes
                            precedence — it's exact; paper_state carries a
                            running avg that shifts on DCA adds).
                            Fallback chain: paper_state → restored_positions
                            (from "[Portfolio] ↩ Strategy: restored OPEN" lines).
      - stop_loss:          always taken from paper_state (not in log at all).
      - unrealized_pnl:     always taken from paper_state (runtime value).
      - unrealized_pnl_pct: always taken from paper_state.

    Silently skips if the file is missing, unreadable, or malformed.
    Bug 3 additions:
      - Debug warning to stderr when a strategy's open position is not found
        in paper_state.json (helps diagnose key-name mismatches).
      - Case-insensitive fallback key lookup in the strategies dict.
      - Final entry_price fallback from restored_positions (checkpoint log lines).
    """
    strategies: dict = {}
    if paper_state_path.exists():
        try:
            state = json.loads(paper_state_path.read_text(encoding="utf-8"))
            strategies = state.get("strategies", {})
        except Exception:
            pass  # malformed JSON — degrade gracefully; still apply restored_positions

    # Build a case-insensitive lookup index for the strategies dict.
    # paper_state.json may store keys as "VolatilityBreakout", "volatility_breakout",
    # or other variations depending on which code path wrote the checkpoint.
    strategies_lower: dict[str, str] = {k.lower(): k for k in strategies}

    for pos in open_positions:
        strat = pos["strategy"]

        # Exact match first; fall back to case-insensitive lookup
        if strat in strategies:
            canonical_key = strat
        else:
            canonical_key = strategies_lower.get(strat.lower())

        strat_data = strategies.get(canonical_key, {}) if canonical_key else {}
        ps_pos     = strat_data.get("position")  # None when strategy has no open position

        if not ps_pos:
            # Bug 3: emit a diagnostic so we can see whether paper_state.json
            # is missing the strategy entirely or just has no open position.
            if pos["entry_price"] is None:
                print(
                    f"[warn] {strat}: open position not found in paper_state.json "
                    f"(keys present: {list(strategies.keys())[:8]})",
                    file=sys.stderr,
                )
        else:
            # entry_price: only fill the gap; don't overwrite a log-resolved value
            if pos["entry_price"] is None:
                raw = ps_pos.get("avg_entry_price")
                pos["entry_price"] = float(raw) if raw is not None else None

            # These fields have no log source at all — always use paper_state
            raw_sl = ps_pos.get("stop_loss")
            pos["stop_loss"]          = float(raw_sl) if raw_sl is not None else None

            raw_upnl = ps_pos.get("unrealized_pnl")
            pos["unrealized_pnl"]     = float(raw_upnl) if raw_upnl is not None else None

            raw_upct = ps_pos.get("unrealized_pnl_pct")
            pos["unrealized_pnl_pct"] = float(raw_upct) if raw_upct is not None else None

        # Fallback 2: checkpoint restore log lines.
        # Parsed from "[Portfolio] ↩ Strategy: restored OPEN long | Entry $71,457.12"
        # by parse_log() into restored_positions dict.
        if pos["entry_price"] is None and restored_positions:
            rp = restored_positions.get(strat)
            if rp is not None:
                pos["entry_price"] = rp

        # Fallback 3: scan paper_buys for a BUY line within ±5 s of open_since.
        # This catches strategies (e.g. VolatilityBreakout, DualMomentum) whose
        # position was opened in the current log session but whose strategy name
        # couldn't be inferred from the Actions timeline at the time of the buy
        # (e.g. no Actions line was logged, or the position was opened before the
        # first Actions line in the window).
        # paper_state.json may be stale (written by an older bot run that predates
        # these strategies), so we fall all the way through to this line-level scan.
        if pos["entry_price"] is None and paper_buys:
            try:
                open_since_ts = datetime.fromisoformat(pos["open_since"])
            except (ValueError, TypeError):
                open_since_ts = None
            if open_since_ts is not None:
                best_price: float | None = None
                best_delta: float = 5.0   # seconds tolerance
                for buy in paper_buys:
                    delta = abs((buy["ts"] - open_since_ts).total_seconds())
                    if delta <= best_delta:
                        best_delta = delta
                        best_price = buy["entry_price"]
                if best_price is not None:
                    pos["entry_price"] = best_price


# ── Entry-price enrichment ────────────────────────────────────────────────────

def enrich_exit_prices(closed_trades: list[dict], paper_sells: list[dict]) -> set[int]:
    """
    Attempt to fill in exit_price on closed_trades by matching [TradeLog] entries
    to [PAPER] SELL entries at the same timestamp (±2 s) and same pnl.
    Modifies closed_trades in place.

    Returns the set of paper_sells indices that were consumed, so the caller can
    identify which SELL lines were NOT matched by any [TradeLog] entry (used by
    synthesize_trades_from_paper_sells to handle trades closed during replay).
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
    return used


# ── Bug 1 helpers: synthesize closed trades from unmatched PAPER SELL lines ────

def _price_to_candidate_strategies(exit_price: float | None) -> list[str]:
    """
    Narrow down candidate strategies based on the exit price of the trade.

    Each strategy trades a fixed symbol (see STRATEGY_SYMBOLS).  Price ranges
    derived from current market levels allow us to identify which symbol was
    traded — even when the Actions timeline is ambiguous or missing.

    Thresholds:
      > $10,000  → BTC/USDT  (DCA, TrendFollowing, BearShort, VolatilityBreakout, DualMomentum)
      $500–$10k  → ETH/USDT  (Supertrend, MeanReversion, VWAP)
      < $500     → SOL/USDT or AVAX/USDT  (GridTrading, Breakout)

    Returns ALL_STRATEGIES when exit_price is None (no filter applicable).
    """
    if exit_price is None:
        return ALL_STRATEGIES
    if exit_price > 10_000:
        return _SYMBOL_TO_STRATEGIES.get("BTC/USDT", ALL_STRATEGIES)
    elif exit_price > 500:
        return _SYMBOL_TO_STRATEGIES.get("ETH/USDT", ALL_STRATEGIES)
    else:
        # SOL/USDT + AVAX/USDT both fall below $500
        low_cap = (
            _SYMBOL_TO_STRATEGIES.get("SOL/USDT", [])
            + _SYMBOL_TO_STRATEGIES.get("AVAX/USDT", [])
        )
        return low_cap if low_cap else ALL_STRATEGIES


def _attribute_sell_to_strategy(
    sell_ts: datetime,
    actions_timeline: list[dict],
    exit_price: float | None = None,
    window_seconds: float = 10.0,
) -> str:
    """
    Return the strategy name most likely responsible for a [PAPER] FULL SELL.

    Attribution uses a three-level hierarchy:

    Level 1 — Price-range filter (PRIMARY, most reliable).
      Each strategy trades a single symbol with a known price range.
      exit_price > $10k  → BTC strategies only
      exit_price $500-10k → ETH strategies only
      exit_price < $500  → SOL/AVAX strategies only
      If the price bucket contains exactly ONE strategy, return it directly.

    Level 2 — Actions timeline within price bucket (SECONDARY).
      Among the price-filtered candidates, walk backwards through the
      Actions timeline looking for:
        a. SELL/CLOSE action (strongest signal — strategy is actively exiting)
        b. Any non-HOLD/SKIP/BLOCKED action (weaker — strategy was recently active)
      Both passes are confined to window_seconds before sell_ts.

    Level 3 — Unconstrained Actions fallback (TERTIARY).
      If no candidate match in the window, relax the price filter and look
      for any SELL/CLOSE across all strategies.  Covers edge cases where
      a strategy's price has shifted outside the expected bucket.

    Falls back to "Unknown" only when the timeline has no usable signal at all.
    """
    # Level 1: derive candidate set from price
    candidates = set(_price_to_candidate_strategies(exit_price))

    if len(candidates) == 1:
        # Price uniquely identifies the strategy — no timeline needed
        return next(iter(candidates))

    cutoff = sell_ts - timedelta(seconds=window_seconds)

    # Level 2a: SELL/CLOSE within the price-filtered candidate set
    for entry in reversed(actions_timeline):
        ts = entry["ts"]
        if ts > sell_ts:
            continue
        if ts < cutoff:
            break
        for strat, action in entry["actions"].items():
            if strat in candidates and action in ("SELL", "CLOSE"):
                return strat

    # Level 2b: any non-HOLD action within the price-filtered candidate set
    for entry in reversed(actions_timeline):
        ts = entry["ts"]
        if ts > sell_ts:
            continue
        if ts < cutoff:
            break
        for strat, action in entry["actions"].items():
            if strat in candidates and action not in ("HOLD", "SKIP", "BLOCKED"):
                return strat

    # Level 3: relax price filter — look for any SELL/CLOSE across all strategies
    for entry in reversed(actions_timeline):
        ts = entry["ts"]
        if ts > sell_ts:
            continue
        if ts < cutoff:
            break
        for strat, action in entry["actions"].items():
            if action in ("SELL", "CLOSE"):
                return strat

    # Last resort: return the lexicographically-first candidate from the price bucket
    # (a named strategy is always better than "Unknown" for downstream grouping)
    if candidates and candidates != set(ALL_STRATEGIES):
        return sorted(candidates)[0]

    return "Unknown"


def synthesize_trades_from_paper_sells(
    closed_trades:    list[dict],
    paper_sells:      list[dict],
    matched_indices:  set[int],
    actions_timeline: list[dict],
) -> None:
    """
    Bug 1 fix: synthesize closed_trade entries for [PAPER] FULL SELL lines that
    were NOT matched to any [TradeLog] entry.

    This covers trades that close during candle replay on bot restart — the
    simulator executes the SL/TP and logs the PAPER SELL, but the [TradeLog]
    path (flush_new_trades in main.py) never fires because the bot is still
    in replay mode, not the live loop.

    Synthesized entries carry synthetic=True so callers can distinguish them
    from the authoritative [TradeLog]-derived entries.
    """
    for i, sell in enumerate(paper_sells):
        if i in matched_indices:
            continue  # already attributed to a [TradeLog] entry
        if sell.get("is_partial"):
            continue  # partial exits don't close the trade; skip

        strategy = _attribute_sell_to_strategy(
            sell["ts"], actions_timeline, exit_price=sell.get("exit_price")
        )

        closed_trades.append({
            "strategy":    strategy,
            "symbol":      STRATEGY_SYMBOLS.get(strategy),
            "side":        "long",
            "pnl":         sell["pnl"],
            "pnl_pct":     sell["pnl_pct"],
            "exit_reason": sell["reason"],
            "exit_time":   _fmt_ts(sell["ts"]),
            "exit_price":  sell["exit_price"],
            "entry_price": None,   # not available from PAPER SELL line alone
            "synthetic":   True,   # flag: inferred from PAPER SELL, no TradeLog
        })


# ── Rolling Sharpe helper ─────────────────────────────────────────────────────

def _compute_rolling_sharpe(
    returns: list[float],
    window: int = 30,
    periods_per_year: int = 365 * 24,
) -> float | None:
    """
    Compute an annualised rolling Sharpe ratio from the last `window` trade
    returns (percentage returns, i.e. pnl_pct).

    Formula:  sharpe = (mean / std) * sqrt(periods_per_year)
    • `periods_per_year` defaults to 365*24 (hourly-candle assumption).
    • Returns None when fewer than 2 returns are available.
    • Returns 0.0 when the std is zero (degenerate, non-negative flat returns).

    Caveat: this annualises trade-level pnl_pct as if each return were a
    one-hour observation. If trades hold for many candles the true annual
    factor should be (hours_per_year / avg_hold_hours). The current formula
    matches the requested spec; revisit if you want a holding-period-aware
    version.
    """
    window_returns = returns[-window:]
    if len(window_returns) < 2:
        return None
    arr  = np.asarray(window_returns, dtype=float)
    mean = float(np.mean(arr))
    std  = float(np.std(arr))
    if std <= 0:
        return 0.0
    return float((mean / std) * np.sqrt(periods_per_year))


# ── Summary stats ─────────────────────────────────────────────────────────────

def compute_summary(closed_trades: list[dict], open_positions: list[dict],
                    current_regime: str | None,
                    actions_timeline: list[dict] | None = None) -> dict:
    total   = len(closed_trades)
    wins    = [t for t in closed_trades if t["pnl"] > 0]
    total_pnl = round(sum(t["pnl"] for t in closed_trades), 2)
    win_rate  = round(len(wins) / total * 100, 1) if total else 0.0

    # Most active strategy (by closed trade count)
    trade_counts: dict[str, int] = defaultdict(int)
    for t in closed_trades:
        trade_counts[t["strategy"]] += 1
    most_active = max(trade_counts, key=trade_counts.get) if trade_counts else None

    # Bug 2 fix: determine active/inactive from the Actions timeline, not from
    # closed_trades alone.  When closed_trades = 0 (e.g. early in a session or
    # after a restart), every strategy used to appear as "inactive" — wrong.
    #
    # A strategy is ACTIVE if it appeared with any non-HOLD/SKIP/BLOCKED action
    # in the reporting window's Actions timeline (BUY, SELL, CLOSE, ADD, etc.).
    # A strategy is INACTIVE only if it never appeared at all, or always HOLD.
    NON_HOLD = {"BUY", "SELL", "CLOSE", "ADD", "PARTIAL_SELL"}
    active_from_actions: set[str] = set()
    if actions_timeline:
        for entry in actions_timeline:
            for strat, action in entry["actions"].items():
                if action.upper() not in ("HOLD", "SKIP", "BLOCKED"):
                    active_from_actions.add(strat)

    inactive = [s for s in ALL_STRATEGIES if s not in active_from_actions]

    # ── Per-strategy rolling Sharpe (last 30 closed trade returns) ──
    # closed_trades is appended in chronological order by parse_log (file order),
    # so grouping preserves chronology per strategy.
    per_strategy_returns: dict[str, list[float]] = defaultdict(list)
    for t in closed_trades:
        per_strategy_returns[t["strategy"]].append(t["pnl_pct"])

    per_strategy: dict[str, dict] = {}
    for strat, rets in per_strategy_returns.items():
        per_strategy[strat] = {
            "trade_count":    len(rets),
            "rolling_sharpe": _compute_rolling_sharpe(rets, window=30),
        }

    # ── Portfolio-level rolling Sharpe (combined returns across all strategies) ──
    all_returns = [t["pnl_pct"] for t in closed_trades]
    rolling_sharpe = _compute_rolling_sharpe(all_returns, window=30)

    return {
        "total_closed_trades": total,
        "total_closed_pnl":    total_pnl,
        "win_rate_pct":        win_rate,
        "most_active_strategy": most_active,
        "current_regime":      current_regime or "UNKNOWN",
        "inactive_strategies": inactive,
        "rolling_sharpe":      rolling_sharpe,
        "per_strategy":        per_strategy,
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
    port_sharpe = summary.get("rolling_sharpe")
    port_sharpe_str = f"{port_sharpe:.2f}" if port_sharpe is not None else "None"
    print(f"  Rolling Sharpe (30): {port_sharpe_str}")
    if summary["inactive_strategies"]:
        print(f"  ⚠ Inactive strats: {', '.join(summary['inactive_strategies'])}")

    # ── Per-strategy metrics ──
    per_strategy = summary.get("per_strategy") or {}
    if per_strategy:
        print(f"\n{sep}")
        print(f"  PER-STRATEGY METRICS  ({len(per_strategy)})")
        print(sep)
        for strat in sorted(per_strategy.keys()):
            metrics = per_strategy[strat]
            sharpe  = metrics.get("rolling_sharpe")
            sharpe_str = f"{sharpe:.2f}" if sharpe is not None else "None"
            tc = metrics.get("trade_count", 0)
            print(f"  • {strat:<18} trades={tc:<3}  Rolling Sharpe (30): {sharpe_str}")

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
    # Returns the set of paper_sells indices consumed so the synthesizer can
    # identify which SELL lines were NOT paired with a [TradeLog] entry.
    matched_sell_indices = enrich_exit_prices(parsed["closed_trades"], parsed["paper_sells"])

    # ── Bug 1: synthesize closed trades from unmatched [PAPER] FULL SELL lines ─
    # Covers trades that closed during candle replay on restart — the simulator
    # executed the SL/TP and logged the PAPER SELL, but flush_new_trades() never
    # fired (bot was still in replay mode, not the live candle loop).
    synthesize_trades_from_paper_sells(
        parsed["closed_trades"],
        parsed["paper_sells"],
        matched_sell_indices,
        parsed["actions_timeline"],
    )

    # ── Infer open positions ───────────────────────────────────────────────
    open_positions = infer_open_positions(parsed)

    # ── Enrich open positions with paper_state.json (checkpoint restores) ─
    # Pass restored_positions and paper_buys as progressively weaker fallbacks
    # for entry_price when paper_state.json is stale or missing a strategy.
    enrich_open_positions_from_paper_state(
        open_positions,
        restored_positions=parsed["restored_positions"],
        paper_buys=parsed["paper_buys"],
    )

    # ── Compute summary stats ──────────────────────────────────────────────
    # Bug 2: pass actions_timeline so inactive_strategies reflects actual
    # trading activity, not just closed-trade count.
    summary = compute_summary(
        parsed["closed_trades"],
        open_positions,
        parsed["current_regime"],
        actions_timeline=parsed["actions_timeline"],
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
