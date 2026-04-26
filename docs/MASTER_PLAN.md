# MASTER PLAN — Crypto Trading Bot

Last updated: 2026-04-25 (Phase 3b Chunks 1-6 complete)
Supersedes the 2026-04-17 plan (prior content preserved in git history). The
primary change since: Phase 2c is complete, Phase 3a has shipped, and the
plan is now organised around a validation-first rescue process rather than
feature expansion.

## Project integrity principle

**Nothing deploys until Phase 3b's verdict tree clears it on holdout data —
meaning a "keep" verdict from compute_verdict, which requires passing the
multiple-testing null (SR > sr_zero_expected(N)), the buy-and-hold baseline,
and both preconditions (trade-count floor + MinTRL).**

This is the single rule that overrides everything else in this document. No
strategy gets paper-deployed, and no paper-deployed strategy gets live-deployed,
until `compute_verdict` returns "keep" on the untouched holdout window. See
`docs/validation_framework.md` for the methodology.

## Phase status

### Phase 2c — Regime-Aware Kelly Wiring — COMPLETE (commit `4a51f0b`)

Code path for regime-aware Kelly sizing is wired. `KellyCalculator` now looks
up `REGIME_PRIORS[regime][strategy]` before falling back to
`ALL_REGIME_FALLBACK`. Per-strategy per-regime Kelly profiles, Bayesian
blending of prior + live trade results, rebuild on regime change and every
50 candles.

### Phase 2c.1 — REGIME_PRIORS Calibration — PENDING (auto-fulfilled by Phase 3d)

`portfolio/kelly.py:223` declares `REGIME_PRIORS` as an empty dict. Every
lookup currently falls back to `ALL_REGIME_FALLBACK` (line 141). This means
regime-aware Kelly is wired but inactive — effectively identical to
pre-Phase 2c behavior.

Calibration will be automatic once Phase 3d produces per-regime Sharpe data
for surviving strategies. No separate project required; Phase 3d's output
populates `REGIME_PRIORS` as its final step.

### Phase 3a — COMPLETE (commit `f2d29cf`)

Backtest redesign. Per-strategy symbols via `config.STRATEGY_SYMBOLS`, L1
OHLCV parquet cache with 24h TTL, DualMomentum multi-symbol rotation,
`base.py` kwarg fix. This is the current `main` tip.

### Phase 3a.1 — COMPLETE (commit `abb796e`)

Supertrend and BearShort vectorization. Approximately 10× speedup on the two
slowest strategies. Supertrend math is identical; BearShort drifts within
noise.

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
  `read_trials`, `latest_final_gate`. `backtest/cpcv.py` shipped 
  as skeleton. `CLAUDE.md` `trials.log` path corrected to 
  `backtest/trials.log`.
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

133/133 tests pass across `backtest/tests/` and `tests/`. Updated 
spec is in `docs/validation_framework.md`.

All Phase 3b infrastructure shipped: holdout split, trials.log writer,
block-Sharpe CPCV, DSR, MinTRL, buy-and-hold baseline, verdict tree,
threshold calibration. See `docs/validation_framework.md` for the live spec.

### Phase 3c — RAN, BLOCKED on Phase 4 scope decision (2026-04-26)

All-strategy dev_cpcv ran 2026-04-25. Result: 9/10 RETIRE + 1/10 CPCVError
(MeanReversion, treated as `under_tested`). Zero strategies cleared
`sr_zero_expected = +1.9007` at N=20. Only VWAP beat its baseline (+1.14
vs +0.68 ETH B&H). Detailed empirical breakdown:
`docs/strategy_evidence_audit_2026-04-26.md`. The structural diagnosis
(1H single-pair substrate, retail-template strategies) means N=20 rescue
variations on the same substrate would be additional draws from the same
noise distribution. Phase 3c rescue iteration is therefore NOT the next
step. The next step is the Phase 4 scope decision (Branch A/B/C) tracked
in `docs/open_questions.md`.

BearShort post-fix re-run (commit `25bd843`, 2026-04-26): observed_sharpe
−2.9643 (was +1.3129 pre-fix), all-quantiles-negative dist, RETIRE. Sign
flipped clean from the simulator short-pnl bug; magnitude amplified ~2.2×
by balance-scaled compounding asymmetry. Branch B (BearShort-only
deployment) effectively foreclosed.

**If Branch A is chosen, the original Phase 3c rescue framing applies to
the redesigned strategies only:** per-strategy rescue or retire decisions
using the Phase 3b framework. Within-strategy iteration cap is 20
variations (`CLAUDE.md`). Execution pattern uses the multi-agent framework
documented in `CLAUDE.md` (parallel literature-review subagents,
cross-model adversarial review, `trials.log` discipline). See
`docs/research_log.md` for the multi-agent evidence basis. If Branch B or
C is chosen, this subsection does not apply.

### Phase 3d

