# Research Log

Evidence-based decisions made during the project, with links to source research
(chat artifacts) when available. Purpose: future Claude sessions start from
these conclusions rather than re-researching.

## Forecast Combining (evaluated 2026-04-25) — DEFERRED

Evaluated pysystemtrade-style continuous forecast combining vs. current
regime-allocation + CorrCap architecture.

**Decision:** Defer until after Phase 5. See MASTER_PLAN "Future phases" section.

**Key evidence:**

- Carver's own pysystemtrade backtest: ~0.53 Sharpe with variable FDM, marginal
  improvement over fixed weights.
- Carver's formal handcrafting test: "indistinguishable" from more complex
  methods (qoppac.blogspot.com 2019-02).
- DeMiguel, Garlappi, Uppal (2009): no optimization method consistently beats
  1/N across 14 methods and 7 datasets. 2024 replication confirms.
- Grinold Fundamental Law: breadth collapses with signal correlation.
  With ρ=0.63 across 6 signals, effective breadth ≈ 1, not 6.
- Crypto-specific: FDM mis-calibration risks leverage creep during regime
  shifts; CorrCap hard-block is defensible risk control.
- Barroso-Santa-Clara vol scaling on crypto momentum: Sharpe 1.12 → 1.42
  (captures most of the uplift without full forecast-combining machinery).

**Reconsideration trigger:** Phase 4 paper monitoring shows CorrCap blocks are
causing >1-2% annualized drag, OR regime transitions cause measurable
whipsaw losses.

## Multi-Agent Claude Patterns (evaluated 2026-04-25) — ADOPTED

Evaluated multi-agent Claude workflows for the strategy rescue project.

**Decision:** Adopted specific patterns, rejected others. See CLAUDE.md for operational rules.

**Patterns adopted:**

- Parallel literature-review subagents (Anthropic orchestrator-worker pattern)
- Cross-model adversarial review (Claude builder + different-model reviewer)
- Parallel git worktrees for independent strategy rescue
- `trials.log` as the sacred append-only statistical record

**Patterns rejected:**

- Agent-to-agent debate on retire/keep decisions (sycophancy + confidence cascade)
- Autonomous hyperparameter search loops on Sharpe (p-hacking via LLM)
- Agents modifying the validation harness mid-iteration (compromised judge)

**Key evidence:**

- Anthropic Research: +90.2% on breadth-first research, but 15× token cost;
  explicitly excludes coding from success claim.
- Cognition "Don't Build Multi-Agents": parallel implementation agents produce
  incompatible work (Flappy Bird failure).
- MAST study (NeurIPS 2025): 41-86.7% failure rates in popular frameworks;
  79% of failures trace to specification/coordination issues.
- Jesse Vincent Superpowers (90k GitHub stars): single driver agent with
  ephemeral bounded subagents is the workable pattern.

## Passivbot Evolutionary Optimization — RETIRED

See MASTER_PLAN "Future phases" section for rationale.

## Profit Reserve System — RETIRED

See MASTER_PLAN "Future phases" section for rationale.

## Strategy Failure Analysis Reference

A detailed per-strategy diagnostic from the 9-of-10 3-year backtest (2026-04-19) lives at `docs/strategy_failure_analysis_2026-04-19.md` (6.5KB). This file predates the consolidated research log but remains authoritative for the specific failure-mode diagnosis of each strategy:

- DCA: high win rate but risk/reward imbalance
- MeanReversion: barely fires, EMA filter too tight
- Supertrend: 29.5% win rate, avg loss > avg win
- TrendFollowing: 28.6% win rate too low for EMA9/21
- Breakout: 91.5% stop_loss exits on AVAX
- VolatilityBreakout: 1-candle exit design flaw
- DualMomentum: incomplete run (150-min timeout)

Phase 3c rescue work should start from that file's diagnosis for each strategy before proposing parameter variations.
