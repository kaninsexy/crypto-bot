#!/usr/bin/env python3
"""post-commit-sync.py — PostToolUse hook on Bash.

Fires ``scripts/post_commit_sync.sh --no-push`` as a detached background
subprocess after any successful ``git commit``. The ``--no-push`` flag is the
whole point: a routine CC commit auto-refreshes ``repomix-output.xml`` and
commits it LOCALLY, but never pushes.

CLAUDE.md 2026-09-02 (mandate G, boundary moved from the push to the GATE):
CC pushes its own finished, gated work by running the SAME script with NO
flag, once a stage's VERIFY block is green. So the two callers differ only in
that flag — the hook never pushes, the deliberate post-VERIFY invocation does.
Force-push, branch deletion, pushing any branch but ``main``, live deploy and
live capital remain Human-only.

Ported 2026-09-02 from siamese-reconcile. Non-blocking. Always exits 0.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import sys
from pathlib import Path


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    if payload.get("tool_name", "") != "Bash":
        return 0

    cmd = payload.get("tool_input", {}).get("command", "")
    if not re.search(r"\bgit\s+commit\b", cmd):
        return 0

    # Only fire if the commit itself succeeded. The live PostToolUse Bash
    # payload carries no exit_code key at all and fires only on success, so an
    # ABSENT key means success; an explicit non-zero one means failure.
    tool_response = payload.get("tool_response") or {}
    if isinstance(tool_response, dict):
        explicit = tool_response.get("exit_code", tool_response.get("returncode"))
        if isinstance(explicit, int) and explicit != 0:
            return 0
        if tool_response.get("is_error") or tool_response.get("error"):
            return 0
        if tool_response.get("interrupted"):
            return 0

    repo_root = Path(os.environ.get("CLAUDE_PROJECT_DIR", ".")).resolve()
    sync_script = repo_root / "scripts" / "post_commit_sync.sh"
    if not sync_script.exists():
        return 0

    # Detached background subprocess — never blocks CC, never inherits stdio.
    proc = subprocess.Popen(  # noqa: S603 — known-trusted local script
        ["bash", str(sync_script), "--no-push"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        cwd=str(repo_root),
    )

    # Best-effort audit log — never raises into the hook return path.
    try:
        log_path = repo_root / ".memory" / "T1_episodic" / "_state" / "post_commit_sync_log.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a") as f:
            f.write(
                json.dumps(
                    {
                        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        "pid": proc.pid,
                        "cmd_excerpt": cmd[:80],
                    }
                )
                + "\n"
            )
    except Exception:
        pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
