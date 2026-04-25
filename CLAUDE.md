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

- **Validation harness is sacred.** Files under `validation/`, `backtest/cpcv.py`,
  `backtest/dsr.py`, `backtest/engine.py`, and `backtest/trials.log` must not be modified
  by iteration-phase agents. Changes to these require human approval.
- **Every experiment counts.** Every backtest, every parameter variation, every
  exploratory test appends a row to `trials.log`. Multiple-testing correction via
  DSR uses this count. Do not bypass.
- **Paper mode is the guard.** `paper_mode=True` must remain the default. Any
  code path that would trigger real OKX API calls requires human approval.

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

- **Claude Code:** implementation, autonomous iteration, backtests, code edits
- **Cowork:** documentation updates, well-specified contained tasks
- **Chat with human:** decisions, design reviews, retire/keep calls requiring judgment

---

## Historical note

An earlier version of this file described a 6-strategy Binance paper bot (Phases A–E)
dated prior to the OKX migration and Phase 2+ redesign. That content is superseded
by this file and by `docs/MASTER_PLAN.md`. The original bug-fix ledger and file
layout from that era are retained in git history if needed for reference.
