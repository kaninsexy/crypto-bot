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

## 2026-04-26 — Phase 3c dev_cpcv all-strategies result + structural diagnosis

**Empirical:** 9/10 RETIRE, 1/10 CPCVError (MeanReversion, treated as under_tested). Zero strategies cleared sr_zero_expected = +1.9007 at N=20. Only VWAP beat its baseline (+1.14 vs +0.68 ETH B&H). Four strategies net-losing in dev (Supertrend −1.64, Breakout −1.33, TrendFollowing −1.77, VolBreakout −3.62, DualMomentum −2.39). Logs: `logs/dev_cpcv_all_20260425_194818.log`. Trial rows: `backtest/trials.log` 2026-04-25 19:48 onward.

**MeanReversion CPCVError diagnosis:** 4-filter AND stack (BB %B + StochRSI K cross + volume + EMA) self-suppressed below `_MIN_TRADES_PER_BLOCK` in 7/10 blocks. The strategy didn't fail to find edge — it failed to fire enough events. No row written (atomicity guarantee).

**Structural diagnosis:** This is not bad luck or 10 independent overfitting events — it's overdetermined by shared substrate. 1H bar sits in a documented academic dead zone (HFT-arbitraged below, factor literature concentrates at weekly+). Single-pair forfeits the CTA √N diversification multiplier (~5× per HOP 2017). Retail-template strategies built on technical-indicator stacks have weak peer-reviewed footing pre-2000 and decayed post-2000 per Bajgrowicz/Scaillet 2012 and Marshall/Cahan/Cahan 2008.

**Detailed audit:** `docs/strategy_evidence_audit_2026-04-26.md`. Per-strategy mechanism evidence, platform reliability, timeframe meta-pattern, recommendations.

**Phase 3c rescue iteration is not the right next step.** N=20 rescue variations on the same substrate would be additional draws from the same noise distribution. The structural finding requires a Phase 4 scope decision before any further rescue work — see `docs/open_questions.md`.

## 2026-04-26 — Simulator short-pnl fix and BearShort dev_cpcv re-run

**Bug fix (commit `25bd843`).** `paper_trading/simulator.py`
`_handle_full_sell` and `_handle_partial_sell` previously computed
`pnl = net_proceeds − total_cost` regardless of side, inverting the realized
PnL sign on every short close. The balance update `self.balance += net_proceeds`
inherited the same flaw. `Position.unrealized_pnl` already branched correctly
on `is_short`, so equity-curve points while a position was open were correct;
the bug surfaced only on close. Refactor added a sign-aware `Position.realized_pnl`
helper mirroring the unrealized variant, and both close paths now route
through it. 6 unit tests in `paper_trading/tests/test_short_pnl.py` cover
winning/losing short full-close, long-control full-close (regression guard),
and the three matching partial-close cases. Independent verification via
`scripts/verify_short_pnl.py` flipped both shorts ✗→✓ post-fix while longs
remained ✓.

**BearShort dev_cpcv re-run (trial_id `4f89d224107c4a61a958f051791c7a51`,
ts 2026-04-26T14:56:07Z).** Same params_hash as the pre-fix row
(`44136fa3...`); only the simulator changed. Result:

- `observed_sharpe`: **−2.9643** (pre-fix +1.3129)
- block Sharpe distribution: mean **−3.4565**, std 1.2563,
  p05 −4.6607 / p25 −4.1574 / p50 −3.5887 / p75 −3.3381 / p95 −1.4290
- `dsr_validation`: 0.0
- `n_trades`: 198 (unchanged from pre-fix; signal logic untouched)
- VerdictResult: **RETIRE**. trade_count_pass=True, mintrl_pass=True,
  mt_mean_pass=False, baseline_pass=False, baseline_sharpe +1.6945,
  mintrl 88.59 bars

**Mechanistic observation.** The sign flipped cleanly but the magnitude
amplified by ~2.2× over a clean-mirror hypothesis (clean mirror would predict
~−1.31 / mean −1.58). Working hypothesis: **balance-scaled compounding
asymmetry**. Pre-fix, the inverted PnL grew the simulator pot on phantom wins
and scaled subsequent positions larger on a fictitious upward equity
trajectory. Post-fix, the correct trajectory shrinks position size on real
losses, compounding the drag. A single-block equity-curve diff pre vs
post-fix would confirm but does not alter the verdict.

