#!/usr/bin/env bash
# PreToolUse hook on Bash (Implementer subagent).
# Companion to commit-heredoc-required.sh: heredoc enforces SHAPE,
# this hook enforces CONTENT. Settled chat 2026-05-03 D2;
# agent-detection rewritten 2026-05-03 fix-forward on a3accde
# to align with architecture.md E.3 (Layer 3 commit-msg parallel).
#
# Validates:
#  1. Subject line matches conventional-commits prefix.
#     ^(feat|fix|chore|docs|test|refactor|perf|build|ci)
#       (\([a-z0-9-]+\))?: .{1,72}$
#  2. If agent-authored, body contains [mandate-H] token.
#     Agent detection: SOLE signal is a body line matching
#     ^Co-authored-by: Claude. Claude Code adds this trailer to
#     subagent commits, so it is the reliable indicator. The
#     previous CLAUDE_AGENT env + email-mismatch detection was
#     unenforceable in practice (env never set; kanin_email line
#     absent from facts.md), making [mandate-H] enforcement opt-in.
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
if printf '%s\n' "$MSG" | grep -qE '^Co-authored-by: Claude'; then
  IS_AGENT=1
fi

if [ "$IS_AGENT" -eq 1 ]; then
  if ! printf '%s\n' "$MSG" | grep -qF '[mandate-H]'; then
    echo "BLOCKED: agent-authored commit must contain [mandate-H] token in body." >&2
    echo "Detected via Co-authored-by: Claude trailer." >&2
    echo "Add a line containing [mandate-H] anywhere in the commit body." >&2
    exit 2
  fi
fi

exit 0
