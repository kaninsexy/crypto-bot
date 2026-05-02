#!/usr/bin/env bash
# UserPromptSubmit hook for crypto-bot.
# Stdout from this script is injected as context Claude sees on
# every user prompt. Keep terse — every byte adds to context cost.
# Persistent reminder of the four mandates most prone to drift,
# established as a class of failures in chat 2026-05-03.

cat <<'REMINDERS'
[CLAUDE.md MANDATE REMINDERS - auto-injected per UserPromptSubmit]

A. Read evidence end-to-end BEFORE responding. Open named project
   files via view or project_knowledge_search. Do not pattern-match
   from memory or repomix summaries.

F. Design choices are agent calls when data answers the question.
   NO option-A/B/C menus, NO sign-off loops. Sign-off is reserved
   for commit / push / deploy / sacred-harness schema changes.

Commit messages: embed inside the git commit command via heredoc
   (git commit -F - <<'EOF' ... EOF). Never as a separate block
   the user pastes manually — that produces empty commits.

Exit-ramp completeness: every state-changing response ends with
   the full forward chain — commit bash + message, repomix regen,
   re-upload list with rationale, next-chat handoff if applicable,
   chat-end flag.
REMINDERS
