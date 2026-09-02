# SPDX-License-Identifier: LicenseRef-Proprietary
# Copyright (c) 2026 Kanin Srijundorn. All rights reserved.
"""PreToolUse hook on Write|Edit — write-origin provenance guard.

Ported (re-implemented) from Hermes' `skill_provenance.py` write-origin model
(playbooks port V6 (BK-0016)). `path-allowlist.py` guards at the FILE-TIER level
(sacred vs schema-stable vs autonomous). This guard adds an orthogonal
PROVENANCE level: even for non-sacred files, an autonomous BACKGROUND write may
only touch artifacts the agent itself authors — human-authored content stays
off-limits to the background loop.

The write-origin signal is the ``CLAUDE_WRITE_ORIGIN`` env var (our analogue of
Hermes' per-write ContextVar):

- unset / ``foreground`` -> a human-driven (foreground) write. This guard is a
  NO-OP; ``path-allowlist.py`` still governs sacred files. Exit 0.
- ``background_review`` -> the autonomous curator/reviewer fork (see
  ``scripts/background_review.py``). It may write ONLY the artifacts it authors
  (``BACKGROUND_WRITABLE`` below: the pending-review queue, fact-health,
  citations, rejected, episodic logs, proposals). A background write to
  ANYTHING ELSE — facts.md, mandates, rules, CLAUDE.md, skills, code — is
  BLOCKED (exit 2). This is what lets continuous learning safely edit its own
  accumulated candidates while never mutating human-authored memory.

Composes with ``path-allowlist.py``: both run on Write|Edit; either may block.
This guard is intentionally simple and robust (a security hook), and it does
NOT parse file contents — the ``created_by`` / ``write_origin`` provenance tags
in ``schemas/pending-review.schema.json`` are RECORDED by the background writer
and READ by the decay/curation layers; this hook enforces the origin×domain
access gate.

Exit codes: 0 allow / 2 block (stderr visible to the agent).
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

# GUARDED import so a broken/absent helper can't crash the module at LOAD
# (exit 1 = fail-OPEN in Claude Code — the BK-0018 vulnerability). The
# block-recorder is best-effort and only runs after a block decision, so a
# no-op fallback never weakens the origin×domain gate.
try:
    from _block_record import _write_block_record
except Exception:  # pragma: no cover - defensive; recorder is best-effort
    def _write_block_record(*_a: object, **_k: object) -> None:
        return None

BACKGROUND_ORIGIN = "background_review"

# Artifacts the autonomous/background loop legitimately AUTHORS (created_by
# agent) and may therefore write under background origin. Repo-relative,
# forward-slash, ``re.match`` (anchored at start). Everything NOT matched here
# is treated as human-/foreground-authored and is off-limits to background.
# crypto-bot layout (2026-09-02 port): the pending-review queue exists BOTH as
# a flat ``_pending_review.jsonl`` (curator's append target, architecture.md
# A.4) and as a ``_pending_review/`` directory holding staged citation files.
# Both are agent-authored; ``facts.md``, ``research_queue.md`` and everything
# under ``T3_procedural/`` are not, and stay off-limits to the background loop.
BACKGROUND_WRITABLE: tuple[str, ...] = (
    r"^\.memory/T2_semantic/_pending_review\.jsonl$",
    r"^\.memory/T2_semantic/_pending_review/.*",
    r"^\.memory/T2_semantic/_fact_health\.jsonl$",
    r"^\.memory/T2_semantic/citations/.*",
    r"^\.memory/T2_semantic/_rejected/.*",
    r"^\.memory/T1_episodic/.*",
    r"^\.memory/_proposals/.*",
    r"^research/discovery/.*",
)


def to_relative(file_path: str, project_dir: str) -> str:
    """Normalize an absolute/relative path to a repo-relative forward-slash path.

    Collapses ``.`` / ``..`` segments so a path-traversal like
    ``.memory/T2_semantic/citations/../facts.md`` cannot masquerade as an
    agent-authored path and slip past the domain allowlist — it normalizes to
    ``.memory/T2_semantic/facts.md`` and is correctly treated as human-authored.
    """
    norm = file_path.replace("\\", "/")
    proj = project_dir.replace("\\", "/").rstrip("/")
    if proj and norm.startswith(proj + "/"):
        norm = norm[len(proj) + 1 :]
    if norm.startswith("./"):
        norm = norm[2:]
    return os.path.normpath(norm).replace("\\", "/")


def is_background_writable(rel: str) -> bool:
    return any(re.match(pat, rel) for pat in BACKGROUND_WRITABLE)


# Path fields across the file-writing tools the `Write|Edit` matcher routes in
# (that regex ALSO matches MultiEdit / NotebookEdit as substrings — so this hook
# MUST handle them, not wave them through as "unknown tool").
_PATH_FIELDS = ("file_path", "path", "notebook_path")


def _extract_target(tool_input: dict) -> str:
    for key in _PATH_FIELDS:
        val = tool_input.get(key)
        if val:
            return str(val)
    return ""


def _block(rel: str, tool_name: str, session_id: str, pattern_id: str, msg: str) -> int:
    print(msg, file=sys.stderr)
    _write_block_record(
        hook_name="provenance-guard",
        tool_name=tool_name or "unknown",
        reason=msg.splitlines()[0],
        blocked_target=rel,
        blocked_pattern_id=pattern_id,
        session_id=session_id,
    )
    return 2


def main() -> int:
    # Origin is read FIRST (from env, no stdin needed). Foreground is trusted,
    # so the guard is a pure no-op there regardless of payload shape. Matching is
    # case-insensitive so a differently-cased token can't silently disable the
    # guard (fail-open to foreground).
    origin = (os.environ.get("CLAUDE_WRITE_ORIGIN") or "foreground").strip().lower()
    if origin != BACKGROUND_ORIGIN:
        return 0

    # From here we are under the UNTRUSTED background_review origin. Posture is
    # FAIL CLOSED: any parse error, unexpected shape, or crash BLOCKS (exit 2) —
    # in Claude Code only exit 2 blocks, so a fail-open (0/1) would let an
    # autonomous write through. Legitimate background writes carry a well-formed
    # payload targeting the agent-authored domain and pass cleanly.
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or str(Path.cwd())
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            return _block(
                "?",
                "unknown",
                "unknown",
                "PROVENANCE_FAILCLOSED_NONDICT_PAYLOAD",
                "[provenance-guard] BLOCKED (fail-closed): non-dict payload "
                "under background_review origin",
            )
        session_id = payload.get("session_id") or "unknown"
        tool_name = payload.get("tool_name") or ""
        tool_input = payload.get("tool_input")
        if not isinstance(tool_input, dict):
            return _block(
                "?",
                tool_name,
                session_id,
                "PROVENANCE_FAILCLOSED_NONDICT_INPUT",
                "[provenance-guard] BLOCKED (fail-closed): non-dict tool_input "
                "under background_review origin",
            )
        if tool_name == "Bash":
            # Background Bash write-paths are gated by the same origin×domain
            # rule, using the SHARED write-target extraction (bash_targets.py —
            # one implementation with path-allowlist / policy-engine). Import
            # lazily so a missing/broken helper module lands in the fail-closed
            # except (exit 2), never a fail-open import crash (exit 1 does not
            # block).
            from bash_targets import candidate_paths_from_command

            cmd = tool_input.get("command", "")
            if not isinstance(cmd, str) or not cmd.strip():
                return 0  # nothing to evaluate
            for candidate, kind in candidate_paths_from_command(cmd):
                if candidate.startswith("$"):
                    # Unresolvable at hook time. Foreground path-allowlist skips
                    # these (trusted origin); under the UNTRUSTED background
                    # origin we fail CLOSED — a variable could hide facts.md.
                    return _block(
                        candidate,
                        tool_name,
                        session_id,
                        f"PROVENANCE_BACKGROUND_BASH_VAR:{kind}",
                        "[provenance-guard] BLOCKED (fail-closed): background_review "
                        f"Bash write target {candidate!r} is variable-expanded and "
                        "cannot be verified at hook time.",
                    )
                rel = to_relative(candidate, project_dir)
                if not is_background_writable(rel):
                    return _block(
                        rel,
                        tool_name,
                        session_id,
                        f"PROVENANCE_BACKGROUND_BASH:{kind}",
                        f"[provenance-guard] BLOCKED: background_review origin may not "
                        f"write {rel} via Bash (human-/foreground-authored). Background "
                        "curation edits only what it authors; to edit this file, do it "
                        "in a foreground session.",
                    )
            return 0  # read-shaped command, or all targets agent-authored

        target = _extract_target(tool_input).strip()
        if not target:
            return 0  # no write target to protect

        rel = to_relative(target, project_dir)
        if is_background_writable(rel):
            print(
                f"[provenance-guard] OK: background write to agent-authored {rel}",
                file=sys.stderr,
            )
            return 0
        return _block(
            rel,
            tool_name,
            session_id,
            "PROVENANCE_BACKGROUND_HUMAN_AUTHORED",
            f"[provenance-guard] BLOCKED: background_review origin may not write {rel} "
            "(human-/foreground-authored). Background curation edits only what it "
            "authors (pending-review queue, fact-health, citations, rejected, "
            "episodic logs, proposals). To edit this file, do it in a foreground session.",
        )
    except Exception as e:  # noqa: BLE001 — FAIL CLOSED under background origin
        print(f"[provenance-guard] BLOCKED (fail-closed on error): {e!r}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    # FAIL CLOSED under the untrusted background origin: any unhandled crash
    # exits 2 (block), never exit 1 (which Claude Code treats as non-blocking).
    # main() already returns 0 immediately for the trusted foreground origin
    # (a crash-proof env read), so this wrapper never blocks a human session.
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as e:  # noqa: BLE001 — deliberate catch-all, fail closed
        print(
            f"[provenance-guard] FAIL-CLOSED: guard crashed ({type(e).__name__}: {e}); "
            "blocking (exit 2).",
            file=sys.stderr,
        )
        sys.exit(2)
