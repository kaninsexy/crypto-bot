#!/usr/bin/env python3
"""
PreToolUse hook on Bash — blocks literal API keys and secret-shaped strings
from appearing in shell commands or being echoed to logs.

Ported 2026-09-02 from siamese-reconcile ``.claude/hooks/no-secrets-in-bash.py``,
with the pattern set merged from crypto-bot's own
``.claude/hooks/no-secrets-in-bash.sh`` (now archived under
``_archive_bash_2026-09/``). The bash original parsed stdin with ``jq``, which
is not installed on this machine, so it exited 127 and failed OPEN on every
call. This one has no external dependency and fails CLOSED.

Two crypto-bot specifics carried over from the bash version:
  - ``${VAR}`` env expansions are stripped BEFORE matching, so legitimate use
    of ``${OPENROUTER_API_KEY}`` / ``${RESEND_API_KEY}`` does not trigger.
  - ``~/.crypto-bot.env`` is the project's credential file (CLAUDE.md
    "Human only": editing or copying secrets is never an agent action), so
    any command that reads it is blocked.

Exit codes (per Claude Code hook protocol):
- 0: allow
- 2: block (stderr message visible to agent)
"""

from __future__ import annotations

import json
import re
import sys

# Same-directory import: when Python runs this file as a script, the
# script's directory is sys.path[0], so this resolves regardless of CWD.
# GUARDED so a broken/absent helper can't crash the module at load (exit 1 =
# fail-OPEN in Claude Code). The block-recorder is best-effort; the block
# decision never depends on it, so a no-op is safe.
try:
    from _block_record import _write_block_record
except Exception:  # pragma: no cover - defensive; recorder is best-effort
    def _write_block_record(*_a: object, **_k: object) -> None:
        return None

# ``${VAR}`` and ``$VAR`` references are stripped before matching so a
# legitimate env-var use never looks like a literal key.
_ENV_REF_RE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*(?::-[^}]*)?\}")

PATTERNS: tuple[str, ...] = (
    # --- credential literals (union of siamese + crypto-bot bash lists) ---
    r"sk-ant-[a-zA-Z0-9_-]{20,}",          # Anthropic
    r"sk-or-[a-zA-Z0-9_-]{20,}",           # OpenRouter
    r"sk-[a-zA-Z0-9]{32,}",                # OpenAI
    # Resend (notifier agent). The left lookbehind and the alnum-only body are
    # both load-bearing: the first draft was `re_[a-zA-Z0-9_-]{20,}`, which
    # matched INSIDE the ordinary path `scripts/p<re_>commit_backlog_check.sh`
    # (the 20 chars after `re_` being `commit_backlog_check`) and blocked every
    # Bash command that named it, commit messages included. A guard that
    # false-blocks routine work gets disabled by whoever hits it, which is a
    # slower way of failing open. Real Resend keys are `re_` + base62.
    r"(?<![A-Za-z0-9_-])re_[A-Za-z0-9]{20,}",
    r"xoxb-[A-Za-z0-9-]{20,}",             # Slack bot token
    r"AKIA[0-9A-Z]{16}",                   # AWS access key id
    r"AIza[0-9A-Za-z_-]{35}",              # Google / Gemini
    r"tvly-[A-Za-z0-9]{20,}",              # Tavily
    r"-----BEGIN[ \t]+(?:[A-Z]+[ \t]+)*PRIVATE[ \t]+KEY-----",
    r"Bearer [A-Za-z0-9+/=]{100,}",
    r"Authorization: *Bearer [A-Za-z0-9._~+/-]{40,}",
    # --- reading a secrets file -------------------------------------------
    r"cat[ \t]+[^|;&\n]*\.env",
    r"cat[ \t]+[^|;&\n]*crypto-bot\.env",
    r"cat[ \t]+/[^|;&\n]*credentials",
    # --- echoing a secret out of the environment --------------------------
    r"echo[ \t]+[^|;&\n]*Authorization",
    r"echo[ \t]+[^\n]*\$(?:ANTHROPIC_API_KEY|OPENROUTER_API_KEY|RESEND_API_KEY|OKX_[A-Z_]*KEY)",
    r"printenv\s+(?:ANTHROPIC_API_KEY|OPENROUTER_API_KEY|RESEND_API_KEY|OKX_[A-Z_]*KEY)",
)


def main() -> int:
    raw = sys.stdin.read()
    if not raw.strip():
        return 0
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"[no-secrets-in-bash] WARN: stdin not JSON ({e}); allowing", file=sys.stderr)
        return 0

    cmd = (payload.get("tool_input") or {}).get("command", "")
    if not cmd:
        return 0

    session_id = payload.get("session_id") or "unknown"
    tool_name = payload.get("tool_name") or "Bash"

    # Strip ``${VAR}`` references: the whole point of using one is that the
    # literal never appears in the command.
    scan_target = _ENV_REF_RE.sub("", cmd)

    for idx, pat in enumerate(PATTERNS):
        if re.search(pat, scan_target):
            print(
                f"[no-secrets-in-bash] BLOCKED: command contains secret-shaped pattern: {pat}",
                file=sys.stderr,
            )
            print(
                "[no-secrets-in-bash] Use the variable reference instead of the literal value, "
                "e.g. ${RESEND_API_KEY:-} not the actual key. Reading ~/.crypto-bot.env is "
                'Human-only per CLAUDE.md "Human only".',
                file=sys.stderr,
            )
            # PII: never capture the raw command (it contains the secret
            # literal that triggered the block). The pattern index is
            # enough for the curator to see what kind was attempted.
            _write_block_record(
                hook_name="no-secrets-in-bash",
                tool_name=tool_name,
                reason="BLOCKED: command contains secret-shaped pattern",
                blocked_target="command",
                blocked_pattern_id=f"SECRET_PATTERN_{idx}",
                session_id=session_id,
            )
            return 2

    return 0


if __name__ == "__main__":
    # FAIL CLOSED: a guard that cannot run must DENY. Any unhandled exception
    # exits 2 (block), never crashes to exit 1 (which Claude Code treats as
    # non-blocking, letting the tool proceed).
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as e:  # noqa: BLE001 — deliberate catch-all, fail closed
        print(
            f"[no-secrets-in-bash] FAIL-CLOSED: guard crashed ({type(e).__name__}: {e}); "
            "blocking (exit 2).",
            file=sys.stderr,
        )
        sys.exit(2)