Portfolio-level validation of the strategies that survive 3c. Pairwise
correlation check, portfolio-level DSR, and a buy-and-hold baseline that the
combined portfolio must beat.

**Inverse-volatility strategy weights + Barroso-Santa-Clara vol scaling.**
Per forecast-combining research (see `docs/research_log.md`, section on
forecast combining), this captures most of the empirical uplift of full
forecast-combining machinery with ~1-2 days of work. Applied to surviving
strategies only, after they pass Phase 3c. Specifically: each strategy's
position gets scaled by `target_vol / rolling_vol_30d`
(Barroso-Santa-Clara), and strategies are weighted within regime buckets by
inverse of their realized return volatility.

### Phase 4

> **Branch decision pending.** The current Phase 4 description below
> assumes the original "deploy validated portfolio" path. The 2026-04-26
> Phase 3c structural finding requires choosing among Branch A (rebuild
> around daily/multi-pair), Branch B (BearShort-only — effectively
> foreclosed by the post-fix verdict), or Branch C (pivot off systematic
> crypto). See `docs/open_questions.md` "Phase 4 scope decision". The
> original Phase 4 description applies only if a path produces validated
> strategies — currently none.

Paper deploy of the validated portfolio only. 4-week monitoring vs backtest
expectations. No live money on the table in this phase.

> **Open question (pre-Phase 4):** Decide deployment mechanics — deploy to
> existing server with fresh $100k paper state, or preserve current paper
> state and deploy alongside for comparison? Decision needed before Phase 4
> begins.

### Phase 5

Live deployment decision. Separate gate, requires Phase 4 monitoring to be
clean (paper behaviour within expected bounds, no unexplained equity drift,
risk guards firing correctly).

## Deploy gate

A strategy is allowed onto paper deploy only when all of the following hold:

1. DSR on holdout data clears the Phase 3b threshold.
2. Portfolio-level DSR of the combined survivors clears the threshold
   (Phase 3d).
3. The combined portfolio beats a buy-and-hold baseline on the same
   instruments over the same period.
4. Per-regime Sharpe attribution has been recorded and fed into
   `REGIME_PRIORS` where applicable.

A paper-deployed portfolio is allowed onto live only when Phase 4 monitoring
concludes cleanly. These gates are not negotiable and are not bypassable by
agents (see `CLAUDE.md`).

## Out of scope for now

The 2026-04-17 plan listed funding-rate arbitrage, ML regime detection,
TradingView integration, LLM-as-signal, and a crisis-alpha strategy as
Phase 4+ items. Those remain parked until the validation framework exists
and the current portfolio has survivors — there is no point expanding alpha
surface before confirming there is any alpha.

## Future phases (deferred)

### Forecast Combining — DEFERRED

Pysystemtrade-style continuous forecast aggregation was evaluated in detail (see `docs/research_log.md` section on forecast combining). Decision: defer until after Phase 5. Revisit only if Phase 4 paper-trading monitoring reveals concrete pathologies — specifically: CorrCap hard-blocks causing measurable opportunity cost (>1-2% annualized drag), or regime transitions causing discrete-switching whipsaw losses.

Evidence summary: Carver's own tests show the approach is "indistinguishable" from simpler alternatives in Sharpe terms; DeMiguel et al. (2009) and its 2024 replication show equal-weight combining is nearly impossible to beat out-of-sample; crypto-specific volatility structure introduces new failure modes (FDM leverage creep during regime shifts). Phase 3d's inverse-vol weights + vol scaling captures most of the demonstrated benefit at a fraction of the implementation cost.

### Passivbot Evolutionary Optimization — RETIRED

Considered as parameter search tool. Retired because it conflicts with the project's DSR-based validation discipline: evolutionary search generates large numbers of parameter variations, which directly inflate the multiple-testing count in `trials.log` and the DSR haircut. The CPCV + DSR framework is specifically designed to prevent this class of p-hacking; adopting Passivbot would undermine the integrity principle.

### Profit Reserve System — RETIRED

Considered as auto-transfer of profits to OKX Earn. Retired because it is premature optimization: there are no validated profitable strategies yet to reserve profits from. If Phase 5 succeeds and the bot becomes consistently profitable in live mode, a simpler manual monthly skim satisfies the same goal without the operational complexity of auto-transfer integration.

## Capital and operational context

- Bot: Python multi-strategy, OKX USDT-M futures, paper trading.
- Server: `kanin@104.248.145.189` (DigitalOcean Singapore), currently on
  commit `4a51f0b`.
- Repo: `kaninsexy/crypto-bot`, local at `~/Documents/crypto-bot`.
- Deploy: git push → SSH → `sudo bash -c "cd /home/botuser/crypto_bot && git pull"`
  → `sudo systemctl restart cryptobot cryptodashboard`.
- Paper capital: $100,000 (fresh restart 2026-04-17).
- Live deployment remains future work.
