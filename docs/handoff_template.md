# Handoff template — workflow procedure for chats

This file holds workflow-specific procedure that doesn't need to
compete for attention in every chat. CLAUDE.md references it via
"READ FIRST at chat start." Re-read at chat start and when drafting
a chat-close handoff for a fresh chat.

## Pre-action checklist

Before any response that asks the user to do something, run this
5-line check:

- First tool call: bash_tool on repomix-output.xml. Not
  project_knowledge_search. Always more current than standalone uploads.
- When reading a specific file from repomix, always run
  grep -n to find its CURRENT line position first. Never use a
  line number carried from a previous chat or repomix version.
  Line numbers shift on every repomix regeneration.
- Never run individual trial scripts. Always use
  run_trial_queue.py. Run warm_google_trends_cache.py first when
  any Google Trends strategy is queued.

1. Can I self-execute this with `bash_tool`, `conversation_search`,
   `project_knowledge_search`, or `view`? If yes — do it; don't ask.
2. Am I outputting 2+ bash blocks for independent commands? If yes
   — recombine into one fenced block.
3. Is the next step predictable from current context? If yes —
   bundle it into this response.
4. Has this chat reached a logical breakpoint (smoke clears, trial
   completes, audit completes, gate clears)? If yes — flag chat
   close and draft new-chat handoff.
5. Is `.memory/T1_episodic/_state/session_start.txt` elapsed time
   under the 4-hour budget? `echo $(( $(date +%s) - $(cat
   .memory/T1_episodic/_state/session_start.txt) ))` should
   return < 14400. If not, reset before launching any
   coordinator-spawning prompt.

If output contains "paste the output" / "let me know" / "can you
check" / "after that, I'll" — STOP and re-check items 1 and 3.

## Logical breakpoints (replaces turn-count flag)

A logical breakpoint is a task milestone, not a turn count. Close
the chat at any of these:

- CC run completes and all self-checks pass — always a breakpoint.
- Pre-trial gates clear → close, new chat for trial run.
- Trial completes (smoke or full_cpcv) → close, new chat for
  review or next trial.
- Review completes (verdict tree forensics, mandate E pass) →
  close, new chat for next variation or next strategy.
- Audit completes (memory, CLAUDE.md, project state) → close,
  new chat for downstream work.
- Chat-side only. At every logical breakpoint: explicitly tell the
  user this is a breakpoint and ask whether to continue in this
  chat or start fresh. Do not silently start the next task.
  The user decides, not Claude.

Difficult work legitimately needs more turns. Don't fixate on a
turn count. The signal is task completion, not turn count.

## New-chat handoff structure

Every new-chat paste block MUST open with this exact line (Block 0):

"Read repomix-output.xml with bash_tool as your FIRST action, then
CLAUDE.md, then docs/handoff_template.md, then userMemories.
Confirm when done."

This line is mandatory. Without it the new chat has no instruction
to read repomix and falls back to pattern-matching from stale memory.

Every chat-close handoff MUST end with three blocks:

1. **CARRY-FORWARD STATE.** What's at HEAD, what's locked, what's
   in flight. Include commit SHAs, manifest entry shape, locked
   parameter values.
2. **READ FIRST list, partitioned by access method.** PROJECT
   KNOWLEDGE (via `project_knowledge_search`): source code, docs,
   literature files. LOCAL ONLY (via Claude Code on the repo):
   runtime artifacts (trials.log, logs/*, git state). Do not list
   both under one header — that makes new chats search project
   knowledge for files that aren't there.
3. **PROJECT-KNOWLEDGE SYNC CHECKLIST as runnable commands.**
   `cd <repo> && git log -N --oneline` to verify HEAD,
   `cd <repo> && repomix` to refresh the bundle when commits
   landed, plus `ls -la <repo>/repomix-output.xml` and an
   enumerated list of files to re-upload. Prose like
   "repomix-output.xml current" fails the runnable-artifacts rule.
   Re-upload list must cover ALL standalone PK files (files outside
   repomix include patterns), not only files touched this session.
   Standalone files include: research/**, docs/strategies.md, and any
   scripts/ files uploaded separately. Check each against current repo
   state before listing as current.

Self-check before sending: Block 0 (standard opening) present.
Each of the three blocks present.

## Memory edits always permitted

Memory updates are user persistent state, not the chat's
deliverable. When the user requests a memory update, comply
directly via `memory_user_edits` — even when the chat's scope is
"report and stop" or the chat is mid-handoff. Memory edits are
NEVER scoped out by a handoff prompt. If a handoff prompt is
silent on memory, memory edits remain permitted.

Memory entries must be short context cues (1-2 sentences max).
Technical rules and implementation details belong in T3_procedural
or CLAUDE.md, not in memory. Long entries cause skimming and rule
violations in downstream chats.

## Project knowledge inspection

Before recommending uploads, deletes, or reorganization of project
knowledge, read `/mnt/project/` directly (`bash_tool` with `ls -la
/mnt/project/` or equivalent). Do not ask the user to confirm
state Claude can verify itself. When naming files for upload,
include the local repo path so the user knows where to find them.
Repomix bundle contents do not need duplicate standalone uploads
unless retrieval reliability for a specific filename-keyed
reference is in question.

## Claude Code prompt construction

When drafting a Claude Code prompt from a chat handoff:

1. Copy every numbered pre-trial gate, scoping constraint, and
   "must hold before X" item from the handoff prompt verbatim
   into the Claude Code prompt — not summarized, not paraphrased,
   not dropped because they "feel covered" by track scope.
   Constraints not in the prompt do not bind the agent.
2. Include explicit "proceed without asking permission for any
   action covered by CLAUDE.md autonomy rules" framing.
3. Pre-authorize the full scope upfront. Auto-accept-edits mode
   still pauses on non-edit actions; pre-authorization is what
   keeps the agent unblocked.
4. Do not write prompts requiring user approval mid-execution
   unless the action is on CLAUDE.md "Human only" list (commit /
   push / deploy / sacred-harness).

## Deliberation handoffs

After a chat-side decision, end with an exit-ramp step — not
rationale + stop. Surface implementation paths (ship now / defer
/ follow-on chat), recommend one, and list required follow-ups
(re-uploads, next handoffs, doc updates). The user should never
finish a deliberation wondering "now what?"

## Empirical-threshold autonomy

Runtime-detectable values are agent-decidable per CLAUDE.md mandate
F. Auto-calibrate via probe; the detection signal IS the value.
"Probe-plus-guard" ("STOP if N out of range") is human-in-the-loop
by another name. Wrong-looking output means probe design is wrong
(statistic, window); fix the design. Picking the statistic
(median / p95 / max) is an agent call — anchor on what the
structural hypothesis names. Sign-off remains commit / push /
deploy / sacred-harness only.

## Response format after Claude Code output

Terse summary. Do not list lines touched or explain code purpose
— the user reads the diff. Format:

- Edits land statement
- One-line per file changed (1-line description)
- Forward plan if applicable
- Stop

New-chat opening with handoff: brief "instructions read; we're at
X; plan Y" — not multi-paragraph confirmation. Do not pad with
rationale the user already has from the prompt.
