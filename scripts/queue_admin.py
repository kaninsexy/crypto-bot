"""scripts/queue_admin.py

CLI admin for the trial queue. Designed for SSH/phone-terminal use:
no external deps beyond stdlib, ASCII-friendly output, pathlib for
paths, atomic JSON writes.

Commands (all mutually exclusive):
  --status                       Print queue table + status counts
  --drop ID --reason "<text>"    Mark item dropped + record reason
  --defer ID --reason "<text>"   Mark item deferred_no_data
  --requeue ID                   Reset item to queued (clears
                                 verdict/error/timestamps)
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
LOG_PATH = ROOT / "logs" / "trial_queue.log"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_queue() -> dict:
    if not QUEUE_PATH.exists():
        print("ERROR: queue file not found: " + str(QUEUE_PATH),
              file=sys.stderr)
        sys.exit(1)
    text = QUEUE_PATH.read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        print(
            "ERROR: queue file is not valid JSON (" + str(exc) + "). "
            "Fix manually before retrying.",
            file=sys.stderr,
        )
        sys.exit(1)


def _save_queue(data: dict) -> None:
    tmp = QUEUE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, QUEUE_PATH)


def _find_item(data: dict, item_id: str) -> dict | None:
    for item in data.get("queue", []):
        if item.get("id") == item_id:
            return item
    return None


def cmd_status(data: dict) -> int:
    queue = data.get("queue", [])
    counts = Counter(it.get("status", "unknown") for it in queue)
    print(
        "id".ljust(8)
        + " " + "strategy_id".ljust(32)
        + " " + "variation_id".ljust(34)
        + " " + "status".ljust(18)
        + " " + "verdict".ljust(10)
    )
    print("-" * 104)
    for it in queue:
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
    last = data.get("last_digest_sent")
    if last:
        print()
        print("last_digest_sent: " + str(last))
    return 0


def cmd_drop(data: dict, item_id: str, reason: str) -> int:
    item = _find_item(data, item_id)
    if item is None:
        print("ERROR: item id '" + item_id + "' not found",
              file=sys.stderr)
        return 1
    item["status"] = "dropped"
    item["drop_reason"] = reason
    item["dropped_at"] = _utcnow_iso()
    _save_queue(data)
    print("Dropped " + item_id + ": " + reason)
    return 0


def cmd_defer(data: dict, item_id: str, reason: str) -> int:
    item = _find_item(data, item_id)
    if item is None:
        print("ERROR: item id '" + item_id + "' not found",
              file=sys.stderr)
        return 1
    item["status"] = "deferred_no_data"
    item["defer_reason"] = reason
    _save_queue(data)
    print("Deferred " + item_id + ": " + reason)
    return 0


def cmd_requeue(data: dict, item_id: str) -> int:
    item = _find_item(data, item_id)
    if item is None:
        print("ERROR: item id '" + item_id + "' not found",
              file=sys.stderr)
        return 1
    item["status"] = "queued"
    item["error"] = None
    item["verdict"] = None
    item["started_at"] = None
    item["finished_at"] = None
    _save_queue(data)
    print("Requeued " + item_id)
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
                       help="Print queue table + status counts")
    group.add_argument("--drop", metavar="ID",
                       help="Mark item ID dropped (requires --reason)")
    group.add_argument("--defer", metavar="ID",
                       help="Mark item ID deferred_no_data "
                            "(requires --reason)")
    group.add_argument("--requeue", metavar="ID",
                       help="Reset item ID to queued")
    group.add_argument("--tail-log", action="store_true",
                       help="Print last N lines of trial_queue.log")
    parser.add_argument("--reason", default=None,
                        help="Reason text for --drop / --defer")
    parser.add_argument("--lines", type=int, default=50,
                        help="Number of log lines to print "
                             "(default 50)")
    args = parser.parse_args()

    if args.status:
        return cmd_status(_load_queue())

    if args.drop:
        if not args.reason:
            print("ERROR: --drop requires --reason", file=sys.stderr)
            return 2
        return cmd_drop(_load_queue(), args.drop, args.reason)

    if args.defer:
        if not args.reason:
            print("ERROR: --defer requires --reason", file=sys.stderr)
            return 2
        return cmd_defer(_load_queue(), args.defer, args.reason)

    if args.requeue:
        return cmd_requeue(_load_queue(), args.requeue)

    if args.tail_log:
        return cmd_tail_log(args.lines)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
