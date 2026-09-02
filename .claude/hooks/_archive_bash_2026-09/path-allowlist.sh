#!/usr/bin/env bash
# PreToolUse hook on Write (Curator subagent). Curator is the only
# agent with write authority into T2 _pending_review.jsonl, and that
# is its ONLY allowed write path. Strategist-side review is the gate
# from _pending_review to facts.md / citations/ — curator must not
# touch either directly (architecture.md A.4 step 1).
INPUT=$(cat)
PATH_BEING_WRITTEN=$(echo "$INPUT" | jq -r '.tool_input.file_path // .tool_input.path // ""')
ALLOWLIST_REGEX='^\.memory/T2_semantic/_pending_review\.jsonl$'
if echo "$PATH_BEING_WRITTEN" | grep -qE "$ALLOWLIST_REGEX"; then
  exit 0
fi
echo "BLOCKED: $PATH_BEING_WRITTEN not in curator path allowlist." >&2
echo "Allowed: .memory/T2_semantic/_pending_review.jsonl (relative to repo root)." >&2
exit 2
