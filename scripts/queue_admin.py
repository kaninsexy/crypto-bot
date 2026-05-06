"""scripts/queue_admin.py

CLI admin for the trial queue. Designed for SSH/phone-terminal use:
no external deps beyond stdlib, ASCII-friendly output, pathlib for
paths, atomic JSON writes.

After the file split (this session):
  backtest/trial_queue.json        Mac-side committed definitions.
                                    --drop and --defer write here
                                    (human decisions are durable +
                                    reviewable in git).
  backtest/trial_queue_state.json  PC-side gitignored runtime state.
                                    --requeue writes here (resets
                                    runtime fields only, never undoes
                                    a human drop/defer).

Commands (all mutually exclusive):
  --status                       Print merged queue table + counts
  --drop ID --reason "<text>"    Mark item dropped + record reason
                                  (writes to trial_queue.json)
  --defer ID --reason "<text>"   Mark item deferred_no_data
                                  (writes to trial_queue.json)
  --requeue ID                   Reset state to queued, clear
                                  runtime fields (writes to
                                  trial_queue_state.json only)
  --tail-log [--lines N]         Print last N lines (default 50)
                                  of logs/trial_queue.log
"""

from __future__ import annotations

import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8',
                              errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8',
                              errors='replace')

import argparse
import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
QUEUE_PATH = ROOT / "backtest" / "trial_queue.json"
STATE_PATH = ROOT / "backtest" / "trial_queue_state.json"
LOG_PATH = ROOT / "logs" / "trial_queue.log"

# Runtime fields live in state file; defaults applied on merge.
RUNTIME_FIELD_DEFAULTS = {
    "status": "queued",
    "started_at": None,
    "finished_at": None,
    "verdict": None,
    "trial_id": None,
    "error": None,
    "email_sent": False,
    "retry_count": 0,
    "last_fetch_attempt": None,
    "needs_script_digested": False,
}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _load_definitions() -> dict:
    if not QUEUE_PATH.exists():
        print("ERROR: definitions file not found: " + str(QUEUE_PATH),
              file=sys.stderr)
        sys.exit(1)
    text = QUEUE_PATH.read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        print(
            "ERROR: trial_queue.json is not valid JSON (" + str(exc)
            + "). Fix manually before retrying.",
            file=sys.stderr,
        )
        sys.exit(1)


def _load_state() -> dict:
    if not STATE_PATH.exists():
        return {"schema_version": 1, "items": {}}
    text = STATE_PATH.read_text(encoding="utf-8").strip()
    if not text:
        return {"schema_version": 1, "items": {}}
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        print(
            "ERROR: trial_queue_state.json is not valid JSON ("
            + str(exc) + ").",
            file=sys.stderr,
        )
        sys.exit(1)


def _save_definitions(data: dict) -> None:
    _atomic_write_json(QUEUE_PATH, data)


def _save_state(state: dict) -> None:
    _atomic_write_json(STATE_PATH, state)


def _find_definition(data: dict, item_id: str) -> dict | None:
    for item in data.get("queue", []):
        if item.get("id") == item_id:
            return item
    return None


def _merge(definition: dict, state_entry: dict | None) -> dict:
    """Combine definition + state into one display dict.

    Order of precedence per field: state entry > legacy field on
    definition (pre-migration) > default. The legacy fallback lets
    `--status` produce a sensible view on Mac before the PC has run
    once and migrated state.
    """
    merged = dict(definition)
    se = state_entry or {}
    for f, default in RUNTIME_FIELD_DEFAULTS.items():
        if f in se:
            merged[f] = se[f]
        elif f in definition:
            merged[f] = definition[f]
        else:
            merged[f] = default
    if definition.get("drop_reason"):
        merged["status"] = "dropped"
    elif (definition.get("defer_reason")
          and merged["status"] not in ("done", "running")):
        merged["status"] = "deferred"
    return merged


