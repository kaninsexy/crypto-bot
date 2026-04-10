"""
dashboard/state.py — Shared state file helpers.

The bot writes JSON snapshots after each candle / backtest run.
The dashboard server reads them to serve the UI.

Files live in dashboard/data/ (auto-created):
  paper_state.json       — portfolio / paper trading state
  backtest_results.json  — last backtest run results
  bot_status.json        — heartbeat + last actions
"""

import json
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

PAPER_STATE_FILE    = DATA_DIR / "paper_state.json"
BACKTEST_FILE       = DATA_DIR / "backtest_results.json"
BOT_STATUS_FILE     = DATA_DIR / "bot_status.json"


# ── Writers ───────────────────────────────────────────────────────────────────

def _write(path: Path, data: dict) -> None:
    """Atomically write a dict to a JSON file."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    tmp.replace(path)


def write_paper_state(data: dict) -> None:
    _write(PAPER_STATE_FILE, data)


def write_backtest_results(data: dict) -> None:
    _write(BACKTEST_FILE, data)


def write_bot_status(data: dict) -> None:
    _write(BOT_STATUS_FILE, data)


# ── Readers ───────────────────────────────────────────────────────────────────

def _read(path: Path) -> dict:
    """Read JSON file; return {} if missing or corrupt."""
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def read_paper_state() -> dict:
    return _read(PAPER_STATE_FILE)


def read_backtest_results() -> dict:
    return _read(BACKTEST_FILE)


def read_bot_status() -> dict:
    return _read(BOT_STATUS_FILE)


# ── Bot-running heuristic ─────────────────────────────────────────────────────

def is_bot_running(stale_seconds: int = 7200) -> bool:
    """
    Returns True if bot_status.json was updated within stale_seconds.
    Default: 2 hours (generous for 1h candle timeframe).
    """
    status = read_bot_status()
    updated_at = status.get("updated_at")
    if not updated_at:
        return False
    try:
        ts = datetime.fromisoformat(updated_at)
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        return age < stale_seconds
    except Exception:
        return False
