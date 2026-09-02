#!/usr/bin/env python3
"""Atomic PreToolUse / PostToolUse observation hook.

Writes one small JSON record per tool call into:

    .memory/T1_episodic/observations/<YYYY-MM-DD>/<HHMMSS_microseconds>_<event>_<tool>.json

These records are harvested by the curator agent to detect cross-session
patterns at finer granularity than the session-end aggregate can express.

Design context:
- `.memory/T2_semantic/design_curator_vs_instincts.md` §"Atomic
  observation hook"
- `phase0_5_curator_atomic_observation_now` decisions_log entry
- `phase0_5_atomic_observation_hook` follow-up

PII boundary (strict):
- For **Bash**, we record `first_token`, command length, and a
  `chained` flag. We **never** record the full command string —
  commands may contain secrets or file paths under sacred trees.
- For **Write / Edit / Read / NotebookEdit / NotebookRead**, we record
  the relative path and its tier classification. We **never** record
  file contents or diffs.
- For **any tool we don't have a specific summary for**, we record the
  tool name and event only — defensive default.
- For **PostToolUse**, the `tool_response` summary captures
  `stdout_bytes`, `stderr_bytes`, `exit_code` (counts only, never raw
  text); plus an `error` boolean if the payload signals failure.

Failure mode:
- Never raises (best-effort writes wrapped in a broad except).
- Always exits 0. This is observational; it does not gate tool calls.
- On disk full / permission errors, logs `[observe] WARN: ...` to
  stderr and returns.

Test isolation:
- `OBSERVATION_DIR` env var, when set, overrides the default output
  directory (`<project>/.memory/T1_episodic/observations/<date>/`).
  Tests set this to a tmpdir to keep the real `.memory/` tree clean.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Tier patterns + classify live in file_tiers.py — the single definition
# site since 2026-07-02 (playbooks port V5). When run as a hook the script's
# directory is sys.path[0]; when loaded via importlib (test_observe_hook.py)
# it is not, so pin it explicitly before the same-directory import.
_HOOKS_DIR = str(Path(__file__).resolve().parent)
if _HOOKS_DIR not in sys.path:
    sys.path.insert(0, _HOOKS_DIR)

from file_tiers import classify as _tier_classify  # noqa: E402

FILE_INPUT_TOOLS: frozenset[str] = frozenset(
    {"Write", "Edit", "Read", "NotebookEdit", "NotebookRead", "MultiEdit"}
)

# Tokens that turn a single bash invocation into a multi-step chain.
# Used for an aggregate flag only — we never echo the matching substring.
_CHAIN_RE = re.compile(r"&&|\|\||;|\||>|<|\$\(|`")


def _classify_path(rel: str) -> str:
    return _tier_classify(rel)


def _normalize_path(raw_path: str, project: Path) -> str:
    """Return ``raw_path`` made repo-relative when possible."""
    p = raw_path.replace("\\", "/").strip()
    proj = str(project).rstrip("/") + "/"
    if p.startswith(proj):
        p = p[len(proj) :]
    return p


def _bash_summary(command: str) -> dict[str, Any]:
    """Summarize a Bash invocation without echoing its body.

    The command string itself is NEVER persisted — only the first token
    (typically the program name), the byte length, and a flag for
    whether it contains shell-chain metacharacters.
    """
    stripped = command.strip()
    first = stripped.split(maxsplit=1)[0] if stripped else ""
    return {
        "kind": "command",
        "first_token": first,
        "argv_len": len(stripped),
        "chained": bool(_CHAIN_RE.search(stripped)),
    }


def _file_summary(tool_input: dict[str, Any], project: Path) -> dict[str, Any]:
    """Summarize a file-input tool (Write/Edit/Read/Notebook*).

    Returns kind="file" plus path and tier; never the file contents.
    """
    raw_path = (
        tool_input.get("file_path")
        or tool_input.get("path")
        or tool_input.get("notebook_path")
        or ""
    )
    if not isinstance(raw_path, str) or not raw_path:
        return {"kind": "file", "path": "", "tier": "tier3_autonomous"}
    rel = _normalize_path(raw_path, project)
    return {"kind": "file", "path": rel, "tier": _classify_path(rel)}


def _input_summary(tool_name: str, tool_input: Any, project: Path) -> dict[str, Any]:
    """PII-safe summary of a tool's input dict."""
    if not isinstance(tool_input, dict):
        return {"kind": "other"}
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        if not isinstance(cmd, str):
            cmd = ""
        return _bash_summary(cmd)
    if tool_name in FILE_INPUT_TOOLS:
        return _file_summary(tool_input, project)
    return {"kind": "other"}


