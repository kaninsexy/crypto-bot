# SPDX-License-Identifier: LicenseRef-Proprietary
# Copyright (c) 2026 Kanin Srijundorn. All rights reserved.
"""PreToolUse + PostToolUse hook on Bash|Write|Edit — the policy DSL gate.

Adapter between Claude Code tool calls and ``scripts/policy_engine.py``
(playbooks port V6 (BK-0016)). Two phases, matching the engine's contract:

- **PreToolUse** — translate the tool call into ToolEvents, EVALUATE the armed
  policies against the current session state, and gate: findings are routed
  through the finding-policy map (``scripts/enforcement_policy.py``), and the
  hook exits 2 (block, with the corrective feedback frame) ONLY if the map
  classifies a surviving finding as blocking. No state is updated at Pre.
- **PostToolUse** — UPDATE the session state (gate epochs, invalidation epochs,
  σ labels) from the tool call that actually EXECUTED. A blocked call never
  reaches Post, so a denied action can never latch a gate. ``exits N`` needs the
  real exit code: the live Bash payload carries NO exit/error key at all
  (captured empirically 2026-07-02, BK-0020 — shape is {stdout, stderr,
  interrupted, isImage, noOutputExpected}), and PostToolUse fires ONLY on
  success ("fires after a tool call succeeds"; a failing command produces no
  Post event, verified live). Post therefore infers: explicit
  ``exit_code``/``returncode`` if present; else the ``is_error``/``error``
  boolean; else the live shape's arrival-means-success (0 unless
  ``interrupted``); else None — unknown, satisfying no ``exits N`` gate.
  Documented in ``.claude/rules/enforcement.md`` (deviation from the
  PreToolUse-only spec sketch — an exits-gated policy is unenforceable at
  Pre).

Failure posture (decided by the map, not hardcoded): a ``.policy`` compile error
DISARMS the engine and surfaces ``POLICY_ENGINE_DISARMED`` loudly on every call
(never silent); an adapter crash surfaces ``POLICY_ENGINE_PRODUCER_ERROR``.
Both default to ADVISORY in the map — a policy typo must not wedge every tool
call — but a repo may escalate either code to ``blocking`` in
``enforcement_policy.json`` for a fail-closed posture. ``path-allowlist.py``
remains unconditional beneath this gate regardless of policy state.

Env knobs (all optional; fixture/harness isolation):
  POLICY_ENGINE_DISABLED=1     -> no-op (off switch)
  POLICY_ENGINE_POLICY_INLINE  -> policy text itself (overrides the dir)
  POLICY_ENGINE_POLICY_DIR     -> dir of *.policy (default .claude/rules/policies)
  POLICY_ENGINE_STATE_DIR      -> per-session state dir (default
                                  .memory/T1_episodic/_state/policy_engine — gitignored)
  POLICY_ENGINE_MAP_PATH       -> enforcement-policy JSON (default the shipped map)

Exit codes: 0 allow (advisories on stderr) / 2 block (feedback frame on stderr).
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent
REPO_ROOT = HOOKS_DIR.parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
for _p in (str(HOOKS_DIR), str(SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import enforcement_policy as ep  # noqa: E402
import policy_engine as pe  # noqa: E402
from bash_targets import (  # noqa: E402
    candidate_paths_from_command,
    to_relative,
    tokenize_segments,
)

_WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
_PATH_FIELDS = ("file_path", "path", "notebook_path")


def _load_merged_policy() -> pe.Policy:
    """Compile every policy source. Fail-closed ACROSS the set: one broken file
    disarms the whole engine (a partially-armed gate is a silently weaker gate).
    No policy files at all -> armed with zero rules (silent no-op: a template
    without policies must not warn on every tool call)."""
    inline = os.environ.get("POLICY_ENGINE_POLICY_INLINE")
    if inline is not None:
        return pe.compile_policy(inline)
    pol_dir = Path(
        os.environ.get("POLICY_ENGINE_POLICY_DIR") or REPO_ROOT / ".claude" / "rules" / "policies"
    )
    merged = pe.Policy()
    if not pol_dir.is_dir():
        return merged
    for f in sorted(pol_dir.glob("*.policy")):
        try:
            text = f.read_text(encoding="utf-8")
        except OSError as e:
            merged.errors.append(f"{f.name}: unreadable ({e})")
            continue
        p = pe.compile_policy(text)
        for err in p.errors:
            merged.errors.append(f"{f.name}: {err}")
        for name, src in p.sources.items():
            if name in merged.sources:
                merged.errors.append(f"{f.name}: duplicate source {name!r} across files")
            else:
                merged.sources[name] = src
        for r in p.rules:
            if any(x.name == r.name for x in merged.rules):
                merged.errors.append(f"{f.name}: duplicate rule {r.name!r} across files")
            else:
                merged.rules.append(r)
    return merged


def _state_path(session_id: str) -> Path:
    state_dir = Path(
        os.environ.get("POLICY_ENGINE_STATE_DIR")
        or REPO_ROOT / ".memory" / "T1_episodic" / "_state" / "policy_engine"
    )
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", session_id or "unknown") or "unknown"
    return state_dir / f"{safe}.json"


# The live Claude Code PostToolUse Bash tool_response (captured empirically
# 2026-07-02, BK-0020): {stdout, stderr, interrupted, isImage, noOutputExpected}
# — NO exit/error key at all. PostToolUse fires ONLY on success (docs:
# "PostToolUse fires after a tool call succeeds"; verified live: exit-1 and
# exit-7 commands produce a PreToolUse event and no Post event). The event's
# ARRIVAL is therefore the exit-0 signal, unless the run was interrupted.
_LIVE_BASH_RESPONSE_KEYS = frozenset({"stdout", "stderr", "interrupted"})


def _infer_exit_code(tool_response) -> int | None:
    """Infer the executed command's exit code from a PostToolUse payload.

    Precedence: an explicit numeric ``exit_code``/``returncode`` wins; then an
    ``is_error``/``error`` boolean (synthetic sequences and forward compat);
    then the live shape above (arrival-means-success, gated on
    ``interrupted`` being false). Anything else is UNKNOWN -> ``None``, which
    satisfies no ``exits N`` constraint in the engine — strictly more
    conservative than any concrete code (a guessed 0 would silently latch an
    ``exits 0`` gate; a guessed 1 would falsely satisfy an ``exits 1`` rule).
    Never silently 0 on an unrecognized shape (BK-0020).
    """
    if not isinstance(tool_response, dict):
        return None
    for key in ("exit_code", "returncode"):
        v = tool_response.get(key)
        if isinstance(v, int) and not isinstance(v, bool):
            return v
    if "is_error" in tool_response or "error" in tool_response:
        return 1 if (tool_response.get("is_error") or tool_response.get("error")) else 0
    if tool_response.keys() >= _LIVE_BASH_RESPONSE_KEYS:
        return None if tool_response.get("interrupted") else 0
    return None


def _build_events(
    tool_name: str, tool_input: dict, project_dir: str, exit_code: int | None
) -> list[pe.ToolEvent]:
    """Translate one tool call into policy events.

    Write/Edit/... -> one write event (repo-relative, traversal-collapsed).
    Bash -> one exec event per chain segment (interpreter-aware target, so
    `uv run python scripts/recon_e2e_harness.py` is the script, not `uv`), plus
    one write event carrying the shared write-target extraction's candidates."""
    if tool_name in _WRITE_TOOLS:
        raw = next((tool_input.get(k) for k in _PATH_FIELDS if tool_input.get(k)), "")
        if not raw:
            return []
        rel = to_relative(str(raw), project_dir, collapse=True)
        return [pe.ToolEvent(op="write", target=rel, exit_code=exit_code)]
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        if not isinstance(cmd, str) or not cmd.strip():
            return []
        events: list[pe.ToolEvent] = []
        for seg in tokenize_segments(cmd):
            if not seg:
                continue
            events.append(
                pe.ToolEvent(
                    op="exec",
                    target=pe.effective_exec_target(seg),
                    argv=seg,
                    exit_code=exit_code,
                )
            )
        writes = []
        for candidate, _kind in candidate_paths_from_command(cmd):
            if candidate.startswith("$"):
                continue  # unresolvable at hook time; the hard guards handle it
            writes.append(to_relative(candidate, project_dir, collapse=True))
        if writes:
            events.append(pe.ToolEvent(op="write", target="", writes=writes, exit_code=exit_code))
        return events
    return []


