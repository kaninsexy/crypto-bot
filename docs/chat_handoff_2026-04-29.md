# Chat Handoff — MASTER_PLAN.md Rewrite

**Source chat date:** 2026-04-29
**Purpose:** Carry the decisions from a long deliberation chat into a fresh chat that will rewrite `docs/MASTER_PLAN.md` with clean context. Read this first, then read project knowledge in the order CLAUDE.md prescribes, then write the rewrite.

---

## What this handoff is, and what it is not

**This is:** a record of what was decided in chat 2026-04-29 about how to proceed after the Phase 4 Branch C selection (2026-04-26), grounded in research run during that chat plus existing project state. Decisions here should land in MASTER_PLAN.md.

**This is not:** a research dump, a strategy spec, or a replacement for reading the existing project docs. The new MASTER_PLAN must preserve everything from the current MASTER_PLAN.md that is still valid (Phase 2c/3a/3a.1/3b infrastructure, deploy gate, project integrity principle, sacred-harness rules, retired phase entries) and only update what changed.

**Why a handoff and not the rewrite directly:** the source chat accumulated several wrong context anchors during deliberation (initially assumed 1H global timeframe, initially assumed 10 strategies were a deployment plan rather than a research methodology, initially missed the Branch C selection). Those got corrected, but a long chat with corrected mistakes is the wrong place to write a canonical document. The next chat starts clean.

---

## What the user decided in this chat

### Top-level direction

The user is **not yet committing to Branch A vs Branch C vs hybrid.** Instead, they want to **let test results decide.** Specifically:

- Take the new findings from chat 2026-04-29 (funding-rate harvest, regime-as-hard-gate, BTC-residual mean-reversion, daily/weekly timeframes for trend, vol-scaling, etc.) and apply them to the retired strategies.
- Resurrection candidates run through the **existing Phase 3b validation harness** — same DSR/CPCV/MinTRL/baseline gate, same trials.log discipline, no special treatment.
- Whatever passes the gate gets to live; whatever fails stays retired.
- Re-decide the Branch A/B/C question **after** the test results come in, not before.
- Funding-rate harvest is added as a new strategy candidate but on a **different substrate** (perp + spot, delta-neutral, funding rate as primary income source) and is sequenced after the resurrection batch (see "Sequencing decision" below).

This is structurally a **Branch A-prime + Branch C exploration**, judged by the existing validation harness. Branch C remains the explicit fallback if nothing passes.

### Sequencing decision: hybrid sequential (not full parallel)

User asked for the agent's reasoning. The recommendation that landed:

- **Phase A — Resurrection batch.** Run resurrections of retired strategies in parallel **within the existing harness**. They share substrate (spot OHLCV, current fee model). Parallelism is safe here because the harness does not change. Different timeframes per strategy is fine — the harness already supports that.
- **Phase B — Funding-rate harvest** (and any other delta-neutral/perp/two-leg additions). Build the perp data layer, funding-rate data ingestion, two-leg position management, and funding cost model first. *Then* validate. This is sequential to Phase A because it requires harness extensions, and CLAUDE.md is explicit that agents do not modify the validation harness mid-iteration.

Rejected alternatives:
- **Full parallel** (resurrection + funding-rate at once): violates harness-stability principle, produces data you can't trust because the harness changes mid-flight.
- **Pure sequential** (one strategy at a time): unnecessarily slow given parallelism within Phase A is cheap.

Secondary benefit of the hybrid order: if Phase A produces 2–3 working strategies, funding-rate becomes additive diversification rather than the only remaining hope. If Phase A produces zero, funding-rate becomes the last roll of the Branch C dice and deserves more careful implementation.

### Iteration discipline

- **No fixed cap on within-strategy variations.** User explicitly overrode the CLAUDE.md 20-cap because they're not time-pressured and want to keep testing until "no genuinely-new variation is on the table." MASTER_PLAN should reflect this.
- **Caveat to bake in:** DSR multiple-testing correction scales with N. More variations = higher statistical bar to clear. This is not a reason to cap iterations, it is a reason to make each variation count. Every new trial must carry a documented hypothesis (what change, why, what mechanism it targets). No random parameter perturbations.

### Research findings as starting reference, not baked answer

User chose: research findings inform a *starting hypothesis* per strategy. Iteration is open from there. This gives cleaner data — the project will see whether the research-suggested starting point was actually best, whether iteration found something better, or whether nothing worked.

