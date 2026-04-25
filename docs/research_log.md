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

## CPCV vs Block Sharpe (decided 2026-04-25) — ADOPTED block Sharpe

Evaluated López de Prado path-CPCV (Advances in Financial Machine
Learning, ch. 7) vs a block-Sharpe distribution for the validation
harness's input to DSR.

**Decision:** Block Sharpe. See `backtest/cpcv.py` and
`docs/validation_framework.md` § "Block Sharpe distribution".

**Key evidence:**

- Path-CPCV's path-variance generation depends on different models being
  fit on different train/test combinations. With rule-based strategies
  that have no in-window fitting, every reconstructed path runs identical
  logic on identical data, so path Sharpes collapse to one value.
  Verified during chunk 6 implementation.
- The alternative — running the engine on concatenated held-out blocks
  per combination — produces non-degenerate variance only via artificial
  time-adjacency at the gluing boundaries. That's a leakage artifact,
  not a property of the data.
- Block Sharpe runs the engine once per block with fresh strategy state,
  producing N independent Sharpe samples. Variance reflects genuine
  across-period dispersion. Structurally similar to walk-forward but
  multi-sample, preserving DSR's multi-sample basis.
- DSR's required upstream inputs (observed Sharpe, σ of Sharpe, skew,
  kurtosis, sample size) are all derivable from the block Sharpe
  distribution.

**Reconsideration trigger:** A future strategy class with a legitimate
fit/predict split (e.g. an ML-meta-labeled strategy) would justify
implementing path-CPCV alongside block Sharpe. `CPCVConfig.k_held_out`
is reserved for that case.

## Passivbot Evolutionary Optimization — RETIRED

See MASTER_PLAN "Future phases" section for rationale.

## Profit Reserve System — RETIRED

See MASTER_PLAN "Future phases" section for rationale.

## 3-Year Backtest Cross-Strategy Lessons (consolidated 2026-04-25) — REFERENCE

Generalisable findings extracted from the per-strategy diagnostics in
`docs/strategy_failure_analysis_2026-04-19.md`. The detailed per-strategy
analysis stays in that file; this section captures the patterns that apply
across strategies and should inform Phase 3c rescue thinking.

**Win rate alone is meaningless.** DCA shows 92.3% OOS win rate with -7.15%
return — wins were small, a handful of large losses dragged the total
negative. The diagnostic question is always expected value per trade:
`win_rate × avg_win - (1 - win_rate) × avg_loss`. Win rate divorced from
avg-win/avg-loss ratio is not a quality signal.

**Required win rate scales with payoff ratio.** At avg_win/avg_loss ≈ 1.1
(TrendFollowing OOS), break-even win rate is ~48%; 28.6% observed win rate
yields -$30 per trade. At avg_win/avg_loss ≈ 0.9 (Supertrend OOS) the bar
is even higher and 29.5% produces -$47 per trade. A trend strategy with
sub-40% win rate is structurally negative-EV regardless of the strategy's
"reputation."

**Structural exit design can guarantee negative EV.** VolatilityBreakout
exits every trade at the next candle's open regardless of P&L. With 37%
win rate and near-symmetric win/loss size, expected value is -$3.63 per
trade × 1,640 trades = -$5,950. No parameter change can fix a strategy
whose exit rule prevents winners from running. Distinguish parameter
problems (rescuable) from design problems (not).

**OOS-better-than-IS is the anti-overfit signature.** VWAP improved from
+1.00 IS Sharpe to +2.30 OOS Sharpe. The reverse — IS-better-than-OOS — is
the overfitting signature (Supertrend, TrendFollowing, Breakout all show
catastrophic IS→OOS degradation). Phase 3b DSR will catch this formally;
the heuristic is robust enough to use during iteration.

**Pair-specific overfitting is real.** Breakout on AVAX showed +$248 avg
win IS, +$138 OOS — winner size collapsed by ~45% out-of-sample. Moving
this strategy to a different pair is a pair-substitution decision (per
`CLAUDE.md`, requires human approval) but the IS performance is consistent
with overfitting to a specific AVAX regime that didn't persist.

