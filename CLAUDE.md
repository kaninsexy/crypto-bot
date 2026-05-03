# CLAUDE.md — Agent operating rules for crypto-bot

Last updated: 2026-05-03

This file is read by Claude Code and other agents when working in this repo.
Read it before starting any task.

READ FIRST at chat start: `docs/handoff_template.md` — workflow
procedure for chat handoffs, Claude Code prompt construction, and
project-knowledge management. CLAUDE.md holds project-wide
behavioral mandates; the handoff template holds task-specific
workflow that doesn't need to compete for attention in every chat.

## Project overview

Cryptocurrency trading bot running on OKX paper mode. Multi-strategy portfolio with
regime-aware Kelly sizing. Currently in Phase 4 (Phase 4.A Resurrection Batch + Phase 4.B
Funding-Rate Harvest exploration; Branch C selected as default). See `docs/MASTER_PLAN.md`
and `docs/bot_status.md` for current state.

## Core principles

- **Sacred-harness files (never edit; human approval required for any
  schema or content change).** Runtime artifacts and the holdout split
  are the audit-critical core. Changes to these break the
  multiple-testing correction or the holdout single-access guarantee:
  `backtest/trials.log`, `backtest/holdout_manifest.json`,
  `backtest/holdout_access.log`, and the schema of `backtest/holdout.py`.
- **Schema-stable code (edit cautiously; contract-preserving changes
  proceed, contract changes need approval).** Validation harness modules
  whose interfaces feed the sacred-harness files. Bug fixes that preserve
  function contracts (signatures, return shapes, schema fields) are agent-
  autonomous; changes that alter contracts require human approval:
  `backtest/cpcv.py`, `backtest/dsr.py`, `backtest/verdict.py`,
  `backtest/engine.py`, `backtest/trials.py`, `backtest/holdout.py`
  (implementation; schema is sacred per above).
- **Every experiment counts.** Every backtest, every parameter variation,
  every exploratory test appends a row to `trials.log`. Multiple-testing
  correction via DSR uses this count. Do not bypass.
- **Trial intentionality.** Commits to the repo are the deliberate human
  act that marks a trial as "this is what I tested, this is the variation
  I am claiming." Autonomous test execution does not extend to autonomous
  commits — the commit gate is what makes the trial record interpretable.
  See "Human only" below.
- **Paper mode is the guard.** `paper_mode=True` must remain the default.
  Any code path that would trigger real OKX API calls requires human
  approval.

## Agent autonomy rules

Agents decide autonomously when data clearly answers the question.
Agents consult with the human when the decision changes what's being tested
or affects money/deployment.
Agents commit autonomously with heredoc-embedded messages per mandate H.
Agents never push or deploy autonomously.

### Agent decides (no approval needed)

- Fix bugs in strategy implementations
- Propose and test parameter variations within theoretically-justified ranges
- **Run pre-justified test batches end-to-end without per-test approval**
  (see "Pre-justified test batch execution" below)
- Run backtests, CPCV, DSR computation
- Retire strategies when data is clearly negative (DSR well below threshold,
  no variation improves it within theoretical bounds)
- Empirically calibrate thresholds (DSR cutoff, CPCV path count, etc.)
- Research alternative pairs, regimes, or filters for a strategy
- Run exploratory tests on alternative pairs as additional data points
- Fix tooling, infrastructure, caches
- Investigate performance issues and implement fixes
- Archive retired experiments (move to `strategies/archive/`)

### Agent consults the human (present findings, wait for decision)

- Pair substitution (swap the canonical pair of a strategy)
- Adding a new strategy category not in the original portfolio
- Adding a new test or strategy variation not enumerated in `docs/MASTER_PLAN.md`
- Modifying the validation harness or `trials.log` schema
- Borderline retire/keep calls (DSR within ±0.05 of threshold on holdout)
- Scope changes that increase the multiple-testing count meaningfully
- Any permanent deletion of code, strategies, or data

### Human only (agents must not perform)

- Pushes to any remote
- Force operations (force-push, hard reset on main, etc.)
- Paper deploy to server
- Live deploy to production
- Capital or risk parameter changes
- Modifying `CLAUDE.md` itself or `docs/validation_framework.md`

> **Pre-authorization exception.** Claude Code may edit any file in
> this list when the user explicitly pre-authorizes the edit in the
> prompt's AUTONOMY section (e.g., "Kanin pre-authorizes edits to
> docs/validation_framework.md for this prompt scope"). The auto-
> accept-edits review-before-commit workflow IS the human-in-the-loop
> the rule protects. Without explicit pre-authorization, the default
> Human-only rule applies and the agent must refuse the edit and
> surface the restriction.

### Agent edits documents autonomously (no approval needed)

