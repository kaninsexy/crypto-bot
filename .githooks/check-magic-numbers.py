#!/usr/bin/env python3
"""Magic-number gate for the Layer-4 pre-commit hook.

For a single staged Python file, find NUMBER tokens on newly-added
lines and require a `# CITATION: <key>` comment within +/- 3 lines.

Whitelist common values that aren't parameters: 0, 1, -1, 2, 0.0, 1.0.

Tokenize-based to avoid the regex false-positives the spec calls out
(string literals, indices, version strings).
"""
from __future__ import annotations

import io
import re
import subprocess
import sys
import tokenize

WHITELIST = {0, 1, -1, 2, 0.0, 1.0, -1.0, 2.0}
CITATION_RE = re.compile(r"#\s*CITATION:\s*\S+")


def staged_added_lines(path: str) -> set[int]:
    """Line numbers (in the staged blob's numbering) added by this commit."""
    out = subprocess.run(
        ["git", "diff", "--cached", "-U0", "--", path],
        capture_output=True, text=True, check=True,
    ).stdout
    added: set[int] = set()
    cur_new = 0
    for line in out.splitlines():
        m = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
        if m:
            cur_new = int(m.group(1))
            continue
        if line.startswith("+++"):
            continue
        if line.startswith("+"):
            added.add(cur_new)
            cur_new += 1
        elif line.startswith("-") or line.startswith("---"):
            pass
        else:
            cur_new += 1
    return added


def staged_blob(path: str) -> str:
    return subprocess.run(
        ["git", "show", f":{path}"],
        capture_output=True, text=True, check=True,
    ).stdout


def parse_value(literal: str):
    try:
        if any(c in literal.lower() for c in ".ej"):
            return float(literal.replace("j", ""))
        return int(literal, 0)
    except (ValueError, TypeError):
        return None


def main(path: str) -> int:
    added = staged_added_lines(path)
    if not added:
        return 0
    blob = staged_blob(path)
    lines = blob.splitlines()
    flagged: list[tuple[int, str]] = []
    try:
        toks = list(tokenize.tokenize(io.BytesIO(blob.encode()).readline))
    except (tokenize.TokenizeError, SyntaxError, IndentationError):
        return 0  # malformed; let other tools surface it
    for tok in toks:
        if tok.type != tokenize.NUMBER:
            continue
        lineno = tok.start[0]
        if lineno not in added:
            continue
        val = parse_value(tok.string)
        if val in WHITELIST:
            continue
        # Window: lines [lineno-3, lineno+3] inclusive (1-indexed in file)
        lo = max(1, lineno - 3)
        hi = min(len(lines), lineno + 3)
        window = lines[lo - 1: hi]
        if any(CITATION_RE.search(ln) for ln in window):
            continue
        flagged.append((lineno, tok.string))

    if flagged:
        print(
            f"BLOCKED: magic numbers in {path} without # CITATION: within 3 lines:",
            file=sys.stderr,
        )
        for lineno, val in flagged:
            print(f"  line {lineno}: {val}", file=sys.stderr)
        print("", file=sys.stderr)
        print(
            "Per architecture.md E.4 + .memory/T3_procedural/no_p_hacking.md mandate P:",
            file=sys.stderr,
        )
        print(
            "  Add `# CITATION: <key>` within 3 lines of each magic number.",
            file=sys.stderr,
        )
        print(
            "  Citations live in .memory/T2_semantic/citations/<key>.md.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