**Scope of re-run.** BearShort is the only production strategy that opens
shorts (verified via `is_short` grep across `strategies/`). No further
short-affected re-runs needed. The trial appears as the last row in
`backtest/trials.log` and is referenced from
`docs/open_questions.md` "Phase 4 implications of the 2026-04-26 short-pnl
fix" and the new "[OPEN, 2026-04-26] Trials.log invalidation policy after
simulator fix" question.

**Reconsideration trigger:** None on the bug fix itself — the test suite
guards regression. The dependent `trials.log` invalidation policy is OPEN
and tracked separately.

## 2026-04-26 — Phase 4 scope decision: Branch C

Phase 3c dev_cpcv at N=20 against sr_zero_expected = +1.9007 produced
9/10 RETIRE + 1/10 UNDER_TESTED, and the BearShort post-fix re-run
(observed_sharpe -2.9643, all block-Sharpe quantiles negative,
dsr_validation 0.0) forecloses Branch B. The dominant input from
`docs/strategy_evidence_audit_2026-04-26.md` is that Hypothesis B
(retail templates lack edge regardless of timeframe) dominates
Hypothesis A (1H/single-pair is the structural issue): the audit's own
best case for Branch A is "rescues 1-2 borderline cases; does not
unlock the cohort," and Cakici et al. (2024) — sophisticated ML on
weekly barely surviving transaction costs — caps the upside of a
daily/multi-pair retail-template redesign well below what 3-4 months
of work justifies.

Branch C preserves the Phase 3b validation harness (block-Sharpe CPCV,
DSR, MinTRL, verdict tree, B&H baseline, threshold calibration) as
substrate-agnostic infrastructure for whichever follow-on direction
comes next, rather than spending it on continued retail-crypto
iteration. The 9/10 RETIRE result is itself a real finding — the
harness correctly identifying that this substrate doesn't carry edge
is what it was built to do. Specific Phase 5 direction (prediction
market bot or alternative) deliberated separately.

## 2026-04-26 — Trials.log invalidation policy (c) implemented

Policy (c) chosen from the three candidates surfaced in
`docs/open_questions.md`: tag superseded rows in place with
`superseded_by: '<fix-commit-sha>'`, and have `count_trials_for_dsr`
exclude tagged rows. Implementation lives in `backtest/trials.py` —
the optional `superseded_by` field is permitted on `smoke` and
`full_cpcv` rows and forbidden on `final_gate` rows (a final_gate is
the deploy-decision audit boundary; supersession would silently
rewrite that boundary). Schema-version unchanged: the addition is
backward-compatible with v2 because the field is optional and
defaults to absent.

The first concrete instance is the BearShort row pair: pre-fix
trial_id `34cac215...` (commit `28cfc7a`) tagged with
`superseded_by: "25bd843"`; post-fix trial_id `4f89d224...` (commit
`25bd843`) untagged. After tagging, `count_trials_for_dsr("BearShort")
= 1` (post-fix only) while `count_distinct_variations("BearShort") =
1` (variation_id `rescue-default`, dedup unaffected).

The asymmetry between `count_trials_for_dsr` (excludes superseded
rows) and `count_distinct_variations` (does not) is intentional. The
former counts statistical trials — each non-superseded row is one DSR
draw against the multiple-testing null. The latter counts attempted
parameter sets for the 20-variation iteration cap, where re-running
the same `variation_id` after a tooling fix is one variation tried,
not two. Re-runs of the same parameter set under different commits
naturally collapse via shared `variation_id` regardless of the
supersession tag.

## AI/algo trading viability and strategy-archetype evidence (consolidated 2026-04-29)

Synthesised from chat 2026-04-29 research tasks. Purpose: future Claude
sessions see *why* the Phase 4.A resurrection hypotheses were chosen
without re-running the underlying research. Hypotheses themselves are in
`docs/MASTER_PLAN.md` Phase 4.A table; this entry covers the evidence
basis.

### Context

This research informed the Phase 4.A Resurrection Batch + Phase 4.B
Funding-Rate Harvest scope decisions. The user explicitly opted not to
commit to Branch A vs Branch C vs hybrid before testing — the
resurrection hypotheses below are *starting points* for the validation
harness, not commitments.

### Edge-source ranking (retail-accessible crypto strategies)

In rough order of peer-reviewed support strength:

1. **Funding-rate harvest** (delta-neutral long-spot / short-perp).
   Strongest peer-reviewed support of any retail-accessible crypto
   strategy candidate. Multiple 2024–2025 papers; baseline funding APY
   ~10.95% before edge selection. Promoted to Phase 4.B.
