# Bot Status

Last updated: 2026-06-11 (holdout regeneration + gate-spec-v2 re-run batch)
Supersedes the 2026-04-17 snapshot (preserved in git history).

## Holdout regeneration 2026-06-11 (extended window) — DISCLOSURE

The holdout split was regenerated on 2026-06-11 (human pre-authorized,
gate-spec-v2 re-run work order; audit events in
`backtest/holdout_access.log`, caller
`phase4.gate_v2_rerun_batch.manifest_regen`):

- `data_start` = max(2021-01-01, first available candle per substrate).
  Backfilled bounds: BTC/ETH/SOL/XRP/ADA/AVAX/DOT/LINK/LTC/UNI spot 1d
  + BTC spot/perp 1h from 2020-11-10/11 (OKX archive depth);
  **BNB/USDT from 2022-12-21** (OKX listing — this caps every
  BNB-containing basket's intersection); BTC funding from
  **2021-08-31** (OKX archive starts 2021-09); Google Trends
  2021-05-02 → 2026-05-03 (pytrends 5-y window).
- Single global boundary: `dev_end = holdout_start =`
  **2025-05-01T00:00Z**; `data_end` = latest complete day (2026-06-11
  bound, earlier where a substrate ends earlier). Dev ≈ 52 months for
  BTC-only substrates, 44 months for FundingRateHarvest_BTC
  (funding-bound), **28.3 months for BNB baskets**.
- Holdout single-access enforcement stays ON; unattributed
  regeneration events no longer reset it (2026-06-11 harness fix).

**Contamination disclosure (required reading before any new-holdout
verdict):**

1. **AttentionMomentum** and **FundingRateHarvest_BTC** evaluated the
   OLD holdout window (2025-09-22 → 2026-04/05) on 2026-05-08;
   verdicts were observed (params NOT retuned since). Their
   new-holdout verdicts carry a contamination asterisk.
2. **Global overlap (applies to every strategy, not just the two
   above):** the new holdout_start (2025-05-01) is EARLIER than the
   old one (2025-09-12/22), so the new holdout's first ~4.5 months
   were inside the OLD dev window, which every pre-2026-06-11 trial
   read freely. No strategy's new-holdout verdict is on fully unseen
   data; the only fully virgin segment is (old data_end ≈ 2026-04/05,
   new data_end]. The work-order text described non-rerun strategies'
   new holdout as "clean" — that holds only for the post-2026-04
   tail; recorded here so the asterisk is applied evidence-side, not
   prompt-side.

## Current state

- **Current commit:** `80fc2c9` — Phase 3b Chunks 1-6 stack pushed 
  to `main` (6 commits on top of `f2d29cf`: holdout accessor + 
  JSONL plumbing; manifest generator + initial manifest; cache 
  enforcement + runner dev-only loading; validation_framework.md 
  correction; `trials.py` writer + `cpcv.py` skeleton; `cpcv.py` 
  block Sharpe distribution). Plus the earlier research 
  consolidation commits (Phase 3a.1 vectorization, docs 
  consolidation, milestone backtest reports, research_log 
  Phase 1-3a findings).
- **Working tree:** clean.
- **Server deployment:** `kanin@104.248.145.189`, still on older commit `4a51f0b`
  (pre-Phase 3a). The server has NOT been updated with Phase 3a changes yet.
- **Bot state:** OKX paper mode. 10 strategies configured in code, of which
  3 are genuinely working, 4 are broken (diagnosed), 3 are borderline
  (need rescue or retire decision).

## Per-strategy status — Phase 3c verdicts (2026-04-26)

Source: `backtest/trials.log` rows from 2026-04-25 19:48 onward + the
BearShort post-fix re-run on commit `25bd843`. All strategies tested at
N=20 against `sr_zero_expected = +1.9007`.

| Strategy | Symbol | Phase 3c verdict | observed_sharpe | dist mean | n_trades | Notes |
|---|---|---|---|---|---|---|
| DCA | BTC/USDT | RETIRE | +1.3527 | +2.0295 | 194 | Failed multiple-testing null at N=20 |
| Supertrend | ETH/USDT | RETIRE | -1.6388 | -1.6235 | 302 | Net-losing in dev |
| GridTrading | SOL/USDT | RETIRE | +1.5004 | +2.3377 | 1035 | Failed multiple-testing null |
| Breakout | AVAX/USDT | RETIRE | -1.3337 | -1.3870 | 134 | Net-losing |
| TrendFollowing | BTC/USDT | RETIRE | -1.7708 | -1.7672 | 374 | Net-losing |
| BearShort | BTC/USDT | RETIRE | -2.9643 | -3.4565 | 198 | Post-fix re-run on commit `25bd843`; pre-fix +1.3129 was sign-inverted by simulator bug |
| VWAP | ETH/USDT | RETIRE | +1.1389 | +1.9539 | 319 | Beat baseline (+0.68 ETH B&H); failed multiple-testing null |
| VolatilityBreakout | BTC/USDT | RETIRE | -3.6221 | -3.9081 | 1687 | Net-losing |
| DualMomentum | BTC/USDT (rotates) | RETIRE | -2.3906 | -2.2850 | 1095 | Net-losing on dev_cpcv; supersedes the incomplete-3-year-run open question |
| MeanReversion | ETH/USDT | UNDER_TESTED | (CPCVError) | — | — | 4-filter stack self-suppressed below `_MIN_TRADES_PER_BLOCK` in 7/10 blocks; no row written (atomicity) |

Phase 3c bucket summary: 9/10 RETIRE + 1/10 UNDER_TESTED (MeanReversion).
Zero strategies cleared the deploy gate. Phase 4 branch decision pending
per `docs/open_questions.md`.

See `docs/strategies.md` for per-strategy diagnosis and next actions
(includes both the Phase 3c verdict and the historical 3-year-backtest
diagnosis), `docs/strategy_evidence_audit_2026-04-26.md` for the
mechanism-level audit, and `docs/strategy_failure_analysis_2026-04-19.md`
for the original failure write-up.

## Phase 4.A resurrection batch — running results

| Strategy | Variation | Trial date | Result | Status |
|---|---|---|---|---|
| Supertrend | `phase4a-daily-resurrection-v1` | 2026-04-29 | CPCVError (per-block trades < 5 floor); headline Sharpe +1.12 vs B&H +0.68 on 13 trades, but `under_tested` per `min_trade_count=30` | Retired — no variation #2 (literature pre-condition) |
| DualMomentum | `phase4a-weekly-5basket-v1` | 2026-04-29 | CPCVError (4/10 valid blocks; warmup amortization on block-isolated CPCV); headline Sharpe −1.20 / 44 trades single-pass; valid-block Sharpe mean −1.11 | Retired — no variation #2 (academic foundation exhausted) |
| GridTrading | `phase4a-regime-conditional-v1` | 2026-04-29 | RETIRE (full_cpcv, all 10 blocks valid). observed_sharpe +0.8805 vs sr_zero_expected +1.9007 (margin −1.02) and SOL B&H +1.8133 (margin −0.93); n_trades 601 (down from Phase 3c 1035, gate −42%); dist mean +2.40 / std 1.37; mt_mean_pass=False, baseline_pass=False | Retired — no variation #2 (no citation supports gate / param / basket perturbation under no-p-hacking) |

`count_distinct_variations("Supertrend")` = 2 / 20 (slot consumed by
the retired variation; cap effectively closed per the
indicator-provenance pre-condition).
`count_trials_for_dsr("Supertrend")` = 1 (smoke row excluded — DSR
multiple-testing inflation unaffected by this trial).

`count_distinct_variations("DualMomentum")` = 2 / 20 (slot consumed
by the retired variation; cap effectively closed per the
academic-foundation-exhausted precondition — Liu/Tsyvinski/Wu 2022
parameters are at the academic standard, further variation requires
a citation that does not exist).
`count_trials_for_dsr("DualMomentum")` = 1 (smoke row excluded — DSR
multiple-testing inflation unaffected by this trial; Phase 3c
`rescue-default` row remains the sole full_cpcv contributor).

`count_distinct_variations("GridTrading")` = 2 / 20 (slot consumed
by the retired variation; cap effectively closed — none of the
candidate variation #2 paths (gate widening, parameter perturbation,
multi-pair basket) clears the no-p-hacking bar, see
`research/gridtrading-literature.md` § "Why no variation #2").
`count_trials_for_dsr("GridTrading")` = 2 (full_cpcv row, NOT smoke
— the gate produced 601 trades distributed across all 10 blocks, so
the harness produced a clean verdict; both this row and the Phase
3c `rescue-default` row count toward DSR multiple-testing).

