#!/usr/bin/env bash
# PreToolUse hook on Edit|Write. Blocks any path matching the sacred
# allowlist. The one rule that absolutely must not be bypassable.
# Spec: architecture.md §E.2 (verbatim).
INPUT=$(cat)
PATH_BEING_EDITED=$(echo "$INPUT" | jq -r '.tool_input.file_path // .tool_input.path // ""')
SACRED_REGEX='(trials\.py$|holdout\.py$|validation_framework/|^CLAUDE\.md$|^MASTER_PLAN\.md$|validation_framework\.md$)'
if echo "$PATH_BEING_EDITED" | grep -qE "$SACRED_REGEX"; then
  echo "BLOCKED: $PATH_BEING_EDITED is sacred-harness; propose, do not edit." >&2
  exit 2
fi
exit 0
