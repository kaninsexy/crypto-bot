#!/usr/bin/env bash
# PreToolUse hook on Bash. Enforces mandate H: git commit messages
# must be heredoc-embedded inside the same Bash call.
# Gate: command contains 'git commit' AND '-m' AND no '<<'.
# Allows: git commit -F - <<'EOF' ... EOF (no -m).
# Allows: git commit --amend (no -m).
# Blocks: git commit -m "literal".
INPUT=$(cat)
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // ""')
if echo "$COMMAND" | grep -qE '\bgit[[:space:]]+commit\b' \
   && echo "$COMMAND" | grep -qE '[[:space:]]-m\b' \
   && ! echo "$COMMAND" | grep -q '<<'; then
  cat >&2 <<'MSG'
BLOCKED: git commit -m must use heredoc-embedded message per mandate H.
Required form:
  git commit -m "$(cat <<'EOF'
  <type>(<scope>): <description>
  ...
  EOF
  )"
or:
  git commit -F - <<'EOF'
  <type>(<scope>): <description>
  ...
  EOF
MSG
  exit 2
fi
exit 0
