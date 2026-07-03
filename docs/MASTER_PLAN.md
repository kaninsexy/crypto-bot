# MASTER PLAN — Crypto Trading Bot

Last updated: 2026-07-03 (Phase 4.E Microstructure/Order-Flow batch
added as a new strategy category. Human pre-authorization: Kanin
explicitly approved the addition in chat 2026-07-03 ("Yes, start
now") after reviewing docs/redesign_proposal_microstructure_2026-07-03.md
and confirming the data substrate is free. Data recon verified:
Binance Vision monthly 1m klines with taker buy/sell split download
and parse correctly back to 2021.)
Previously 2026-05-08 (autonomy expansion landed -- paper deploy,
MASTER_PLAN.md outcome rows, paper-mode capital sweeps moved from
Human-only to Agent-decides per CLAUDE.md "Human only" / "Agent
decides" 2026-05-08 update; cron orchestrator switched from --once
to --continuous; orchestrator-digest interval dropped from 8h to 0
so every run with unreported activity sends an email; agent-hook
paths corrected from $HOME/dev/crypto-bot to $CLAUDE_PROJECT_DIR
which fixes the scripter-rc=1 fall-through observed 2026-05-08)
Previously 2026-05-04 (Phase 4.B Variation #1 retired 2026-05-02;
analyst overlay v3 Phase A approved 2026-05-04 per architecture D.4;
Phase 4.C "0 pass" branch reframed as continuation via autonomous
research loop)
Supersedes the 2026-04-25 plan. The primary changes since: Phase 3c
verdict landed (9/10 RETIRE + 1/10 UNDER_TESTED), Branch C was
selected on the empirical anchor, and a structured pre-commitment
exploration (Phase 4.A Resurrection Batch + Phase 4.B Funding-Rate
Harvest) has been authorised to test whether retired strategies can
be unstuck with new evidence or whether new substrates produce edge
before final commitment to Branch C-only.

## Project integrity principle

**Nothing deploys until Phase 3b's verdict tree clears it on holdout data —
meaning a "keep" verdict from compute_verdict, which requires passing the
multiple-testing null (SR > sr_zero_expected(N)), the buy-and-hold baseline,
and both preconditions (trade-count floor + MinTRL).**

This is the single rule that overrides everything else in this document. No
strategy gets paper-deployed, and no paper-deployed strategy gets
live-deployed, until `compute_verdict` returns "keep" on the untouched
holdout window. See `docs/validation_framework.md` for the methodology.

## Timeframe-per-strategy principle

**Each strategy's optimal timeframe is a discovered variable, validated
per strategy by the harness. There is no global project timeframe.**

This applies retroactively (the 1H single-pair substrate is a Phase 3c
artifact, not a project commitment) and prospectively (the Phase 4.A
resurrection batch deliberately runs different strategies at different
timeframes — Supertrend at daily, DualMomentum at weekly, MeanReversion
on residuals, etc.). Anywhere this document or downstream docs name a
timeframe, that name is per-strategy and per-experiment, never global.

## Phase status

### Phase 2c — Regime-Aware Kelly Wiring — COMPLETE (commit `4a51f0b`)

Code path for regime-aware Kelly sizing is wired. `KellyCalculator` now
looks up `REGIME_PRIORS[regime][strategy]` before falling back to
`ALL_REGIME_FALLBACK`. Per-strategy per-regime Kelly profiles, Bayesian
blending of prior + live trade results, rebuild on regime change and every
50 candles.

### Phase 2c.1 — REGIME_PRIORS Calibration — PENDING (auto-fulfilled by Phase 3d)

`portfolio/kelly.py:223` declares `REGIME_PRIORS` as an empty dict. Every
lookup currently falls back to `ALL_REGIME_FALLBACK` (line 141). Regime-
aware Kelly is wired but inactive — effectively identical to pre-Phase 2c
behavior until Phase 3d produces per-regime Sharpe data for surviving
strategies.

Calibration remains automatic once Phase 3d runs. **Conditional on Phase
4.A and/or 4.B producing at least one survivor**; if Branch C is
confirmed by data (no survivors), `REGIME_PRIORS` stays empty by design
and Phase 2c.1 closes as not-applicable.

### Phase 3a — COMPLETE (commit `f2d29cf`)

Backtest redesign. Per-strategy symbols via `config.STRATEGY_SYMBOLS`, L1
OHLCV parquet cache with 24h TTL, DualMomentum multi-symbol rotation,
`base.py` kwarg fix.

### Phase 3a.1 — COMPLETE (commit `abb796e`)

Supertrend and BearShort vectorization. ~10× speedup on the two slowest
strategies. Supertrend math is identical; BearShort drifts within noise.

### Phase 3b — COMPLETE

Statistical validation framework. Built incrementally as 4-chunk human-
gated commits.

**Chunks 1-6 complete (2026-04-25):**
- **Chunk 1:** `backtest/holdout.py` accessor module with strict
  single-access enforcement and structured caller validation
  (`phase.strategy_id.purpose` regex grammar). `backtest/logs.py`
  JSONL plumbing.
- **Chunk 2:** `backtest/generate_holdout_manifest.py` with
  `generate_initial()` and `regenerate_manifest()` entry points.
  `backtest/holdout_manifest.json` generated for all 10 strategies.
  Calendar 80/20 split at 2025-09-12 UTC (~29 months dev, ~7 months
  holdout). `backtest/holdout_access.log` initialised empty.
- **Chunk 3:** Cache-layer enforcement in `backtest/cache.py`.
  `HoldoutBypass` raised on any read overlapping holdout window
  unless caller is `load_holdout` (via contextvar).
  `EnforcementManifestMissing` and `EnforcementManifestMalformed`
  raised on bad manifest state — no silent fallback. `backtest/runner.py`
  routes dev-only via `until_ts=get_symbol_dev_cutoff(sym)`.
- **Chunk 4:** `docs/validation_framework.md` corrected to match
  implementation (50/30/20 → 80/20 dev-only split; CPCV span
  reference fixed; infrastructure pointer section added).
- **Chunk 5 (commit `a7361a3`):** `backtest/trials.py` —
  schema-validating JSONL writer for `backtest/trials.log`.
  Schema v1, sacred-harness-adjacent. Per-trial-type required-field
  enforcement (smoke / full_cpcv / final_gate). Canonical sha256
  `params_hash`. Final-gate guard cross-referenced against
  `holdout_access.log`. Public API: `record_trial`,
  `count_trials_for_dsr`, `count_distinct_variations`,
  `read_trials`, `latest_final_gate`. **Schema also supports
  `superseded_by: '<fix-commit-sha>'` tagging; `count_trials_for_dsr`
  filters tagged rows out of the multiple-testing budget** (see
  `docs/open_questions.md` resolved entry "Trials.log invalidation
  policy after simulator fix").
- **Chunk 6:** `backtest/cpcv.py` implements block Sharpe
  distribution (NOT López de Prado path-CPCV — see
  `docs/validation_framework.md` § "Block Sharpe distribution"
  for why path reassembly was rejected for rule-based strategies
  with no fit/predict split). `run_cpcv` runs the engine once per
  block via a `strategy_factory` pattern, computes per-block
  Sharpe via the engine's formula, applies purge/embargo at block
  boundaries, and produces an N-element Sharpe distribution that
  feeds DSR. `CPCVConfig.k_held_out` is reserved for future
  fit/predict-capable strategies. 133/133 tests pass.

All Phase 3b infrastructure shipped: holdout split, trials.log writer
(with supersession tagging), block-Sharpe CPCV, DSR, MinTRL, buy-and-hold
baseline, verdict tree, threshold calibration. See
`docs/validation_framework.md` for the live spec.

**Phase 4.A and Phase 4.B both run within this harness unchanged. The
harness is the gate; Phase 4.A and 4.B are inputs to it.**

### Phase 3c — RAN, 9/10 RETIRE + 1/10 UNDER_TESTED (2026-04-26)

All-strategy dev_cpcv ran 2026-04-25 against `sr_zero_expected = +1.9007`
at N=20. Result: 9/10 RETIRE + 1/10 CPCVError (MeanReversion, treated as
`under_tested`). Zero strategies cleared the threshold. Only VWAP beat
its baseline (+1.14 vs +0.68 ETH B&H) but failed the multiple-testing
null. Detailed empirical breakdown: `docs/strategy_evidence_audit_2026-04-26.md`.

BearShort post-fix re-run (commit `25bd843`, 2026-04-26): observed_sharpe
−2.9643 (was +1.3129 pre-fix), all-quantiles-negative dist, RETIRE. Sign
flipped clean from the simulator short-pnl bug; magnitude amplified ~2.2×
by balance-scaled compounding asymmetry. Branch B (BearShort-only
deployment) effectively foreclosed by the post-fix verdict.

This empirical result is the anchor for the Phase 4 scope decision (Branch
C selected) and for the Phase 4.A resurrection scope below. It is not
softened or revisited by the resurrection exploration — Phase 4.A asks
whether retired strategies *can be unstuck with new evidence*, not whether
the original Phase 3c finding was correct.

### Phase 3d — CONDITIONAL

Portfolio-level validation of strategies that survive Phase 4.A/4.B.
Pairwise correlation check, portfolio-level DSR, and a buy-and-hold
baseline that the combined portfolio must beat.

**Inverse-volatility strategy weights + Barroso-Santa-Clara vol scaling.**
Per forecast-combining research (see `docs/research_log.md`, section on
forecast combining), this captures most of the empirical uplift of full
forecast-combining machinery with ~1-2 days of work. Applied to surviving
strategies only. Each strategy's position gets scaled by
`target_vol / rolling_vol_30d` (Barroso-Santa-Clara), and strategies are
weighted within regime buckets by inverse of their realized return
volatility.

Phase 3d runs only if Phase 4.A or 4.B produces ≥2 survivors. With 1
survivor, see Phase 4.C. With 0 survivors, Branch C is confirmed and
Phase 3d closes as not-applicable.

### Phase 4 — Branch C selected (2026-04-26) + Resurrection-and-Extension exploration (2026-04-29)

**Top-level status:** Branch C remains the default direction based on
the Phase 3c empirical anchor (9/10 RETIRE + 1/10 UNDER_TESTED on the
1H single-pair substrate). **A structured pre-commitment exploration
has been approved before final commitment to Branch C-only:** Phase
4.A applies new evidence (chat 2026-04-29 research synthesis) to the
retired strategies via the existing harness; Phase 4.B introduces
funding-rate harvest as a new substrate. Whichever strategies pass the
Phase 3b verdict tree get to live; whichever fail stay retired.

**This is structurally a Branch A-prime + Branch C exploration, judged
by the existing validation harness. Branch C remains the explicit
fallback if nothing passes.** Phase 4.A and 4.B do not reverse Branch
C; they test whether the data justifies amending it.

**No calendar timelines.** Phases gate on results, not dates. Phase 4.A
runs until its verdicts are in; Phase 4.B starts after 4.A; Phase 4.C
decides afterward.

**Operational state during Phase 4.A/4.B:** the DigitalOcean droplet is
**paused** to avoid paying for idle compute. Paper trading and live
deployment are both gated behind backtest survival. The droplet stays
off until Phase 4.A or 4.B produces a passer. If nothing passes, the
droplet stays off — that is Branch C confirmed by data.

#### Phase 4.A — Resurrection Batch

**Scope.** Apply the chat 2026-04-29 research synthesis to the retired
strategies as starting hypotheses. Each retired strategy is treated as a
candidate for resurrection-by-redesign. Hypotheses are *starting points*,
not commitments — variation space remains open within each strategy
under the existing iteration discipline (`CLAUDE.md` no-p-hacking rule;
20-variation cap per strategy; theoretical justification per variation;
3-failure escalation).

**Harness.** Existing Phase 3b harness, unchanged. Same DSR / CPCV /
MinTRL / B&H baseline / verdict tree. Same `trials.log` discipline.
**Trials.log supersession tagging (`superseded_by: '<fix-commit-sha>'`)
is in place from Phase 3b Chunk 5; pre-fix BearShort row does not count
toward Phase 4.A's multiple-testing budget.** Phase 3c rows count as
prior trials per the existing schema.

**Execution model.** Resurrections may be developed in parallel within
the harness — they share substrate (spot OHLCV, current fee model)
where the strategy's redesign keeps that substrate. No iteration cap
on the *batch* (each strategy individually retains the 20-variation cap
per `CLAUDE.md`). Hypothesis-required-per-trial discipline applies.

**Pass/fail.** The existing Phase 3b verdict tree, applied unchanged.
Pass → strategy is restored and proceeds to Phase 3d. Fail → strategy
stays retired.

**Per-strategy starting hypothesis table.** These are *starter points*
from the chat 2026-04-29 research synthesis. Variation space within
each strategy remains open per the iteration discipline above.

| Strategy | Phase 3c verdict | Starting hypothesis | Resurrection status |
|---|---|---|---|
| Supertrend | RETIRE, −1.64 dev | Daily TF + Barroso-Santa-Clara vol-scaling, regime-gated to trending only | Retired (Phase 4.A trial #1, 2026-04-29 — daily-TF density floor) |
| TrendFollowing | RETIRE, −1.77 dev | Daily multi-asset, HOP-style vol-targeting, ≥10 instruments | Resurrect candidate (harness extension required, see below) |
| DualMomentum | RETIRE, −2.39 dev | Weekly TF, ≥5 majors per Liu/Tsyvinski/Wu (2022) | Resurrect candidate |
| MeanReversion | UNDER_TESTED (CPCVError) | Rebuild as **BTC-residual mean-reversion on alt basket** (not absolute-price MR) | Resurrect candidate (harness extension required, see below) |
| VolatilityBreakout | RETIRE, −3.62 dev | Daily multi-coin, relative-volume selection, **redesigned exit rule** (current 1-candle exit guarantees negative EV regardless of entry) | Resurrect candidate (harness extension required, see below) |
| Breakout | RETIRE, −1.33 dev | Zarattini-style daily ensemble (lookbacks 5/10/20/30/60/90/150/250/360) on top-20 rotational basket | Resurrect candidate (harness extension required, see below) |
| GridTrading | RETIRE, +1.50 obs | Demote to **regime-conditional only** — fires only when regime detector confirms range/low-trend/mid-vol; otherwise dormant | Resurrect candidate (narrowed scope) |
| VWAP | RETIRE, +1.14 obs | Retire fully; consider folding into MeanReversion as a filter signal if useful | **Hard retire (not in resurrection batch)** |
| DCA | RETIRE, +1.35 obs | Demote to scheduled monthly fiat inflow into BTC/ETH, configured outside the Kelly-sized strategy portfolio, not subject to the deploy gate | **Architectural demotion (not in resurrection batch, not a strategy)** |
| BearShort | RETIRE, −2.96 dev (post-fix) | Excluded from Phase 4.A. Not eligible for resurrection on the existing substrate. Future BearShort variation requires its own substrate-change rationale (e.g., Phase 4.B perp + funding-rate-aware short) — not a permanent ban, but no Phase 4.A entry. | **Excluded from 4.A** |

**Harness-extension scope inside Phase 4.A.** Three resurrections name
a starting hypothesis that is not drop-in on the current spot-OHLCV +
single-pair backtest infrastructure. The harness extension is scoped
*before* the strategy backtest runs, not during, and is the first
deliverable of each named resurrection:

- **TrendFollowing daily multi-asset (≥10 instruments)** — requires
  per-instrument data ingestion at scale, holdout manifest entries for
  the new symbol set, position-management for simultaneous holdings
  across the basket (not single-rotational-pick), and per-instrument
  vol-targeting under HOP. Engine path needs to support concurrent
  long positions across symbols.
- **Breakout Zarattini-style daily ensemble on top-20 rotational
  basket** — requires the same multi-asset data ingestion as above plus
  the rotational-basket selection logic (top-20 by some criterion,
  refreshed at a defined cadence) and ensemble lookback aggregation.
- **MeanReversion BTC-residual on alt basket** — substrate change.
  Requires alt-basket data ingestion, beta-estimation pipeline (alt
  return regressed on BTC return over a rolling window), residual
  return computation, and pair-trading-style execution (long alt /
  short BTC at residual extremes, or similar). Not a parameter change
  to the existing MeanReversion implementation.

These extensions land before each strategy's first dev_cpcv trial.
They are sacred-harness-adjacent (touching `backtest/cache.py`,
`backtest/holdout_manifest.json` symbol set, and engine position
management) but do not change the harness's statistical contract
(DSR / CPCV / MinTRL / verdict-tree). `CLAUDE.md` schema-stable-code
rule applies: contract-preserving extensions proceed agent-autonomously,
contract changes require human approval.

#### Phase 4.B — Funding-Rate Harvest (and similar delta-neutral additions)

**Scope.** Long spot + short perp, equal notional, delta-neutral. Income
source: positive funding rate paid to shorts during normal/bullish
markets. Strongest peer-reviewed support of any retail-accessible crypto
strategy candidate identified to date (multiple 2024–2025 papers; baseline
~10.95% annualised funding APY before edge selection — see
`docs/research_log.md` 2026-04-29 entry).

**Sequencing.** Starts after Phase 4.A's verdict batch is in. Substrate is
materially different from 4.A (perp + spot, two-leg position management,
funding rate as primary income source) and 4.B requires harness extensions
that 4.A does not need. Sequencing is therefore hybrid sequential, not
fully parallel — 4.A first, 4.B second, on the same harness.

**Harness extensions required (deliverables of Phase 4.B before its first
dev_cpcv trial).**
- Perp data ingestion (OHLCV + funding rate timeseries).
- Funding rate ingestion at the funding-settlement cadence (8h on most
  venues).
- Two-leg position management (concurrent long spot + short perp on the
  same underlying, equal notional, delta-neutral).
- Funding cost model in the simulator (funding paid/received at
  settlement times applied to the short-perp leg's cash balance).
- Holdout manifest entries for the perp + spot pairs in scope.

**Risk to engineer for explicitly.**
- Liquidation of the short-perp leg on a sharp upside spike (margin
  management on the short leg; long spot leg has no liquidation but
  doesn't help the short).
- Funding-rate flip to negative (strategy becomes a funding payer; exit
  rule must handle this).
- Exchange counterparty risk on the spot leg (custody risk distinct from
  the perp's clearing risk).

**Pass/fail.** Same Phase 3b verdict tree, applied unchanged.

**Open question — RESOLVED 2026-04-29:** Phase 4.B venue locked to OKX
(USDT-M perp + USDT spot), accept Thai PIT on funding income (Branch 1
of three branches surfaced at scoping). The 2025–2029 Thai PIT
exemption applies only to SEC-licensed digital-asset operators, none of
which currently offer perpetual futures with funding-rate settlement.
The April 2026 SEC consultation (release No. 81/2026, closes 20 May
2026) proposes a path to licensed perp products but no Thai venue
offers the substrate today. OKX selected over Binance.com because the
bot's existing OKX paper-mode plumbing reduces operational migration
cost. See `docs/open_questions.md` § "Phase 4.B venue choice" and
`docs/research_log.md` § "Thai SEC venue / derivatives status (logged
2026-04-29)" for the full evidence base. Watch item: a Thai-SEC-
licensed exchange launches a perp + funding product with SEC-final
derivatives licensing — at that point 4.B (or surviving live-deploy
of a 4.B passer) migrates to the licensed venue for the PIT
exemption.

**Variation scope — locked 2026-04-29 (persisted 2026-04-30):**
Variation #1 = single-pair (legs: spot BTC/USDT + perp
BTC-USDT-SWAP). Multi-pair top-N-from-basket selection is
Variation #2 with its own hypothesis-of-record entry,
manifest schema extension, and CPCV multi-pair path
verification. Source: chat 2026-04-29 venue scoping pre-trial
gates list (gate #8). See research/funding-rate-literature.md
§ "Pre-trial gates (locked)" for the persistent
statement of this constraint.

**Variation #1 status — RETIRED 2026-05-02.** First full holdout/final_gate run on Variation #1 (single-pair BTC delta-neutral, locked params per `research/funding-rate-literature.md`) failed the verdict tree on regime-decay holdout (dev_cpcv +5.17 mean → holdout decay reflecting Schmeling et al. 2025 negative-funding regime). Variation #2 design constraint: structural redesign sourced from a specific paper (different leg construction, different instrument family, or different rebalancing rule) — NOT a parameter perturbation of V1. V2 hypothesis-of-record entry pending in research/funding-rate-literature.md. Per CLAUDE.md no-p-hacking rule, V2 cannot enter trials.log without peer-reviewed source citation.

**Variation #2b + strategy status — RETIRED 2026-06-11 (Phase 4.B closed).** V2b (vol-regime-conditional structural redesign per Almeida et al. 2024) passed the old bull-heavy dev window (+2.90, 2026-05-08) but failed its holdout (−1.14) and failed the 2026-06-11 extended-window re-test under gate spec v2 (dev 2021-08-31→2025-05-01: sharpe +0.5007, family DSR 0.044 vs ≥0.95, MinTRL ~20y at realized SR; trial 2567dbd3). Two structural designs failed across three windows — carry edge is a 2023–2024 regime artifact, consistent with Schmeling et al.'s own sub-sample decay. Strategy archived to `strategies/archive/funding_rate_harvest/` with kill report; shared perp/funding harness retained for future two-leg candidates. Phase 4.C tally: Phase 4.B contributes 0 passers.

#### Phase 4.C — Branch decision (revisited)

After 4.A and 4.B verdicts are in, the Branch A vs Branch C question is
re-decided **with data**, not before:

- **≥2 strategies pass** (any combination of resurrections + funding-
  rate): proceed to original Phase 4 paper-deploy plan (paper deploy of
  validated portfolio, 4-week monitoring vs backtest expectations, no
  live money) per the description below. Branch A path partially
  confirmed; the validated portfolio reflects what survived, not the
  original 10.
- **1 strategy passes**: portfolio-of-one is high-risk (zero
  diversification, single-point-of-failure). User decides whether to
  deploy the single strategy, defer for more candidates, or commit to
  Branch C anyway. Tracked as an open question to revisit at that point.
- **0 strategies pass**: Branch C is NOT immediately confirmed; the autonomous research loop (architecture.md § D.4 closing paragraphs) continues. The loop driver is Strategist's scope extension with the `next-variation-selector` skill, reading research_queue.md (T2). When V1 retires, the loop selects the next citation lead for testing; retired strategies remain on cooldown queues (initial 30d, 60d after re-test, capped at 180d) per architecture D.4. **Wind-down decision is deferred to a separate gate**: triggers when the loop produces N additional consecutive failures (N to be specified at gate-decision time) AND the analyst-overlay Phase B gate has resolved. Until then, the project is in continuous-research mode, not wind-down. Preserves the validation harness as substrate-agnostic infrastructure regardless of outcome.

#### Phase 4.D — Analyst Overlay (Phase A shadow mode)

**Scope.** Add 5 LLM agents (market-analyst, social-analyst, news-analyst, fundamentals-analyst, research-manager) to the existing fleet (15 → 20 agents). Research-manager synthesizes the 4 analyst reports into binary strategy enable/disable flags + binary risk flags. Cross-model dual pass (Sonnet primary + Gemini secondary). See architecture.md § D.4 for full workflow.

**Phase A — Shadow mode, no live wiring.** Synthesis output is written to disk only; portfolio.manager.py and CapGuard do NOT read it. Duration: 2 months minimum (~180 cycles at 8h cadence aligned to OKX funding settlements). Cost: ~$32/mo at 8h cadence; ~$65/mo at 4h. Capped at $30/mo via existing `budget-check.sh`.

**Phase B — Gate decision.** Chat-side, after ≥30 paired observations of (verdict_outcome, concurrent_synthesis). Metric chosen at gate-decision time. Three outcomes: proceed to Phase C live wiring; keep gathering data; retire the overlay.

**Phase C — Live wiring (post-gate).** Synthesis flags drive CapGuard excluded_strategies + portfolio.manager.py rebalance risk-flag check. paper_mode=True for first month; live-mode wiring is separate Phase 5 deploy decision.

**Sequencing.** Phase 4.D Phase A runs in parallel with Phase 4.B/4.C exploration — analyst overlay is shadow-only and doesn't touch live strategies, so it doesn't gate anything else. Phase 4.D Phase B gate decision is independent of Phase 4.C branch decision; both can resolve in any order. Phase 4.D Phase C wiring depends only on its own gate, not on Phase 4.A/4.B/4.C outcomes.

**Why this is added.** TradingAgents repo (chat 099a169c, 2026-05-03) was reviewed and pivot-adapted: its analyst-layer pattern adapts to crypto with discrete strategy enable/disable + binary risk flags (overlay v3), not full TradingAgents-style trade execution. This adds market context to the autonomous research loop's direction-finding (Phase 4.C continuation) and provides regime-aware sizing inputs for surviving strategies (if Phase 4.B/4.C produces survivors).

#### Phase 4.E — Microstructure / Order-Flow batch (added 2026-07-03, human pre-authorized)

**Full design: `docs/redesign_proposal_microstructure_2026-07-03.md`
(canonical for this phase). Summary:**

**Scope.** A new strategy family cluster `microstructure-orderflow`
fed by genuinely new input data: 1m volume-at-price (volume profile)
and per-candle taker buy/sell aggressor volume (order-flow delta).
Seven pre-registered starting hypotheses: VolumeProfileAcceptance,
LiquiditySweepReversal, LVNTraversal, HVNMeanReversion,
DeltaDivergence, VWAPInstitutionalBand, BreakoutDeltaConfirmed.
Concepts sourced from practitioner literature (volume profile /
order flow / ICT), mechanized into exact rules in per-strategy
literature files BEFORE any trial runs, per the no-p-hacking rule's
written-hypothesis path.

**Statistical rationale.** MinTRL is calendar-bound: ~2.7y validates
only true SR ≥ 1.0. Intraday microstructure is the only strategy
class whose plausible true SR (1.2–1.5+ net) sits inside the
validatable zone on available data. New family cluster ⇒ family-layer
multiple-testing null starts fresh (legitimate because the data
substrate is genuinely new, not a re-slice of daily closes).

**Data substrate.** Binance Vision free public data (spot monthly 1m
klines incl. taker buy volume; optionally aggTrades later). Research
substrate is Binance; execution venue remains OKX. Cross-venue
provenance disclosure per the 2026-06-11 BNB-backfill precedent.
Verified 2026-07-03: BTCUSDT-1m-2021-01 downloads, parses, taker
split present; ~2.4 MB/month zipped; total basket cost $0.

**Batch-specific gate (locked).** Every trial runs at standard taker
fees + slippage AND at 2× fees; edge must survive both or the
verdict is retire. Intraday cost realism is the known killer of this
strategy class.

**Discipline.** Existing rules unchanged: 7 enumerated starting
hypotheses, 20-variation cap per strategy, 3-consecutive-failure
batch stop, every trial appends to trials.log, no grid searches.
Timeframe per strategy (expected default: 15m–1h signal bars on 1m
profile data).

**Sequencing.** (1) Data layer (`data/binance_vision.py`,
`data/microstructure.py`) + tests — agent-autonomous infrastructure.
(2) Literature files with locked pre-trial gates for all 7. (3)
Manifest entries — rides on the open dev/holdout boundary question
(docs/project_diagnosis_2026-07-02.md §4); new substrate rows need
their own dev/holdout split decision (human). (4) Trials through the
unchanged harness.

**Relation to parked work.** The four near-misses (CSMom,
AltcoinSeason, NewsSent, AttentionMom) stay parked; their trial
budget is not spent during 4.E. Paper trading stays deferred per
Kanin 2026-07-03 until a backtest survivor exists.

#### Phase 4 (paper deploy) — applies only if Phase 4.C produces ≥1 deployable strategy

Paper deploy of the validated portfolio (or single strategy in the 1-
survivor case). 4-week monitoring vs backtest expectations. No live money
in this phase.

> **Open question (pre-Phase 4 paper deploy):** Decide deployment
> mechanics — deploy to existing server with fresh $100k paper state, or
> preserve current paper state and deploy alongside for comparison?
> Decision needed before paper deploy begins.

### Phase 5

Live deployment decision. Separate gate, requires Phase 4 paper monitoring
to be clean (paper behaviour within expected bounds, no unexplained equity
drift, risk guards firing correctly). If Phase 4.C produced 0 survivors,
Phase 5 does not apply on this project; Phase 5 direction (prediction
market bot or alternative) is acknowledged as out-of-scope and tracked
separately.

## Deploy gate

A strategy is allowed onto paper deploy only when all of the following hold:

1. DSR on holdout data clears the Phase 3b threshold.
2. Portfolio-level DSR of the combined survivors clears the threshold
   (Phase 3d).
3. The combined portfolio beats a buy-and-hold baseline on the same
   instruments over the same period.
4. Per-regime Sharpe attribution has been recorded and fed into
   `REGIME_PRIORS` where applicable.

A paper-deployed portfolio is allowed onto live only when Phase 4 paper
monitoring concludes cleanly. These gates are not negotiable and are not
bypassable by agents (see `CLAUDE.md`).

## Out of scope for now

The 2026-04-17 plan listed funding-rate arbitrage, ML regime detection,
TradingView integration, LLM-as-signal, and a crisis-alpha strategy as
Phase 4+ items.

**Funding-rate arbitrage / harvest has been promoted out of "out of scope"
into Phase 4.B (2026-04-29).** The remaining items (ML regime detection,
TradingView integration, LLM-as-signal, crisis-alpha) stay parked. There
is no point expanding alpha surface further before Phase 4.A and 4.B
verdicts are in.

## Future phases (deferred)

### Forecast Combining — DEFERRED

Pysystemtrade-style continuous forecast aggregation was evaluated in detail (see `docs/research_log.md` section on forecast combining). Decision: defer until after Phase 5. Revisit only if Phase 4 paper-trading monitoring reveals concrete pathologies — specifically: CorrCap hard-blocks causing measurable opportunity cost (>1-2% annualized drag), or regime transitions causing discrete-switching whipsaw losses.

Evidence summary: Carver's own tests show the approach is "indistinguishable" from simpler alternatives in Sharpe terms; DeMiguel et al. (2009) and its 2024 replication show equal-weight combining is nearly impossible to beat out-of-sample; crypto-specific volatility structure introduces new failure modes (FDM leverage creep during regime shifts). Phase 3d's inverse-vol weights + vol scaling captures most of the demonstrated benefit at a fraction of the implementation cost.

### Passivbot Evolutionary Optimization — RETIRED

Considered as parameter search tool. Retired because it conflicts with the project's DSR-based validation discipline: evolutionary search generates large numbers of parameter variations, which directly inflate the multiple-testing count in `trials.log` and the DSR haircut. The CPCV + DSR framework is specifically designed to prevent this class of p-hacking; adopting Passivbot would undermine the integrity principle.

### Profit Reserve System — RETIRED

Considered as auto-transfer of profits to OKX Earn. Retired because it is premature optimization: there are no validated profitable strategies yet to reserve profits from. If Phase 5 succeeds and the bot becomes consistently profitable in live mode, a simpler manual monthly skim satisfies the same goal without the operational complexity of auto-transfer integration.

## 3-Year Backtest Cross-Strategy Lessons (REFERENCE)

These cross-strategy patterns from the 2026-04-19 backtest write-up
remain valid as design discipline regardless of which Phase 4 path runs.
They directly inform the Phase 4.A resurrection variation discipline.

- **Win-rate alone is meaningless.** DCA had 92% win rate and lost
  money (avg loss > avg win × loss-frequency). Win rate without payoff-
  ratio is not edge.
- **Structural exit design can guarantee negative EV.** VolatilityBreakout's
  1-candle exit ensured negative expected value regardless of entry quality.
  Exit rules must be designed against the entry's edge, not as boilerplate.
- **OOS-better-than-IS is the anti-overfit signature.** VWAP's IS Sharpe
  +1.00 → OOS Sharpe +2.30 is the inverse of the canonical overfitting
  pattern and should be preserved as a "not overfit" diagnostic when it
  appears in resurrection backtests.
- **Pair-specific overfitting is real.** Breakout's IS-on-AVAX collapse
  in OOS confirms that single-pair channel breakout calibration does not
  generalise. Multi-asset rotation is the structural fix, not a
  parameter retune.

## Capital and operational context

- Bot: Python multi-strategy, OKX USDT-M futures, paper trading.
- Server: `kanin@104.248.145.189` (DigitalOcean Singapore), currently on
  commit `4a51f0b`. **Droplet paused 2026-04-29** pending Phase 4.A/4.B
  passer.
- Repo: `kaninsexy/crypto-bot`, local at `~/Documents/crypto-bot`.
- Deploy: git push → SSH → `sudo bash -c "cd /home/botuser/crypto_bot && git pull"`
  → `sudo systemctl restart cryptobot cryptodashboard`.
- Paper capital: $100,000 (fresh restart 2026-04-26 post-shortpnl-fix).
- Live deployment remains future work; see Phase 5.