2. **Time-series momentum at daily/weekly multi-asset.** Moskowitz/Ooi/
   Pedersen 2012, Hurst/Ooi/Pedersen 2017, Liu/Tsyvinski/Wu 2022.
   Diversified TSMOM Sharpe ~1.3 across 67 markets vs 0.3–0.5 single-
   instrument — ~80% of Sharpe from cross-market diversification.
   Informs TrendFollowing daily-multi-asset and DualMomentum weekly-
   majors hypotheses.
3. **BTC-residual mean-reversion on alts.** Published evidence: Sharpe
   ~2 post-2021 on BTC-neutral residual MR. Substantially different
   substrate from absolute-price MR. Informs MeanReversion rebuild.
4. **Daily ensemble breakouts on rotational basket.** Zarattini/Pagani/
   Barbon 2025 — ensemble Donchian on top-20 rotational basket, vol-sized
   positions, Sharpe >1.5, alpha 10.8% vs BTC. Informs Breakout redesign.
5. **Volatility breakouts on daily multi-coin with relative-volume
   selection.** Williams (1999) practitioner-only at single-name, but
   modern academic edge (Zarattini/Barbon/Aziz 2024) is in the selection
   layer. Informs VolatilityBreakout redesign.

### Strategy-archetype regime suitability

- **Trend-following** concentrates at multi-day to monthly. 1H is in an
  academic dead zone — Brock/Lakonishok/LeBaron 1992 EMA-crossover edge
  was killed out-of-sample by Sullivan/Timmermann/White 1999 and
  Bajgrowicz/Scaillet 2012 under FDR with realistic costs. Supertrend
  has zero peer-reviewed support at any timeframe.
- **Mean-reversion** in crypto specifically: Caporale/Plastun/Oliinyk
  2019 tested hourly counter-movement on BTC/LTC/Ripple/Dash and found
  it not profitable after costs. Crypto evidence points the other
  direction at retail timeframes — momentum at daily, not reversal.
  Residual MR (cross-sectional, BTC-neutral) is the substrate where
  evidence supports MR.
- **Momentum** literature concentrates at monthly (Jegadeesh/Titman 1993,
  Antonacci 2012/2014, Asness/Moskowitz/Pedersen 2013, Geczy/Samonov
  2017) or weekly for crypto (Liu/Tsyvinski/Wu 2022, 3-week formation).
  Hourly momentum is a ~720× contraction of the framework with no
  peer-reviewed support.
- **Breakout** literature: Lukac/Brorsen/Irwin 1988 found channel
  breakouts profitable on 12 commodity futures 1978–1984. Park/Irwin
  2007 meta-survey shows post-1990 weakening; Marshall/Cahan/Cahan 2008
  and Park/Irwin 2010 (Reality Check / Hansen SPA) show no consistent
  profitability post-1990 after data-snooping correction. Crypto-
  specific edge (Zarattini 2025) is in the ensemble + rotational-basket
  + vol-sizing layer, not single-pair single-lookback.
- **Grid trading** is mathematically zero-EV pre-fees under symmetric
  random walk (Chen/Chen/Jang 2025). Edge requires regime-detection
  conditioning to range/low-trend/mid-vol — informs the GridTrading
  regime-conditional demotion hypothesis.

### Why daily/weekly/multi-asset, not hourly/single-pair

The 1H single-pair substrate sits in an academic dead zone for nearly
every retail strategy archetype. Hourly is too noisy for trend
(literature monthly), too coarse for high-frequency mean-reversion
(literature minute-level or below), and lacks the cross-sectional
breadth that drives most published Sharpe in TSMOM. The Phase 3c 9/10
RETIRE result is consistent with this — the substrate, not the
strategies, is the dominant failure mode for most of the cohort.

The Phase 4.A resurrection hypotheses move each strategy to the
timeframe / instrument-set / substrate where its archetype has the
strongest published support. Whether *any* of them survive Phase 3b
validation in our specific implementation remains an open empirical
question — that's what 4.A is for.

### Operational risk patterns

- **Capital floor matters more than strategy count.** Spreading thin
  capital across 10 strategies leaves each below minimum viable
  threshold given exchange fee structure. Paper trading all 10 was
  correct (to identify performers); real capital deployment must
  concentrate. This argues for the Phase 4.C verdict-tree approach (≥2
  passes → portfolio; 1 pass → user decides; 0 → Branch C confirmed)
  rather than auto-deploy of any survivor.