Every doc not in the "Human only" list above is agent-editable when the
data clearly answers the question. Specifically: `docs/MASTER_PLAN.md`,
`docs/bot_status.md`, `docs/open_questions.md`, `docs/strategies.md`,
`docs/research_log.md`, `docs/strategy_evidence_audit_2026-04-26.md`,
and any future audit or per-strategy doc. Sacred-harness rule (above)
covers runtime artifacts, not docs. Do not gate doc edits on judgment
that doesn't apply.

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

## Code conventions

- Python 3.12
- Use explicit `is None` / `== 0` checks; never `or` on numeric or DataFrame values
- `self._slots` not `self.slots` in `portfolio/manager.py`
- Never change `total_capital` semantics
- Server deploy uses `sudo bash -c` (NOT `sudo -u botuser`)
- `holdout_manifest.json` is source of truth for BOTH timeframe
  AND symbol/symbols/legs per strategy. When auditing a trial,
  check the manifest entry, not the variation_id label.
- `portfolio/regime_detector.py` is the regime detector module
  (NOT `strategies/regime.py`). Public API: `RegimeDetector`
  class with `detect(df) -> RegimeReading` and `current_regime`
  property; module exports `REGIME_STRONG_BULL`/`BULL`/`RANGE`/
  `VOLATILE`/`BEAR`/`CRASH` constants and `ALL_REGIMES` list.

## When to use which tool

- **Claude Code.** All implementation work, in chunks. Multi-file or
  single-file, simple or complex — CC is the route. Cowork retired
  2026-05-03; one tool eliminates the routing decision.
- **Chat with human.** Decisions, design reviews, retire/keep calls,
  scope choices. Plan together; execute via Claude Code.

## Behavioral mandates

These rules govern how agents communicate work, not what work to do.

### Runnable artifacts only

If a step is executable (shell, git, str_replace, create_file, prompt for
another agent), provide the runnable artifact, not a prose description.
Test: could the user copy-paste the response and execute it? If no,
rewrite as code. Prose is reserved for decisions, trade-offs, and
explanations — never for actions. "You should run X" is wrong; the
command for X is right.

### Bundle by default