Source: `research/supertrend-literature.md` § "Trial #1 outcome
(2026-04-29)", `research/dualmomentum-literature.md` § "Trial #1
outcome (2026-04-29)", and `research/gridtrading-literature.md` §
"Trial #1 outcome (2026-04-29)" for the full forensics;
`backtest/trials.log` last three rows for the harness records.

Cross-strategy harness finding: Supertrend and DualMomentum hit
`CPCVError` for the same structural reason (block-isolated CPCV
pays the warmup window in every block, eating a significant
fraction of each ~2078-candle block on the current dev-window
length). GridTrading did NOT hit this — its 24-candle warmup is
small relative to block size, the gate produced 601 trades, and
all 10 blocks were valid. Tracked as an open harness-design
question in `docs/open_questions.md` § "Block-isolated CPCV
warmup amortization (structural)".

## Phase 4.B kickoff — data layer running results

Tracks A-D delivered 2026-04-29; Track C drift caught and
corrected 2026-04-30.

| Track | Deliverable | Status |
|---|---|---|
| A | data/okx_perp.py — OKX USDT-M perp OHLCV ingestion (CCXT swap-mode, separate cache namespace) + 13 unit tests | Landed |
| B | data/okx_funding.py — funding-rate history with 1h mark-OHLCV snap-merge + 12 unit tests | Landed |
| C | research/funding-rate-literature.md — hypothesis-of-record with Variation #1 single-pair + Variation #2 stub, pre-trial gates persisted | Landed (drift corrected 2026-04-30) |
| D | research/funding-rate-risk-model.md — per-leg margin, liquidation, funding payment math, exit triggers (variation-agnostic per Task 2 scope statement) | Landed |