- **Pair selection is strategy-critical.** LINK trends rather than
  reverts — wrong pairing for MeanReversion. VWAP has structural
  limitations in 24/7 crypto markets (no session anchor for the daily
  VWAP that the literature uses).
- **Simulator fix can flip sign, not just magnitude.** The BearShort
  Sharpe inversion (pre-fix +1.31 → post-fix −2.96, commit `25bd843`)
  demonstrates that pre-fix backtest results can be directionally
  wrong, not just imprecise. The trials.log supersession-tagging policy
  (open_questions.md, resolved 2026-04-26) was implemented in response
  to this.

### Thai tax considerations (Phase 4.B venue choice)

The 2025–2029 Thai personal-income-tax exemption applies only to
Thai-SEC-licensed exchanges. Binance.com is not on that list. This
affects the venue choice for Phase 4.B funding-rate harvest if the
strategy survives the 4.A → 4.B sequence. Decision deferred until 4.A
verdicts are in and 4.B is actually scheduled. Tracked in
`docs/open_questions.md`.

### Sources

Primary literature touched in the chat 2026-04-29 research synthesis (not
exhaustive; the named-paper citations in `docs/strategies.md` and
`docs/strategy_evidence_audit_2026-04-26.md` cover the per-strategy
evidence in more detail):

- Brock, Lakonishok, LeBaron 1992; Sullivan, Timmermann, White 1999;
  Bajgrowicz, Scaillet 2012 (technical-rule profitability under FDR)
- Moskowitz, Ooi, Pedersen 2012; Hurst, Ooi, Pedersen 2017 (TSMOM)
- Jegadeesh, Titman 1993; Antonacci 2012, 2014; Asness, Moskowitz,
  Pedersen 2013; Geczy, Samonov 2017 (momentum)
- Liu, Tsyvinski, Wu 2022 (crypto momentum at weekly)
- Caporale, Plastun, Oliinyk 2019 (crypto hourly counter-movement)
- Lukac, Brorsen, Irwin 1988; Park, Irwin 2007, 2010; Marshall, Cahan,
  Cahan 2008 (channel breakouts)
- Zarattini, Pagani, Barbon 2025; Zarattini, Barbon, Aziz 2024
  (crypto rotational-basket breakouts and selection-layer edge)
- Chen, Chen, Jang 2025 (grid trading EV)
- Wen, Bouri, Xu, Zhao 2022; Li, Sakkas, Urquhart 2022 (1H crypto TSM,
  rare peer-reviewed support — informs BearShort academic basis)
- Cakici et al. 2024 (sophisticated ML on weekly crypto barely
  surviving costs — caps upside of retail-template daily/multi-pair
  redesign)
- Daniel, Moskowitz; Barroso, Santa-Clara (vol-scaling for momentum
  crash protection)
- Constantinides 1979 (averaging-down dominated under standard expected
  utility — informs DCA structural diagnosis)
- DeMiguel, Garlappi, Uppal 2009 + 2024 replication (1/N nearly
  impossible to beat OOS — informs Phase 3d weighting choice)
- Funding-rate-harvest 2024–2025 papers (multiple; specific citations to
  be re-verified before Phase 4.B harness extension scoping)

The funding-rate-harvest citations should be re-verified specifically
before Phase 4.B begins — that strategy depends most heavily on a
specific empirical literature, and the exact paper set should be in
`research/funding-rate-literature.md` per the no-p-hacking rule before
the first 4.B trial appends to `trials.log`.

## TradingAgents Multi-Agent Framework (logged 2026-04-29) — REFERENCE

Logged as a reference for the deferred prediction market bot, which
enters scope only on the MASTER_PLAN Phase 4.C / Phase 5 "0 survivors"
branch. NOT applicable to current crypto bot work. No adoption decision
attached — purpose is so future Claude sessions don't re-research the
same repo.

### Source

github.com/TauricResearch/TradingAgents, paper arXiv 2412.20138 (Xiao,
Sun, Luo, Wang 2025). Multi-agent LLM trading framework built on
LangGraph. Originally for equities (Fundamentals/Sentiment/News
analysts → bullish/bearish researcher debate → Trader → Risk →
Portfolio Manager). Community fork AlpacaTradingAgent adds crypto.
Version 0.2.4 (April 2026) ships Pydantic structured-output decision
agents and a persistent decision log with outcome-grounded reflections,
replacing per-agent BM25 memory.

### Patterns worth lifting

