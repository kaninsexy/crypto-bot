# Exit-ramp — Mandate X

## Rule
Bundling-by-default does NOT extend across Claude Code turns. Do not
pre-write the next CC prompt before the prior CC turn's output is in
and verified. If prior CC has issues, the pre-written next prompt
becomes wasted tokens and a drift risk. Sequence: prior CC report →
chat-side review → user commits → THEN draft next CC prompt.

Breakpoint rule: at every logical breakpoint — including every CC
run that completes with self-checks passing — explicitly tell the
user this is a breakpoint and ask whether to continue in this chat
or start fresh. Do not silently start the next task.
The user decides, not Claude. This avoids both failure modes:
exiting mid-task and running past the point where handoff is reliable.

Every deliberation/state-change response that produces or accepts a
state change ends with the full forward chain in ONE message. The user
should never finish a deliberation wondering "now what do I do?"

## Required components (when applicable)
1. **Commit bash** — runnable `git add` + `git commit` with a heredoc-
   embedded message composed from the work just done. The full message
   text is Claude's job. Embed inside the git commit command via
   `git commit -F - <<'EOF' ... EOF`. NEVER separate the message into
   a different block the user pastes manually — that produces empty
   commits when the user runs the bash and skips the editor.

2. **Repomix regen** — the runnable command to regenerate the project
   knowledge bundle, when source files changed.

3. **Re-upload list** — paths + per-file rationale. Files NOT in the
   repomix include patterns (e.g., strategies.md, research/**) need
   standalone uploads — list them explicitly.

4. **Next-chat handoff prompt** — runnable artifact (a code block the
   user can paste into a fresh chat), not a prose description.

## Bundle by default — within a chat, not across CC turns
Bundle all N known actions for a single goal in one response. Multiple
code blocks per response are fine. Independent actions bundle; only
hard sequential dependencies split across turns.

(See ## Rule above for the no-pre-write-CC-prompt invariant — moved
to the top of the file because it gets violated when buried mid-file.)

## Don't pre-write downstream content
After Claude Code reports completion, deliver verification (tests
pass/fail, flagged items) and stop. Do not pre-write doc edits or
commit-status checklists unless explicitly asked. Doc updates are the
user's job at commit time. Commit messages ARE Claude's job per
mandate H.

## Shell-bundle ASCII rule
Bash blocks intended for copy-paste must contain ONLY ASCII
characters — including inside `#` comments. No em-dashes, en-dashes,
smart quotes, or non-ASCII anywhere. Em-dashes break zsh paste.

## Enforcement
- Layer 2 (deterministic): `exit-ramp-check.sh` Stop hook verifies the
  agent emitted commit bash, repomix regen, re-upload list, and
  next-chat handoff. If any missing, returns continuation message
  asking for them.

See also: CLAUDE.md "Behavioral mandates", "Commit and shell-bundle
rule"; architecture.md §E.1 mandate X.