**Reconsideration trigger:** None — these are reference findings, not
decisions. They become actionable inputs to Phase 3c rescue parameter
proposals (must satisfy the no-p-hacking rule's "explicit theoretical
justification" requirement per `CLAUDE.md`).

## Per-Strategy Pair Selection Rationale (consolidated 2026-04-25) — REFERENCE

The current `config.STRATEGY_SYMBOLS` mapping was chosen during initial
strategy creation. Rationales were captured in code comments at the time
but not consolidated. Recording here so Phase 3c pair-substitution
decisions can be made with full context.

| Strategy | Pair | Original rationale | 3-year OOS verdict |
|---|---|---|---|
| DCA | BTC/USDT | Blue-chip, reliable long-term upside | Negative; martingale design issue, not pair |
| TrendFollowing | BTC/USDT | Most reliable directional instrument | Negative; choppy regime + EMA9/21 too noisy |
| Supertrend | ETH/USDT | Liquid, well-behaved trend structure | Catastrophic; flip-exit gives back too much |
| MeanReversion | ETH/USDT (was LINK) | LINK strong range-reversion → moved to ETH for liquidity | Failing; barely fires under current EMA filter |
| GridTrading | SOL/USDT | Volatile but bounded, high grid profit density | Working (small edge) |
| Breakout | AVAX/USDT | Strong momentum moves, clean volume surges | Catastrophic; 91.5% stop_loss exits, fakeouts dominate |
| BearShort | BTC/USDT | Most reliable directional futures instrument | Working (hedge contributor, small return) |
| VWAP | ETH/USDT | Institutional volume patterns present | Strongest OOS Sharpe in portfolio |
| VolatilityBreakout | BTC/USDT | High liquidity for high-frequency entries | Catastrophic; structural design flaw not pair |
| DualMomentum | BTC/USDT (rotates BTC/ETH/BNB) | Multi-symbol momentum rotation | Incomplete (3-year run timeout) |

**Pair-substitution candidates suggested by the OOS data:**

- **Breakout away from AVAX.** 91.5% stop_loss exits suggests the pair
  doesn't produce clean breakouts at 1h. Pair substitution is human-approval
  per `CLAUDE.md` — flag for explicit Phase 3c discussion if the parameter
  variations (volume threshold, retest entry) don't recover EV on AVAX.
- **MeanReversion pair re-evaluation.** ETH at 1h with 14-period RSI / 20-period
  Bollinger may not have enough mean-reversion signal at this timeframe.
  Original LINK choice was theoretically motivated; the move to ETH was for
  liquidity. If parameter variations don't fire enough trades on ETH,
  testing on LINK 1h or moving to a higher timeframe on ETH are both
  candidates — but each is a fresh pair-substitution decision, not a
  parameter variation.

**Pairs to leave alone:** BTC for DCA / TrendFollowing / BearShort /
VolatilityBreakout / DualMomentum — diagnoses are not pair-related. ETH
for VWAP / Supertrend — VWAP is the portfolio's best performer on this
pair, Supertrend's failure is the exit logic, not the pair.

## Regime Allocation History (consolidated 2026-04-25) — REFERENCE

Notable past changes to `REGIME_ALLOCATIONS` in `portfolio/regime_detector.py`.
These entries explain *why* current weights are what they are, so Phase 3c/3d
adjustments don't undo prior reasoned decisions without knowing what they were.

**MeanReversion in BULL/RANGE — re-enabled with funded reduction.**

- **Before:** 0% in all regimes (paper-trading suspension after OOS backtest
  showed -9.36% return, Sharpe -2.80 in a bear-market test period — an
  inappropriate regime for MeanReversion anyway, but the broader allocation
  was suspended out of caution).
- **After:** RANGE 10%, BULL 5%, all other regimes 0%.
- **Funding source:** DCA reduced by 0.10 in RANGE (0.30→0.20) and 0.05 in BULL
  (0.20→0.15). DCA chosen as donor because it is the most buffer-like strategy
  and least harmed by a small trim in regimes where MeanReversion is actively
  earning.
- **Phase 3c implication:** If MeanReversion is retired in Phase 3c, return
  the donated allocation to DCA in those regimes rather than leaving it
  unallocated.

**STRONG_BULL adjustments to fund VolatilityBreakout and DualMomentum.**

- Supertrend: -0.05 (was 0.30, now 0.25)
- Breakout: -0.05 (was 0.35, now 0.30)
- TrendFollowing: -0.05 (was 0.15, now 0.10)
- → VolatilityBreakout: +0.05, DualMomentum: +0.10
- **Phase 3c implication:** If VolatilityBreakout and/or DualMomentum are
  retired (likely for VolBreakout per the failure analysis), return their
  STRONG_BULL allocation to Supertrend / Breakout / TrendFollowing in the
  proportions they were taken from — but only if those strategies survive
  Phase 3c. If they don't either, the regime needs a fresh allocation
  design rather than a revert.

**General principle observed:** Allocation changes were funded explicitly
(donor → recipient) rather than expanding total weight. This preserves the
constraint that each regime row sums to 1.0. Future Phase 3c/3d allocation
edits should follow the same convention.

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
