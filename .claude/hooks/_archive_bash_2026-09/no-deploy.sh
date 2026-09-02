#!/usr/bin/env bash
# PreToolUse hook on Bash. Blocks deploy verbs.
# Patterns: doctl, kubectl, ssh, docker push, digitalocean, dokku, deploy.
# Word-boundary anchored to avoid matching e.g. "redeploy" or "deployable".
# Does NOT block git push; that boundary is enforced separately
# (architecture.md §C.4 open record).
INPUT=$(cat)
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // ""')
DEPLOY_REGEX='(\bdoctl\b|\bkubectl\b|\bssh\b|docker[[:space:]]+push|\bdigitalocean\b|\bdokku\b|\bdeploy\b)'
if echo "$COMMAND" | grep -qE "$DEPLOY_REGEX"; then
  echo "BLOCKED: deploy verb detected. Recommend, do not execute." >&2
  echo "Command: $COMMAND" >&2
  exit 2
fi
exit 0
