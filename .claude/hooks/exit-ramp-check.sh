#!/usr/bin/env bash
# Stop hook. Verifies the agent emitted the four exit-ramp components
# (commit bash, repomix regen, re-upload list, next-chat handoff).
# If any missing, exit 2 with stderr message which becomes a
# continuation request to the model.
INPUT=$(cat)
TRANSCRIPT_PATH=$(echo "$INPUT" | jq -r '.transcript_path // ""')
if [ -z "$TRANSCRIPT_PATH" ] || [ ! -f "$TRANSCRIPT_PATH" ]; then
  exit 0
fi
MISSING=()
if ! grep -q 'git commit' "$TRANSCRIPT_PATH"; then
  MISSING+=("commit-bash (git commit + heredoc message)")
fi
if ! grep -qi 'repomix' "$TRANSCRIPT_PATH"; then
  MISSING+=("repomix-regen (command to regenerate project knowledge)")
fi
if ! grep -qiE 're-?upload|upload list' "$TRANSCRIPT_PATH"; then
  MISSING+=("re-upload-list (paths + per-file rationale)")
fi
if ! grep -qi 'handoff' "$TRANSCRIPT_PATH"; then
  MISSING+=("next-chat-handoff (runnable artifact for fresh chat)")
fi
if [ ${#MISSING[@]} -eq 0 ]; then
  exit 0
fi
echo "Exit-ramp incomplete. Missing components:" >&2
for item in "${MISSING[@]}"; do
  echo "  - $item" >&2
done
echo "Per mandate X, every state-changing session ends with the full forward chain." >&2
echo "See .memory/T3_procedural/exit_ramp.md." >&2
exit 2