def cmd_status() -> int:
    definitions = _load_definitions()
    state = _load_state()
    state_items = state.get("items", {})
    queue = definitions.get("queue", [])

    merged_items = [_merge(it, state_items.get(it.get("id"))) for it in queue]
    counts = Counter(it.get("status", "unknown") for it in merged_items)

    print(
        "id".ljust(8)
        + " " + "strategy_id".ljust(32)
        + " " + "variation_id".ljust(34)
        + " " + "status".ljust(18)
        + " " + "verdict".ljust(10)
    )
    print("-" * 104)
    for it in merged_items:
        print(
            str(it.get("id", "?"))[:8].ljust(8)
            + " " + str(it.get("strategy_id", "?"))[:32].ljust(32)
            + " " + str(it.get("variation_id", "?"))[:34].ljust(34)
            + " " + str(it.get("status", "?"))[:18].ljust(18)
            + " " + str(it.get("verdict") or "-")[:10].ljust(10)
        )
    print()
    print("Counts:")
    for k in sorted(counts):
        print("  " + k + ": " + str(counts[k]))
    last = state.get("last_digest_sent_at")
    if last:
        print()
        print("last_digest_sent_at: " + str(last))
    last_run = state.get("last_run_at")
    if last_run:
        print("last_run_at:         " + str(last_run))
    return 0


def cmd_drop(item_id: str, reason: str) -> int:
    data = _load_definitions()
    item = _find_definition(data, item_id)
    if item is None:
        print("ERROR: item id '" + item_id + "' not found",
              file=sys.stderr)
        return 1
    item["drop_reason"] = reason
    item["dropped_at"] = _utcnow_iso()
    _save_definitions(data)
    print("Dropped " + item_id + ": " + reason)
    print("(written to backtest/trial_queue.json -- commit + push "
          "manually to propagate to PC)")
    return 0


def cmd_defer(item_id: str, reason: str) -> int:
    data = _load_definitions()
    item = _find_definition(data, item_id)
    if item is None:
        print("ERROR: item id '" + item_id + "' not found",
              file=sys.stderr)
        return 1
    item["defer_reason"] = reason
    _save_definitions(data)
    print("Deferred " + item_id + ": " + reason)
    print("(written to backtest/trial_queue.json -- commit + push "
          "manually to propagate to PC)")
    return 0


def cmd_requeue(item_id: str) -> int:
    # First confirm the definition exists; otherwise --requeue is a no-op
    # against a non-existent item.
    definitions = _load_definitions()
    if _find_definition(definitions, item_id) is None:
        print("ERROR: item id '" + item_id + "' not found in "
              "trial_queue.json", file=sys.stderr)
        return 1
    state = _load_state()
    state.setdefault("items", {})
    state["items"][item_id] = {
        "status": "queued",
        "started_at": None,
        "finished_at": None,
        "verdict": None,
        "trial_id": None,
        "error": None,
        "email_sent": False,
        # retry_count and digest fields preserved if present, else 0/false
        "retry_count": state["items"].get(item_id, {}).get(
            "retry_count", 0
        ),
        "last_fetch_attempt": state["items"].get(item_id, {}).get(
            "last_fetch_attempt"
        ),
        "needs_script_digested": False,
    }
    _save_state(state)
    print("Requeued " + item_id
          + " (state-side reset; definition unchanged)")
    return 0


def cmd_tail_log(lines: int) -> int:
    if not LOG_PATH.exists():
        print("ERROR: log file not found: " + str(LOG_PATH),
              file=sys.stderr)
        return 1
    with LOG_PATH.open("r", encoding="utf-8", errors="replace") as f:
        all_lines = f.readlines()
    for line in all_lines[-lines:]:
        print(line.rstrip())
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Trial queue admin CLI")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--status", action="store_true",
                       help="Print merged queue table + status counts")
    group.add_argument("--drop", metavar="ID",
                       help="Mark item ID dropped (requires --reason); "
                            "writes to trial_queue.json")
    group.add_argument("--defer", metavar="ID",
                       help="Mark item ID deferred_no_data (requires "
                            "--reason); writes to trial_queue.json")
    group.add_argument("--requeue", metavar="ID",
                       help="Reset item ID runtime state to queued; "
                            "writes to trial_queue_state.json only")
    group.add_argument("--tail-log", action="store_true",
                       help="Print last N lines of trial_queue.log")
    parser.add_argument("--reason", default=None,
                        help="Reason text for --drop / --defer")
    parser.add_argument("--lines", type=int, default=50,
                        help="Number of log lines to print "
                             "(default 50)")
    args = parser.parse_args()

    if args.status:
        return cmd_status()
    if args.drop:
        if not args.reason:
            print("ERROR: --drop requires --reason", file=sys.stderr)
            return 2
        return cmd_drop(args.drop, args.reason)
    if args.defer:
        if not args.reason:
            print("ERROR: --defer requires --reason", file=sys.stderr)
            return 2
        return cmd_defer(args.defer, args.reason)
    if args.requeue:
        return cmd_requeue(args.requeue)
    if args.tail_log:
        return cmd_tail_log(args.lines)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
