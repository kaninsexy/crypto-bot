# No p-hacking — Mandate P

## Rule
Every parameter variation tested must cite an explicit theoretical source
(peer-reviewed paper, validated blog post, or written hypothesis in
`research/<strategy>-literature.md`) BEFORE the trial runs.

## Why
Every variation appends to `trials.log` and inflates the multiple-testing
correction in Deflated Sharpe Ratio. A grid search of 50 unjustified
parameter combinations does not produce a "best Sharpe" — it produces 50
trials whose DSR haircut renders any survivor statistically insignificant.

## What this prohibits
- Hyperparameter searches over numeric ranges without per-variation
  justification, even if the search space is bounded.
- Running a variation and citing the source retroactively.
- "Tweaking until it works" loops.

## What this permits
- Pre-justified test batches enumerated in `docs/MASTER_PLAN.md` (e.g.,
  Phase 4.A Resurrection Batch). The citation gate is passed at batch
  entry — running each enumerated row is execution of an
  already-justified plan, not new exploration.
- Variations beyond a starting hypothesis IF the agent first proposes
  the variation, cites a source, and the source justification is sound.

## Hard limits
- **20-variation cap per strategy.** If no variation survives DSR within
  20 attempts, the strategy does not have edge. Cap applies whether
  variations run via single-prompt manual triggers or pre-justified
  batch — autonomy does not raise the cap.
- **3-consecutive-failure escalation.** After 3 consecutive failed
  hypotheses on a strategy, stop and consult before attempting a 4th.
  Likely indicates the edge theory is wrong, not that the next tweak
  will find it. Applies inside batch execution too.

## When unsure
Consult the human before running. The cost of one extra clarification
turn is much less than the cost of an unjustified trial polluting
trials.log forever.

## Enforcement
- Layer 1 (advisory): this file + CLAUDE.md "No p-hacking rule" section.
- Layer 2 (deterministic): `citation-required.sh` PreToolUse hook on
  Proposer subagents — blocks WebFetch/WebSearch unless the prompt
  declares `citation_key:`.
- Layer 4 (commit gate): `pre-commit` greps staged diffs for new magic
  numbers in strategy parameter files; aborts if no `# CITATION: <key>`
  comment within 3 lines.

See also: CLAUDE.md "No p-hacking rule"; architecture.md §E.1 mandate P.
