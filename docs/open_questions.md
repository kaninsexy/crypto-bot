# Open Questions

Last updated: 2026-09-02

Running list of items that are unresolved, blocked, or carried from earlier
work. Entries are grouped by theme, not priority. When an item is resolved,
move it to a "Resolved" section at the bottom (keep for reference for one
revision cycle, then prune).

## Mac/PC `trials.log` split (OPEN, 2026-09-02)

`backtest/trials.log` is gitignored and moved from the Mac to the Windows
PC around 2026-05-05. Every row before 2026-05-05 (Phase 3c incl. BearShort,
Phase 4.A Supertrend/DualMomentum smoke + GridTrading + TrendFollowing_multi,
IntradaySeasonalityEffects, the 4.B V1 chain) exists only on the Mac file,
not the PC file this repo now reads from. The 2026-06 gate-recalibration
audit ("38 rows") and gate-spec-v2 family variance/N were computed on the
incomplete PC file, so the multiple-testing count is understated relative
to full project history. Decide: merge the Mac file's pre-2026-05-05 rows
into the PC file, or document the two-machine split as a permanent
asterisk on every N-based statistic computed before the merge. See
`docs/revival_handoff_2026-09-02.md` §2 item 1.

## Fee-model caveat on pre-Phase-4.E trials (OPEN, 2026-09-02)

Every trial before Phase 4.E used `paper_trading/simulator.py` defaults
(`FEE_MARKET = 0.04%`, a Binance futures taker rate, + 0.05% slippage) on
a spot substrate whose real taker fee is 0.10% (OKX/Binance spot). Phase
4.E corrected this to 0.10% + a 2x stress. High-turnover pre-4.E rows
(e.g. CrossSectionalReversal 1432 trades, NewsSent 2071 trades) are
fee-optimistic and should be treated with that caveat until re-checked
at 0.10%. See `docs/revival_handoff_2026-09-02.md` §2 item 4.

## Holdout regeneration timing for NewsSent/AttentionMom (OPEN, 2026-09-02)

Both under_tested strategies (NewsSentimentMomentum, AttentionMomentum)
sit below the MinTRL floor by 7-10 months and, even if extended, land at
SR ~0.75 — below the 52-month floor of 0.79 and far below anything
forward-confirmable. Extending either requires a holdout regeneration
(human-only; shrinks the virgin window for every other strategy).
Recommendation per `docs/research_revival_2026-09.md` §A.4: **not worth
a regen** — park both indefinitely, revisit only if a regen is triggered
by something else. Recorded here as open rather than resolved because no
human decision has been made to formally park them.

## Binance UM recon data caveats (OPEN, 2026-09-02)

Carried forward from `docs/recon_binance_um_2026-09.md` so future perp-
substrate trial design doesn't rediscover these the hard way: (1) the
`sum_open_interest` metrics feed drops to a hard 0 for ~10 hours per
symbol (a feed glitch, not a real OI wipeout) — any 24h-OI-change feature
must mask or repair these hours before use, or it will manufacture
spurious collapse events; (2) alt (non-BTC) OI/L-S/taker-ratio metrics
only start 2021-12, so a cross-sectional OI study before that date is
BTC-only; (3) `funding_interval_hours` is not always 8h — 5 symbols show
a 2h interval and 4 show 4h at some point (per-symbol regime changes,
heaviest around Nov 2022: SOLUSDT/SOLBUSD/FTTUSDT/FTTBUSD show the
largest settlement-count deviations from the standard 3/day that
month, adjacent to the FTX collapse), so any funding-carry design must
read the interval per settlement rather than assume 8h.

## `REGIME_PRIORS` — still empty

Currently blocked on Phase 4 scope decision: 0/10 strategies survived
Phase 3c, so there are no strategies to attribute per-regime Sharpe to.
Branch C selected 2026-04-26: `REGIME_PRIORS` remains empty by design.

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

- `portfolio/manager.py:1-19` — module docstring refers to "StrategySlot × 6"
  and "Phase D + E combined". Both are pre-OKX-migration phase letters and
  the wrong slot count (current portfolio is 10 strategies). Batch cleanup
  task, not urgent.
