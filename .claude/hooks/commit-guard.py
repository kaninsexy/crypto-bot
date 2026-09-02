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

# Shared, quote-aware Bash parsing. Guarded like every other cross-hook import:
# an ImportError at load exits 1, which Claude Code treats as NON-blocking, so
# it must never escape.
try:
    from bash_targets import strip_heredoc_bodies as _strip_bodies
    from bash_targets import tokenize_segments as _tokenize
    _PARSE_IMPORT_ERROR: Exception | None = None
except Exception as _e:  # pragma: no cover - defensive
    _PARSE_IMPORT_ERROR = _e

    def _strip_bodies(cmd: str) -> str:  # type: ignore[misc]
        return cmd

    def _tokenize(cmd: str):  # type: ignore[misc]
        return []
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


def _is_real_git_commit(cmd: str) -> bool:
    """True only when the command actually INVOKES ``git commit``.

    Not a regex over the raw string. Heredoc bodies are stripped first (they
    are data), then the command is tokenized into pipeline segments and each
    segment's argv is inspected: argv0 must be ``git`` and the first non-flag
    argument must be ``commit``.

    Why the care. The first draft used ``re.search(r"\\bgit\\s+commit\\b")`` on
    the raw command, which blocked a ``python - <<'PY' ... PY`` call whose
    Python source merely CONTAINED the string ``git commit -F -`` inside a test
    fixture. It then validated the PYTHON heredoc as if it were a commit
    message and refused it for a non-conventional subject. Same failure class
    as BK-0011: a guard that false-blocks routine work gets disabled by
    whoever hits it, which is a slower way of failing open.

    This also gets ``git log --grep commit`` right for free (``--grep`` is a
    flag, ``commit`` is its value, so the first NON-flag arg is ``commit``…
    which is why the value-of-a-flag case is excluded explicitly below).
    """
    for seg in _tokenize(_strip_bodies(cmd)):
        if not seg or seg[0].rsplit("/", 1)[-1] != "git":
            continue
        skip_next = False
        for tok in seg[1:]:
            if skip_next:
                skip_next = False
                continue
            if tok.startswith("-"):
                # `--grep commit` / `-C dir`: a flag's VALUE is not the
                # subcommand. `--flag=value` carries its value inline.
                if "=" not in tok:
                    skip_next = True
                continue
            if tok == "commit":
                return True
            # Some OTHER git subcommand in this segment. Keep scanning the
            # remaining segments — `git add -A && git commit ...` is the
            # canonical form in this repo, and returning False on the first
            # git segment would wave every chained commit straight past the
            # gate.
            break
    return False


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
    if not cmd or not _is_real_git_commit(cmd):
        return 0
    if _AMEND_RE.search(_strip_bodies(cmd)):
        return 0

    session_id = payload.get("session_id") or "unknown"
    tool_name = payload.get("tool_name") or "Bash"

    bodies = _extract_heredoc_bodies(cmd)

    # --- SHAPE: -m without a heredoc is the archived commit-heredoc rule. ---
    if _DASH_M_RE.search(_strip_bodies(cmd)) and not bodies:
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
