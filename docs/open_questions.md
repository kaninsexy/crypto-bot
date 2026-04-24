# Open Questions

Last updated: 2026-04-25

Running list of items that are unresolved, blocked, or carried from earlier
work. Entries are grouped by theme, not priority. When an item is resolved,
move it to a "Resolved" section at the bottom (keep for reference for one
revision cycle, then prune).

## Phase 3a.1 — commit decision pending

Supertrend and BearShort vectorization sits in the working tree with a
verified ~10× speedup, identical math on Supertrend, and within-noise drift
on BearShort. Awaiting a human commit. Agents are not permitted to commit
(per `CLAUDE.md`).

## DualMomentum — incomplete 3-year run

The 3-year backtest was killed at the 150-min process cap mid-in-sample.
The 3-month smoke showed 55 rotations firing correctly, so the engine
behaviour looks right. Two options:

- Re-run with no time limit (or chunk the run by year and stitch the equity
  curves).
- Accept 9/10 as the deploy baseline and park DualMomentum.

Blocked on the human call between those options.

## Phase 3b buildout — next focused session

Implementation work: CPCV path generator, DSR computation, holdout split
enforcement, `trials.log` writer. Full spec in `docs/validation_framework.md`.
This is the next planned multi-hour session.

## `REGIME_PRIORS` — still empty

Blocked on Phase 3b completion. `REGIME_PRIORS` is populated from per-regime
Sharpe attribution of the strategies that survive holdout DSR — that data
does not exist until Phase 3b has run.

## Checkpoint balance mismatch

Pre-existing item. The "$100k Apr 17 fresh" balances in the server
checkpoint don't match the bot's internal state. Needs reproduction from
logs and a deliberate reset. Low-priority until Phase 3b work lands.

## Threadripper PC setup

Remote-execution plumbing for Claude Code on the Threadripper: SSH tunnel,
passwordless keys, tmux persistent sessions, outbound firewall rules.
Blocked on PC specs from Kanin (OS / CPU / RAM). Spec answer determines SSH
tunnel setup steps (WSL2 vs native Linux). Unlocks long-running backtests
and CPCV sweeps off the MacBook.

## Stale docstrings (cosmetic)

- `portfolio/manager.py:13` — docstring refers to the pre-Phase 3a layout.
- `config.py` — comments say MeanReversion is on LINK but the code has it
  on ETH.

Batch cleanup task, not urgent.

## Phase 4 deployment mechanics

Deploy to existing server with fresh $100k paper state, or preserve current
paper state and deploy in parallel for comparison? Decision needed before
Phase 4 begins.

## Kelly low-trade-count behavior

`portfolio/kelly.py:353` and `:336` produce zero or quarter Kelly for
low-trade strategies. Resolved automatically by Phase 3c data accumulation
but worth tracking — strategies that survive validation but have few trades
will be sized conservatively until trade count catches up.

## Regime-tagged trade logging for live-stats blending

Carried from the earlier plan. Per-trade regime tag so Bayesian blending of
prior + live-trade Kelly inputs can be done per regime. Requires schema
addition to the trade log. Appropriate to do alongside Phase 3b.

## Potential Phase 6+ Strategy Categories

These were identified in the earlier FUTURE_IMPROVEMENTS.md (since archived) as potential future strategies — distinct from improvements to existing strategies. Preserved here so they're not lost. Not actionable until Phase 5 complete and bot is consistently profitable.

### Funding Rate Arbitrage

- Pattern: short perpetual + long spot (or vice versa), delta-neutral
- Entry condition: funding rate > 0.03% per 8h
- Expected yield: 10-30% annualized, uncorrelated with other strategies
- Expected max drawdown: under 2%
- Complexity: Medium (requires perpetual + spot execution, reconciliation)
- Priority if pursued: HIGH (high expected impact, low risk)

### Crisis-Alpha (Liquidation Cascade)

- Pattern: detect liquidation cascades, enter momentum crash shorts
- Allocation range: 7-10% of portfolio
- Expected win rate: 20-35% (low), but payoff: 5-20x
- Complexity: High (requires reliable liquidation detection, fast execution)
- Priority if pursued: MEDIUM (high impact but high complexity and operational risk)

### Basis Trading (Cash-and-Carry)

- Pattern: long spot + short future to capture contango premium
- Entry condition: annualized basis > 8-10%
- Current market state: basis compressed to 3-5% — not attractive now
- Complexity: High (requires futures margin management)
- Priority if pursued: LOW (currently uneconomic, revisit if basis widens)

### Cross-Strategy Conflict Detection with Weighted Netting

- Considered and partially addressed: the Phase 3d inverse-volatility weights decision captures most of the intended benefit. Full weighted netting conflicts with the DSR validation discipline (more combining = more trials). Mark as superseded by Phase 3d decision unless Phase 4 monitoring reveals inverse-vol is insufficient.