def _response_summary(tool_response: Any) -> dict[str, Any]:
    """PII-safe summary of a PostToolUse tool_response payload.

    Captures byte counts and exit codes only — never raw stdout / stderr
    / file contents.
    """
    if not isinstance(tool_response, dict):
        return {"present": False}

    def _byte_len(value: Any) -> int | None:
        if value is None:
            return None
        if isinstance(value, (bytes, bytearray)):
            return len(value)
        if isinstance(value, str):
            return len(value.encode("utf-8", errors="replace"))
        return None

    exit_code = tool_response.get("exit_code")
    if exit_code is None:
        exit_code = tool_response.get("returncode")
    return {
        "present": True,
        "error": bool(tool_response.get("is_error") or tool_response.get("error")),
        "stdout_bytes": _byte_len(tool_response.get("stdout")),
        "stderr_bytes": _byte_len(tool_response.get("stderr")),
        "exit_code": exit_code,
    }


def _observation_dir(project_dir: Path) -> Path:
    override = os.environ.get("OBSERVATION_DIR")
    if override:
        return Path(override)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return project_dir / ".memory" / "T1_episodic" / "observations" / today


def _detect_event(payload: dict[str, Any]) -> str:
    """Infer pre/post from the payload shape.

    Claude Code passes a `hook_event_name` field on modern versions; we
    fall back to presence-of-tool_response on older ones.
    """
    name = payload.get("hook_event_name") or ""
    if isinstance(name, str):
        if name == "PreToolUse":
            return "pre_tool_use"
        if name == "PostToolUse":
            return "post_tool_use"
    return "post_tool_use" if "tool_response" in payload else "pre_tool_use"


def _write_observation(payload: dict[str, Any]) -> None:
    """Write a single observation record. Best-effort, never raises."""
    try:
        now = datetime.now(timezone.utc)
        ts_iso = now.isoformat().replace("+00:00", "Z")
        ts_part = now.strftime("%H%M%S_%f")

        project_dir = Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
        out_dir = _observation_dir(project_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        event = _detect_event(payload)
        tool_name = payload.get("tool_name") or "unknown"
        if not isinstance(tool_name, str):
            tool_name = "unknown"

        record: dict[str, Any] = {
            "ts": ts_iso,
            "event": event,
            "session_id": payload.get("session_id") or "unknown",
            "tool_name": tool_name,
            "input_summary": _input_summary(tool_name, payload.get("tool_input"), project_dir),
        }
        if event == "post_tool_use":
            record["tool_response"] = _response_summary(payload.get("tool_response"))

        # Sanitize tool name for filename (safe across filesystems).
        safe_tool = re.sub(r"[^A-Za-z0-9_-]", "_", tool_name) or "unknown"
        filename = f"{ts_part}_{event}_{safe_tool}.json"
        (out_dir / filename).write_text(json.dumps(record))
    except Exception as exc:  # noqa: BLE001 — explicitly best-effort
        print(f"[observe] WARN: {exc}", file=sys.stderr)


def main() -> int:
    try:
        raw = sys.stdin.read()
    except OSError:
        return 0
    if not raw.strip():
        return 0
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return 0
    if not isinstance(payload, dict):
        return 0
    _write_observation(payload)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 — observational hook
        print(f"[observe] WARN: {exc}", file=sys.stderr)
        sys.exit(0)
