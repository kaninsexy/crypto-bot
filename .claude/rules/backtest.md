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

## Discovery / confirmation split

Adopted from `docs/research_revival_2026-09.md` §C.2, itself adapted
from Harvey–Liu (t > 3 for multiply-tested claims) and
Arnott–Harvey–Markowitz (2019) on pre-registration, trial documentation
and OOS awareness. Applies to substrates whose manifest entry declares a
`discovery_end`; every other strategy is unaffected and every existing
rule in this file continues to bind.

**1. Discovery window.** For a substrate with a declared discovery
window (Binance UM: 2020-01-01 → 2022-12-31, sealed by manifest, never
read by any prior trial), exploratory analysis is permitted WITHOUT a
`trials.log` row, under three conditions:

  (a) every screen run is logged in a discovery ledger,
      `research/discovery/<family>.md` (signal, universe rule, horizon,
      statistic, value, t-stat, N, data range, script + commit,
      conclusion);
  (b) the ledger's row count `N_disc` is carried into the confirmation
      trial's pre-registration and applied as an additional
      Bonferroni-style haircut on the confirmation DSR;
  (c) discovery never reads 2023+ data. Screens hard-assert this and
      abort on violation.

A discovery screen writes NO trials.log row; the ledger row is its
record. A confirmation trial writes exactly one full_cpcv row and
carries N_disc from the ledger into its pre-registration, and the
confirmation DSR is additionally haircut by N_disc.

**2. Confirmation window.** Confirmation = 2023-01-01 → 2025-05-01
(dev, counted in `trials.log` exactly as today) and holdout =
2025-05-01 → 2026-08-31, never read until `final_gate`. For the Binance
UM substrate the holdout is genuinely virgin — no prior trial touched
Binance UM data — with the standing disclosure that the agents KNOW the
2025-10-10 cascade happened and that this knowledge is not removable.

**3. Pre-registration content.** Extends the literature-file template.
Before a confirmation trial runs, `research/<strategy>-literature.md`
must state: the mechanism in one paragraph; the counterparty and why
they pay; the expected SR with the discovery number that supports it;
turnover and cost at the OKX perp taker fee; the kill test and its
threshold; and `N_disc`.

**4. Forward stage.** Only designs whose dev SR makes the 12-month
forward test decisive (SR ≥ 2) proceed. Paper deploy on OKX perps;
success = PSR ≥ 0.9 after 12 months; fail-fast if realised SR < 0.5
after 6.

**5. Pre-flight power gate.** No screen or confirmation trial may run until
its minimum detectable effect (MDE) at the pre-registered significance bar is
computed and recorded beside the pre-registration.

    MDE = t_bar × σ / √N_expected

with σ the UNCONDITIONAL dispersion of the outcome variable over the same
window and universe — a design input, never the conditional statistic the
test is about. If MDE exceeds the pre-registered effect threshold, the test
CANNOT pass at the effect size it was designed to detect: the run is
REFUSED, and the universe, horizon or window is widened until MDE ≤
threshold, or the family is closed as untestable on the available data.

Widening scope to satisfy this gate is COMPLETING the pre-registered test,
not a new screen — `N_disc` is unchanged. Narrowing scope for cost is what
this gate exists to catch.

*Worked example, 2026-09-02 (deleveraging reversal).* The screen was about to
run on 30 symbols to bound a download cost. Unconditional 3-day return σ over
the window was 9.69 %, giving ~189 events and MDE = 3 × 9.69 / √189 =
**2.11 %** against a pre-registered **1.5 %** bar. A TRUE 1.5 % effect would
have returned t ≈ 2.13 and been logged "killed" — the ledger row would have
recorded the sample size, not the substrate. At 100 symbols (~630 events) MDE
is 1.16 % and the same true effect returns t ≈ 3.88. The screen ran at ≥ 100.

An underpowered null is the most expensive kind of wrong answer this project
can produce: it looks exactly like evidence, it is cheap to generate, and it
closes a question that was never actually asked. It is also invisible to
every other gate — CPCV, DSR and the verdict tree all take N as given.

**6. Fetch standard.** Archive prefetches use a `ThreadPoolExecutor`
(~24 workers) against
`https://s3-ap-northeast-1.amazonaws.com/data.binance.vision`, one task per
zip. These are independent GETs of static objects, so the serial bottleneck is
round-trip latency, not the archive or any rate limit. **A serial fetch that
would exceed one hour is a bug, not a budget** — measured 2026-09-02, the same
job ran at 1.5 req/s serially (~11 h) and 39 req/s threaded (~18 min). Treat a
multi-hour download estimate as a signal to fix the fetcher, never as a reason
to shrink the universe: shrinking the universe to fit a slow fetcher is
precisely how item 5's underpowered null gets created.

**7. Data-defects registry.** Any screen or trial reading a substrate must
first consult that substrate's data-defects registry and apply its guards; a
defect list that exists but is not applied is the same failure as no list.
Registry for the Binance UM archive: `docs/data_defects_binance_um.md`
(guards: `data.binance_vision_um.clean_metrics`, `defect_report`). This is
item 7 because run 2's deleveraging screen was about to count feed gaps as
liquidation events, and the defect was ALREADY documented in the recon — it
was caught only because someone remembered reading it. Memory is not a
control.

**What this does NOT relax.** The 20-variation cap, the
3-consecutive-failure escalation, the no-p-hacking rule for
confirmation-stage variations, the archive-by-default rule, the
compute-budget circuit breaker, and the human-only push/deploy boundary
all bind unchanged. Discovery screens may not be used to select a
confirmation hypothesis after the fact by re-reading a screen's output
window: the kill test, its threshold, and any pre-specified event
window are frozen in the ledger's header before the screen runs, copied
from the batch table in `docs/MASTER_PLAN.md`. Moving a threshold or a
window after seeing the statistic is p-hacking regardless of which
window the data came from.

**Budget for the first batch.** Family `perp-structural`, at most 5
`full_cpcv` rows across the three families (1 per family + 2 variation
slots), 3-consecutive-failure stop.

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

## Proposal agent queue exception

Adding a new test or strategy variation not enumerated in `docs/MASTER_PLAN.md` — UNLESS the addition comes from `scripts/propose_next_variation.py` with a citation quality score >= 3.0 (≥3 qualifying peer-reviewed or SSRN citations) AND the variation has not been tested before (checked against trials.log). In that case the proposal agent adds the item to the queue autonomously; the human reviews trial RESULTS after the run, not hypotheses before.
