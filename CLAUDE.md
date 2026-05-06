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
Agents commit autonomously with heredoc-embedded messages per mandate G.
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
- Adding a new test or strategy variation not in MASTER_PLAN.md
  (see `.claude/rules/backtest.md` proposal agent exception)
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

> **Trial queue orchestrator exception.** See
> `.claude/rules/backtest.md` for the full scoped commit rules
> for `scripts/run_trial_queue.py`. Push remains human-only.

### Agent edits documents autonomously (no approval needed)

Every doc not in the "Human only" list above is agent-editable when the
data clearly answers the question. Specifically: `docs/MASTER_PLAN.md`,
`docs/bot_status.md`, `docs/open_questions.md`, `docs/strategies.md`,
`docs/research_log.md`, `docs/strategy_evidence_audit_2026-04-26.md`,
and any future audit or per-strategy doc. Sacred-harness rule (above)
covers runtime artifacts, not docs. Do not gate doc edits on judgment
that doesn't apply.

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

> Communication and output rules: see `.claude/rules/communication.md`
> (always loaded). Mandates F and G remain here as core invariants.

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

## Execution autonomy

Claude Code proceeds without approval on everything in "Agent
decides" above. Claude Code must not proceed without approval on
everything in "Human only" above. When verification passes, commit
autonomously with a heredoc message and stop; push remains
human-only.