- `paper_trading/simulator.py:107-108` — `TradeRecord.entry_time` and
  `exit_time` record `datetime.now()` (wall-clock at the moment the
  simulator processes the trade), not the historical candle's timestamp.
  Surfaced during DualMomentum smoke v2 (2026-04-29): bucketing the 44
  dev-period trades by `t.entry_time` placed all of them in the actual
  wall-clock instant of the backtest run (2026-04-29 ~11:45 UTC) rather
  than across the 2023-04-30 → 2025-09-12 dev window. Workaround used:
  wrap `generate_signal` in the strategy and capture `df.index[-1]` on
  every non-HOLD signal. Affects any retrospective trade-time analysis
  that consumes `TradeRecord.entry_time` directly. Not blocking; needs a
  separate fix that threads the candle timestamp through
  `simulator.execute_signal` → `_handle_buy` / `_handle_full_sell`.

(`config.py` MeanReversion comment was fixed alongside this batch — line 75
now correctly reflects the ETH/USDT pair with the LINK→ETH provenance noted
inline.)

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

## Phase 4 implications of the 2026-04-26 short-pnl fix

The simulator short-pnl sign bug (fixed in commit `25bd843`) inverted realized
PnL on every short close. BearShort is the only production strategy that opens
shorts, so its empirical track record on dev is the leg that shifts. The
academic basis (Wen 2022, Li-Sakkas-Urquhart 2022) is unaffected.

**Empirical result of the post-fix re-run (commit `25bd843`, ts
2026-04-26T14:56:07Z, trial_id `4f89d224107c4a61a958f051791c7a51`,
params_hash unchanged from pre-fix `44136fa3...`):**

- `observed_sharpe`: **−2.9643** (pre-fix: +1.3129)
- block-Sharpe distribution: mean **−3.46**, std 1.26, p05 **−4.66** / p25 −4.16 /
  p50 −3.59 / p75 −3.34 / p95 **−1.43** — every quantile negative
- `dsr_validation`: **0.0** (pre-fix: positive)
- `n_trades`: **198** (pre-fix: 198 — strategy entry/exit logic untouched by the fix)
- VerdictResult: **RETIRE**. trade_count_pass=True, mintrl_pass=True,
  mt_mean_pass=False, baseline_pass=False, baseline_sharpe +1.6945 (BTC B&H),
  mintrl=88.59 bars

The sign flipped cleanly. Magnitude amplified by ~2.2× over a clean-mirror
hypothesis (clean mirror: ~−1.31 / mean −1.58). Working mechanistic
hypothesis: **balance-scaled compounding asymmetry**. Pre-fix, inverted PnL
grew the simulator pot on phantom wins, scaling subsequent positions larger
on a fictitious upward equity trajectory. Post-fix, the correct trajectory
shrinks position size on real losses, compounding the drag. This is a
*hypothesis*, not a proof — a single-block equity-curve diff pre vs post-fix
would confirm but does not change the verdict either way.

Phase 4 branch implications (verdict-confirmed, not provisional):

- **Branch A (rebuild around daily/multi-pair frameworks; keep BearShort and
  harden):** the framing for BearShort changes from "tune the existing
  strategy" to "build a new strategy guided by the BearShort idea." The
  4-filter signal stack as implemented does not have positive dev edge; any
  hardening (vol-scaling, funding-rate cost model, rebound-state filter) is
  now a redesign that proceeds without the assumption of an existing edge to
  protect. Schedule estimate inside Branch A may need to extend.
- **Branch B (BearShort-only deployment):** effectively no longer tractable.
  A one-strategy deployment cannot be justified with `dsr_validation = 0.0`
  on dev and a block-Sharpe distribution with all quantiles below zero.
- **Branch C (pivot off systematic crypto):** gains marginal relative weight.
  One negative single-strategy result is weak evidence against the broader
  systematic-crypto thesis — most strategies were going to fail anyway, and
  one being net-negative rather than baseline-flat doesn't change that
  distribution much. The structural diagnosis from the 9/10 retire result
  remains the dominant input to Branch C.

**Preconditions tracker:**
- (a) BearShort dev_cpcv re-run with corrected Sharpe — **RESOLVED** 2026-04-26.
- (b) `trials.log` invalidation policy — still **OPEN** (see new section below).
- (c) Portfolio-level trials affected by short-PnL corruption — N/A. BearShort
  is the only short-touching strategy in the production portfolio (verified
  via `is_short` grep across `strategies/`); no portfolio-level rerun needed.

