#!/usr/bin/env python3
"""PreCompact hook — saves a state snapshot before context compaction.

Ported 2026-09-02 from siamese-reconcile. Completes the memory-persistence
trio (SessionStart / PreCompact / Stop). Compaction is exactly when context is
about to be lost, so this is the moment to flush a durable marker. We write a
small snapshot to:

    .memory/T1_episodic/_state/precompact_<YYYY-MM-DDTHHMMSSZ>.md

and remind the agent (via stdout) to refresh the handoff if mid-task. The
snapshot records WHEN a compaction happened and the trigger (auto vs manual),
which the curator and the SessionStart loader can use. We do NOT attempt to
dump the transcript — the hook payload doesn't reliably carry it, and a raw
transcript can contain credential-shaped strings the no-secrets guard exists
to keep out of durable files.

Always exits 0. Never blocks compaction. Never raises.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    raw = sys.stdin.read()
    payload: dict = {}
    if raw.strip():
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {}

    try:
        now = datetime.now(timezone.utc)
        ts = now.strftime("%Y-%m-%dT%H%M%SZ")
        project = Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
        state_dir = project / ".memory" / "T1_episodic" / "_state"
        state_dir.mkdir(parents=True, exist_ok=True)

        trigger = payload.get("trigger") or payload.get("compact_trigger") or "unknown"
        session_id = payload.get("session_id") or "unknown"

        snapshot = (
            f"# Pre-compact snapshot {ts}\n\n"
            f"- session_id: {session_id}\n"
            f"- trigger: {trigger}  (auto = context full; manual = /compact)\n"
            f"- note: context was compacted at this point. If a task was in\n"
            f"  flight, the handoff (docs/handoff_template.md), the megaloop\n"
            f"  status doc, and .memory/T2_semantic/backlog.jsonl should reflect\n"
            f"  current state so the next SessionStart load is accurate.\n"
        )
        (state_dir / f"precompact_{ts}.md").write_text(snapshot, encoding="utf-8")
    except Exception as e:  # noqa: BLE001 — best-effort
        print(f"[pre-compact] WARN: {e}", file=sys.stderr)
        return 0

    # Surface a reminder into the post-compaction context.
    print(
        "[pre-compact] State snapshot saved. If mid-task, ensure the handoff, "
        "the status doc and backlog.jsonl are current before continuing."
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001 — must never break compaction
        print(f"[pre-compact] WARN: {e}", file=sys.stderr)
        sys.exit(0)
