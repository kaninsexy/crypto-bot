# SPDX-License-Identifier: LicenseRef-Proprietary
# Copyright (c) 2026 Kanin Srijundorn. All rights reserved.
"""Validate .memory/T2_semantic/backlog.jsonl against schemas/backlog.schema.json.

Mandate L companion (playbooks port V1). Called by
scripts/pre_commit_backlog_check.sh on every commit, and runnable standalone
as a gate-suite check:

    python scripts/validate_backlog.py                 # validate the file
    git show :.memory/T2_semantic/backlog.jsonl | \
        python scripts/validate_backlog.py -           # validate staged content

Uses ``jsonschema`` when importable (full draft-07 validation); otherwise a
built-in stdlib fallback pins the schema's load-bearing constraints (required
keys, BK-NNNN id pattern, status/severity enums) — git hooks run under the
bare system python3, which has no jsonschema.

Exit 0 = every line valid. Exit 1 = at least one invalid line (each reported
to stderr). Exit 2 = could not run (missing file).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
BACKLOG_PATH = ROOT / ".memory" / "T2_semantic" / "backlog.jsonl"
SCHEMA_PATH = ROOT / "schemas" / "backlog.schema.json"

REQUIRED = ("id", "status", "title", "severity", "owner", "created_at")
ID_RE = re.compile(r"^BK-[0-9]{4}$")
STATUS_ENUM = {"open", "in_progress", "closed", "wontfix", "duplicate"}
SEVERITY_ENUM = {"critical", "high", "medium", "low"}


def builtin_errors(rec: dict[str, Any]) -> list[str]:
    """Stdlib fallback mirroring the schema's required keys, patterns, enums."""
    errs: list[str] = []
    for key in REQUIRED:
        if key not in rec:
            errs.append(f"missing required key: {key}")
    if "id" in rec and not ID_RE.match(str(rec["id"])):
        errs.append(f"id must match BK-NNNN, got {rec.get('id')!r}")
    if "status" in rec and rec["status"] not in STATUS_ENUM:
        errs.append(f"status must be one of {sorted(STATUS_ENUM)}, got {rec.get('status')!r}")
    if "severity" in rec and rec["severity"] not in SEVERITY_ENUM:
        errs.append(f"severity must be one of {sorted(SEVERITY_ENUM)}, got {rec.get('severity')!r}")
    title = rec.get("title")
    if "title" in rec and (not isinstance(title, str) or len(title) < 3):
        errs.append("title must be a string of length >= 3")
    owner = rec.get("owner")
    if "owner" in rec and (not isinstance(owner, str) or len(owner) < 2):
        errs.append("owner must be a string of length >= 2")
    return errs


def make_checker(schema_path: Path) -> Any:
    """Return a callable(record) -> list[str] using jsonschema when available."""
    schema: dict[str, Any] | None = None
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except OSError:
        schema = None  # schema file absent: builtin checks still apply
    if schema is not None:
        try:
            import jsonschema

            validator = jsonschema.Draft7Validator(schema)

            def schema_errors(rec: dict[str, Any]) -> list[str]:
                return [
                    f"{'/'.join(map(str, err.path)) or '<root>'}: {err.message}"
                    for err in validator.iter_errors(rec)
                ]

            return schema_errors
        except ImportError:
            pass
    return builtin_errors


def validate_lines(lines: list[str], checker: Any) -> int:
    bad = 0
    for i, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as exc:
            print(f"line {i}: not valid JSON: {exc}", file=sys.stderr)
            bad += 1
            continue
        if not isinstance(rec, dict):
            print(f"line {i}: not a JSON object", file=sys.stderr)
            bad += 1
            continue
        for err in checker(rec):
            print(f"line {i} ({rec.get('id', '?')}): {err}", file=sys.stderr)
            bad += 1
    return bad


def main(argv: list[str]) -> int:
    src = argv[1] if len(argv) > 1 else str(BACKLOG_PATH)
    if src == "-":
        lines = sys.stdin.read().splitlines()
        label = "<stdin>"
    else:
        path = Path(src)
        if not path.exists():
            print(f"[validate-backlog] no such file: {path}", file=sys.stderr)
            return 2
        lines = path.read_text(encoding="utf-8").splitlines()
        label = str(path)
    bad = validate_lines(lines, make_checker(SCHEMA_PATH))
    n_records = sum(1 for ln in lines if ln.strip())
    if bad:
        print(f"[validate-backlog] {label}: {bad} problem(s) across {n_records} record(s)")
        return 1
    print(f"[validate-backlog] {label}: {n_records} record(s), all valid")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
