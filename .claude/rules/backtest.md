---
globs: ["backtest/**", "scripts/run_*.py", "strategies/**"]
---

# Backtest and trial rules — loaded for backtest/, scripts/run_*, strategies/

### Pre-justified test batch execution

When a test set is enumerated in `docs/MASTER_PLAN.md` with named source
citations per the no-p-hacking rule (e.g., the Phase 4.A Resurrection
Batch table), the agent runs the full batch autonomously without
per-test chat approval. Justification + source is the gate that
protects against p-hacking; the gate is passed when the test enters the
plan, not when each individual run is triggered. Per-test chat approval
would be friction that protects nothing.

**This applies to:**
- Running each enumerated starting hypothesis as a CPCV trial
- Appending the resulting row to `trials.log` per existing schema
- Surfacing the verdict-tree result and proceeding to the next hypothesis
  in the batch

**This does NOT extend to:**
- **Variations beyond the starting hypothesis if it fails.** Variation #2
  of a strategy still requires a written hypothesis citing a source
  before the trial runs (no-p-hacking rule). The agent may propose the
  variation, run it if the source justification is sound, and continue
  — but stops at the 20-variation cap or after 3 consecutive failed
  hypotheses, whichever comes first (see "Iteration cap" and
  "Consecutive failure escalation" below).
- **Tests outside the enumerated batch.** Adding a strategy or test
  not in MASTER_PLAN.md still requires the consult-the-human rule.
- **Push and deploy.** Trial intentionality is preserved by mandatory
  heredoc-embedded commit messages enforced at the hook layer
  (commit-heredoc-required.sh, sacred-block.sh, pre-commit). Agents
  commit autonomously per mandate G; agents stop short of git push
  and any deploy command. The deliberate human act is the push,
  where remote state changes and audit exposure begin.

The 20-variation cap, 3-failure escalation rule, and no-p-hacking rule
all remain in force during pre-justified batch execution. The batch
permission is about removing per-test chat friction, not about
removing the discipline rules that make trials.log interpretable.

**CPCVError handling (mandatory in all trial scripts):** Every call to
`run_cpcv_multi()` must be wrapped in `try/except CPCVError`. On catch:
call `_trials.record_trial()` with `verdict="retire"`, `sr_observed=nan`,
`n_trades=0`, and a notes string containing the CPCVError message, then
`return 0`. This ensures insufficient-trades failures produce a clean
trial row and a `done` queue status instead of a crash and an `error`
status requiring manual cleanup.

**A. Read evidence end-to-end before acting.** When working on
a strategy, trial, or harness component, read the following
end-to-end before responding or acting (each is a separate
authority and skipping any of them produces drift):

Repomix beats standalone uploads: repomix-output.xml reflects the
current repo state for backtest/, strategies/, portfolio/, scripts/;
standalone project knowledge files may lag by one or more commits.
Search repomix first; use standalone uploads only for files repomix
excludes (research/, most docs/).

  1. `research/<strategy>-literature.md` — hypothesis-of-
     record + locked pre-trial gates + Variation #1/#N rows
  2. `backtest/holdout_manifest.json` entry — substrate
     truth (timeframe, symbol/symbols/legs, dev/holdout
     boundaries)
  3. `backtest/trials.log` rows for the strategy — ground
     truth for what's been tested (variation_id, params_hash,
     observed_sharpe, distribution stats, smoke vs full_cpcv
     tagging, supersession status)
  4. `docs/bot_status.md` row — running results table +
     forensic links
  5. `docs/strategies.md` section — Phase 3c verdict + 3-year
     diagnosis + Phase 4.A outcome subsection if applicable
  6. `docs/research_log.md` relevant section — *why* the
     hypothesis was chosen, especially "AI/algo trading
     viability and strategy-archetype evidence (consolidated
     2026-04-29)" for resurrection-batch hypotheses, and the
     venue/tax sections for Phase 4.B work