So MASTER_PLAN should structure each retired strategy as:
1. **Current state** (Phase 3c verdict, latest observed Sharpe, n_trades)
2. **Research-suggested starting point** (specific change, source, mechanism)
3. **Variation space to explore** (menu, not lock-in)
4. **Pass/fail criteria** (the existing Phase 3b verdict tree, applied unchanged)

### Operations during the resurrection phase

User has **stopped the DigitalOcean droplet** to avoid paying for idle compute. Paper trading and live deployment are both gated behind backtest survival. The droplet stays off until something passes the validation gate. If nothing passes from Phase A + Phase B, the droplet may never come back on — that's the honest version of Branch C confirmed by data, and MASTER_PLAN should explicitly say so.

---

## Per-strategy starting hypotheses (for MASTER_PLAN's resurrection table)

These come from the chat 2026-04-29 research synthesis, scoped to retired strategies. They are *starting points*, not commitments. The variation space remains open per the iteration discipline above.

**Critical reminder for the rewrite:** timeframe is per-strategy, not global. Each strategy's starting hypothesis below names a specific timeframe based on the evidence; do not collapse these to a universal "the project runs on X timeframe" anywhere in MASTER_PLAN. Make this a first-class principle near the top.

| Retired strategy | Phase 3c verdict | Starting hypothesis | Why this might unstick |
|---|---|---|---|
| **Supertrend** | RETIRE, –1.64 dev | Test at **daily** timeframe with Barroso-Santa-Clara vol-scaling, regime-gated to trending only | 1H sits in academic dead zone; trend-following literature concentrates at multi-day; vol scaling captured most of the Sharpe uplift in your own forecast-combining research |
| **TrendFollowing** | RETIRE, –1.77 dev | Test at **daily multi-asset** with HOP-style vol targeting across ≥10 instruments | Single-pair forfeits the CTA √N diversification multiplier (~5× per HOP 2017); time-series momentum has the strongest peer-reviewed support in crypto |
| **DualMomentum** | RETIRE, –2.39 dev | Test at **weekly** with ≥5 majors per Liu/Tsyvinski/Wu (2022) | Momentum-on-crypto literature concentrates at weekly+; 1H rotation is noise |
| **MeanReversion** | UNDER_TESTED (CPCVError) | Rebuild as **BTC-residual mean-reversion on alt basket**, not absolute-price MR | Published evidence: BTC-neutral residual MR Sharpe ~2 post-2021; absolute-price MR works only in flat regimes; current 4-filter stack self-suppressed below trade floor |
| **VolatilityBreakout** | RETIRE, –3.62 dev | Test at **daily on multi-coin** with relative-volume selection AND **redesigned exit rule** (current 1-candle exit guarantees negative EV regardless of entry) | Exit design was the documented structural failure; volatility breakout literature (Larry Williams) is daily-bar-based |
| **Breakout** | RETIRE, –1.33 dev | Test as **Zarattini-style daily ensemble** (lookbacks 5/10/20/30/60/90/150/250/360) on top-20 rotational basket | Single-lookback breakout is overfit; ensemble across lookbacks reduces single-parameter risk |
| **GridTrading** | RETIRE, +1.50 obs / +2.34 dist | Demote to **regime-conditional only** — fires only when regime detector confirms range/low-trend/mid-vol; otherwise dormant | Grid is structurally negative-EV in trends; the Phase 3c result (failed multi-testing null despite +1.50 obs) is consistent with edge concentrated in a small fraction of regime-time |
| **VWAP** | RETIRE, +1.14 obs (beat baseline) | **Retire fully** — fold into MeanReversion as a filter signal if useful, do not run as standalone | VWAP-deviation-as-return-predictor has no top-tier peer-reviewed support; was the closest borderline case at 3-year backtest but failed Phase 3c multi-testing null; not worth the multiple-testing budget |
| **DCA** | RETIRE, +1.35 obs / +2.03 dist | **Demote to non-strategy savings track** — runs the user's $1,650/month inflow into BTC/ETH on schedule, NOT competing for risk capital, NOT receiving Kelly allocation | DCA is a savings discipline, not an edge; its 92.3% win rate with –7.15% return (per failure analysis) confirms it cannot compete in a Kelly-sized portfolio |
| **BearShort** | RETIRE, –2.96 dev (post-fix) | **Stay retired.** Branch B foreclosed by post-fix verdict. Do not resurrect under Phase A. | Sign-clean retire; balance-scaled compounding asymmetry confirmed; one-strategy short portfolio has zero diversification anyway |