**Status: empirical leg resolved 2026-04-26. Linked to Phase 4 branches
deliberation above.**

## Harness design notes

### Block-isolated CPCV warmup amortization (structural)

Both Supertrend trial #1 (commit `d29e604`, daily-TF resurrection) and
DualMomentum trial #1 (this commit, weekly-equivalent on a 5-asset
basket) hit the same wall: block-isolated CPCV pays the strategy's
formation/warmup period in *every* block, eating a significant fraction
of each ~2078-candle block on the current dev-window length. Long-
formation strategies (≥168 candles or so) risk falling below
`_MIN_TRADES_PER_BLOCK = 5` in a majority of blocks regardless of how
many trades the strategy fires on a single full-dev-window pass.

Numerical pattern observed:

| Strategy | Lookback | Single-pass trades | Block-isolated CPCV result |
|---|---:|---:|---|
| Supertrend (daily TF) | 21 daily ≈ 504 hourly | 13 over ~880 days | 0/10 valid blocks (severe) |
| DualMomentum (weekly-eq, 5-asset) | 504 hourly | 44 over ~865 days | 4/10 valid blocks (marginal) |

Implication for remaining Phase 4.A resurrections:

- **TrendFollowing daily-multi-asset HOP-style:** likely affected (HOP
  needs ≥126-day vol windows per Daniel-Moskowitz pattern).
- **Breakout Zarattini ensemble:** definitely affected (lookbacks up to
  250–360 candles, with the 360-candle lookback at ~17% of each
  2078-candle block).
- **MeanReversion BTC-residual:** depends on residual-estimation window
  length; if rolling-beta lookback ≥168 candles, affected.

Decision deferred until those resurrections are scoped. Three candidate
responses, none locked in yet:

(a) **Accept the rejection as harness-correct** — strategies whose
    warmup-to-block ratio is too high are genuinely under-tested on
    this dev-window length. Long-formation strategies need more dev
    history to validate; the harness is correctly refusing to certify
    them on insufficient data.

(b) **Reduce CPCV block count from 10 to a smaller value** to increase
    per-block size. Trade-off: fewer Sharpe samples → weaker DSR
    statistical power. Phase 3c was calibrated at `n_blocks=10`;
    changing it changes the multiple-testing math across all strategies
    and is sacred-harness-adjacent. Probably not the right move.

(c) **Add a strategy-warmup-aware block sizer to `cpcv.py`** that gives
    long-warmup strategies fewer/larger blocks automatically (e.g.,
    via a new `min_block_candles_after_warmup` config field). This is a
    contract-preserving change to `cpcv.CPCVConfig` if added as an
    optional field; could be a contract change if the block-count
    semantics shift. Sacred-harness schema-stable code change, requires
    explicit human approval per CLAUDE.md.

Tracked separately from per-strategy retirement decisions — both
Supertrend and DualMomentum are retired regardless of which response
is chosen, since neither has academic foundation supporting further
variation under the no-p-hacking rule.

## Resolved

Recently resolved items, kept here for one revision cycle so the audit
trail is visible in-document. Prune at next sweep.

### Phase 3a.1 — commit decision

