#!/usr/bin/env bash
# PreToolUse hook on Bash. 4-hour compute budget circuit breaker.
# Reads .memory/T1_episodic/_state/session_start.txt (epoch seconds).
# Exits 2 if (now - session_start) >= 14400 seconds.
# Fails open on missing/malformed state file (SessionStart not run yet).
INPUT=$(cat)
CWD=$(echo "$INPUT" | jq -r '.cwd // ""')
[ -z "$CWD" ] && CWD="$PWD"
STATE_FILE="$CWD/.memory/T1_episodic/_state/session_start.txt"
BUDGET_SECONDS=14400
if [ ! -f "$STATE_FILE" ]; then
  exit 0
fi
SESSION_START=$(tr -d '[:space:]' < "$STATE_FILE")
if [ -z "$SESSION_START" ] || ! [[ "$SESSION_START" =~ ^[0-9]+$ ]]; then
  exit 0
fi
NOW=$(date +%s)
ELAPSED=$((NOW - SESSION_START))
if [ "$ELAPSED" -ge "$BUDGET_SECONDS" ]; then
  echo "BLOCKED: 4-hour compute budget exhausted (elapsed=${ELAPSED}s, budget=${BUDGET_SECONDS}s)." >&2
  echo "Strategist must end session. See architecture.md section B.9." >&2
  exit 2
fi
exit 0
