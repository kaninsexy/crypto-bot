#!/usr/bin/env python3
"""PreToolUse hook on Write (curator subagent) — single-path write allowlist.

Python port (2026-09-02) of ``.claude/hooks/path-allowlist.sh``, archived under
``_archive_bash_2026-09/``. RENAMED so it cannot be confused with the new
repo-wide tier gate ``path-allowlist.py``: these are different guards.

  - ``path-allowlist.py``          — repo-wide three-tier gate, wired globally.
  - ``curator-write-allowlist.py`` — curator-ONLY: the curator has exactly one
    writable path.

Curator is the only agent with write authority into the T2 pending-review
queue, and that is its ONLY allowed write path. The strategist-side review is
the gate from ``_pending_review`` to ``facts.md`` / ``citations/`` — curator
must not touch either directly (architecture.md A.4 step 1).

The bash original parsed stdin with ``jq``, absent here, so it exited 127 and
failed OPEN: the curator was effectively unconstrained. This one fails CLOSED.

Bind it from ``.claude/agents/curator.md`` frontmatter only, never globally.

Exit codes: 0 allow / 2 block.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

_HOOKS_DIR = str(Path(__file__).resolve().parent)
if _HOOKS_DIR not in sys.path:
    sys.path.insert(0, _HOOKS_DIR)

try:
    from _block_record import _write_block_record
except Exception:  # pragma: no cover - defensive; recorder is best-effort
    def _write_block_record(*_a: object, **_k: object) -> None:
        return None

# Curator's one writable path, plus the per-day episodic directory it writes
# its own observations into (architecture.md A.4). Anchored ``re.match``.
ALLOWLIST: tuple[str, ...] = (
    r"^\.memory/T2_semantic/_pending_review\.jsonl$",
    r"^\.memory/T1_episodic/.*",
)


def _to_relative(file_path: str, project_dir: str) -> str:
    norm = file_path.replace("\\", "/")
    proj = project_dir.replace("\\", "/").rstrip("/")
    if proj and norm.startswith(proj + "/"):
        norm = norm[len(proj) + 1 :]
    if norm.startswith("./"):
        norm = norm[2:]
    return os.path.normpath(norm).replace("\\", "/")


def main() -> int:
    raw = sys.stdin.read()
    if not raw.strip():
        # No payload to evaluate: fail closed, this hook only ever runs for
        # the curator, whose every write must be checked.
        print("[curator-write-allowlist] BLOCKED (fail-closed): empty stdin.", file=sys.stderr)
        return 2
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"[curator-write-allowlist] BLOCKED (fail-closed): stdin not JSON ({e}).",
              file=sys.stderr)
        return 2

    tool_input = payload.get("tool_input") or {}
    target = tool_input.get("file_path") or tool_input.get("path") or ""
    if not target:
        return 0

    project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or str(Path.cwd())
    rel = _to_relative(str(target), project_dir)

    if any(re.match(pat, rel) for pat in ALLOWLIST):
        return 0

    print(
        f"[curator-write-allowlist] BLOCKED: {rel} is not in the curator path allowlist.\n"
        "Allowed: .memory/T2_semantic/_pending_review.jsonl and .memory/T1_episodic/**.\n"
        "Promotion from _pending_review to facts.md / citations/ is the strategist's "
        "review gate (architecture.md A.4 step 1), not the curator's write.",
        file=sys.stderr,
    )
    _write_block_record(
        hook_name="curator-write-allowlist",
        tool_name=payload.get("tool_name") or "Write",
        reason=f"BLOCKED: {rel} not in curator path allowlist",
        blocked_target=rel,
        blocked_pattern_id="CURATOR_PATH_NOT_ALLOWED",
        session_id=payload.get("session_id") or "unknown",
    )
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as e:  # noqa: BLE001 — deliberate catch-all, fail closed
        print(
            f"[curator-write-allowlist] FAIL-CLOSED: guard crashed ({type(e).__name__}: {e}); "
            "blocking (exit 2).",
            file=sys.stderr,
        )
        sys.exit(2)
