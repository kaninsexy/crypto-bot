# CLAUDE.md — Agent operating rules for crypto-bot

Last updated: 2026-04-29

This file is read by Claude Code and other agents when working in this repo.
Read it before starting any task.

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
Agents never commit, push, or deploy autonomously.

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

- Commits to git
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
- **Commits.** Trial intentionality is enforced by the human-commits-
  only rule. Running tests autonomously does not extend to committing
  the results autonomously. The agent surfaces the diff + verdict and
  the user commits manually.

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

## When to use which tool

- **Claude Code (default).** All multi-file work, iterative implementation,
  backtests, doc edits across multiple files, anything requiring cross-file
  context. This is the default — when in doubt, route here.
- **Cowork.** Small, contained, well-specified single-file fixes only.
  Not multi-file doc updates; those go to Claude Code.
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

### Pre-trial gates persist in project files, not just chat handoffs

When a chat-level scoping decision creates a constraint on
future variation design ("Variation #1 must be single-pair",
"manifest schema extends additively only", etc.), the
constraint is persisted in the relevant per-strategy
literature file under `research/` AND in `docs/MASTER_PLAN.md`
before the chat closes. Chat handoff prompts are ephemeral;
project files are durable. A gate that lives only in a
handoff prompt will be lost the moment a future chat starts
without that prompt.

The persistence pattern: each `research/<strategy>-
literature.md` file carries a "Pre-trial gates (locked)"
section near the top listing every locked constraint with
source citation (chat date, venue chat, MASTER_PLAN section).
Variation rows below reference these gates explicitly. A
Variation row that contradicts a gate is a drift bug — caught
by reading the literature file end-to-end before any
variation work, not by re-reading the chat handoff.

Today's drift case (2026-04-30 chat): Phase 4.B venue scoping
locked gate #8 ("first dev_cpcv trial single-pair before adding
alts") in chat 2026-04-29. Track C produced a literature file
with Variation #1 = top-1-from-basket because the gate wasn't
persisted in any project file and the Claude Code prompt
didn't carry it forward. Persistence rule prevents this class
of drift.

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
- git commit, regardless of message quality
- git push, regardless of branch
- Any deployment command (DigitalOcean, Binance API, production env edits)
- Edits to sacred-harness files (see Core principles for the canonical two-tier list; the runtime artifacts + holdout schema are the never-edit tier)
- Edits to .env or any secrets file
- Schema changes to validation framework artifacts

When verification passes, surface the diff plus test output and stop. The user reviews and commits manually.