**Resolved 2026-04-26 — shipped at commit `abb796e` ("Phase 3a.1:
vectorize Supertrend and BearShort (~10x speedup)").** The vectorization
landed on `main`; ~10× speedup, identical math on Supertrend, within-noise
drift on BearShort. Item closed.

### DualMomentum — incomplete 3-year run

**Resolved 2026-04-26 — DualMomentum failed Phase 3c dev_cpcv at
−2.39 Sharpe regardless of 3-year-run status. The completion-vs-park
question is moot.** The dev_cpcv result is conclusive on its own terms;
re-running the 3-year backtest would not change the verdict. See
`docs/bot_status.md` Phase 3c table for the full row.

### Phase 3b buildout — next focused session

**Resolved 2026-04-25 — Phase 3b chunks 1-6 shipped, plus DSR + MinTRL +
baseline + verdict tree.** See `docs/MASTER_PLAN.md` Phase 3b status
(now marked COMPLETE) for the file inventory.

### Phase 4 scope decision: post-3c structural fork

**Trigger.** Phase 3c dev_cpcv: 9/10 RETIRE + 1/10 CPCVError. Detailed analysis: `docs/strategy_evidence_audit_2026-04-26.md`. Retail-template strategies on 1H single-pair substrate are not producing edge that survives proper validation. The structural diagnosis forces a scope decision before further rescue iteration.

**The three branches.**

**Branch A: Rebuild around daily/multi-pair frameworks.**
- TrendFollowing redesigned as daily multi-asset portfolio (HOP-style, vol-targeted, ≥10 instruments)
- Breakout redesigned as Zarattini-style daily ensemble (lookbacks 5/10/20/30/60/90/150/250/360) on top-20 rotational basket
- DualMomentum redesigned at weekly with ≥5 majors per Liu/Tsyvinski/Wu (2022)
- VolatilityBreakout redesigned at daily on multi-coin with relative-volume selection
- BearShort kept and hardened (vol-scaling, funding-rate cost model, rebound-state filter)
- Drop: DCA, Grid, VWAP, MeanReversion, Supertrend
- Scope: ~2–3 months of substantial redesign work (data infrastructure for multi-pair daily, portfolio-level CPCV, position sizing under volatility targeting)
- Risk: even at correct timeframe, retail technical-indicator implementations may still fail proper DSR — cf. Cakici et al. 2024 showing sophisticated ML on weekly barely survives costs

**Branch B: Accept BearShort-only with hardened implementation.**
- Drop 9/10 strategies. Keep BearShort as the one academically defensible candidate (peer-reviewed support for 1H crypto TSM via Wen et al. 2022 and Li/Sakkas/Urquhart 2022)
- Hardening scope: Daniel/Moskowitz vol-scaling, funding-rate cost model, rebound-state filter, bear-regime-specific holdout window for proper evaluation
- Scope: ~3–4 weeks of focused work
- Risk: dramatic narrowing of opportunity set; one-strategy portfolio has zero diversification; bear regimes are ~25% of crypto time so capital deployment is mostly idle

**Branch C: Pivot off systematic crypto entirely.**
- Accept that the substrate (retail crypto, technical indicators, single-asset) cannot produce edge that survives validation
- Redirect effort toward different markets (equities, futures), different strategy categories (event-driven, fundamental, on-chain factor), or non-systematic discretionary
- Scope: depends on direction; treat the existing infrastructure (CPCV, DSR, MinTRL, validation discipline) as transferable
- Risk: sunk-cost fallacy in reverse — abandoning genuinely valuable infrastructure (the validation moat) when it's the most valuable thing produced

**Decision criteria.**
1. **Capital concentration tolerance.** Branches A and B both require concentrating real capital on fewer strategies than originally planned (10). Is that acceptable?
2. **Time/effort budget.** Branch A is 2–3× the work of Branch B. Are 2–3 months of redesign acceptable?
3. **Belief about retail-template edge at correct TF.** If you believe retail templates can work at daily multi-pair (the academic evidence is *mixed*), Branch A. If you believe only the strongest-grounded strategy survives, Branch B. If you believe none survives at retail scale, Branch C.
4. **What BearShort needs to be evaluated properly.** Branch A and B both require bear-regime-specific evaluation infrastructure (regime-conditional holdout) that doesn't yet exist. Branch C doesn't need it.

**What this question does NOT decide.**
- Specific parameter values for any redesign
- Sizing/leverage decisions
- Exchange/execution venue
- Live vs paper deployment timeline

Those are downstream decisions inside whichever branch is chosen.

**Recommended deliberation process.**
1. Sleep at least one night on this before deciding. The data is too consequential to decide under fatigue or the day of the result.
2. Open a fresh chat with a tight handoff prompt referencing this open question. Don't re-litigate Phase 3c findings; treat them as established.
3. State which branch you're leaning toward and why before exploring; otherwise the conversation drifts into re-justifying the data.
3a. **If no lean exists**, the agent walks through the four decision criteria applied to the user's current situation and offers a tentative recommendation. The "no anchoring" rule resumes once the user has a position to push back from. The fallback exists because rule 3 produces a stuck state when the user genuinely hasn't formed a prior — leaving the user staring at a blank prompt is worse than a tentative recommendation that gets pressure-tested.
4. Whichever branch is chosen, the deliberation chat ends with an EXIT RAMP — not just a rationale paragraph. The exit ramp lists the implementation paths (e.g., "Option A: ship the implementation now via Claude Code prompt / Option B: defer with one-line doc update / Option C: separate follow-on chat needed"), recommends one, and surfaces what files / re-uploads / next-chat handoffs are needed. The user should never finish a deliberation chat wondering "now what?" — the predictable next ask belongs in the chat's final message per CLAUDE.md "Single-message completeness."
5. Whichever branch is chosen, update `docs/MASTER_PLAN.md` and `docs/bot_status.md` deliberately as part of the kickoff — not before. (Was step 4 in the prior version; renumbered.)

**Resolved 2026-04-26 — Branch C selected.** See `docs/bot_status.md` '## Phase 4 scope: Branch C' for the rationale. Trials.log invalidation policy (separate question) remains OPEN.

### Trials.log invalidation policy after simulator fix

**Trigger.** `backtest/trials.log` now contains two BearShort `full_cpcv`
rows with identical `params_hash` (`44136fa3...`), distinguished only by
`git_commit`: pre-fix `28cfc7a` and post-fix `25bd843`. Both rows count
toward `count_trials_for_dsr`, which has no notion of "superseded by
simulator fix" vs "fresh variation." Without a policy, BearShort's effective
multiple-testing budget is silently consumed by an artifact of the bug fix.

**Three candidate policies:**

- **(a) Quarantine pre-fix rows.** Move them to a sidecar file (e.g.
  `backtest/trials.log.quarantine`) and stop counting. Cleanest separation,
  but requires defining a sidecar schema and a process for adding future
  bug-superseded rows. No schema change to `trials.log` itself.
- **(b) Leave both rows counting toward the N=20 budget.** Most
  conservative — penalises the strategy for the simulator bug. Aligns with
  the no-bypass spirit of `trials.log` but conflates a tooling defect with
  the strategy's edge claim. After 19 more BearShort variations under
  this policy, BearShort exhausts its iteration cap from this single
  combined-row pair.
- **(c) Tag pre-fix rows in place with a bug-suppression flag** (e.g.
  `superseded_by: "simulator_fix_25bd843"`) and have `count_trials_for_dsr`
  exclude tagged rows. Cleanest semantics, but requires a schema addition
  to `trials.log` — a sacred-harness file. Per CLAUDE.md the schema change
  itself needs human approval; the agent cannot implement (c) autonomously
  even after policy is chosen.

**Resolved 2026-04-26 — Policy (c) implemented (tag in place + filter). See backtest/trials.py for the schema + filter, and the tagged BearShort pre-fix row trial_id 34cac215...** Future tooling-defect events follow the same pattern: tag superseded rows with `superseded_by: '<fix-commit-sha>'` and the DSR counter handles the rest.

### Phase 4.B venue choice (Thai-SEC vs offshore)

**Resolved 2026-04-29 — OKX offshore, accept Thai PIT on funding income
(Branch 1 of three branches surfaced at Phase 4.B kickoff scoping).**

The 2025–2029 Thai PIT exemption applies only to SEC-licensed digital-
asset operators, none of which currently offer perpetual futures with
funding-rate settlement. The April 2026 SEC consultation (release No.
81/2026, closes 20 May 2026) proposes a path to licensed perp products
but no Thai venue offers the substrate today. OKX selected for offshore
deployment over Binance.com because the bot's existing OKX paper-mode
plumbing reduces operational migration cost.

Implication for edge claim: post-tax funding APY is ~7.1–8.2% before
costs, vs the ~10.95% pre-tax baseline cited in the 2026-04-29 research
synthesis. Per-strategy hypotheses in `research/funding-rate-literature.md`
must use post-tax expected Sharpe.

**Watch item (reconsideration trigger):** A Thai-SEC-licensed exchange
launches a perp + funding product with SEC-final derivatives licensing.
At that point 4.B runs (or surviving live-deploy migrates) to the
licensed venue for the PIT exemption. Currently TBD; earliest
mid-2026.

See `docs/research_log.md` § "Thai SEC venue / derivatives status
(logged 2026-04-29)" for the full evidence base.

### Phase 4.B Variation #1 scope (single-pair vs multi-pair)

**Resolved 2026-04-29 (persisted 2026-04-30) — single-pair
for Variation #1.** Variation #1 trades equal-notional long
spot BTC/USDT + short perp BTC-USDT-SWAP. Multi-pair selection
(top-N by funding rate across a basket) is Variation #2 with
its own hypothesis row, manifest schema extension, and CPCV
multi-pair path.

Today's Tracks A-D produced research/funding-rate-literature.md
with a Variation #1 that drifted to multi-pair top-1 selection.
Drift caught and corrected 2026-04-30; pre-trial gate #8 now
persisted in research/funding-rate-literature.md § "Pre-trial
gates (locked)" + docs/MASTER_PLAN.md Phase 4.B section so the
gate cannot be lost in a future chat handoff.

See research/funding-rate-literature.md for the persistent
gate statement and Variation #1/#2 hypothesis structure.
### [OPEN, 2026-04-30] Multi-agent orchestration upgrade — Ruflo evaluation

ruflo (https://github.com/ruvnet/ruflo) is a 32.9k-star multi-agent
swarm orchestration platform for Claude. Considered 2026-04-30,
deferred. Trigger points to re-evaluate:
- After Phase 4.B FundingRateHarvest produces dev_cpcv verdict
- After OpenClaw crypto research skill + summarize/session-logs/
  GOG skills are landed
- If prediction-market bot enters scoping (Phase 5+)

Deferred because: (1) OpenClaw orchestration layer already in place
and underused; (2) drift-prevention mandates A-G in CLAUDE.md
assume bounded agent count for review-against-scoping to remain
tractable; (3) Phase 4.B Tracks E-I have sequential dependencies
that don't benefit from agent parallelism.

### [OPEN, 2026-05-03] architecture.md C.1 phase5-coordinator hooks row

[OPEN, 2026-05-03] architecture.md C.1 phase5-coordinator
hooks row reads "Same as phase4b-coord", which would include
PostToolUse failcount-update. D8 implementation
(commit 6733131) deliberately drops failcount-update because
Phase 5 sessions are atomic (fan-out, not sequential
variation loop). Reconciliation owed: either edit
architecture.md C.1 to spell out the divergence, or add a
footnote explaining the operational reason. Either is fine;
pick at next architecture.md edit.
Settled in chat 2026-05-03; reference: D8 commit 6733131.

## Day 9 fleet-replay outcome — body-prompt findings + failcount override

**Date:** 2026-05-03 (Day 9 fleet shakedown)
**Run:** `phase4b-v1-fleet-replay` via fresh phase4b-coordinator (agent ID a99a7d01a853d0a26)
**Outcome:** VERDICT=FAIL via citation-verifier REJECT at D.2 step 3
**Verdict semantics:** FAIL is the correct outcome — the strategy verdict is moot (V1 was retired pre-replay), the FAIL signals two real fleet-architecture gaps surfaced by the shakedown.

### Finding 1 — citation-verifier protocol insufficient for multi-source parameter derivation

The verifier (Gemini 2.5 Pro) rejected V1's hypothesis on three objections:

1. `target_vol_annual=0.05` not directly supported by Schmeling/Schrimpf/Todorov "Crypto Carry" — it's a position-sizing target reverse-engineered from the post-tax economics gate (Branch 1 of 2026-04-29 venue scoping), not a paper-derived realized vol.
2. The 22%-liquidation finding in the paper is a macro return predictor, not an intra-position risk signal.
3. `exit_mr_ratio_threshold=0.01` derives from OKX maintenance-margin spec, not the paper.

Objections 2 and 3 are academically valid. The current proposer body prompt provides a single `citation_key:` field with a 3-sentence `citation_excerpt:` cap; this cannot carry the multi-source derivation chain that V1's parameters actually rely on (paper for the carry signal, exchange spec for liquidation guard, post-tax model for vol target). Chat-side review on 2026-05-02 accepted the heterogeneous derivation; the strict cross-model verifier rejected it on a single citation_key.

**Generalizes beyond V1.** Any strategy whose risk-guard parameters are exchange-derived rather than paper-derived hits the same gap (V2 multi-pair selection layer, Phase 4.A re-attempts if any, future Phase 5 variations).

**Fix shape (chat scope, body-prompt edits):**

- `proposer.md`: replace single `citation_key:` field with a `citation_set:` block enumerating parameter -> source mapping. Each entry types its source (peer-reviewed / exchange-spec / chat-decision-with-rationale / post-tax-model).
- `citation-verifier.md`: per-parameter source evaluation. Each parameter must have a typed source. Verifier accepts non-paper sources for risk-guard parameters when the source-type is exchange-spec or chat-decision-with-explicit-rationale.

Architecture.md D.2 step 3 (citation-verifier protocol) may need a parallel update to reflect the multi-source contract. Architecture is sacred-harness — flag for chat-side decision before edit.

### Finding 2 — Notifier log path divergence

The Day 9 prompt specified `.memory/T1_episodic/_state/notifier.log` for the degraded log-only mode. The notifier.md body default writes to `.memory/T1_episodic/episodes/<date>/notifier/<run-id>.jsonl`. Two writers, two paths.

**Origin of the divergence:** the chat-side path was specified in the Day 9 prompt without verifying notifier.md's body default. Coordinator-spawned-degraded-mode wrote the chat-side path; standalone-spawned notifier writes the body-default path. Both writers are correct under their own contracts; the contracts disagree.

**Fix shape (chat scope, body-prompt edit):**

- `notifier.md`: mode-split paths. Degraded log-only mode writes `.memory/T1_episodic/_state/notifier.log` (rolling, append-only, single line per invocation). Real-email mode writes `.memory/T1_episodic/episodes/<date>/notifier/<run-id>.jsonl` (per-episode, structured). Both contracts documented inline in the body prompt.

### Failcount override

`phase4b_failure_count.txt` was incremented 0 -> 1 by the PostToolUse `failcount-update.sh` hook on the VERDICT=FAIL marker. Hook behavior is correct.

**Override decision (chat-side, 2026-05-03 D9):** failcount reset to 0 by user via direct file write.

**Rationale:** the 3-fail counter exists semantically to stop coordinators grinding on bad hypotheses ("strategy isn't working, stop trying"). Day 9's FAIL wasn't that — the strategy hypothesis (V1) was a known-RETIRE replay, and the FAIL surfaced a body-prompt design gap (Finding 1) rather than a strategy-mechanism failure. Leaving failcount at 1 would penalize the next variation (V2 or successor) for reasons unrelated to that variation's merit. Override is documented here for audit; protocol-side, this is an explicit human exception, not a precedent for routine resets.

**Override is a one-time exception.** Future VERDICT=FAIL outcomes increment failcount per protocol unless similarly documented as fleet-architecture diagnostics rather than hypothesis failures. The bar for "fleet-architecture diagnostic" is: the FAIL surfaced a gap in agent body prompts or hook scripts, AND the gap is being actively fixed in chat scope, AND the next variation can't run cleanly until the fix lands.

### Day 10 prerequisite list (carry-forward)

Before any subsequent fleet exercise:

1. Body-prompt fixes for Findings 1 and 2 land in chat scope (proposer.md, citation-verifier.md, notifier.md).
2. If architecture.md D.2 update is needed for the multi-source citation contract, that's separate sacred-harness work.
3. Resend setup + RESEND_API_KEY provisioning (open record from Day 9 chat handoff; required before any run that needs real email validation).
4. `session_start.txt` elapsed-time check added to the chat-side pre-flight protocol per `handoff_template.md` § "Pre-action checklist" (Day 9 caught the gap when the budget hook fired before any worker spawned; pre-flight didn't include this check).

### State delta (this run)

- `M .memory/T1_episodic/_state/phase4b_failure_count.txt` (0 -> 1 via hook, then 1 -> 0 via chat-side override)
- `M .memory/T1_episodic/_state/session_start.txt` (chat-side reset pre-run)
- `M .memory/T2_semantic/decisions_log.jsonl` (0 -> 1 line, coordinator-written)
- `?? .memory/T1_episodic/_state/notifier.log` (new file, coordinator-written, degraded log-only)

No commits landed. No trials.log row appended. No code touched. No sacred-harness path touched.