Do not pattern-match from variation names or chat-memory
summaries. Variation names describe intent ("phase4a-daily-
resurrection-v1"); the literature file describes what was
actually tested. The chat 2026-04-30 audit-loop happened
because variation names were treated as if they specified
harness behavior; they don't.

  7. Past chats are part of the evidence. Before answering
     questions about what was decided, why a choice was made,
     what state the harness is in, or whether a prior decision
     still holds — call `conversation_search` and/or
     `recent_chats`. Do not answer from `/mnt/project/` files
     alone; scoping decisions, pre-trial gates, and rationale
     often live in chats and don't always make it into
     `MASTER_PLAN.md`. Single-source-from-project-files is a
     recurring failure mode.

**B. holdout_manifest.json is source of truth for substrate.**
Per-strategy timeframe AND symbol/symbols/legs are both in
the manifest entry, not in code. When auditing a trial or
designing a new entry, check the manifest's timeframe AND
the manifest's symbol/symbols/legs against the strategy's
hypothesis-of-record. Mismatch is drift; surface and fix the
manifest before re-running, not after. Per the timeframe-per-
strategy principle in MASTER_PLAN.md, the same applies to
pair/basket — there is no global project pair, just as there
is no global project timeframe.

**C. Pre-trial gates persist in project files, not just chat
handoffs.** Chat-level scoping decisions that constrain
future variation design ("Variation #1 must be single-pair",
"manifest schema extends additively only", etc.) are
persisted in the relevant per-strategy literature file under
`research/` AND in `docs/MASTER_PLAN.md` before the chat
closes. Each `research/<strategy>-literature.md` carries a
"Pre-trial gates (locked)" section near the top listing
every locked constraint with source citation. Variation rows
reference these gates; rows that contradict a gate are drift
bugs caught by reading the literature end-to-end.

## Safety guardrails

### Compute budget circuit breaker

If a single strategy's iteration exceeds 4 hours of PC compute without converging
to a surviving candidate, stop and report. Do not grind indefinitely on a
strategy that isn't going to work.

### Archive by default, delete only with approval

When retiring a strategy, move its files to `strategies/archive/<strategy>/` with
a kill report documenting why. Do not delete unless explicitly approved by the
human. Reversibility is cheap; lost work is not.

### Iteration cap per strategy

Maximum 20 parameter variations per strategy before the strategy is retired.
Prevents p-hacking via unlimited iteration. If no variation passes DSR within
20 attempts, the strategy does not have edge. **This cap applies whether
variations are run via single-prompt manual triggers or via pre-justified
batch execution — autonomy does not raise the cap.**

### Consecutive failure escalation

If 3 consecutive variations have failed their hypothesis, stop and consult before
attempting a 4th. Likely indicates the strategy's edge theory is wrong, not that
the next tweak will find it. **This applies in batch execution: after 3
consecutive failures, the agent stops the batch on that strategy and
surfaces the failure pattern, regardless of how many starting-hypothesis
slots remain.**

## No p-hacking rule

Agents may only propose parameter variations that have an explicit theoretical
justification citing a source (paper, validated blog post, or a written
hypothesis documented in `research/<strategy>-literature.md`). Hyperparameter
searches over numeric ranges without per-variation justification are prohibited
— even if the search space is bounded.

This rule applies because every tested variation appends to `trials.log` and
inflates the multiple-testing correction in Deflated Sharpe Ratio. An agent
running a grid search of 50 parameter combinations does not produce a "best
Sharpe" — it produces 50 trials whose DSR haircut makes any result
statistically insignificant.

Pre-justified batches enumerated in `docs/MASTER_PLAN.md` satisfy this rule
*at batch entry* — each row in the resurrection table cites its source,
which is what the rule requires. Running the batch is execution of an
already-justified plan, not new exploration. Variations beyond the
enumerated starting hypothesis are new exploration and require fresh
per-variation justification before the trial runs.

Agents unsure whether a proposed test violates this rule must consult before
running it.

## Trial queue orchestrator exception

> **Trial queue orchestrator exception.** `scripts/run_trial_queue.py`
> may commit autonomously, scoped strictly to: `backtest/trials.log`
> (row already appended by the trial script before the orchestrator
> runs), `docs/strategies.md` (trial outcome subsection update),
> `research/<strategy>-literature.md` or `research/<substrate>-
> literature.md` (outcome row in the variation table), and
> `backtest/trial_queue.json` (status field update only). The
> orchestrator MUST NOT commit harness code, scripts, sacred-harness
> files, `CLAUDE.md`, `docs/MASTER_PLAN.md`, or any file outside the
> above list. Commit is gated on: (1) trial script exit code 0,
> (2) JSON summary block successfully parsed, (3) `git diff --name-only
> --cached` containing only files in the permitted list — if any file
> is outside the list, unstage everything, email the violation, and
> abort the commit. Commit message format:
> `trials: <strategy_id> <variation_id> <verdict>`. This exception
> does not extend to git push; push remains human-only.
