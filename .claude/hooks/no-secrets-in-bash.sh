#!/usr/bin/env bash
# PreToolUse hook on Bash (Notifier subagent). Blocks credential-shaped
# strings: sk-*, sk-or-*, sk-ant-*, xoxb-*, AWS access key IDs (AKIA*),
# Google API keys (AIza*), Tavily keys (tvly-*), PEM private key fragments.
# Allows ${VAR} env expansion: literal references are stripped before
# pattern matching so legitimate env-var use (e.g. ${OPENROUTER_API_KEY})
# does not trigger.
# Stderr does NOT echo the matched string to avoid leaking it to logs.
INPUT=$(cat)
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // ""')
SAFE_COMMAND=$(echo "$COMMAND" | sed -E 's/\$\{[A-Za-z_][A-Za-z0-9_]*\}//g')
SECRET_PATTERNS=(
  'sk-[A-Za-z0-9_-]{20,}'
  'xoxb-[A-Za-z0-9-]{20,}'
  'AKIA[0-9A-Z]{16}'
  '-----BEGIN[[:space:]]+(RSA[[:space:]]+)?(EC[[:space:]]+)?(OPENSSH[[:space:]]+)?(DSA[[:space:]]+)?PRIVATE[[:space:]]+KEY-----'
  'sk-or-[A-Za-z0-9_-]{20,}'
  'sk-ant-[A-Za-z0-9_-]{20,}'
  'AIza[0-9A-Za-z_-]{35}'
  'tvly-[A-Za-z0-9]{20,}'
)
for pattern in "${SECRET_PATTERNS[@]}"; do
  if echo "$SAFE_COMMAND" | grep -qE -- "$pattern"; then
    echo "BLOCKED: credential-shaped string detected in Bash command." >&2
    echo "Move secrets to env vars or files; never inline in commands." >&2
    exit 2
  fi
done
exit 0
