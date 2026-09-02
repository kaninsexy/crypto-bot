#!/usr/bin/env python3
# SPDX-License-Identifier: LicenseRef-Proprietary
# Copyright (c) 2026 Kanin Srijundorn. All rights reserved.
"""SessionStart hook — loads prior context at the start of a new session.

Completes the memory-persistence trio (SessionStart / PreCompact / Stop) for
siamese-reconcile (playbooks port V5, BK-0015). On a fresh session this
prints a compact, bounded context block to stdout, which Claude Code injects
as additional session context. The goal: the agent opens already knowing the
live facts, the open backlog, and the last handoff — without the human
re-pasting them.

What it loads (in priority order, until the char budget is spent):
  1. .memory/T2_semantic/facts.md            (curator-promoted facts)
  2. open/in_progress items in .memory/T2_semantic/backlog.jsonl (Mandate L)
  3. confidence-weighted fact-health recall with anchor-freshness labels
     (.memory/T2_semantic/_fact_health.jsonl + scripts/fact_anchors.py)
  4. the most recent handoff/state snapshot in .memory/T1_episodic/_state/

Budget: SESSION_START_MAX_CHARS env var (default 8000). Set
SESSION_START_CONTEXT=off to disable entirely; SESSION_START_RECALL=off
disables just the fact-health block. The template's structural session
search + frozen-snapshot layers are deliberately NOT ported (deferred per
.memory/_proposals/playbooks_port_evaluation_2026-07-02.md rows 9–10).

Always exits 0. Observational; never blocks a session. Never raises.
Reads only; writes nothing (PII-safe — facts.md is curator-curated, not raw).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _read_text(p: Path, limit: int) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")[:limit]
    except Exception:  # noqa: BLE001 — best-effort
        return ""


def _open_backlog_items(path: Path, limit: int) -> list[str]:
    """Return one-line summaries of open / in_progress backlog items.

    backlog.jsonl is append-only; the latest line for a given id wins. We
    fold to latest-status-per-id, then keep the open ones.
    """
    if not path.exists():
        return []
    latest: dict[str, dict] = {}
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            bid = obj.get("id")
            if bid:
                latest[bid] = obj
    except Exception:  # noqa: BLE001
        return []
    out: list[str] = []
    for bid, obj in latest.items():
        if obj.get("status") in ("open", "in_progress"):
            title = (obj.get("title") or obj.get("summary") or "").strip()
            sev = obj.get("severity") or "?"
            out.append(f"- [{obj.get('status')}/{sev}] {bid}: {title}"[:200])
            if len(out) >= limit:
                break
    return out


def _fact_health_block(project: Path, mem: Path, limit: int = 10) -> str:
    """Render non-archived fact-health records ordered by confidence DESC with
    the score visible ("- (0.87) fact-id") and anchor-freshness labels appended
    (scripts/fact_anchors.py). Records without a confidence field weigh the
    0.5 default. Returns "" when there is nothing to show."""
    health = mem / "T2_semantic" / "_fact_health.jsonl"
    if not health.exists():
        return ""
    # Import fact_anchors from the target project's scripts/ when present,
    # else from this hook's own repo (siamese adaptation: lets the Tier-1
    # fixtures run this hook against a synthetic sandbox project while using
    # the real module — freshness checks take `project` as an argument).
    for scripts_dir in (
        str(project / "scripts"),
        str(Path(__file__).resolve().parents[2] / "scripts"),
    ):
        if Path(scripts_dir).is_dir() and scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
    import fact_anchors  # noqa: PLC0415 — lazy; scripts/ not on default path

    records: list[dict] = []
    for line in health.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("state") != "archived":
            records.append(obj)
    if not records:
        return ""

    def conf(r: dict) -> float:
        try:
            c = float(r.get("confidence", 0.5))
        except (TypeError, ValueError):
            return 0.5
        return c if 0.0 <= c <= 1.0 else 0.5

    records.sort(key=lambda r: (-conf(r), str(r.get("id", ""))))
    lines: list[str] = []
    suspects: list[str] = []
    for r in records[:limit]:
        rid = str(r.get("id", "?"))
        label = fact_anchors.freshness_label(r.get("anchors"), project)
        entry = f"- ({conf(r):.2f}) {rid}"
        if label:
            entry += f" {label}"
            suspects.append(rid)
        if r.get("state") == "stale":
            entry += " (stale)"
        lines.append(entry)
    if suspects:
        lines.append("Suspect (anchor drift — re-verify before relying): " + ", ".join(suspects))
    return "\n".join(lines)


def main() -> int:
    if os.environ.get("SESSION_START_CONTEXT", "").lower() == "off":
        return 0

    budget = int(os.environ.get("SESSION_START_MAX_CHARS", "8000") or "8000")
    project = Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    mem = project / ".memory"

    parts: list[str] = ["=== Session context (auto-loaded by session-start hook) ==="]

    facts = mem / "T2_semantic" / "facts.md"
    if facts.exists():
        body = _read_text(facts, budget // 2)
        if body.strip():
            parts.append("\n## Live facts (T2)\n" + body)

    backlog = _open_backlog_items(mem / "T2_semantic" / "backlog.jsonl", limit=25)
    if backlog:
        parts.append("\n## Open backlog (Mandate L)\n" + "\n".join(backlog))

    # Fact health — confidence-weighted recall with freshness anchors: facts
    # ordered by confidence (score rendered); a fact whose anchored file
    # changed is LABELED [drifted: path] / [orphaned: path] instead of being
    # served silently. Best-effort; never breaks startup.
    if os.environ.get("SESSION_START_RECALL", "").lower() != "off":
        try:
            block = _fact_health_block(project, mem, limit=10)
            if block.strip():
                parts.append("\n## Facts — confidence-weighted recall (T2 health)\n" + block)
        except Exception:  # noqa: BLE001 — additive; startup must not break
            pass

    state_dir = mem / "T1_episodic" / "_state"
    if state_dir.is_dir():
        snaps = sorted(
            (p for p in state_dir.glob("*.md") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if snaps:
            remaining = max(0, budget - sum(len(p) for p in parts))
            body = _read_text(snaps[0], min(2000, remaining))
            if body.strip():
                parts.append(f"\n## Last state snapshot ({snaps[0].name})\n" + body)

    text = "\n".join(parts)
    if len(text) <= len(parts[0]) + 1:
        # Nothing useful loaded (fresh repo). Stay silent.
        return 0
    print(text[:budget])
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001 — observational hook must never break startup
        print(f"[session-start] WARN: {e}", file=sys.stderr)
        sys.exit(0)
