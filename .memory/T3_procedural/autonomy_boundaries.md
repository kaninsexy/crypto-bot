# Autonomy boundaries — Mandates A and F

## Decision authority by category

### Agent decides (no approval needed)
When the data clearly answers the question (project files + past chats +
handoff prompt), the agent decides and executes. No options-menu,
no "pick one and confirm" loops back to the human.

- Fix bugs in strategy implementations.
- Propose and test parameter variations within theoretically-justified
  ranges (subject to no-p-hacking).
- Run pre-justified test batches enumerated in MASTER_PLAN.md
  end-to-end without per-test approval.
- Run backtests, CPCV, DSR computation.
- Retire strategies when data is clearly negative.
- Empirically calibrate thresholds (DSR cutoff, CPCV path count, etc.).
- Research alternative pairs, regimes, or filters.
- Fix tooling, infrastructure, caches.
- Investigate performance issues and implement fixes.
- Archive retired experiments to `strategies/archive/`.
- Edit any doc not in the Human-only list (bot_status.md,
  open_questions.md, strategies.md, research_log.md, audits,
  per-strategy docs).

### Agent consults the human (present findings, wait for decision)
- Pair substitution (swap the canonical pair of a strategy).
- Adding a new strategy category not in the original portfolio.
- Adding a new test or strategy variation not in MASTER_PLAN.md.
- Modifying the validation harness or trials.log schema.
- Borderline retire/keep calls (DSR within ±0.05 of threshold on holdout).
- Scope changes that increase the multiple-testing count meaningfully.
- Any permanent deletion of code, strategies, or data.

### Human only (agents must not perform)
- git push, force operations.
- Paper deploy to server, live deploy to production.
- Capital or risk parameter changes.
- Edits to CLAUDE.md, MASTER_PLAN.md, validation_framework.md
  (unless pre-authorized in AUTONOMY section).
- Edits to sacred-harness Tier 1 (see sacred_harness.md).

## Mandate F — no options-menu
When evidence answers the question, recommend ONE option with reasoning
and execute. If truly torn, list the two and pick one anyway with the
deciding factor named. Do not fragment a decided plan into N approval
rounds.

Sign-off is reserved exclusively for: git push, deploy, and
sacred-harness Tier 1 schema changes. Design choices (abstraction layer,
dispatch pattern, manifest field shape, module location, naming) are
agent calls when the evidence answers them.

## Mandate A — read-before-respond
Before claiming knowledge of a file's content, Read it this turn. This
applies in chat and in Claude Code. Pattern-matching from chat memory or
variation names is the recurring drift failure mode. Past chats are
evidence — call `conversation_search` and/or `recent_chats` before
answering questions about settled decisions, harness state, or prior
rationale.

## Trial intentionality
Trial intentionality is preserved by mandatory heredoc-embedded commit
messages enforced at the hook layer. The boundary fires at git push,
not at git commit. Agents may run trials, append rows to trials.log
via record_trial, edit literature files, update bot_status.md, and
commit autonomously with heredoc messages — but stop short of git
push. The user reviews commit history and pushes manually if/when
ready.

See also: CLAUDE.md "Agent autonomy rules", "Drift prevention"
mandates A and F; architecture.md §E.1 mandates A and F.
