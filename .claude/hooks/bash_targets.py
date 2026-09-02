# SPDX-License-Identifier: LicenseRef-Proprietary
# Copyright (c) 2026 Kanin Srijundorn. All rights reserved.
"""Shared Bash command-parsing helpers for the guard hooks (single source).

Factored out of ``path-allowlist.py`` (playbooks port V6 (BK-0016)) so the three
consumers — ``path-allowlist.py`` (tier gate), ``provenance-guard.py``
(origin×domain gate, Bash coverage), and ``policy-engine.py`` (policy DSL gate) —
parse Bash commands ONE way. Change a write pattern here and every gate stays in
agreement (same rationale as ``file_tiers.py``).

Contents:

- ``BASH_WRITE_PATTERNS`` + ``candidate_paths_from_command()`` — extract
  candidate WRITE-target paths from a command string (tee, redirects, sed -i,
  cp, mv, python open(w/a), ln, rm). Intentionally permissive: a false-block
  costs an agent message; a false-allow costs a corrupted file.
- ``strip_heredoc_bodies()`` — heredoc bodies are data, not commands; a commit
  message that QUOTES ``rm <path>`` must not register as a write.
- ``tokenize_segments()`` — quote-aware tokenization split into pipeline
  segments (``&&``, ``||``, ``;``, ``|``) for per-command policy evaluation.
- ``to_relative()`` — repo-relative normalization, optionally collapsing
  ``.``/``..`` segments (the traversal-hardened variant).

Behavior of the extraction is pinned by ``eval/fixtures/hooks/*.jsonl`` — run
``python eval/run_tier1.py`` after any edit here.
"""

from __future__ import annotations

import os
import re
import shlex

# A shell path token: double-quoted, single-quoted, or unquoted run of
# non-shell-special chars. The unquoted variant allows `$` so variable-
# expanded paths get captured here and handled by the caller's
# `startswith("$")` policy rather than slipping past the regex entirely.
_PATH = r"""(?:"[^"\n]+"|'[^'\n]+'|[^\s'"<>|;&\n]+)"""

# Each regex captures the candidate write-target path in the named group
# `path`. The label is the pattern-kind id surfaced into block records.
BASH_WRITE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("TEE", rf"\btee\b(?:\s+-+[a-zA-Z]+)*\s+(?P<path>{_PATH})"),
    ("REDIRECT", rf"(?<!<)>>?\s*(?P<path>{_PATH})"),
    ("SED", rf"\bsed\b\s+(?:-i\b|--in-place\b)[^|;&\n]*?\s+(?P<path>{_PATH})\s*(?:$|[|;&\n])"),
    ("CP", rf"\bcp\b(?:\s+-+[a-zA-Z]+)*\s+{_PATH}[^|;&\n]*?\s+(?P<path>{_PATH})\s*(?:$|[|;&\n])"),
    ("MV", rf"\bmv\b(?:\s+-+[a-zA-Z]+)*\s+{_PATH}[^|;&\n]*?\s+(?P<path>{_PATH})\s*(?:$|[|;&\n])"),
    ("PYTHON_OPEN", r"""open\(\s*['"](?P<path>[^'"\n]+)['"]\s*,\s*['"][wa]"""),
    ("LN", rf"\bln\b\s+-+[a-zA-Z]+\s+{_PATH}\s+(?P<path>{_PATH})\s*(?:$|[|;&\n])"),
    ("RM", rf"\brm\b(?:\s+-+[a-zA-Z]+)*\s+(?P<path>{_PATH})"),
)

_HEREDOC_OPEN_RE = re.compile(
    r"""<<-?\s*(?P<quote>['"]?)(?P<tag>[A-Za-z_][A-Za-z0-9_]*)(?P=quote)"""
)


def strip_quotes(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    return s


def strip_heredoc_bodies(cmd: str) -> str:
    """Blank out heredoc bodies (data, not commands) before scanning.

    A ``git commit -F - <<'EOF'`` body that mentions ``rm /some/path`` must
    not be interpreted as a write target. Walk line by line; once a heredoc
    opener is seen, blank subsequent lines until the closing tag appears
    alone on its own line. Line breaks are preserved.
    """
    out_lines: list[str] = []
    pending_tag: str | None = None
    for line in cmd.split("\n"):
        if pending_tag is not None:
            if line.strip() == pending_tag:
                pending_tag = None
            out_lines.append("")
            continue
        m = _HEREDOC_OPEN_RE.search(line)
        if m is not None:
            pending_tag = m.group("tag")
        out_lines.append(line)
    return "\n".join(out_lines)


def candidate_paths_from_command(cmd: str) -> list[tuple[str, str]]:
    """Extract (path, pattern_kind) tuples from a Bash command string."""
    stripped = cmd.lstrip()
    if not stripped or stripped.startswith("#"):
        return []
    scan_target = strip_heredoc_bodies(cmd)
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for kind, pat in BASH_WRITE_PATTERNS:
        for m in re.finditer(pat, scan_target):
            cleaned = strip_quotes(m.group("path"))
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                out.append((cleaned, kind))
    return out


_SEPARATORS = frozenset({"&&", "||", ";", "|", "&"})


def tokenize_segments(cmd: str) -> list[list[str]]:
    """Quote-aware token lists, one per pipeline/chain segment.

    Heredoc bodies are stripped first (data, not commands). ``shlex`` does the
    tokenizing; on a shlex failure (unbalanced quote, stray control char) we
    fall back to whitespace splitting so the caller always gets SOMETHING to
    evaluate — never an unexamined command. Segments split on ``&&``, ``||``,
    ``;``, ``|``, ``&`` so a ``git commit`` buried in a chain is still seen as
    its own command.
    """
    text = strip_heredoc_bodies(cmd)
    try:
        tokens = shlex.split(text, comments=False, posix=True)
    except ValueError:
        tokens = text.split()
    segments: list[list[str]] = []
    cur: list[str] = []
    for tok in tokens:
        if tok in _SEPARATORS:
            if cur:
                segments.append(cur)
            cur = []
            continue
        # shlex keeps redirect chars attached or separate depending on spacing;
        # a bare redirect token is not an argv word.
        if tok in (">", ">>", "<", "2>", "2>>", "&>"):
            continue
        cur.append(tok)
    if cur:
        segments.append(cur)
    return segments


def to_relative(file_path: str, project_dir: str, *, collapse: bool = False) -> str:
    """Normalize an absolute/relative path to a repo-relative forward-slash path.

    With ``collapse=True``, ``.``/``..`` segments are folded (the
    traversal-hardened variant used by provenance-guard and policy-engine) so
    ``citations/../facts.md`` cannot masquerade as an allowed path.
    """
    norm = file_path.replace("\\", "/")
    proj = project_dir.replace("\\", "/").rstrip("/")
    if proj and norm.startswith(proj + "/"):
        norm = norm[len(proj) + 1 :]
    if norm.startswith("./"):
        norm = norm[2:]
    if collapse:
        norm = os.path.normpath(norm).replace("\\", "/")
    return norm
