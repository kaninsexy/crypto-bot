#!/usr/bin/env bash
# PreToolUse hook on Bash (Phase-4B Coordinator).
# 3-consecutive-failure escalation. Reads
# .memory/T1_episodic/_state/phase4b_failure_count.txt; exits 2 if >=3.
INPUT=$(cat)
CWD=$(echo "$INPUT" | jq -r '.cwd // ""')
[ -z "$CWD" ] && CWD="$PWD"
STATE_FILE="$CWD/.memory/T1_episodic/_state/phase4b_failure_count.txt"
THRESHOLD=3
if [ ! -f "$STATE_FILE" ]; then
  exit 0
fi
COUNT=$(tr -d '[:space:]' < "$STATE_FILE")
if [ -z "$COUNT" ] || ! [[ "$COUNT" =~ ^[0-9]+$ ]]; then
  exit 0
fi
if [ "$COUNT" -ge "$THRESHOLD" ]; then
  echo "BLOCKED: 3-fail threshold hit (count=${COUNT}, threshold=${THRESHOLD})." >&2
  echo "Strategist must escalate to human before next variation." >&2
  echo "See architecture.md section B.8." >&2
  exit 2
fi
exit 0
