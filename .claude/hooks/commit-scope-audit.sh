#!/usr/bin/env bash
# commit-scope-audit.sh — advisory diff-size sanity check on git commit.
#
# Phase-1 unsupervised-run guardrail (2026-05-08): warn (rc=0) on
# suspicious diffs that don't match the commit message scope.  The
# 2026-05-08 FIX B incident bundled 154 unstaged proposal-agent
# additions into a "fix(sq-012): ... single-line flip" commit because
# the agent rewrote the JSON file via Python without checking what
# was already dirty.  Diff-stat audit caught it on manual review;
# this hook catches it automatically.
#
# Heuristics (advisory only — exits 0; commit proceeds):
#   - fix/chore/revert commit with > 50 total lines changed
#   - fix/chore/revert commit touching > 2 files without
#     [multi-file] marker in the message
#   - any commit with > 500 total lines changed without a
#     [large-diff] / "feat(" / "refactor(" marker
#
# Ratchet to rc=2 (blocking) once the heuristic is well-tuned.
#
# Reads PreToolUse JSON from stdin.  Only inspects `git commit`
# invocations; non-commit Bash calls pass through silently.

INPUT=$(cat)
# Use `python` (not `python3`) for JSON parsing — jq is not always
# available on Windows Git-Bash, and `python3` is a broken pyenv-win
# shim on this dev machine while `python` is the working Python 3.12
# install.  Try `python` first; fall back to `python3` for portability
# to non-Windows systems where `python` may not exist.
PY=""
if command -v python >/dev/null 2>&1; then
    PY=python
elif command -v python3 >/dev/null 2>&1; then
    PY=python3
fi
if [ -z "$PY" ]; then
    # No python: skip the audit silently (advisory hook, no-op
    # is the correct fail-open behaviour).
    exit 0
fi
COMMAND=$(printf '%s' "$INPUT" | "$PY" -c "
import json, sys
try:
    data = json.load(sys.stdin)
    print(data.get('tool_input', {}).get('command', ''), end='')
except Exception:
    pass
" 2>/dev/null)

# Only inspect git commit calls
if ! echo "$COMMAND" | grep -qE '\bgit[[:space:]]+commit\b'; then
    exit 0
fi

# Skip --amend (the staged diff doesn't reflect the cumulative change)
if echo "$COMMAND" | grep -qE -- '--amend'; then
    exit 0
fi

# Skip --allow-empty (no diff to audit)
if echo "$COMMAND" | grep -qE -- '--allow-empty'; then
    exit 0
fi

# Get staged diff stats; if git fails, exit 0 silently (don't block)
STAT=$(git diff --cached --shortstat 2>/dev/null || true)
if [ -z "$STAT" ]; then
    exit 0
fi

# Parse "<n> file(s) changed, <i> insertion(s)(+), <d> deletion(s)(-)"
FILES=$(echo "$STAT" | grep -oE '[0-9]+ file' | grep -oE '[0-9]+' | head -1)
INS=$(echo "$STAT" | grep -oE '[0-9]+ insertion' | grep -oE '[0-9]+' | head -1)
DEL=$(echo "$STAT" | grep -oE '[0-9]+ deletion' | grep -oE '[0-9]+' | head -1)
FILES=${FILES:-0}
INS=${INS:-0}
DEL=${DEL:-0}
TOTAL=$((INS + DEL))

# Detect commit type from the COMMAND string.  We don't try to
# extract the full message — too brittle across heredoc / -m / -F
# forms.  Looking for "<type>(<scope>):" or "<type>:" prefix anywhere
# in the command (works for both "$(cat <<'EOF' ... EOF)" and -m
# forms because the prefix appears verbatim in the bash command).
TYPE=""
if echo "$COMMAND" | grep -qE '\bfix(\([a-zA-Z0-9_-]+\))?:'; then
    TYPE="fix"
elif echo "$COMMAND" | grep -qE '\bchore(\([a-zA-Z0-9_-]+\))?:'; then
    TYPE="chore"
elif echo "$COMMAND" | grep -qE '\brevert(\([a-zA-Z0-9_-]+\))?:'; then
    TYPE="revert"
elif echo "$COMMAND" | grep -qE '\bfeat(\([a-zA-Z0-9_-]+\))?:'; then
    TYPE="feat"
elif echo "$COMMAND" | grep -qE '\brefactor(\([a-zA-Z0-9_-]+\))?:'; then
    TYPE="refactor"
fi

WARN_COUNT=0
WARN_LINES=""

# Heuristic 1: fix/chore/revert commit with > 50 total lines changed.
# These commit types are "small surgical change" by convention; > 50
# lines suggests scope creep or accidental-bundling (FIX B pattern).
if echo "$TYPE" | grep -qE '^(fix|chore|revert)$' && [ "$TOTAL" -gt 50 ]; then
    WARN_COUNT=$((WARN_COUNT + 1))
    WARN_LINES="$WARN_LINES
  - $TYPE commit changed $TOTAL lines (cap is 50 for this type)."
fi

# Heuristic 2: fix/chore/revert touching > 2 files without [multi-file]
# marker.  Single-scope commits should generally be single-file.
if echo "$TYPE" | grep -qE '^(fix|chore|revert)$' \
   && [ "$FILES" -gt 2 ] \
   && ! echo "$COMMAND" | grep -qiE '\[multi-?file\]|multi[ -]?file'; then
    WARN_COUNT=$((WARN_COUNT + 1))
    WARN_LINES="$WARN_LINES
  - $TYPE commit touches $FILES files without [multi-file] marker."
fi

# Heuristic 3: any commit with > 500 total lines changed.  Always
# warn-worthy as a sanity check; large diffs deserve explicit
# acknowledgement.  feat/refactor types are exempt from the upper
# bound only when the message contains [large-diff].
if [ "$TOTAL" -gt 500 ]; then
    if echo "$TYPE" | grep -qE '^(feat|refactor)$' \
       && echo "$COMMAND" | grep -qiE '\[large-diff\]'; then
        : # acknowledged; pass
    else
        WARN_COUNT=$((WARN_COUNT + 1))
        WARN_LINES="$WARN_LINES
  - large diff: $TOTAL lines across $FILES files (add [large-diff] marker if intentional)."
    fi
fi

if [ "$WARN_COUNT" -gt 0 ]; then
    cat >&2 <<EOF
[commit-scope-audit] $WARN_COUNT advisory warning(s) on this commit:$WARN_LINES
  diff: $FILES file(s), $INS+/$DEL- ($TOTAL total lines)
  type: ${TYPE:-<unknown>}
  [advisory only; commit proceeds. Ratchet to rc=2 when heuristic stabilises.]
EOF
fi

exit 0