**Funding-rate harvest (Phase B addition, not a resurrection):**
- Long spot + short perp, equal notional, delta-neutral
- Income source: positive funding rate paid to shorts during normal/bullish markets
- Strongest peer-reviewed support of any retail-accessible crypto strategy (multiple 2024–2025 papers; baseline ~10.95% annualized funding APY before edge selection)
- **Requires harness extensions:** perp data ingestion, funding rate ingestion, two-leg position management, funding cost model
- **Risk to engineer for explicitly:** liquidation of short leg on a sharp upside spike, funding-rate flip to negative, exchange counterparty risk on the spot leg
- Phase B starts only after Phase A has produced its verdict batch

---

## What MASTER_PLAN must preserve from current state (do not rewrite these)

The next chat must read the current `docs/MASTER_PLAN.md` carefully and **preserve, not replace**:

1. **Project integrity principle** — "Nothing deploys until Phase 3b's verdict tree clears it on holdout data" — keep verbatim.
2. **Phase 2c COMPLETE** — Regime-Aware Kelly Wiring at commit `4a51f0b`. Do not re-describe; reference.
3. **Phase 2c.1** — REGIME_PRIORS calibration auto-fulfilled by Phase 3d's per-regime Sharpe attribution. Keep as-is; it remains correct.
4. **Phase 3a COMPLETE** — Backtest redesign at commit `f2d29cf`.
5. **Phase 3a.1 COMPLETE** — Vectorization at commit `abb796e`.
6. **Phase 3b COMPLETE** — Validation harness shipped (block-Sharpe CPCV, DSR, MinTRL, B&H baseline, verdict tree, threshold calibration, trials.log writer, holdout enforcement). 133/133 tests pass. **The new resurrection runs all use this harness unchanged.**
7. **Phase 3c — RAN, 9/10 RETIRE + 1/10 UNDER_TESTED** at sr_zero_expected = +1.9007, N=20. This is the empirical anchor; do not soften it.
8. **Deploy gate** — four-condition gate (DSR on holdout, portfolio DSR, beat B&H baseline, REGIME_PRIORS attribution). Keep verbatim.
9. **Sacred harness rule from CLAUDE.md** — agents do not modify the validation harness mid-iteration; trials.log is append-only with the supersession policy already implemented.
10. **Out of scope** section — funding-rate arbitrage was previously parked here. The rewrite **promotes funding-rate harvest into Phase B** but should note the change explicitly so the audit trail is clear.
11. **Future phases (deferred)** — Forecast Combining, Passivbot Evolutionary Optimization, Profit Reserve System. Keep deferred status unchanged.
12. **3-Year Backtest Cross-Strategy Lessons** — keep as REFERENCE section. The lessons (win-rate-alone-is-meaningless, structural-exit-design-can-guarantee-negative-EV, OOS-better-than-IS-is-the-anti-overfit-signature, pair-specific-overfitting) directly inform the resurrection variation discipline.

---

## What MASTER_PLAN must change

1. **Phase 4 status header** — change from "Branch decision pending" to **"Branch C selected 2026-04-26; Resurrection-and-Extension exploration approved 2026-04-29 to test whether retired strategies can be unstuck or new substrates produce edge before final commitment to Branch C-only."** Make clear: Branch C remains the default; this is a structured pre-commitment exploration, not a Branch A reversal.
2. **Insert new Phase 4.A — Resurrection Batch.** Specify: existing harness, parallel execution within harness, no iteration cap, hypothesis-required-per-trial discipline, the per-strategy starting-hypothesis table, pass/fail = existing Phase 3b verdict tree. Pass = strategy is restored; fail = strategy stays retired.
3. **Insert new Phase 4.B — Funding-Rate Harvest (and similar delta-neutral additions).** Specify: harness extensions required (perp data, funding rate data, two-leg position management, funding cost model), sequenced after 4.A, same verdict-tree gate.
4. **Insert new Phase 4.C — Branch decision (revisited).** After 4.A and 4.B verdicts are in, reassess Branch A vs Branch C with data:
   - **≥2 strategies pass** (any combination of resurrections + funding-rate): proceed to original Phase 4 (paper deploy of validated portfolio) per existing description, then Phase 5 live gate. Branch A path partially confirmed.
   - **1 strategy passes**: portfolio-of-one is high-risk; user decides whether to deploy single strategy, defer for more candidates, or commit to Branch C.
   - **0 strategies pass**: Branch C confirmed by data. Wind down crypto bot project; preserve harness as substrate-agnostic infrastructure for whatever comes next (prediction market bot or alternative — out of scope for this MASTER_PLAN).
