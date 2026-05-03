#!/usr/bin/env bash
# PreToolUse hook on WebFetch|WebSearch (Proposer subagent only).
# Enforces mandate P: requires a 'citation_key:' line in the transcript
# before any web search. Fail-closed when transcript is unreadable.
INPUT=$(cat)
TRANSCRIPT_PATH=$(echo "$INPUT" | jq -r '.transcript_path // ""')
if [ -z "$TRANSCRIPT_PATH" ] || [ ! -f "$TRANSCRIPT_PATH" ]; then
  echo "BLOCKED: transcript_path missing or unreadable; citation_key cannot be verified." >&2
  echo "Per mandate P, WebFetch/WebSearch require declared citation_key." >&2
  exit 2
fi
if grep -q 'citation_key:' "$TRANSCRIPT_PATH"; then
  exit 0
fi
cat >&2 <<'MSG'
BLOCKED: WebFetch/WebSearch requires a declared citation_key per mandate P.
Add 'citation_key: <slug>' to the proposer prompt before searching.
This forces declaration of what you are looking for, preventing
speculative searches that retroactively justify a parameter choice.
MSG
exit 2
