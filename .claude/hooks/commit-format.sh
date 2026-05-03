#!/usr/bin/env bash
# PreToolUse hook on Bash (Implementer subagent).
# Companion to commit-heredoc-required.sh: heredoc enforces SHAPE,
# this hook enforces CONTENT. Settled chat 2026-05-03 D2.
#
# Validates:
#  1. Subject line matches conventional-commits prefix.
#     ^(feat|fix|chore|docs|test|refactor|perf|build|ci)
#       (\([a-z0-9-]+\))?: .{1,72}$
#  2. If agent-authored, body contains [mandate-H] token.
#     Detection precedence:
#       a. Env CLAUDE_AGENT set -> agent.
#       b. facts.md kanin_email present AND git user.email mismatches
#          -> agent.
#       c. Otherwise -> non-agent (no [mandate-H] required).
#     The (c) default is permissive so manual Kanin commits don't
#     block when facts.md hasn't been seeded yet.
INPUT=$(cat)
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // ""')

if ! echo "$COMMAND" | grep -qE '\bgit[[:space:]]+commit\b'; then
  exit 0
fi
if echo "$COMMAND" | grep -qE '\-\-amend'; then
  exit 0
fi

MSG=$(printf '%s\n' "$COMMAND" | awk '
  /<<.?[A-Za-z_][A-Za-z0-9_]*/ && !in_heredoc { in_heredoc=1; next }
  in_heredoc && /^[[:space:]]*EOF[[:space:]]*$/ { in_heredoc=0; next }
  in_heredoc { print }
')

if [ -z "$MSG" ]; then
  echo "BLOCKED: commit-format could not extract heredoc message body." >&2
  echo "Use: git commit -F - <<'EOF' ... EOF  (or -m \"\$(cat <<'EOF' ... EOF)\")" >&2
  exit 2
fi

SUBJECT=$(printf '%s\n' "$MSG" | head -n1)
SUBJECT_REGEX='^(feat|fix|chore|docs|test|refactor|perf|build|ci)(\([a-z0-9-]+\))?: .{1,72}$'
if ! echo "$SUBJECT" | grep -qE "$SUBJECT_REGEX"; then
  echo "BLOCKED: commit subject does not match conventional format." >&2
  echo "Required: <type>(<scope>): <description>  (subject 1..72 chars)" >&2
  echo "  type in: feat fix chore docs test refactor perf build ci" >&2
  echo "Got: $SUBJECT" >&2
  exit 2
fi

IS_AGENT=0
if [ -n "${CLAUDE_AGENT:-}" ]; then
  IS_AGENT=1
else
  HUMAN_EMAIL=$(grep -E '^kanin_email:' .memory/T2_semantic/facts.md 2>/dev/null | sed -E 's/^kanin_email:[[:space:]]*//')
  GIT_EMAIL=$(git config user.email 2>/dev/null)
  if [ -n "$HUMAN_EMAIL" ] && [ -n "$GIT_EMAIL" ] && [ "$HUMAN_EMAIL" != "$GIT_EMAIL" ]; then
    IS_AGENT=1
  fi
fi

if [ "$IS_AGENT" -eq 1 ]; then
  if ! printf '%s\n' "$MSG" | grep -qF '[mandate-H]'; then
    echo "BLOCKED: agent-authored commit must contain [mandate-H] token in body." >&2
    echo "Add a line containing [mandate-H] anywhere in the commit body." >&2
    exit 2
  fi
fi

exit 0
