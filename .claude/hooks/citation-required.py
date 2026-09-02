#!/usr/bin/env python3
"""PreToolUse hook on WebFetch|WebSearch (proposer subagent) — mandate P.

Python port (2026-09-02) of ``.claude/hooks/citation-required.sh``, archived
under ``_archive_bash_2026-09/``. The bash original read ``transcript_path``
via ``jq``; with ``jq`` absent it exited 127, which Claude Code treats as
non-blocking — so mandate P was not enforced at all.

Mandate P: a ``citation_key:`` line must be declared in the transcript BEFORE
any web search. Forcing the declaration first is what stops a speculative
search from retroactively justifying a parameter choice — the no-p-hacking
rule (``.claude/rules/backtest.md``) at the tool layer.

Also gates edits to ``research/*-literature.md``: a literature file is the
hypothesis-of-record, and a variation row added to it without a declared
citation_key is the p-hacking shape the rule exists to stop.

FAIL CLOSED: an unreadable transcript blocks, because a citation that cannot
be verified is indistinguishable from one that was never declared.

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

_LITERATURE_RE = re.compile(r"^research/.*-literature\.md$")

HELP = """[citation-required] BLOCKED: mandate P requires a declared citation_key.
Add a line 'citation_key: <slug>' to the proposer prompt before searching or
before editing research/<strategy>-literature.md. Declaring what you are
looking for FIRST is what prevents a search from retroactively justifying a
parameter choice (.claude/rules/backtest.md, no-p-hacking rule)."""


def _relevant(payload: dict) -> bool:
    """True when this call is in scope: a web tool, or a literature-file edit."""
    tool = payload.get("tool_name") or ""
    if tool in ("WebSearch", "WebFetch"):
        return True
    if tool in ("Write", "Edit", "MultiEdit"):
        ti = payload.get("tool_input") or {}
        raw = ti.get("file_path") or ti.get("path") or ""
        rel = str(raw).replace("\\", "/")
        rel = rel.split("/crypto-bot/", 1)[-1] if "/crypto-bot/" in rel else rel
        return bool(_LITERATURE_RE.match(rel.lstrip("./")))
    return False


def main() -> int:
    raw = sys.stdin.read()
    if not raw.strip():
        return 0
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        # A guard that cannot read its input must deny (mandate P is a
        # p-hacking gate, not an ergonomics nicety).
        print("[citation-required] BLOCKED (fail-closed): stdin not JSON.", file=sys.stderr)
        return 2

    if not isinstance(payload, dict) or not _relevant(payload):
        return 0

    session_id = payload.get("session_id") or "unknown"
    tool_name = payload.get("tool_name") or "unknown"
    transcript = payload.get("transcript_path") or ""

    if not transcript or not Path(transcript).is_file():
        print(
            "[citation-required] BLOCKED: transcript_path missing or unreadable; "
            "citation_key cannot be verified.\n" + HELP,
            file=sys.stderr,
        )
        _write_block_record(
            hook_name="citation-required",
            tool_name=tool_name,
            reason="BLOCKED: transcript unreadable; citation_key unverifiable",
            blocked_target="transcript",
            blocked_pattern_id="CITATION_TRANSCRIPT_UNREADABLE",
            session_id=session_id,
        )
        return 2

    try:
        text = Path(transcript).read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"[citation-required] BLOCKED (fail-closed): {e}", file=sys.stderr)
        return 2

    if "citation_key:" in text:
        return 0

    print(HELP, file=sys.stderr)
    _write_block_record(
        hook_name="citation-required",
        tool_name=tool_name,
        reason="BLOCKED: no citation_key declared in transcript",
        blocked_target="transcript",
        blocked_pattern_id="CITATION_KEY_MISSING",
        session_id=session_id,
    )
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as e:  # noqa: BLE001 — deliberate catch-all, fail closed
        print(
            f"[citation-required] FAIL-CLOSED: guard crashed ({type(e).__name__}: {e}); "
            "blocking (exit 2).",
            file=sys.stderr,
        )
        sys.exit(2)