def _advisory_line(f: dict) -> str:
    rule = f.get("rule")
    who = f" (rule `{rule}`)" if rule else ""
    detail = f.get("reason") or f.get("message") or ""
    return f"[policy] ADVISORY {f.get('code')}{who}: {detail}"


def main() -> int:
    if os.environ.get("POLICY_ENGINE_DISABLED"):
        return 0
    raw = sys.stdin.read()
    if not raw.strip():
        return 0
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return 0
    if not isinstance(payload, dict):
        return 0

    tool_name = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    session_id = payload.get("session_id") or "unknown"
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or str(Path.cwd())
    event_name = payload.get("hook_event_name") or ""
    is_post = event_name == "PostToolUse" or (
        event_name != "PreToolUse" and "tool_response" in payload
    )

    policy = _load_merged_policy()
    findings: list[dict] = []
    if not policy.armed:
        findings.append(
            {
                "code": pe.CODE_DISARMED,
                "source": "policy-engine",
                "message": "policy engine DISARMED (fail-closed compile): "
                + "; ".join(policy.errors[:5]),
            }
        )

    map_policy = ep.load_policy(os.environ.get("POLICY_ENGINE_MAP_PATH"))

    if is_post:
        # Update phase: state reflects only tool calls that actually executed.
        if policy.armed and (policy.rules or policy.sources):
            exit_code = _infer_exit_code(payload.get("tool_response"))
            events = _build_events(tool_name, tool_input, project_dir, exit_code)
            if events:
                sp = _state_path(session_id)
                state = pe.load_state(sp)
                for ev in events:
                    pe.update_gates(policy, state, ev)
                    pe.update_labels_and_inval(policy, state, ev)
                pe.save_state(sp, state)
        # A disarmed engine still surfaces its advisory at Post (never silent),
        # via the classification below.
    else:
        # Evaluate phase: no state update (a blocked call must not stamp).
        if policy.armed:
            state = pe.load_state(_state_path(session_id))
            for ev in _build_events(tool_name, tool_input, project_dir, None):
                findings.extend(pe.evaluate(policy, state, ev))

    if not findings:
        return 0
    result = ep.classify_findings(findings, map_policy)
    for f in result["advisory"]:
        print(_advisory_line(f), file=sys.stderr)
    if result["gated"]:
        for f in result["blocking"]:
            print(pe.format_feedback(f), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001 — producer error degrades to advisory
        # The gate never crashes and never hard-blocks on its own bug (advisory
        # posture). The finding code is printed so the map's class is auditable.
        print(
            f"[policy] ADVISORY POLICY_ENGINE_PRODUCER_ERROR: policy engine "
            f"crashed evaluating this call ({e!r}); degraded to advisory.",
            file=sys.stderr,
        )
        sys.exit(0)
