#!/usr/bin/env python3
"""Block-record helper shared by guard hooks.

When a PreToolUse guard hook (path-allowlist, no-secrets-in-bash)
exits 2 to block a tool call, it also writes a small JSON record to:

    .memory/T1_episodic/blocks/<YYYY-MM-DD>/<HHMMSS_microseconds>_<hook>_<tool>.json

The record is consumed by the curator agent as a high-signal
observation ("agent tried X, was blocked, here's the context"). Blocks
fire rarely under normal operation, so volume is low and signal per
record is high.

Record schema:

    {
      "ts": "<ISO-8601 UTC>",
      "hook": "path-allowlist" | "no-secrets-in-bash",
      "tool_name": "Write" | "Edit" | "Bash",
      "reason": "<short string the hook already prints to stderr — first line>",
      "blocked_target": "<relative path or 'command' for no-secrets-in-bash>",
      "blocked_pattern_id": "<regex idx, or 'SACRED'+entry, or 'BASH_<kind>'>",
      "session_id": "<from payload.get('session_id') or 'unknown'>"
    }

PII boundary:
- For no-secrets-in-bash, `blocked_target` is the literal string
  ``"command"`` and never the actual command string (the command may
  contain the secret literal that triggered the block). The regex
  pattern id is the curator-visible signal.
- For path-allowlist, `blocked_target` is the relative repo path,
  which is always a Tier 1 sacred path by definition of why the block
  fired — already a known-and-named file, no PII risk.

Failure mode:
- Writing the record MUST NOT change the exit code. Both hooks call
  this helper after deciding to exit 2; the helper's success or
  failure is irrelevant to the security decision.
- Writing the record MUST NOT raise. On disk full, permission denied,
  or any other I/O error, the helper logs `[block-recorder] WARN: ...`
  to stderr and returns silently.
- A single short ``Path.write_text(json.dumps(...))`` call. No fsync,
  no retries, no fancy locking — microsecond-precision filenames make
  collisions effectively impossible for the volumes we expect.

Test isolation:
- ``BLOCK_RECORD_DIR`` env var, when set, overrides the default output
  directory (``<project>/.memory/T1_episodic/blocks/<date>/``). Tests
  set this to a tmpdir so block writes don't pollute the real
  ``.memory/`` tree during pytest runs.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def _write_block_record(
    hook_name: str,
    tool_name: str,
    reason: str,
    blocked_target: str,
    blocked_pattern_id: str,
    session_id: str,
) -> None:
    """Append a single block-record JSON file. Best-effort, never raises."""
    try:
        now = datetime.now(timezone.utc)
        date_dir = now.strftime("%Y-%m-%d")
        # Microsecond-precision so two blocks in the same second can't collide.
        ts_part = now.strftime("%H%M%S_%f")

        override = os.environ.get("BLOCK_RECORD_DIR")
        if override:
            base = Path(override)
        else:
            project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or str(Path.cwd())
            base = Path(project_dir) / ".memory" / "T1_episodic" / "blocks" / date_dir

        base.mkdir(parents=True, exist_ok=True)
        filename = f"{ts_part}_{hook_name}_{tool_name or 'unknown'}.json"
        record = {
            "ts": now.isoformat().replace("+00:00", "Z"),
            "hook": hook_name,
            "tool_name": tool_name or "unknown",
            "reason": reason.splitlines()[0] if reason else "",
            "blocked_target": blocked_target,
            "blocked_pattern_id": blocked_pattern_id,
            "session_id": session_id or "unknown",
        }
        (base / filename).write_text(json.dumps(record))
    except Exception as e:  # noqa: BLE001 — explicitly best-effort
        print(f"[block-recorder] WARN: {e}", file=sys.stderr)