When a goal needs N known actions, deliver all N in one response. Multiple
code blocks per response are fine. Independent actions bundle together;
only hard sequential dependencies (where action 2 needs action 1's output)
justify splitting across turns. Dribbling fixes one-per-turn wastes context
re-establishment cost and forces the user to carry state.

### Single-message completeness

Before sending: does this message contain everything the user needs to act
on the current goal without coming back to ask? If "now do X" is
predictable, X belongs in the current response.

### Self-execute mechanically-derivable steps

Anything Claude can do with available tools (`bash_tool`,
`conversation_search`, `project_knowledge_search`, `view`,
`str_replace`, `create_file`), Claude does — never routes through
the user as "paste this output and I'll respond." Includes checking
`/mnt/project/` state, comparing repo to project knowledge, reading
file headers to verify scope, splitting hunks, staging git
operations. Routing mechanical inspection through the user is the
broadest version of the bundle-violation pattern.

### Commit and shell-bundle rule

(1) Every "stop for commit" surface bundles the runnable git
command (scoped `git add` + `git commit` with message composed from
the work just done) in the same response. The user should never
need to ask for the commit code separately.

(2) Independent shell commands sharing a goal go in ONE bash block,
not N. Three commits, three test runs, three stagings = one block,
chained via `&&` or sequential lines under one fence. Splitting an
N-action shell sequence into N blocks violates bundle-by-default
even when each block is technically runnable.

### Don't pre-write downstream content

After Claude Code reports completion, deliver verification (tests
pass/fail, flagged items) and stop. Do not pre-write doc edits,
commit messages, or commit-status checklists unless explicitly
asked. Doc updates and commit content are the user's job at commit
time. Distinct from the autonomy-sign-off rule: that one is about
not gating on permission, this one is about not producing
unsolicited downstream content.

### Pushback re-check

When the user pushes back ("is X right?", "shouldn't this be Y?"),
do NOT immediately validate or flip the answer. First re-read
evidence (handoff verbatim, project files, past chats via
`conversation_search`). Then judge if pushback is right, partially
right, or wrong. Reflexive flipping creates wrong-fix loops. If
right, say so after verifying. If partially right, separate right
from wrong. Better to take a turn re-checking than flip twice.

### Missing-or-stale evidence

When project files contradict the handoff prompt, when load-bearing
fields are absent (manifest schema slots, commit hashes, citations),
or when sources disagree on a locked decision: STOP. Do not fill
the gap with judgment, do not assume the newer-looking source wins.
Surface the discrepancy explicitly; resolve via
`conversation_search` if past chats answer, else ask the user.
Does NOT fire on routine search-empties or expected lag
(bot_status updates, log appends).

### Drift prevention

Seven mandates persisted from chat 2026-04-30 audit, where a
Phase 4.B Track C drift bug surfaced a class of failures: the
constraint existed in chat memory or a handoff prompt but
not in any project file, and the agent producing work didn't
read the right evidence to catch the drift.

**A. Read evidence end-to-end before acting.** When working on
a strategy, trial, or harness component, read the following
end-to-end before responding or acting (each is a separate
authority and skipping any of them produces drift):
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

**D. Pre-trial gates carry verbatim into Claude Code prompts
from chat handoffs.** When the chat agent drafts a Claude
Code prompt from a handoff prompt, every numbered pre-trial
gate, scoping constraint, and "must hold before X" item from
the handoff copies verbatim into the Claude Code prompt — not
summarized, not paraphrased, not dropped because they "feel
covered" by track scope. Constraints not in the prompt do not
bind the agent. Today's Track C drift happened because gate
#8 ("first dev_cpcv trial single-pair before adding alts")
was in the chat handoff prompt but not in the Claude Code
prompt, so the literature stub the agent produced drifted to
multi-pair selection without violating any constraint
visible to it.

**E. Review agent output against original scoping, not just
against tests.** Two passes: (1) Claude Code self-check
before reporting completion — re-read the handoff prompt's
pre-trial gates and verify each produced artifact (literature
file, spec doc, manifest entry, code module) satisfies them.
A test-passing artifact that contradicts a scoping decision
is still wrong; surface as a drift flag, do not report
completion as clean. (2) Chat agent review after Claude Code
reports — open the produced artifact and compare substantive
content against each gate from the handoff. Drift detection
is the chat agent's job, not the implementation agent's, but
the implementation agent's self-check makes drift visible
earlier. Today's review missed Track C drift because the
review was against test results and Claude Code's own report,
not against the literature file's actual content vs. gate #8.

**F. Decision authority — design choices are agent calls.**
When the data answers the question (project files + past
chats + handoff prompt), the agent decides and executes — no
option-A/B/C menus, no "pick one and confirm" loops back to
the human. Sign-off is reserved exclusively for: git push, deploy,
and sacred-harness file schema changes per CLAUDE.md "Human only"
list. Design choices like which
abstraction layer, dispatch pattern, manifest field shape,
module location, naming convention are agent calls when the
evidence answers them. Bit-by-bit sign-off cycles waste time
and tokens on already-planned work and create the failure
mode where a chat fragments a decided plan into N approval
rounds. The user has stated this preference repeatedly; the
mandate persists it in the repo so it doesn't depend on
chat-side memory.

**G. Trial intentionality boundary at push, not at commit.**
Trial intentionality is preserved by mandatory heredoc-embedded
commit messages with full context, enforced at the hook layer
(commit-heredoc-required.sh, sacred-block.sh, commit-msg,
pre-commit). The boundary moved at architecture.md commit
831be25 (chat 2026-05-03 deliberation): mandatory message
embedding via hooks is safer than manual commit typing, which
twice produced empty commits when humans skipped the editor.
Agents commit autonomously — git add, git commit with heredoc
message — and stop short of git push. The deliberate human
act is the push, where remote state changes and audit exposure
begin. This is the same boundary as mandate F: design decisions
do not need sign-off, but the irreversible step (push, deploy)
does.

Historical drift cases and worked examples: see
`docs/drift_history.md` (do not load unless investigating a
specific past failure pattern).

### Response format after Claude Code output

Terse summary. Do not list which lines were touched or explain code
purpose — the user reads the diff. Format: edits-land statement, one-line
per file changed, forward-plan if applicable, stop.

## Execution autonomy (default)

Claude Code proceeds without asking for consent on (default mode, applies
always — user not being physically present is not a special case):
- File edits within the repo
- Running tests, linters, and verification scripts
- Reading any non-secret file
- Installing dependencies into the project venv
- Running diagnostic commands such as grep, git status, git diff, git log
- Iterating on a fix until verification passes
- Executing pre-justified test batches enumerated in `docs/MASTER_PLAN.md`
  end-to-end (see "Pre-justified test batch execution" above)

Claude Code MUST NOT proceed without explicit approval on:
- git push, regardless of branch
- Any deployment command (DigitalOcean, Binance API, production env edits)
- Edits to sacred-harness files (see Core principles for the canonical two-tier list; the runtime artifacts + holdout schema are the never-edit tier)
- Edits to .env or any secrets file
- Schema changes to validation framework artifacts

When verification passes, commit autonomously with a heredoc-embedded message per mandate H, surface git log -1 plus test output, and stop. The user reviews the commit and pushes manually if/when ready.