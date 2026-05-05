"""scripts/intake_pdf_research.py — PDF-to-queue bridge.

CLI tool for adding human-reviewed PDF research to the trial queue.
Called by Claude Code after a human provides a PDF in chat that has
already been evaluated and determined to pass the quality bar.

Usage:
  python scripts/intake_pdf_research.py \\
      --strategy-id "StrategyName" \\
      --variation-id "variation-slug" \\
      --hypothesis "one sentence" \\
      --source-citation "Author et al. (YEAR) Journal" \\
      --literature-doc "research/strategy-literature.md" \\
      --quality 3.8 \\
      --citations-json '[{"title":"...","quality_score":4}]' \\
      --implementation-notes "brief notes"

Validation gates (both must pass before any write):
  * quality >= 3.0
  * at least 1 citation in --citations-json

The script does NOT write the trial script itself — that is handled
by a follow-up Claude Code session triggered when the orchestrator
hits needs_trial_script=true (or by the human manually).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
QUEUE_PATH = ROOT / "backtest" / "trial_queue.json"

MIN_QUALITY = 3.0
ID_PATTERN = re.compile(r"^sq-(\d+)$")


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_queue() -> dict:
    if not QUEUE_PATH.exists():
        return {
            "schema_version": 1,
            "batch_alert_sent_at_position": 0,
            "queue": [],
        }
    text = QUEUE_PATH.read_text(encoding="utf-8").strip()
    if not text:
        return {
            "schema_version": 1,
            "batch_alert_sent_at_position": 0,
            "queue": [],
        }
    return json.loads(text)


def save_queue(data: dict) -> None:
    """Atomic write: tmp file + os.replace."""
    tmp = QUEUE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, QUEUE_PATH)


def next_id(queue: list[dict]) -> str:
    """Pick the next sq-NNN id one above the current max."""
    max_n = -1
    for item in queue:
        m = ID_PATTERN.match(item.get("id", "") or "")
        if m:
            n = int(m.group(1))
            if n > max_n:
                max_n = n
    return f"sq-{max_n + 1:03d}"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Add a PDF-reviewed research proposal to the trial queue.",
    )
    parser.add_argument("--strategy-id", required=True)
    parser.add_argument("--variation-id", required=True)
    parser.add_argument("--hypothesis", required=True)
    parser.add_argument("--source-citation", required=True)
    parser.add_argument("--literature-doc", required=True)
    parser.add_argument("--quality", type=float, required=True)
    parser.add_argument(
        "--citations-json",
        required=True,
        help="JSON array of citation objects.",
    )
    parser.add_argument("--implementation-notes", required=True)
    args = parser.parse_args()

    # ── 1. Validate ─────────────────────────────────────────────────────────
    if args.quality < MIN_QUALITY:
        print(
            f"VALIDATION FAILED: quality {args.quality} < "
            f"MIN_QUALITY {MIN_QUALITY}",
            file=sys.stderr,
        )
        return 1
    try:
        citations = json.loads(args.citations_json)
    except json.JSONDecodeError as exc:
        print(
            f"VALIDATION FAILED: --citations-json is not valid JSON: {exc}",
            file=sys.stderr,
        )
        return 1
    if not isinstance(citations, list):
        print(
            "VALIDATION FAILED: --citations-json must decode to a list "
            f"(got {type(citations).__name__})",
            file=sys.stderr,
        )
        return 1
    if len(citations) < 1:
        print(
            "VALIDATION FAILED: --citations-json must contain at least "
            "one citation",
            file=sys.stderr,
        )
        return 1

    # ── 2. Load queue + dedupe check ────────────────────────────────────────
    queue_data = load_queue()
    queue = queue_data.setdefault("queue", [])
    for item in queue:
        if (
            item.get("strategy_id") == args.strategy_id
            and item.get("variation_id") == args.variation_id
        ):
            print(
                f"VALIDATION FAILED: existing queue item "
                f"{item.get('id')} has same strategy_id + variation_id "
                f"({args.strategy_id} / {args.variation_id})",
                file=sys.stderr,
            )
            return 1

    # ── 3. Build new entry ──────────────────────────────────────────────────
    new_id = next_id(queue)
    script_path = (
        f"scripts/run_{args.strategy_id.lower()}_trial.py"
    )
    entry = {
        "id": new_id,
        "status": "queued",
        "strategy_id": args.strategy_id,
        "variation_id": args.variation_id,
        "script_path": script_path,
        "trial_type": "full_cpcv",
        "hypothesis_one_line": args.hypothesis,
        "source_citation": args.source_citation,
        "literature_doc": args.literature_doc,
        "citations": citations,
        "overall_quality": float(args.quality),
        "implementation_notes": args.implementation_notes,
        "added_by": "kanin-pdf-intake",
        "added_at": utcnow_iso(),
        "started_at": None,
        "finished_at": None,
        "verdict": None,
        "trial_id": None,
        "error": None,
        "email_sent": False,
        "needs_trial_script": True,
    }
    queue.append(entry)

    # ── 4. Persist atomically ───────────────────────────────────────────────
    save_queue(queue_data)

    print(f"QUEUED: {new_id} {args.strategy_id} {args.variation_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
