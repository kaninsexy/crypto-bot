#!/usr/bin/env python3
"""
Stop hook — writes a T1 episodic log when a Claude Code session ends.

This is the learning loop's input. The curator agent (Phase 0.5 stub,
real Phase 1) reads these episodes, extracts cross-session patterns,
and promotes them to facts.md.

What gets logged:
- Date and run-end timestamp (UTC).
- Session id, agent type (sub-agent or main), if available from payload.
- A compact summary of: tools used, files touched (counts + tiers), and
  whether any path-allowlist or no-secrets blocks fired.
- A free-text section the agent body wrote in its turn (the agent can
  put a one-line "what I learned" in stdout that this hook scrapes).

What does NOT get logged:
- Raw extracted document values. PII boundary holds.
- Full file diffs. Just file paths and tier classification.

Exit code: 0 always. This is observational; it never blocks.

Placement: .claude/settings.json wires this to the Stop hook event.

Reference: anthropic-skills:hooks docs, bria-ops T1 episodic pattern.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Tier patterns + classify live in file_tiers.py — the single definition
# site since 2026-07-02 (playbooks port V5). Same-directory import: the
# script's directory is sys.path[0] when run as a hook.
from file_tiers import classify


def main() -> int:
    raw = sys.stdin.read()
    if not raw.strip():
        return 0
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return 0

    project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or str(Path.cwd())
    project = Path(project_dir)
    today = datetime.now(timezone.utc).date().isoformat()
    ts = datetime.now(timezone.utc).strftime("%H%M%S")

    episodes_dir = project / ".memory" / "T1_episodic" / "episodes" / today
    episodes_dir.mkdir(parents=True, exist_ok=True)

    agent = payload.get("agent_type") or payload.get("agent_id") or "main"
    session_id = payload.get("session_id") or "unknown"

    # Aggregate: files touched (if Claude Code provides them in the stop
    # payload — protocol may evolve, so we read defensively).
    tool_uses = payload.get("tool_uses") or []
    file_touches: dict[str, int] = {}
    blocks_fired: list[str] = []
    for use in tool_uses:
        if not isinstance(use, dict):
            continue
        tool = use.get("tool") or use.get("name") or ""
        if tool in ("Write", "Edit") and isinstance(use.get("input"), dict):
            fp = use["input"].get("file_path") or use["input"].get("path") or ""
            if fp:
                rel = fp.replace("\\", "/")
                if rel.startswith(str(project) + "/"):
                    rel = rel[len(str(project)) + 1 :]
                tier = classify(rel)
                file_touches[tier] = file_touches.get(tier, 0) + 1
        if use.get("blocked"):
            blocks_fired.append(use.get("block_reason", "unknown"))

    episode = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "agent": agent,
        "session_id": session_id,
        "file_touches": file_touches,
        "blocks_fired": blocks_fired,
        "tool_uses_count": len(tool_uses),
    }

    out_path = episodes_dir / f"session_end_{agent}_{ts}.json"
    out_path.write_text(json.dumps(episode, indent=2) + "\n")

    # Also append a one-line summary to a per-day index so the curator can
    # find sessions quickly without scanning every file.
    index_path = episodes_dir / "_index.jsonl"
    with index_path.open("a") as f:
        f.write(json.dumps({"agent": agent, "session": session_id, "file": out_path.name}) + "\n")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        # Observational hook must never break the session.
        print(f"[session-end] WARN: {e}", file=sys.stderr)
        sys.exit(0)
