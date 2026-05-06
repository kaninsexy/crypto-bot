# CLAUDE.md — Agent operating rules for crypto-bot

Last updated: 2026-05-06

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
- **Sacred-harness documents (never edit without explicit pre-authorization).**
  Project-constitution documents whose content is the canonical record of
  agent rules, plan state, validation methodology, and architectural design.
  Changes require human pre-authorization in the prompt's AUTONOMY section
  (per the Pre-authorization exception below) OR a human-only edit:
  `CLAUDE.md`, `docs/MASTER_PLAN.md`, `docs/architecture.md`,
  `docs/validation_framework.md`. The existing `.claude/hooks/sacred-block.sh`
  blocks edits by default; pre-authorization works via the
  `SACRED_OVERRIDE_FILES` env var on the CC invocation line, which lives
  only for that session.
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
  (see `.claude/rules/backtest.md` for full discipline rules)
- Run backtests, CPCV, DSR computation
- Retire strategies when data is clearly negative (DSR well below threshold,
  no variation improves it within theoretical bounds)
- Empirically calibrate thresholds (DSR cutoff, CPCV path count, etc.)
- Research alternative pairs, regimes, or filters for a strategy
- Run exploratory tests on alternative pairs as additional data points
- Fix tooling, infrastructure, caches
- Investigate performance issues and implement fixes
- Archive retired experiments (move to `strategies/archive/`)
- Add new strategy variations to `backtest/trial_queue.json` when `scripts/propose_next_variation.py` produces a qualifying proposal (citation score >= 3.0, not previously tested per trials.log)

### Agent consults the human (present findings, wait for decision)

- Pair substitution (swap the canonical pair of a strategy)
- Adding a new strategy category not in the original portfolio
- Adding a new test or strategy variation not enumerated in `docs/MASTER_PLAN.md` — UNLESS the addition comes from `scripts/propose_next_variation.py` with a citation quality score >= 3.0 (≥3 qualifying peer-reviewed or SSRN citations) AND the variation has not been tested before (checked against trials.log). In that case the proposal agent adds the item to the queue autonomously; the human reviews trial RESULTS after the run, not hypotheses before.
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
- Modifying sacred-harness documents (`CLAUDE.md`, `docs/MASTER_PLAN.md`,
  `docs/architecture.md`, `docs/validation_framework.md`) without
  explicit pre-authorization in the prompt's AUTONOMY section. The
  Pre-authorization exception below covers the bypass mechanism.

> **Pre-authorization exception.** Claude Code may edit any file in
> this list when the user explicitly pre-authorizes the edit in the
> prompt's AUTONOMY section (e.g., "Kanin pre-authorizes edits to
> docs/validation_framework.md for this prompt scope"). The auto-
> accept-edits review-before-commit workflow IS the human-in-the-loop
> the rule protects. Without explicit pre-authorization, the default
> Human-only rule applies and the agent must refuse the edit and
> surface the restriction.

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

### Agent edits documents autonomously (no approval needed)

Every doc not in the "Human only" list above is agent-editable when the
data clearly answers the question. Specifically: `docs/MASTER_PLAN.md`,
`docs/bot_status.md`, `docs/open_questions.md`, `docs/strategies.md`,
`docs/research_log.md`, `docs/strategy_evidence_audit_2026-04-26.md`,
and any future audit or per-strategy doc. Sacred-harness rule (above)
covers runtime artifacts, not docs. Do not gate doc edits on judgment
that doesn't apply.

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

> Path-scoped rules: `.claude/rules/backtest.md` (trial
> discipline, CPCVError, evidence mandates A-C) and
> `.claude/rules/prompts.md` (CC prompt construction, mandates
> D-E). These load automatically when CC touches the relevant
> paths.

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
  end-to-end (see `.claude/rules/backtest.md`)

Claude Code MUST NOT proceed without explicit approval on:
- git push, regardless of branch
- Any deployment command (DigitalOcean, Binance API, production env edits)
- Edits to sacred-harness runtime artifacts and holdout schema (see Core principles "Sacred-harness files" bullet for the canonical list)
- Edits to sacred-harness documents (CLAUDE.md, MASTER_PLAN.md, architecture.md, validation_framework.md) without SACRED_OVERRIDE_FILES env-var pre-authorization on the invocation line
- Edits to .env or any secrets file
- Schema changes to validation framework artifacts

When verification passes, commit autonomously with a heredoc-embedded message per mandate H, surface git log -1 plus test output, and stop. The user reviews the commit and pushes manually if/when ready.