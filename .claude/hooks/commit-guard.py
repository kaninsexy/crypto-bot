#!/usr/bin/env python3
"""PreToolUse hook on Bash — mandate G/H commit-message shape + content gate.

Python port (2026-09-02) merging two jq-dependent bash hooks, both archived
under ``_archive_bash_2026-09/``:

  - ``commit-heredoc-required.sh`` — SHAPE. A ``git commit`` message must be
    heredoc-embedded inside the same Bash call. CLAUDE.md mandate G:
    "mandatory message embedding via hooks is safer than manual commit typing,
    which twice produced empty commits when humans skipped the editor."
  - ``commit-format.sh``           — CONTENT. Conventional-commit subject, and
    an agent-authored commit (detected by the ``Co-authored-by: Claude``
    trailer, per architecture.md E.3) must carry the ``[mandate-H]`` token.

Merging them is the point of the port: they always fired together, on the same
matcher, parsing the same field, and both failed OPEN because ``jq`` is not
installed on this machine. One Python hook, no external dependency, fails
CLOSED.

Passes through (exit 0) without inspection:
  - any command with no ``git commit`` in it
  - ``git commit --amend`` (no new message body to validate)
  - ``git commit`` with neither ``-m`` nor a heredoc, i.e. an editor commit —
    the ``.githooks/commit-msg`` layer gates that one (architecture.md E.3
    layer 3), and blocking here would make an interactive commit impossible.

Exit codes: 0 allow / 2 block.
"""

from __future__ import annotations

import json
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

_GIT_COMMIT_RE = re.compile(r"\bgit\s+commit\b")
_AMEND_RE = re.compile(r"--amend\b")
_DASH_M_RE = re.compile(r"(?:^|\s)-m\b")
_HEREDOC_OPEN_RE = re.compile(
    r"""<<-?\s*(?P<quote>['"]?)(?P<tag>[A-Za-z_][A-Za-z0-9_]*)(?P=quote)"""
)

SUBJECT_RE = re.compile(
    r"^(feat|fix|chore|docs|test|refactor|perf|build|ci|trials|governance|data|"
    r"retire|discovery|harness)(\([a-zA-Z0-9._-]+\))?: .{1,72}$"
)

HEREDOC_HELP = """[commit-guard] BLOCKED: git commit message must be heredoc-embedded
(CLAUDE.md mandate G / mandate H). Required form:

  git commit -F - <<'EOF'
  <type>(<scope>): <description>

  Body explaining the change.

  [mandate-H]
  Co-authored-by: Claude <noreply@anthropic.com>
  EOF

A literal -m message is refused because a message typed outside the command
is the failure mode that produced empty commits twice."""


def _extract_heredoc_bodies(cmd: str) -> list[str]:
    """Return each heredoc body in the command, in order of appearance."""
    bodies: list[str] = []
    pending_tag: str | None = None
    current: list[str] = []
    for line in cmd.split("\n"):
        if pending_tag is not None:
            if line.strip() == pending_tag:
                bodies.append("\n".join(current))
                current = []
                pending_tag = None
            else:
                current.append(line)
            continue
        m = _HEREDOC_OPEN_RE.search(line)
        if m is not None:
            pending_tag = m.group("tag")
    if pending_tag is not None and current:
        # Unterminated heredoc (truncated command); treat what we have as the body.
        bodies.append("\n".join(current))
    return bodies


def _block(msg: str, tool_name: str, session_id: str, pattern_id: str) -> int:
    print(msg, file=sys.stderr)
    _write_block_record(
        hook_name="commit-guard",
        tool_name=tool_name or "Bash",
        reason=msg.splitlines()[0],
        blocked_target="command",
        blocked_pattern_id=pattern_id,
        session_id=session_id,
    )
    return 2


def main() -> int:
    raw = sys.stdin.read()
    if not raw.strip():
        return 0
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"[commit-guard] WARN: stdin not JSON ({e}); allowing", file=sys.stderr)
        return 0

    cmd = (payload.get("tool_input") or {}).get("command", "") or ""
    if not cmd or not _GIT_COMMIT_RE.search(cmd):
        return 0
    if _AMEND_RE.search(cmd):
        return 0

    session_id = payload.get("session_id") or "unknown"
    tool_name = payload.get("tool_name") or "Bash"

    bodies = _extract_heredoc_bodies(cmd)

    # --- SHAPE: -m without a heredoc is the archived commit-heredoc rule. ---
    if _DASH_M_RE.search(cmd) and not bodies:
        return _block(HEREDOC_HELP, tool_name, session_id, "COMMIT_NO_HEREDOC")

    if not bodies:
        # Editor commit (no -m, no heredoc). .githooks/commit-msg gates it.
        return 0

    # --- CONTENT: validate the LAST heredoc body (the message). -------------
    msg = bodies[-1].strip("\n")
    lines = [ln for ln in msg.split("\n")]
    subject = next((ln.strip() for ln in lines if ln.strip()), "")
    if not SUBJECT_RE.match(subject):
        return _block(
            "[commit-guard] BLOCKED: commit subject does not match conventional format.\n"
            "Required: <type>(<scope>): <description>   (subject 1..72 chars)\n"
            "  type in: feat fix chore docs test refactor perf build ci trials "
            "governance data retire discovery harness\n"
            f"Got: {subject}",
            tool_name,
            session_id,
            "COMMIT_SUBJECT_FORMAT",
        )

    if re.search(r"^Co-authored-by: Claude", msg, flags=re.MULTILINE | re.IGNORECASE):
        if "[mandate-H]" not in msg:
            return _block(
                "[commit-guard] BLOCKED: agent-authored commit must contain the "
                "[mandate-H] token in its body (architecture.md E.3 layer 2).\n"
                "Detected via the 'Co-authored-by: Claude' trailer.",
                tool_name,
                session_id,
                "COMMIT_MISSING_MANDATE_H",
            )

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as e:  # noqa: BLE001 — deliberate catch-all, fail closed
        print(
            f"[commit-guard] FAIL-CLOSED: guard crashed ({type(e).__name__}: {e}); "
            "blocking (exit 2).",
            file=sys.stderr,
        )
        sys.exit(2)