For the prediction market bot's "scanner → parallel research agents →
XGBoost+LLM calibration → Kelly sizing → postmortem loop" pipeline
already sketched in MASTER_PLAN/memory:

1. Bullish/bearish adversarial debate as a bias-reduction input before
   XGBoost calibration — surfaces counter-evidence before probabilities
   lock in. Use as calibration input only, NOT as the final trade
   arbiter. The "Multi-Agent Claude Patterns" section in this same file
   already flagged "agent-to-agent debate on retire/keep decisions" as
   sycophancy- and confidence-cascade-prone; same risk applies if used
   as the arbiter.
2. Persistent decision log with outcome-grounded reflections —
   ready-made substrate for the postmortem-loop stage.
3. Pydantic structured outputs (`llm.with_structured_output(Schema)`)
   for typed probability + rationale outputs feeding the Kelly sizer.
   Adopt this hygiene pattern independent of any framework decision.

### Patterns NOT to lift

LangGraph as orchestration (too heavy for an inspectable bot); the
equity-analyst roster (fundamentals / sentiment / news archetypes don't
map to prediction-market substrate — base-rate research is the analog
there, not earnings).

**Reconsideration trigger:** prediction market bot enters active scope
per MASTER_PLAN Phase 4.C "0 strategies pass" branch or Phase 5 "0
survivors" branch.

## Phase 4.A trial #1 — Supertrend daily-resurrection retired (2026-04-29)

### Trial outcome

Variation `phase4a-daily-resurrection-v1` tested per
`research/supertrend-literature.md` (committed `bf4b9ca`): daily TF
(internal resample of the manifest's 1h frame) + Barroso & Santa-Clara
(2015) vol-scaling + 6-regime gate restricting longs to STRONG_BULL ∪
BULL.

**Validation harness:** CPCV-10 raised `CPCVError`. All 10 dev-window
blocks fell under `_MIN_TRADES_PER_BLOCK = 5`; per-block trade counts
`[1, 1, 0, 1, 1, 1, 1, 0, 1, 2]`. The harness cannot certify the
variation; trial appended as `trial_type="smoke"` (excluded from
`count_trials_for_dsr` per Phase 3b Chunk 5; `count_distinct_variations`
incremented from 1 to 2, consuming one slot of the 20-cap).

**Headline run (full dev window, single backtest):** Sharpe +1.1182,
n_trades 13, return +26.39%, max DD 11.59%, win rate 46.1%, profit
factor 2.78. Beats ETH/USDT B&H baseline +0.6836 by +0.43 Sharpe — but
verdict tree's `min_trade_count = 30` precondition fires (n=13), so
forensic verdict is `under_tested`.

**No variation #2.** The hypothesis-of-record's pre-condition triggered:
Supertrend has no peer-reviewed academic foundation (Olivier Seban,
2009), so a single failed structural-change variation is enough to make
the indicator-without-edge-theory prior dominant. Variation budget
capped at 1 attempt post-Phase-3c. Branch C of `MASTER_PLAN.md`
strengthens for this strategy.

### Reusable lesson — daily-TF density floor on single-asset dev windows

The CPCV harness's `_MIN_TRADES_PER_BLOCK = 5` floor combines with
single-asset 880-day dev windows to set a structural minimum signal
cadence: 880 / 10 / 5 ≈ one trade per 17.6 days. Strategies whose theory
implies daily-or-slower entry cadence on a single asset cannot pass
CPCV-10 without either:

- **(a) Multi-asset breadth** — more symbols multiply trades-per-block
  proportionally. DualMomentum already runs multi-symbol (3 symbols);
  any future single-asset, daily-TF candidate should consider a
  multi-asset cousin from the start to clear the density floor.
- **(b) A manifest re-cut** that reduces `n_blocks`, extends
  `data_start`, or changes `timeframe` — sacred-harness change, not
  casually invokable. Reducing `n_blocks` also degrades the block
  Sharpe distribution's statistical power (fewer Sharpe samples for
  DSR), so this is rarely a win.
- **(c) Faster signal cadence within the same TF** — but for Supertrend
  this would mean tighter ATR multipliers, which on daily candles
  reintroduces chop whipsaws (the failure mode the daily-TF
  hypothesis was specifically designed to escape). Self-defeating for
  trend-following indicators.

The lesson is not Supertrend-specific. Any strategy proposed in
extension-track or Phase 5 work that operates on daily-or-slower TF on a
single asset should expect to either be multi-asset by design or to
hit this density floor on first CPCV. Pre-flag low-cadence candidates
during literature review, not at trial-execution time.

