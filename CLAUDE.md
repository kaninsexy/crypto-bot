# CLAUDE.md — Agent operating rules for crypto-bot

Last updated: 2026-04-25

This file is read by Claude Code and other agents when working in this repo.
Read it before starting any task.

## Project overview

Cryptocurrency trading bot running on OKX paper mode. Multi-strategy portfolio with
regime-aware Kelly sizing. Currently in Phase 3 (validation framework buildout +
per-strategy rescue). See `docs/MASTER_PLAN.md` and `docs/bot_status.md` for
current state.

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
- Modifying `CLAUDE.md` itself, `docs/MASTER_PLAN.md`, or `docs/validation_framework.md`

> **Pre-authorization exception.** Claude Code may edit any file in
> this list when the user explicitly pre-authorizes the edit in the
> prompt's AUTONOMY section (e.g., "Kanin pre-authorizes edits to
> docs/MASTER_PLAN.md for this prompt scope"). The auto-accept-edits
> review-before-commit workflow IS the human-in-the-loop the rule
> protects. Without explicit pre-authorization, the default Human-only
> rule applies and the agent must refuse the edit and surface the
> restriction.

### Agent edits documents autonomously (no approval needed)

Every doc not in the "Human only" list above is agent-editable when the
data clearly answers the question. Specifically: `docs/bot_status.md`,
`docs/open_questions.md`, `docs/strategies.md`, `docs/research_log.md`,
`docs/strategy_evidence_audit_2026-04-26.md`, and any future audit or
per-strategy doc. Sacred-harness rule (above) covers runtime artifacts,
not docs. Do not gate doc edits on judgment that doesn't apply.

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
20 attempts, the strategy does not have edge.

### Consecutive failure escalation

If 3 consecutive variations have failed their hypothesis, stop and consult before
attempting a 4th. Likely indicates the strategy's edge theory is wrong, not that
the next tweak will find it.

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
- Running diagnostic commands such as grep, git status, git diff, git log
- Iterating on a fix until verification passes

Claude Code MUST NOT proceed without explicit approval on:
- git commit, regardless of message quality
- git push, regardless of branch
- Any deployment command (DigitalOcean, Binance API, production env edits)
- Edits to sacred-harness files (see Core principles for the canonical two-tier list; the runtime artifacts + holdout schema are the never-edit tier)
- Edits to .env or any secrets file
- Schema changes to validation framework artifacts

When verification passes, surface the diff plus test output and stop. The user reviews and commits manually.
