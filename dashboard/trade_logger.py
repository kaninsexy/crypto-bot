"""
dashboard/trade_logger.py — Persistent trade log using SQLite.

Every time a strategy closes a position during paper trading, that trade is
appended here permanently. Survives bot restarts — unlike the in-memory
trade_history on the simulator.

DB: dashboard/data/trades.db

Usage (called from main.py):
    from dashboard.trade_logger import log_trade, get_all_trades, get_strategy_stats
    log_trade(trade_dict)         # called after each closed trade
    trades = get_all_trades()     # dashboard reads all trades
    stats  = get_strategy_stats() # per-strategy summary
"""

from __future__ import annotations
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "trades.db"


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB_PATH))
    c.row_factory = sqlite3.Row
    return c


def init_db() -> None:
    """Create the trades table if it doesn't exist (idempotent)."""
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                logged_at    TEXT    NOT NULL,
                strategy     TEXT    NOT NULL,
                symbol       TEXT,
                side         TEXT,
                entry_price  REAL,
                exit_price   REAL,
                quantity     REAL,
                pnl          REAL,
                pnl_pct      REAL,
                fees_paid    REAL,
                exit_reason  TEXT,
                entry_time   TEXT,
                exit_time    TEXT,
                is_partial   INTEGER DEFAULT 0
            )
        """)
        c.commit()


def log_trade(trade: dict) -> None:
    """
    Append one closed trade to the persistent SQLite log.

    Expected keys in trade dict:
        strategy, symbol, side, entry_price, exit_price,
        quantity, pnl, pnl_pct, fees_paid, exit_reason,
        entry_time, exit_time, is_partial
    """
    init_db()
    with _conn() as c:
        c.execute("""
            INSERT INTO trades
                (logged_at, strategy, symbol, side,
                 entry_price, exit_price, quantity,
                 pnl, pnl_pct, fees_paid,
                 exit_reason, entry_time, exit_time, is_partial)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            datetime.now(timezone.utc).isoformat(),
            trade.get("strategy", ""),
            trade.get("symbol", ""),
            trade.get("side", ""),
            trade.get("entry_price"),
            trade.get("exit_price"),
            trade.get("quantity"),
            trade.get("pnl"),
            trade.get("pnl_pct"),
            trade.get("fees_paid"),
            trade.get("exit_reason", ""),
            trade.get("entry_time", ""),
            trade.get("exit_time", ""),
            1 if trade.get("is_partial") else 0,
        ))
        c.commit()


def get_all_trades(limit: int = 1000, strategy: str | None = None) -> list[dict]:
    """
    Return all logged trades ordered by exit_time DESC.
    Optionally filter by strategy name.
    """
    init_db()
    with _conn() as c:
        if strategy:
            rows = c.execute(
                "SELECT * FROM trades WHERE strategy = ? ORDER BY exit_time DESC LIMIT ?",
                (strategy, limit),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM trades ORDER BY exit_time DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [dict(r) for r in rows]


def get_strategy_stats() -> list[dict]:
    """
    Return one summary row per strategy:
        strategy, total_trades, wins, win_rate,
        total_pnl, avg_pnl_pct, best_trade, worst_trade
    """
    init_db()
    with _conn() as c:
        rows = c.execute("""
            SELECT
                strategy,
                COUNT(*)                                            AS total_trades,
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END)          AS wins,
                ROUND(
                    100.0 * SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END)
                    / NULLIF(COUNT(*), 0), 1
                )                                                   AS win_rate,
                ROUND(SUM(pnl),     2)                             AS total_pnl,
                ROUND(AVG(pnl_pct), 2)                             AS avg_pnl_pct,
                ROUND(MAX(pnl),     2)                             AS best_trade,
                ROUND(MIN(pnl),     2)                             AS worst_trade,
                MIN(exit_time)                                      AS first_trade,
                MAX(exit_time)                                      AS last_trade
            FROM trades
            GROUP BY strategy
            ORDER BY total_pnl DESC
        """).fetchall()
    return [dict(r) for r in rows]


def get_equity_curve(strategy: str | None = None) -> list[dict]:
    """
    Return cumulative P&L over time for charting.
    Each point: {exit_time, pnl, cumulative_pnl, strategy}
    Ordered by exit_time ASC.
    """
    init_db()
    with _conn() as c:
        if strategy:
            rows = c.execute(
                "SELECT exit_time, pnl, strategy FROM trades WHERE strategy = ? ORDER BY exit_time ASC",
                (strategy,),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT exit_time, pnl, strategy FROM trades ORDER BY exit_time ASC"
            ).fetchall()

    result = []
    running = 0.0
    for r in rows:
        running += r["pnl"] or 0
        result.append({
            "exit_time":      r["exit_time"],
            "pnl":            round(r["pnl"] or 0, 2),
            "cumulative_pnl": round(running, 2),
            "strategy":       r["strategy"],
        })
    return result
