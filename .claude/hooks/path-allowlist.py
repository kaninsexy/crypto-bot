#!/usr/bin/env python3
"""
PreToolUse hook on Write/Edit/Bash — enforces three-tier file autonomy.

Reads tool-input JSON from stdin (Claude Code hook protocol). Three-tier
gate (CLAUDE.md "Core principles"):

- Tier 1 sacred: ALWAYS blocked unless SACRED_OVERRIDE_FILES env var
  names the path. That env var is a COMMA-SEPARATED LIST of repo-relative
  paths, matched EXACTLY (see ``_parse_override`` / BK-0304). An entry
  ending in ``/``, or an entry that names an existing DIRECTORY, is a
  directory scope covering everything beneath it. Nothing else
  authorises: an entry that merely CONTAINS a sacred path as a substring
  does not.
- Tier 2 schema-stable: allowed but flagged in stderr so the human sees
  it in the session log. Not blocked because bug fixes are legitimate.
- Tier 3 agent-autonomous: allowed silently.

Dispatches on ``tool_name``:

- ``Write`` / ``Edit`` (the original behaviour): inspects
  ``tool_input.file_path`` / ``tool_input.path`` and checks the single
  target.
- ``Bash`` (added 2026-05-15, closes ``hook_path_allowlist_bash_bypass``
  from CLAUDE.md §14): inspects ``tool_input.command`` and runs a small
  list of regex patterns over the command string to extract candidate
  write-target paths. Each captured path goes through the same Tier
  check. Patterns covered: ``tee [-a] PATH``, ``cat > PATH`` /
  ``cat >> PATH`` (incl. heredocs), bare ``> PATH`` / ``>> PATH`` for
  any command, ``sed -i / --in-place ... PATH``, ``cp [-r] SRC PATH``,
  ``mv [-f] SRC PATH``, ``python -c '... open("PATH", "w"|"a") ...'``,
  ``ln -s[f] TARGET LINK``. The list is intentionally permissive — a
  false-block costs an agent message; a false-allow costs a corrupted
  sacred file.

Paths that start with ``$`` are skipped: we can't know the value at
hook time, and false-blocking shell-variable redirects would be more
disruptive than the rare false-allow it produces. Same trade-off the
no-secrets-in-bash hook accepts for variable refs.

Exit codes (per Claude Code hook protocol):
- 0: allow
- 2: block (stderr message visible to agent)

Why Python: crypto-bot's bash predecessors (sacred-block.sh,
path-allowlist.sh, no-secrets-in-bash.sh, ...) all parsed stdin with jq,
which is NOT installed on this machine — every one of them exited 127 and
FAILED OPEN. Python removes the jq dependency and unblocks the safety layer.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

# Same-directory imports: when Python runs this file as a script, the
# script's directory is sys.path[0], so these resolve regardless of CWD.
#
# Both imports are GUARDED so a broken/absent helper cannot crash the module at
# LOAD time. In Claude Code only exit 2 blocks a tool call; an import-time
# crash (exit 1) is treated as non-blocking — i.e. FAIL OPEN. This is exactly
# the BK-0018 vulnerability: `_block_record` used to import `datetime.UTC`
# (Python 3.11+), so on 3.10 this guard crashed at load and silently let a
# sacred write through. We now (a) don't import a 3.11-only symbol, and (b)
# never let an import failure fail open.
#
# The block-recorder is best-effort — the SECURITY decision never depends on
# it — so a no-op fallback is safe.
try:
    from _block_record import _write_block_record
except Exception:  # pragma: no cover - defensive; recorder is best-effort
    def _write_block_record(*_a: object, **_k: object) -> None:
        return None

# The tier map is CRITICAL: without SACRED_PATTERNS the guard cannot identify a
# sacred file, so a failed import must FAIL CLOSED (block every write), never
# fall through to "nothing matched -> allow". Record the error; main() converts
# it to exit 2. Tier patterns live in file_tiers.py — the single definition
# site since 2026-07-02 (playbooks port V5). Edit tiers THERE; the Tier-1 eval
# fixtures (eval/run_tier1.py) pin the enforced behavior.
try:
    from file_tiers import SACRED_PATTERNS, SCHEMA_STABLE_PATTERNS  # noqa: F401
    _TIERS_IMPORT_ERROR: Exception | None = None
except Exception as _e:  # pragma: no cover - defensive
    _TIERS_IMPORT_ERROR = _e
    SACRED_PATTERNS: tuple[str, ...] = ()  # type: ignore[no-redef]
    SCHEMA_STABLE_PATTERNS: tuple[str, ...] = ()  # type: ignore[no-redef]

# A shell path token: double-quoted, single-quoted, or unquoted run of
# non-shell-special chars. The unquoted variant allows `$` so that
# variable-expanded paths get captured here and rejected by the
# `startswith("$")` guard at the call site (rather than slipping past
# the regex entirely).
_PATH = r"""(?:"[^"\n]+"|'[^'\n]+'|[^\s'"<>|;&\n]+)"""

# Each regex captures the candidate write-target path in the named
# group `path`. Order doesn't matter; we iterate all of them and
# deduplicate by normalized path before checking. The label is the
# pattern-kind id surfaced into block records as ``BASH_<kind>``.
BASH_WRITE_PATTERNS: tuple[tuple[str, str], ...] = (
    # tee [flags] PATH  (also covers tee -a / tee --append)
    ("TEE", rf"\btee\b(?:\s+-+[a-zA-Z]+)*\s+(?P<path>{_PATH})"),
    # > PATH and >> PATH (any command). Lookbehind excludes the `<>`
    # read-write redirect shape; we accept the false-negative there.
    ("REDIRECT", rf"(?<!<)>>?\s*(?P<path>{_PATH})"),
    # sed -i / sed --in-place ... PATH  (PATH is the final positional;
    # the lazy `*?` + tail-anchor lets the regex backtrack past the
    # quoted sed expression to find the file).
    ("SED", rf"\bsed\b\s+(?:-i\b|--in-place\b)[^|;&\n]*?\s+(?P<path>{_PATH})\s*(?:$|[|;&\n])"),
    # cp [flags] SRC ... DST  (DST is the final positional)
    ("CP", rf"\bcp\b(?:\s+-+[a-zA-Z]+)*\s+{_PATH}[^|;&\n]*?\s+(?P<path>{_PATH})\s*(?:$|[|;&\n])"),
    # mv [flags] SRC DST
    ("MV", rf"\bmv\b(?:\s+-+[a-zA-Z]+)*\s+{_PATH}[^|;&\n]*?\s+(?P<path>{_PATH})\s*(?:$|[|;&\n])"),
    # python -c '... open("PATH", "w"|"a") ...'  (works for python3 too;
    # matches inside whatever outer quoting the shell uses).
    ("PYTHON_OPEN", r"""open\(\s*['"](?P<path>[^'"\n]+)['"]\s*,\s*['"][wa]"""),
    # ln -s[f] TARGET LINK  (LINK is the final positional)
    ("LN", rf"\bln\b\s+-+[a-zA-Z]+\s+{_PATH}\s+(?P<path>{_PATH})\s*(?:$|[|;&\n])"),
    # rm [flags] PATH ... — added per AgentShield audit C4 finding
    # 2026-05-16. Delete-of-sacred-file is a write operation in the
    # Tier 1 sense (the file changes from "exists" to "doesn't"). One
    # pattern per positional argument, since `rm` accepts multiple
    # paths and we want each one gated.
    ("RM", rf"\brm\b(?:\s+-+[a-zA-Z]+)*\s+(?P<path>{_PATH})"),
)


def to_relative(file_path: str, project_dir: str) -> str:
    """Normalize an absolute or relative path to a repo-relative forward-slash path."""
    norm = file_path.replace("\\", "/")
    proj = project_dir.replace("\\", "/").rstrip("/")
    if proj and norm.startswith(proj + "/"):
        norm = norm[len(proj) + 1 :]
    if norm.startswith("./"):
        norm = norm[2:]
    return norm


def _strip_quotes(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    return s


_HEREDOC_OPEN_RE = re.compile(
    r"""<<-?\s*(?P<quote>['"]?)(?P<tag>[A-Za-z_][A-Za-z0-9_]*)(?P=quote)"""
)


def _strip_heredoc_bodies(cmd: str) -> str:
    """Replace heredoc bodies with a blank placeholder, preserving line shape.

    Per AgentShield audit C7 (CLAUDE.md §14
    ``hook_path_allowlist_heredoc_false_positive``), the scanner was
    matching delete-verb / redirect patterns inside heredoc bodies (e.g.
    a commit-message body that mentions ``rm /some/path``). Heredoc
    bodies are data, not commands, so the path-allowlist must not
    interpret them as write targets.

    Implementation: walk the command line by line. When we see a
    heredoc opener (``<<EOF``, ``<<'EOF'``, ``<<-EOF``), set a state
    flag and replace subsequent lines with empty strings until we see
    the closing tag on its own line. We preserve line breaks so the
    regex coordinates aren't shifted in surprising ways.
    """
    out_lines: list[str] = []
    pending_tag: str | None = None
    for line in cmd.split("\n"):
        if pending_tag is not None:
            # We're inside a heredoc body. Check for the close tag.
            if line.strip() == pending_tag:
                pending_tag = None
            # Either way, the body line itself is dropped.
            out_lines.append("")
            continue
        # Not inside a heredoc. Scan the line for a heredoc opener.
        m = _HEREDOC_OPEN_RE.search(line)
        if m is not None:
            pending_tag = m.group("tag")
            # Keep the line up to and including the opener — it may be
            # the command invocation we want to scan ("git commit -F -
            # <<'EOF'"). Truncate at end-of-line; the body lives below.
        out_lines.append(line)
    return "\n".join(out_lines)


def _candidate_paths_from_command(cmd: str) -> list[tuple[str, str]]:
    """Extract (path, pattern_kind) tuples from a Bash command string.

    Returns entries in order of first appearance, deduplicated by
    normalized path. Empty if the command is empty or commented out.

    Heredoc bodies are stripped before scanning per AgentShield audit
    C7 — see ``_strip_heredoc_bodies``.
    """
    stripped = cmd.lstrip()
    if not stripped or stripped.startswith("#"):
        return []
    scan_target = _strip_heredoc_bodies(cmd)
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for kind, pat in BASH_WRITE_PATTERNS:
        for m in re.finditer(pat, scan_target):
            raw = m.group("path")
            cleaned = _strip_quotes(raw)
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                out.append((cleaned, kind))
    return out


def _parse_override(override: str, project_dir: str = "") -> tuple[str, ...]:
    """Parse SACRED_OVERRIDE_FILES into a tuple of normalized scope entries.

    The env var is a COMMA-SEPARATED list of repo-relative paths. Each entry
    is stripped of surrounding whitespace, normalized to forward slashes, and
    made repo-relative (an absolute path under the project dir, or a leading
    ``./``, is reduced the same way a write target is). Empty entries are
    dropped, and a bare ``/`` is dropped too — a whole-repo scope is never
    what an owner means to type, and honouring it would disarm the guard.

    A trailing ``/`` is PRESERVED: it is the explicit directory-scope marker
    consumed by ``_override_authorises``.
    """
    out: list[str] = []
    for chunk in override.split(","):
        entry = to_relative(chunk.strip(), project_dir)
        if not entry or entry == "/":
            continue
        if entry not in out:
            out.append(entry)
    return tuple(out)


def _override_authorises(
    rel: str, entries: tuple[str, ...], project_dir: str = ""
) -> bool:
    """Does the parsed override list authorise a write to ``rel``?

    EXACT match on the full relative path, with two directory-scope
    exceptions: an entry ending in ``/`` is an explicit directory scope, and
    an entry that NAMES AN EXISTING DIRECTORY in the repo is treated as the
    same scope (crypto-bot adaptation, 2026-09-02).

    Why the second form. The megaloop launch line authorises
    ``.claude/hooks`` — the directory — without a trailing slash, matching how
    the prompt's AUTONOMY block writes it (``.claude/hooks/**``). Under
    exact-match-only that authorises nothing, so the guard would lock the
    agent out of the very files it was pre-authorised to port, mid-run. The
    ``isdir`` test keeps BK-0304's fix intact: a bystander entry that merely
    CONTAINS a sacred path as a substring (``docs/.environment_notes.md``
    contains ``.env``) is not a directory, so it still authorises nothing.

    This replaces a substring test (``rel in override``) that failed both
    ways — BK-0304. It UNDER-authorised, because a directory form could
    never cover the files under it (the containment ran backwards), and it
    OVER-authorised, because any sacred path that happened to appear inside
    an unrelated entry was waved through: ``docs/.environment_notes.md``
    contains ``.env``, so an override naming an ordinary documentation file
    silently authorised writes to the secrets file. Exact matching removes
    the bystander authorisation; the directory form is now something the
    owner opts into by typing the slash, not something that falls out of
    string containment by accident.
    """
    base = Path(project_dir) if project_dir else Path.cwd()
    for entry in entries:
        if entry.endswith("/"):
            if rel.startswith(entry):
                return True
            continue
        if rel == entry:
            return True
        if rel.startswith(entry + "/"):
            try:
                if (base / entry).is_dir():
                    return True
            except OSError:  # pragma: no cover - defensive
                pass
    return False


def _check_path(rel: str, override: str, project_dir: str = "") -> tuple[int, str | None]:
    """Apply Tier 1 / Tier 2 / Tier 3 gate to a single relative path.

    Returns ``(exit_code, matched_sacred_pattern_or_None)``. Exit code
    is 2 only for blocked Tier 1 sacred. Tier 2 prints a warning;
    Tier 3 is silent. The second element of the tuple carries the
    SACRED_PATTERNS regex that matched, so the caller can surface it
    in a block record.
    """
    entries = _parse_override(override, project_dir)
    for pat in SACRED_PATTERNS:
        if re.match(pat, rel):
            if _override_authorises(rel, entries, project_dir):
                print(
                    f"[path-allowlist] SACRED OVERRIDE: {rel} (allowed by env var)",
                    file=sys.stderr,
                )
                return 0, None
            # Echo the PARSED scope, not the raw env string: a launch that
            # used the wrong delimiter (spaces instead of commas) otherwise
            # looks authorized to the human and blocked to the agent, with
            # nothing on screen explaining the disagreement.
            scope = ", ".join(entries) if entries else "<empty>"
            print(
                f"[path-allowlist] BLOCKED: {rel} is Tier 1 sacred (CLAUDE.md 'Core principles'). "
                "Human must commit edits to this file, OR pre-authorize via the "
                "SACRED_OVERRIDE_FILES env var — a COMMA-separated list of "
                "repo-relative paths, matched exactly (a trailing '/' scopes a "
                f"whole directory). Authorized this session: [{scope}]",
                file=sys.stderr,
            )
            return 2, pat
    for pat in SCHEMA_STABLE_PATTERNS:
        if re.match(pat, rel):
            print(
                f"[path-allowlist] NOTE: {rel} is Tier 2 schema-stable. "
                "Bug fixes OK; signature / schema changes need human approval. "
                "Surface the diff in your turn summary.",
                file=sys.stderr,
            )
            return 0, None
    return 0, None


def main() -> int:
    # FAIL CLOSED: if the tier map could not be imported the guard cannot
    # identify sacred files, so it must deny rather than wave writes through.
    if _TIERS_IMPORT_ERROR is not None:
        print(
            f"[path-allowlist] FAIL-CLOSED: tier map failed to import "
            f"({_TIERS_IMPORT_ERROR!r}); blocking to avoid a fail-open guard.",
            file=sys.stderr,
        )
        return 2

    raw = sys.stdin.read()
    if not raw.strip():
        return 0
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"[path-allowlist] WARN: stdin not JSON ({e}); allowing", file=sys.stderr)
        return 0

    tool_name = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input") or {}
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or str(Path.cwd())
    override = os.environ.get("SACRED_OVERRIDE_FILES", "")
    session_id = payload.get("session_id") or "unknown"

    # Bash dispatch — scan the command string for write-target paths.
    if tool_name == "Bash":
        cmd = tool_input.get("command", "") or ""
        if not cmd:
            return 0
        for candidate, kind in _candidate_paths_from_command(cmd):
            if candidate.startswith("$"):
                # Variable-expanded path; can't be evaluated at hook time.
                continue
            rel = to_relative(candidate, project_dir)
            code, matched = _check_path(rel, override, project_dir)
            if code != 0:
                _write_block_record(
                    hook_name="path-allowlist",
                    tool_name=tool_name,
                    reason=f"BLOCKED: {rel} is Tier 1 sacred (CLAUDE.md 'Core principles').",
                    blocked_target=rel,
                    blocked_pattern_id=f"BASH_{kind}:{matched}" if matched else f"BASH_{kind}",
                    session_id=session_id,
                )
                return code
        return 0

    # Write/Edit dispatch (and legacy: empty tool_name with a file_path).
    if tool_name in ("Write", "Edit", ""):
        file_path = tool_input.get("file_path") or tool_input.get("path") or ""
        if not file_path:
            return 0
        rel = to_relative(file_path, project_dir)
        code, matched = _check_path(rel, override, project_dir)
        if code != 0:
            _write_block_record(
                hook_name="path-allowlist",
                tool_name=tool_name or "unknown",
                reason=f"BLOCKED: {rel} is Tier 1 sacred (CLAUDE.md 'Core principles').",
                blocked_target=rel,
                blocked_pattern_id=f"SACRED:{matched}" if matched else "SACRED",
                session_id=session_id,
            )
        return code

    # Unknown future tool — fail-open per protocol; the matcher in
    # settings.json is what binds this hook to specific tools.
    return 0


if __name__ == "__main__":
    # FAIL CLOSED: a guard that cannot run must DENY. Any unhandled exception
    # exits 2 (block), never crashes to exit 1 — which Claude Code treats as a
    # non-blocking error, letting the tool proceed (the BK-0018 failure mode).
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as e:  # noqa: BLE001 — deliberate catch-all, fail closed
        print(
            f"[path-allowlist] FAIL-CLOSED: guard crashed ({type(e).__name__}: {e}); "
            "blocking (exit 2).",
            file=sys.stderr,
        )
        sys.exit(2)
