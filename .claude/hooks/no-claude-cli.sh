#!/usr/bin/env bash
# no-claude-cli.sh — block recursive claude CLI calls from subagents
# Reads JSON from stdin; exits 2 if the bash command spawns the claude CLI.
INPUT=$(cat)
CMD=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('tool_input',{}).get('command',''))" 2>/dev/null || true)
if echo "$CMD" | grep -qE '(^|[[:space:];|&])claude([[:space:]]|-p|--)'; then
    echo "BLOCKED: recursive claude CLI call detected in subagent Bash tool. Subagents must not spawn claude." >&2
    exit 2
fi
exit 0
