#!/usr/bin/env python3
"""PreToolUse hook on Bash — blocks live-deploy verbs.

Python port (2026-09-02) of ``.claude/hooks/no-deploy.sh``, archived under
``_archive_bash_2026-09/``. The bash original parsed stdin with ``jq``, absent
on this machine, so it exited 127 and failed OPEN — the single most important
guard in the repo was not running.

Scope, per CLAUDE.md "Human only": live deploy to production (real OKX API or
any non-paper venue) is human-only. This hook blocks the transport verbs that
would carry a deploy: ``doctl``, ``kubectl``, ``ssh``, ``docker push``,
``digitalocean``, ``dokku``, and a bare ``deploy`` verb.

It does NOT block ``git push``: since the 2026-09-02 boundary move (CLAUDE.md
mandate G) an ordinary ``git push origin main`` of finished, gated work is
agent-autonomous. Force-push, branch deletion and pushing any branch but
``main`` remain human-only and are blocked below.

Heredoc bodies are stripped before scanning (shared ``bash_targets`` helper):
a commit message that QUOTES the word "deploy" is data, not a command.

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

try:
    from bash_targets import strip_heredoc_bodies
    _IMPORT_ERROR: Exception | None = None
except Exception as _e:  # pragma: no cover - defensive
    _IMPORT_ERROR = _e

    def strip_heredoc_bodies(cmd: str) -> str:  # type: ignore[misc]
        return cmd


# (pattern_id, regex) — word-boundary anchored so "redeploy" / "deployable"
# do not match, matching the bash original's stated intent.
#
# ALWAYS: unambiguous transport verbs plus the irreversible git operations
# that stayed Human-only after the 2026-09-02 push-boundary move (mandate G).
# These are safe to run on EVERY Bash call, so settings.json wires them
# globally — the bash original ran only inside three agent frontmatters, which
# left the main session unguarded.
DEPLOY_PATTERNS: tuple[tuple[str, str], ...] = (
    ("DOCTL", r"\bdoctl\b"),
    ("KUBECTL", r"\bkubectl\b"),
    ("DOCKER_PUSH", r"\bdocker\s+push\b"),
    ("DIGITALOCEAN", r"\bdigitalocean\b"),
    ("DOKKU", r"\bdokku\b"),
    ("GIT_FORCE_PUSH", r"\bgit\s+push\b[^|;&\n]*(?:--force\b|--force-with-lease\b|\s-f\b)"),
    ("GIT_BRANCH_DELETE", r"\bgit\s+push\b[^|;&\n]*(?:--delete\b|\s:[A-Za-z0-9._/-]+)"),
    ("GIT_RESET_HARD_MAIN", r"\bgit\s+reset\s+--hard\b[^|;&\n]*\bmain\b"),
)

# --strict ADDS the two broad verbs from the bash original. They are broad
# enough to false-block ordinary work (`grep deploy docs/`, an ssh-config
# read), which is why the bash hook was only ever bound to the three agents
# that can reach a venue. Those agent frontmatters keep the strict form; the
# global wiring does not.
STRICT_PATTERNS: tuple[tuple[str, str], ...] = (
    ("SSH", r"\bssh\b"),
    ("DEPLOY_VERB", r"\bdeploy\b"),
)


def main() -> int:
    raw = sys.stdin.read()
    if not raw.strip():
        return 0
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"[no-deploy] WARN: stdin not JSON ({e}); allowing", file=sys.stderr)
        return 0

    cmd = (payload.get("tool_input") or {}).get("command", "") or ""
    if not cmd:
        return 0

    scan_target = strip_heredoc_bodies(cmd)
    session_id = payload.get("session_id") or "unknown"
    tool_name = payload.get("tool_name") or "Bash"

    patterns = DEPLOY_PATTERNS
    if "--strict" in sys.argv[1:]:
        patterns = patterns + STRICT_PATTERNS

    for kind, pat in patterns:
        if re.search(pat, scan_target):
            print(
                f"[no-deploy] BLOCKED: irreversible/live operation detected ({kind}). "
                'CLAUDE.md "Human only": live deploy, live capital, force-push, branch '
                "deletion and pushing any branch but main are human-only. Recommend, "
                "do not execute.",
                file=sys.stderr,
            )
            _write_block_record(
                hook_name="no-deploy",
                tool_name=tool_name,
                reason=f"BLOCKED: live/irreversible operation ({kind})",
                blocked_target="command",
                blocked_pattern_id=f"DEPLOY_{kind}",
                session_id=session_id,
            )
            return 2

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as e:  # noqa: BLE001 — deliberate catch-all, fail closed
        print(
            f"[no-deploy] FAIL-CLOSED: guard crashed ({type(e).__name__}: {e}); "
            "blocking (exit 2).",
            file=sys.stderr,
        )
        sys.exit(2)
