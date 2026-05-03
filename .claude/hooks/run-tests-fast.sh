#!/usr/bin/env bash
# PostToolUse hook on Edit|Write (Implementer subagent).
# Runs the fast pytest suite as a guardrail after every edit.
# Exits 2 on red (prints last 50 lines of pytest output to stderr).
# Exits 0 on green or when no tests are tagged 'fast' (warns once/day).
INPUT=$(cat)
CWD=$(echo "$INPUT" | jq -r '.cwd // ""')
[ -z "$CWD" ] && CWD="$PWD"
cd "$CWD" 2>/dev/null || exit 0

TMP_OUT=$(mktemp)
trap 'rm -f "$TMP_OUT"' EXIT

pytest -m fast -q > "$TMP_OUT" 2>&1
RC=$?

if [ "$RC" -eq 0 ]; then
  exit 0
fi

# pytest exit code 5 = no tests collected. Treat as soft-skip.
if [ "$RC" -eq 5 ]; then
  WARN_FLAG="/tmp/run-tests-fast.warned-no-tests.$(date +%Y%m%d)"
  if [ ! -f "$WARN_FLAG" ]; then
    echo "warn: pytest -m fast collected 0 tests; tag tests with @pytest.mark.fast to enable this guardrail." >&2
    touch "$WARN_FLAG"
  fi
  exit 0
fi

echo "BLOCKED: pytest -m fast failed (rc=${RC}). Last 50 lines:" >&2
tail -n 50 "$TMP_OUT" >&2
exit 2
