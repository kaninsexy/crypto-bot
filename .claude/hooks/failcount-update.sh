#!/usr/bin/env bash
# PostToolUse hook on Bash (Phase-4B Coordinator). Hook-as-writer
# pattern (settled chat 2026-05-03 D1):
# Coordinator emits a single line `VERDICT=PASS` or `VERDICT=FAIL`
# in stdout as its final Bash echo before yielding. This hook parses
# tool_response.stdout, then writes the failure counter:
#   - 0 markers  -> no-op (most Bash calls don't yield verdicts).
#   - 1 PASS     -> write 0.
#   - 1 FAIL     -> read counter, increment by 1, write back.
#   - 2+ markers -> ambiguous; exit 2.
# The coordinator MUST NOT touch the counter file directly.
INPUT=$(cat)
CWD=$(echo "$INPUT" | jq -r '.cwd // ""')
[ -z "$CWD" ] && CWD="$PWD"
STATE_FILE="$CWD/.memory/T1_episodic/_state/phase4b_failure_count.txt"

STDOUT=$(echo "$INPUT" | jq -r '.tool_response.stdout // ""')
PASS_COUNT=$(printf '%s\n' "$STDOUT" | grep -cE '^VERDICT=PASS$' || true)
FAIL_COUNT=$(printf '%s\n' "$STDOUT" | grep -cE '^VERDICT=FAIL$' || true)
TOTAL=$((PASS_COUNT + FAIL_COUNT))

if [ "$TOTAL" -eq 0 ]; then
  exit 0
fi
if [ "$TOTAL" -gt 1 ]; then
  echo "BLOCKED: ambiguous verdict markers in coordinator stdout (PASS=${PASS_COUNT} FAIL=${FAIL_COUNT})." >&2
  exit 2
fi

if [ "$PASS_COUNT" -eq 1 ]; then
  echo "0" > "$STATE_FILE"
  exit 0
fi

CURRENT=$(tr -d '[:space:]' < "$STATE_FILE" 2>/dev/null)
if [ -z "$CURRENT" ] || ! [[ "$CURRENT" =~ ^[0-9]+$ ]]; then
  CURRENT=0
fi
echo $((CURRENT + 1)) > "$STATE_FILE"
exit 0