Live OKX halt-consult verification (scripts/phase_4b_halt_consult_check.py):
BTC-USDT-SWAP and ETH-USDT-SWAP both PASS history-depth (>= dev_end
2025-09-12) and 8h funding cadence (90 settlements, exact 8h).

Tests: 263 passed (238 pre-existing backtest + 25 new data layer).
No changes to trials.log, holdout_manifest.json, holdout_access.log.

Tracks E-I (perp simulator, engine_perp, run_cpcv_perp, manifest
schema for first pair, position management) are gated on G1/G2/G3
chat decisions (G1 = StrategySlot unchanged + new BaseSimulator
Protocol; G2 = parallel run_cpcv_perp with shared cpcv_common.py;
G3 = `legs` field + `funding_cadence_hours` for single-pair
Variation #1 entry, multi-pair manifest schema deferred to
Variation #2). Sign-offs not yet committed.

## Phase 4.B Variation results

| Strategy | Variation | Trial date | Result | Status |
|---|---|---|---|---|
| FundingRateHarvest | `phase4b-delta-neutral-singlepair-btc-v1` | 2026-05-02 | FINAL_GATE RETIRE. Holdout sharpe +0.3527 vs sr_zero_expected +0.5198 (margin -0.1671), dsr_holdout 0.005407, n_trades 11 (10 funding_flip + 1 backtest_end), signal_event_count 663. Mechanism worked (656 settlements processed at 0.989 ratio), economics did not clear MT null. Dev_cpcv historical context (passed 2026-05-02): observed_sharpe +4.3395, cpcv mean +5.1669, dsr_validation 0.999995, n_trades 36. Dev↔holdout sharpe gap (5.17 → 0.35) = structural-failure-mode signal. trial_id 199abc0a (final_gate) inheriting cpcv block + dsr_validation from f2c343c3 (full_cpcv). | RETIRE — V2 structural redesign required |

`count_distinct_variations("FundingRateHarvest")` = 1 / 20
(slot consumed by Variation #1, retired post-holdout; 19
remain for V2+ structural-redesign hypotheses).
`count_trials_for_dsr("FundingRateHarvest")` = 2 (full_cpcv
f2c343c3 + final_gate 199abc0a; smoke 014bc0d9 superseded
by 6c395ab and excluded).

Source: `research/funding-rate-literature.md` § "Variation #1 —
phase4b-delta-neutral-singlepair-btc-v1" for the hypothesis-of-
record + parameter row + source citations; `backtest/trials.log`
trial_id `f2c343c3fb2c4c029b66063d38a96605` for the harness
record. Three superseded prior trial_ids document the
diagnostic chain (`8acd27ae` stale-cache → `2b9bd83b` script-
level months-math → `e7eba18a` harness-level months-math); see
git log for fix-SHA chain (`6c395ab` → `1e71be4` → `2817c3f`).

Cross-strategy harness finding: the substrate-coverage assertion
+ ANOMALY D (per-block zero-coverage abort) added in the fix-
SHA chain are reusable for any future Phase 4.B variation
using funding-rate substrate. Defence-in-depth against the
months-math class of bugs.

## Phase 4 scope: Branch C (selected 2026-04-26)

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

## Pre-Phase-3c reference (2026-04-19 3-year backtest)

Preserved for context. Superseded as a deploy signal by the Phase 3c
verdicts above; retained because the OOS bucket diagnosis is still useful
for any future redesign.

| Strategy | Symbol | Status | OOS Ret% | OOS Sharpe | Trades | Notes |
|---|---|---|---|---|---|---|
| VWAP | ETH/USDT | Working | +17.43% | +2.30 | 123 | Only strong OOS Sharpe in 3-year |
| BearShort | BTC/USDT | Working | +0.35% | +1.11 | 69 | Shorts verified working (side="short") |
| GridTrading | SOL/USDT | Working | +0.20% | +0.73 | 359 | Grinding positive |
| DCA | BTC/USDT | Concerning | -7.15% | -0.83 | 52 | 92% win rate but losing — risk/reward imbalance |
| MeanReversion | ETH/USDT | Failing | -4.19% | -2.27 | 13 | Barely fires; EMA filter too tight |
| Supertrend | ETH/USDT | Broken | -46.07% | -2.78 | 95 | 29.5% win rate, avg loss > avg win |
| TrendFollowing | BTC/USDT | Broken | -38.17% | -2.64 | 119 | 28.6% win rate too low for EMA9/21 |
| Breakout | AVAX/USDT | Broken | -36.16% | -2.78 | 47 | 91.5% stop_loss exits — breakouts fail on AVAX |
| VolatilityBreakout | BTC/USDT | Broken | -21.87% | -2.98 | 415 | 1-candle exit design flaw, net-negative EV |
| DualMomentum | BTC/USDT (rotates BTC/ETH/BNB) | Incomplete | — | — | — | 3-year run killed at 150-min timeout; 3-month smoke showed 55 rotations working |

## Phase 3b infrastructure (COMPLETE)

The full validation harness is live: holdout split, trials.log writer,
block-Sharpe CPCV, DSR, MinTRL, buy-and-hold baseline, verdict tree, and
threshold calibration all shipped. Future backtest data access goes
through `backtest/holdout.py`, not directly through the L1 cache.

| File | Role |
|------|------|
| `backtest/holdout_manifest.json` | Per-strategy `data_start`, `dev_end`, `holdout_start`, `data_end`, `timeframe`. Source of truth for the split. |
| `backtest/holdout.py` | Public accessors `load_dev(strategy_id)` and `load_holdout(strategy_id, caller=..., reason=...)`. Strict single-access enforced on holdout. |
| `backtest/holdout_access.log` | Append-only audit log. Every `load_holdout` call appends one event; every regenerate appends one `regenerated=true` event. |
| `backtest/generate_holdout_manifest.py` | `generate_initial()` for first setup; `regenerate_manifest()` for redrawing the split. |
| `backtest/cache.py` | Enforces `HoldoutBypass` on any read overlapping holdout. Pass `until_ts=get_symbol_dev_cutoff(symbol)` to restrict to dev. |
| `backtest/logs.py` | JSONL read/write plumbing, schema-agnostic. |
| `backtest/trials.py` | Schema-validating JSONL writer for `backtest/trials.log`. Schema v1, sacred-harness-adjacent. Per-trial-type required-field enforcement; canonical sha256 `params_hash`; final-gate guard cross-referenced against `holdout_access.log`. |
| `backtest/cpcv.py` | Block Sharpe distribution. Equal-row block split of the dev window, fresh `strategy_factory()` per block, per-block Sharpe via the engine's formula, purge/embargo at boundaries. Replaces López de Prado path-CPCV (see `docs/validation_framework.md` § "Block Sharpe distribution"). |
| `backtest/dsr.py` | Deflated Sharpe Ratio per Bailey & López de Prado 2014. Inputs: observed Sharpe, σ of Sharpe, skew, kurtosis, sample size, multiple-testing N. |
| `backtest/baseline.py` | Buy-and-hold baseline Sharpe per strategy. Comparison surface, NOT sacred (per file docstring). |
| `backtest/verdict.py` | Verdict tree orchestration. Combines DSR + MinTRL + trade-count + baseline tests into a final keep / retire / under_tested decision. |
| `backtest/calibration.py` | Empirical threshold calibration via synthetic signal-vs-noise test bench. NOT sacred (per file docstring). |

Caller convention for `load_holdout`: 
`<phase>.<strategy_id>.<purpose>` — phase ∈ {phase3c, phase3d, 
phase4, phase5, manual}, strategy_id matches manifest key 
case-sensitively, purpose ∈ {final_dsr, regression_check, 
manual_inspection}. Validated by regex; invalid callers raise 
without writing to the audit log.

Modifying `backtest/holdout.py`, `backtest/holdout_manifest.json` 
schema, or `docs/validation_framework.md` requires human approval 
per `CLAUDE.md` (sacred harness).

## Kelly sizing state

Phase 2c regime-aware wiring is in place (`KellyCalculator` checks
`REGIME_PRIORS[regime][strategy]` first). However, `REGIME_PRIORS` at
`portfolio/kelly.py:223` is currently an empty dict. Every Kelly calculation
falls back to `ALL_REGIME_FALLBACK` at line 141. Result: the bot is
functionally using pre-Phase 2c Kelly logic until Phase 3d produces
per-regime Sharpe data to populate `REGIME_PRIORS`.

Additionally, `portfolio/kelly.py:353` sets `recommended_kelly = half_kelly
if n_trades >= 20 else quarter_kelly`, and line 336 returns
`recommended_kelly=0.0` in an early-return case. Low-trade strategies
therefore have Kelly sizing at zero or quarter fractions. Phase 3c/3d will
resolve this as validated strategies accumulate enough trades.

## Validation performance

- Single-strategy dev_cpcv (10 blocks, BTC/USDT 1H, ~29 months dev):
  ~15-30 minutes wall-clock on the dev machine.
- All-strategy dev_cpcv: ~13 hours wall-clock (the 2026-04-25 run from
  19:48 ICT to ~next-day-morning UTC).
- 3-year full backtest: deprecated as a deploy gate. CPCV on dev + DSR
  on holdout is the gate per `docs/validation_framework.md`.

## Infrastructure

- **L1 OHLCV parquet cache** with a 24-hour TTL — persistent across runs, keyed
  by `(exchange, symbol, timeframe, since)`.
- **Per-strategy symbols** sourced from `config.STRATEGY_SYMBOLS`, replacing
  the earlier "everything trades BTC" assumption.
- **Multi-symbol rotation** support in the engine for DualMomentum
  (BTC/ETH/BNB basket, best-momentum wins each evaluation window).

### Development infrastructure

- **Mac (`kanin@MacBook-Pro-khxngKanin`):** primary development. Code edits,
  chat with Claude, git operations.
- **Threadripper PC (specs pending from Kanin):** planned compute node for
  heavy backtest runs. Integration via Claude Code SSH tunnel (Option B).
  Setup pending Phase 3b start.
- **DigitalOcean Singapore (`kanin@104.248.145.189`):** paper-trading
  production. Currently on commit `4a51f0b` (pre-Phase 3a).

### Deploy gates

- **Iteration:** 3-month smoke test (fast, local, exploratory).
- **Deploy gate:** 3-year full backtest + CPCV + DSR on holdout (slow,
  complete validation).

Iterative work uses the former; nothing ships to paper without passing the
latter.

## Key files

- Checkpoint (server): `/home/botuser/crypto_bot/dashboard/data/portfolio_checkpoint.json`
- Paper state (server): `/home/botuser/crypto_bot/dashboard/data/paper_state.json`
- Bot logs (server): `sudo journalctl -u cryptobot --no-pager`

## Server commands

- SSH: `ssh kanin@104.248.145.189`
- Git pull: `sudo bash -c "cd /home/botuser/crypto_bot && git pull"`
- Restart: `sudo systemctl restart cryptobot cryptodashboard`
- Logs: `sudo journalctl -u cryptobot -n 50 --no-pager`

## What changed since the 2026-04-17 snapshot

- Phase 2c (regime-aware Kelly) was completed at commit `4a51f0b`.
- Phase 3a shipped (commit `f2d29cf`): backtest redesign with per-strategy
  symbols, L1 OHLCV cache, DualMomentum multi-symbol rotation, and the
  `base.py` kwarg fix.
- 3-year backtest was run across 9/10 strategies; DualMomentum hit the
  150-min process cap.
- Supertrend and BearShort were vectorized in the working tree for the
  ~10× speedup noted above.
- Strategy reality check: only 3 of 10 show positive OOS Sharpe on 3-year data,
  which forces the Phase 3b validation framework before any further
  iteration or deploy.
- Phase 3b chunk 5 shipped: `trials.py` writer for `backtest/trials.log`,
  CPCV skeleton, schema-validating JSONL append, final-gate guard.
- Phase 3b chunk 6 shipped: block Sharpe distribution implementation in
  `backtest/cpcv.py` replacing path-CPCV (rule-based strategies have no
  fit/predict split, so path reassembly degenerates; block Sharpe preserves
  the multi-sample basis DSR requires). `docs/validation_framework.md` §
  "Block Sharpe distribution" updated to match.
- Phase 3b infrastructure completed: DSR, MinTRL, buy-and-hold baseline,
  verdict tree, and empirical threshold calibration all live alongside
  CPCV. Phase 3b is now a closed item.
- Phase 3c all-strategies dev_cpcv ran 2026-04-25: 9/10 RETIRE +
  1/10 UNDER_TESTED (MeanReversion CPCVError). Zero strategies cleared
  the deploy gate. Per-strategy verdicts in the table above; structural
  diagnosis in `docs/strategy_evidence_audit_2026-04-26.md`.
- Simulator short-pnl sign bug fixed (commit `25bd843`,
  `paper_trading/simulator.py`). BearShort dev_cpcv re-run on the post-fix
  simulator: observed_sharpe −2.9643 (was +1.3129 pre-fix, sign-inverted),
  RETIRE. Branch B of the Phase 4 fork is effectively foreclosed; see
  `docs/open_questions.md`.

## 2026-04-26 — Paper state reset

Paper state reset to fresh $100k baseline (both `portfolio_checkpoint.json`
and `paper_state.json` moved to `.corrupted-pre-shortpnl-fix-*` backups).

Reason: short-position realized PnL was sign-inverted by simulator bug;
fixed in commit `25bd843`. All accumulated paper-state numbers prior to
this reset reflected the inverted accounting and are not a valid baseline.

Trials.log invalidation policy is a separate followup — historical trial
rows touching shorts are sign-flipped but remain in the file pending
policy decision.
