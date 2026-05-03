#!/usr/bin/env bash
# PreToolUse hook on Bash (Notifier subagent). Blocks credential-shaped
# strings: sk-*, xoxb-*, AWS access key IDs, PEM private key fragments.
# Stderr does NOT echo the matched string to avoid leaking it to logs.
INPUT=$(cat)
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // ""')
SECRET_PATTERNS=(
  'sk-[A-Za-z0-9_-]{20,}'
  'xoxb-[A-Za-z0-9-]{20,}'
  'AKIA[0-9A-Z]{16}'
  '-----BEGIN[[:space:]]+(RSA[[:space:]]+)?(EC[[:space:]]+)?(OPENSSH[[:space:]]+)?(DSA[[:space:]]+)?PRIVATE[[:space:]]+KEY-----'
)
for pattern in "${SECRET_PATTERNS[@]}"; do
  if echo "$COMMAND" | grep -qE -- "$pattern"; then
    echo "BLOCKED: credential-shaped string detected in Bash command." >&2
    echo "Move secrets to env vars or files; never inline in commands." >&2
    exit 2
  fi
done
exit 0