5. **Add explicit timeframe-is-per-strategy principle** near the top, just under the project integrity principle. Each strategy's optimal timeframe is a discovered variable, validated per strategy by the harness. Do not specify a global timeframe anywhere.
6. **Note the droplet is paused.** Paper trading and live deployment both gated on backtest survival. Droplet stays off until Phase 4.A or 4.B produces a passer.
7. **Update "Out of scope for now" section** — funding-rate arbitrage moves out of "out of scope" into Phase 4.B. The remaining items (ML regime detection, TradingView integration, LLM-as-signal, crisis-alpha) stay parked.

---

## Companion file (separate from MASTER_PLAN)

The chat 2026-04-29 research findings (institutional benchmarks, retail base rates, edge-source ranking, strategy-archetype regime suitability, capital threshold analysis, Thai tax considerations, operational risk patterns) are research material that informs the plan but **does not belong in MASTER_PLAN.md.**

Recommendation: append to `docs/research_log.md` as a new entry titled **"AI/algo trading viability and strategy-archetype evidence (consolidated 2026-04-29)"** with subsections for each research area. Keep it short — link out rather than embed where possible. The purpose is so future Claude sessions see *why* the resurrection hypotheses were chosen without re-running the research.

If `research_log.md` is getting long, an alternative is a new file `docs/research_findings_2026-04-29.md` referenced from `research_log.md`. User has not specified preference; the next chat can decide based on `research_log.md`'s current size.

---

## What the next chat should do, in order

1. **Read project knowledge clean** in CLAUDE.md's prescribed order: CLAUDE.md → docs/MASTER_PLAN.md → docs/bot_status.md → docs/strategies.md → docs/validation_framework.md → docs/open_questions.md.
2. **Read this handoff.**
3. **Confirm with the user** that the resurrection table per-strategy starting hypotheses match their understanding before writing them into MASTER_PLAN. The user explicitly said "use what we found as ref as a starter point" — these are starter points, not commitments. If the user wants to modify any before they go in, that's expected.
4. **Write MASTER_PLAN.md as a full replacement** preserving items in the "must preserve" list above and changing items in the "must change" list. Output as an artifact in chat for the user to apply manually (CLAUDE.md autonomy rules: MASTER_PLAN.md is off-limits for agent edits).
5. **Write the research_log.md addendum** (or new research_findings file) as a second artifact. Same manual-apply pattern.
6. **Stop after producing the artifacts.** Do not proceed to implementation, Cowork prompts, or Claude Code handoffs in the same chat. The next move (starting Phase 4.A on a specific strategy) is its own session.

---

## Things the next chat should *not* do

- Do not re-run research. Three research tasks already ran in chat 2026-04-29; the synthesis is in this handoff and in chat artifacts the user has access to. More research at this point is a substitute for finishing the plan.
- Do not assume any global timeframe. Each strategy's timeframe is in its starting hypothesis row.
- Do not treat funding-rate harvest as part of the resurrection batch. It is its own phase (4.B) because it is a different substrate and requires harness extensions.
- Do not re-litigate the Branch C selection from 2026-04-26. The empirical anchor (9/10 RETIRE) is unchanged. The new exploration is *whether the retired strategies can be unstuck with the new evidence*, not whether the original Phase 3c finding was correct.
- Do not bake calendar timelines anywhere. Phases gate on results, not dates. The user explicitly does not want fixed timelines.

---

## Open questions the user has not yet decided (carry forward, do not force a decision)

1. **Phase 5 direction if Branch C is confirmed.** Prediction market bot vs alternative (equities, futures, on-chain factor, etc.). Currently deferred. MASTER_PLAN should mention this as out-of-scope for the bot project but acknowledged.
2. **Exchange/venue choice for Phase 4.B funding-rate harvest.** Binance perp + Binance spot is the obvious starting point but has Thai tax-residence implications (the 2025–2029 Thai personal-income-tax exemption applies only to Thai-SEC-licensed exchanges; Binance.com is not on that list). The user has not yet decided whether to run on Binance.com offshore or move to a Thai-licensed venue.
3. **Resurrection priority order within Phase 4.A.** All resurrections run in parallel within the existing harness, but if developer attention forces serialization, which goes first? User has not specified. Tentative recommendation if asked: TrendFollowing daily-multi-asset first (strongest peer-reviewed support for the change), MeanReversion BTC-residual second (highest documented Sharpe uplift), then the rest.

---

## End of handoff

The new chat should be able to write MASTER_PLAN.md cleanly from this handoff plus project knowledge. If anything in this handoff conflicts with project knowledge, project knowledge wins and the conflict should be flagged to the user before writing.