### Aside: smoke-tagging as the natural overflow path

This trial's outcome confirmed an expected use case for the
`trial_type="smoke"` row: when CPCV raises `CPCVError` for reasons
intrinsic to the variation (not infrastructure failure), the headline
single-window backtest is still a meaningful data point but is not
DSR-quality. Smoke captures the signal without inflating multiple-
testing accounting. The schema's three trial-types (smoke, full_cpcv,
final_gate) cover this case cleanly without needing a fourth type.

## Thai SEC venue / derivatives status (logged 2026-04-29) — Phase 4.B venue resolution

Logged at Phase 4.B kickoff scoping to capture the venue/tax research
behind the OKX-offshore-with-PIT decision. Locks in Branch 1 of the
three branches surfaced in the kickoff chat. Companion entry to the
"AI/algo trading viability" synthesis above; that entry treated funding-
rate harvest as the strongest peer-reviewed substrate but did not
model the Thai-PIT haircut. This entry closes that gap.

### Findings

**Thai-SEC-licensed digital-asset operator list (as of 2026-04-29) is
spot-only.** The licensed-exchange tier (Bitkub, Gulf Binance / Binance
TH, Orbix, Upbit, KuCoin TH / ERX, WaanX, TDX, Bitazza, plus broker
tier — 17 licensed entities total) does not currently include any
operator offering perpetual futures with funding-rate settlement. The
2025–2029 personal-income-tax exemption (Ministerial Regulation 399)
applies only to trades executed via these SEC-licensed digital-asset
operators. Net effect: Phase 4.B's required substrate is not available
on tax-exempt terms today.

**Thai SEC consultation (release No. 81/2026, 20 April 2026; closes
20 May 2026)** proposes letting licensed digital-asset operators apply
for derivatives licenses without setting up separate entities. This is
a proposal; no exchange has yet been granted a derivatives license. SEC
has not published a target date for finalizing the rules. Earliest
plausible Thai-licensed perp + funding product launch: Q3 2026
optimistic / 2027 realistic.

**OKX is not on the Thai-SEC-licensed list.** The bot's existing OKX
paper-mode plumbing is operationally cheap to extend but carries no
tax-exemption benefit. Same applies to Binance.com, Bybit, and other
offshore venues.

### Decision

**Phase 4.B runs on OKX (USDT-M perp + USDT spot), Thai PIT applies to
funding income.** Branch 1 of the three branches surfaced at scoping.
Selected over Branch 2 (defer pending Thai-licensed perp launch) and
Branch 3 (skip 4.B entirely) by the user.

### Implications for the edge claim

The 2026-04-29 chat synthesis quoted a baseline ~10.95% annualised
funding APY before edge selection. After Thai PIT (marginal rate
estimated 25–35% on funding income classified as Section 40(4)(h)
benefit per the Revenue Code) the post-tax baseline narrows to
~7.1–8.2% before perp fees, spot fees, slippage, and long-leg vol
drag. The edge gate is materially tighter than the pre-tax framing
suggested. Per-strategy hypotheses in `research/funding-rate-literature.md`
must derive their expected Sharpe / required-funding-rate threshold
*post-tax*, not pre-tax.

### Reconsideration trigger

A Thai-SEC-licensed exchange launches a perp + funding product with
SEC-final derivatives licensing. At that point Phase 4.B runs (or any
surviving live-deployment of a 4.B passer migrates) to that venue for
the PIT exemption. Watch item: SEC consultation final rules date
(currently TBD; earliest mid-2026).

### Sources

- Fintech Singapore "List of Licensed Cryptocurrency Exchanges in
  Thailand" (March 2026) — licensed-list snapshot
- BitcoinIst "Thailand Considers Opening Door Wider To Crypto Futures
  in Licensing Revamp" (~2026-04-22) — consultation paper coverage
- The CCPress "Thailand SEC Weighs Rule Changes to Let Crypto Firms
  Offer Derivatives" — release No. 81/2026 dates and license categories
  (S-3, D-DAIA, D-DAF)
- BitZup 2026 guide — Ministerial Regulation 399 PIT exemption scope
  (trades executed via SEC-licensed digital-asset operators only)
- Global Legal Insights "Blockchain & Cryptocurrency Laws 2026 |
  Thailand" — Revenue Code Section 40(4)(h)(i) tax classification of
  crypto-derived income