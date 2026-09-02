# SPDX-License-Identifier: LicenseRef-Proprietary
# Copyright (c) 2026 Kanin Srijundorn. All rights reserved.
"""Lint every .claude/agents/*.md frontmatter against the agent contract.

Ported 2026-09-02 from siamese-reconcile and re-fitted to crypto-bot
(governance port, S1.5).

Two checks, and the SECOND is the one this repo actually needed:

1. **Contract fields.** crypto-bot's real contract is
   ``name / description / model / tools / permissionMode / maxTurns / memory``
   — all 21 agents carry exactly those. siamese additionally requires
   ``agent_id / version / model_rationale``, which no crypto-bot agent has
   ever carried; requiring them here would fail 21 files for a field this
   repo does not use, which is a schema bug, not 21 agent bugs.

2. **Hook-command existence.** Every ``command:`` in an agent's ``hooks``
   block must point at a file that EXISTS. This is the regression the
   2026-09-02 port could actually cause: seven hooks were renamed
   (``sacred-block.sh`` -> ``path-allowlist.py``, ``path-allowlist.sh`` ->
   ``curator-write-allowlist.py``, ``commit-format.sh`` merged into
   ``commit-guard.py``, ...), and a frontmatter still naming a moved hook
   fails SILENTLY at runtime — Claude Code cannot run it, and a non-2 exit is
   treated as non-blocking. That is the same fail-open shape the whole port
   exists to remove, so it gets a gate.

Run under plain ``python`` (the working 3.12 install on this machine;
``python3`` is a broken pyenv-win shim). Needs PyYAML. Uses ``jsonschema``
for the full draft-07 pass only when ``--schema`` is passed AND the library
is importable; the built-in check is the default and is what CI-equivalent
runs rely on.

Usage:  python scripts/validate_agent_frontmatter.py
        python scripts/validate_agent_frontmatter.py .claude/agents/curator.md
        python scripts/validate_agent_frontmatter.py --schema
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML required — run under plain python (3.12)", file=sys.stderr)
    sys.exit(3)

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "schemas" / "agent_frontmatter.schema.json"
AGENTS_DIR = ROOT / ".claude" / "agents"

REQUIRED = ("name", "description", "model", "tools", "permissionMode", "maxTurns", "memory")
# ``inherit`` is a real Claude Code value and crypto-bot uses it deliberately:
# adversarial-reviewer, citation-verifier and deep-researcher run their
# SUBSTANTIVE work on Gemini 2.5 Pro via a direct OpenRouter call, so the
# wrapper's own model is whatever the parent session is. siamese's enum omits
# it because siamese has no cross-model agents.
MODEL_ENUM = {"haiku", "sonnet", "opus", "inherit"}
NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")

# A hook command looks like:
#   "$CLAUDE_PROJECT_DIR/.claude/hooks/x.sh"
#   "python $CLAUDE_PROJECT_DIR/.claude/hooks/x.py --strict"
_HOOK_PATH_RE = re.compile(r"\$CLAUDE_PROJECT_DIR/(\S+)")


def extract_frontmatter(text: str) -> dict | str | None:
    """Return the frontmatter dict, None if absent, or an error string if unparseable."""
    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    try:
        loaded = yaml.safe_load(parts[1])
    except yaml.YAMLError as exc:
        return f"invalid YAML: {exc}".splitlines()[0]
    return loaded if isinstance(loaded, dict) else None


def builtin_check(fm: dict) -> list[str]:
    errs: list[str] = []
    for k in REQUIRED:
        if k not in fm:
            errs.append(f"missing required key: {k}")
    if "model" in fm and fm["model"] not in MODEL_ENUM:
        errs.append(f"model must be one of {sorted(MODEL_ENUM)}, got {fm['model']!r}")
    if "name" in fm and not NAME_RE.match(str(fm["name"])):
        errs.append(f"name must be kebab-case, got {fm['name']!r}")
    if "maxTurns" in fm and not isinstance(fm["maxTurns"], int):
        errs.append(f"maxTurns must be an integer, got {fm['maxTurns']!r}")
    for sect in ("inputs", "outputs"):
        for i, item in enumerate(fm.get(sect, []) or []):
            if not isinstance(item, dict) or "path" not in item or "mode" not in item:
                errs.append(f"{sect}[{i}] needs both 'path' and 'mode'")
    return errs


def _walk_hook_commands(node) -> list[str]:
    """Collect every ``command`` string anywhere under the hooks block."""
    out: list[str] = []
    if isinstance(node, dict):
        cmd = node.get("command")
        if isinstance(cmd, str):
            out.append(cmd)
        for v in node.values():
            out.extend(_walk_hook_commands(v))
    elif isinstance(node, list):
        for v in node:
            out.extend(_walk_hook_commands(v))
    return out


def hook_path_check(fm: dict) -> list[str]:
    """Every hook command must resolve to a file that exists on disk."""
    errs: list[str] = []
    for cmd in _walk_hook_commands(fm.get("hooks")):
        m = _HOOK_PATH_RE.search(cmd)
        if not m:
            errs.append(f"hook command has no $CLAUDE_PROJECT_DIR-relative path: {cmd!r}")
            continue
        rel = m.group(1)
        if not (ROOT / rel).exists():
            errs.append(
                f"hook command points at a MISSING file: {rel} "
                "(renamed or archived? see .claude/hooks/_archive_bash_2026-09/README.md)"
            )
    return errs


def schema_check(fm: dict, schema: dict) -> list[str]:
    try:
        import jsonschema
    except ImportError:
        return builtin_check(fm)
    validator = jsonschema.Draft7Validator(schema)
    return [
        f"{'/'.join(map(str, e.path)) or '<root>'}: {e.message}" for e in validator.iter_errors(fm)
    ]


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if a != "--schema"]
    use_schema = "--schema" in argv[1:]
    schema: dict = {}
    if use_schema and SCHEMA_PATH.exists():
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    files = [Path(a) for a in args] or sorted(AGENTS_DIR.glob("*.md"))
    if not files:
        print("no agent files found (nothing to validate)")
        return 0
    failed = False
    for f in files:
        fm = extract_frontmatter(f.read_text(encoding="utf-8"))
        if fm is None:
            print(f"FAIL {f}: no YAML frontmatter")
            failed = True
            continue
        if isinstance(fm, str):
            print(f"FAIL {f}: {fm}")
            failed = True
            continue
        errs = (schema_check(fm, schema) if schema else builtin_check(fm)) + hook_path_check(fm)
        if errs:
            failed = True
            print(f"FAIL {f}:")
            for e in errs:
                print(f"   - {e}")
        else:
            print(f"OK   {f}  (model={fm.get('model')}, hooks={'yes' if fm.get('hooks') else 'no'})")
    print("\nRESULT:", "FAILURES" if failed else "all agent frontmatter valid")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
