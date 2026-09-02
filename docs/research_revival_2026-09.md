# Research report — a new approach for crypto-bot (2026-09-02)

Companion to `docs/revival_handoff_2026-09-02.md` (verified state). This
document answers the research brief in that file, §6, sections A–D, and
ends with an exit ramp. Written by the Cowork session; no trial was run,
no file outside `docs/` was touched. Every data-availability claim below
was verified by direct download on 2026-09-02; literature claims carry
their source and sample period, and "not replicated" is stated where true.

Kanin's framing for this pass: citation-first testing has failed
(~44 designs, zero holdout passers); anyone with a working plan is not
publishing it; the goal is a bot that runs with data behind it, capital
to be decided by results. That framing is accepted and is, in fact, what
the evidence says.

---

## 0. Bottom line (read this if nothing else)

1. **The failure was the pipeline, not the harness.** The rules made every
   backtest a counted trial, so exploration was forbidden and the only
   legal source of hypotheses was other people's papers. Papers about
   crypto anomalies are (a) mostly measured on thousands of illiquid
   coins, (b) long-short, (c) pre-2022, and (d) published because the
   author is not trading them. Copying them to a 10-coin long-only spot
   basket and racing BTC buy-and-hold was a test designed to fail.
2. **The arithmetic is unforgiving and should drive the choice of edge
   family, not taste.** Validating a strategy at 95% needs ≈ (1.645/SR)²
   years of history; confirming it in a forward paper test at 80% power
   needs ≈ 4.5/SR² years. Only designs with true Sharpe ≳ 1.5–2 are
   confirmable within this project's lifetime. Those exist only in
   market-neutral / structural families, never in daily long-only
   directional trading. This single fact should end the debate about
   "which anomaly next".
3. **A materially better substrate exists, free, and was never used:**
   Binance USDT-M perpetuals public archive — 986 symbols with klines,
   952 with 8h funding history from 2020-01, 5-minute open-interest and
   long/short/taker-ratio metrics from 2020-09, delisted names retained
   (LUNA, FTT, SRM, …). That is a survivorship-bias-free, 6.7-year
   cross-section with funding and positioning data, none of which has
   touched a trial. Execution maps to OKX's 443 USDT swaps.
4. **Recommended approach (§C):** mechanism-first, market-neutral by
   construction, on the perp universe, with an explicit discovery /
   confirmation split so hypotheses can come from data instead of
   citations without corrupting the multiple-testing record; forward
   paper testing reserved for designs whose Sharpe makes it decisive.
   First batch: three mechanism families (§C.4), each with a named
   counterparty and a kill test, budgeted at ≤ 5 confirmation trials.
5. **What to stop doing:** re-slicing OHLCV on ≤ 11 spot coins; long-only
   versions of long-short factors; anything intraday-directional on BTC
   (4.E was wrong-signed gross of fees); LLM sentiment as alpha; forward
   paper-testing the two IR-0.5 near-misses as if it were evidence (it
   cannot be, §B).

---

## A. Direction-by-direction assessment

Format per the brief: (i) mechanism and counterparty; (ii) best evidence
with sample / universe / long-short / costs; (iii) fit against
`revival_handoff` §4 constraints; (iv) repo work; (v) capacity and Thai
constraints; (vi) cheapest killing experiment.

### A.1 Forward paper test of CrossSectionalMomentum + AltcoinSeasonRotation

- (i) Momentum in the alt cross-section; counterparty is under-reaction /
  attention-driven flow. Long-only, so it is BTC beta plus a tilt.
- (ii) Own evidence only: 52-month dev Sharpe +0.94 / +0.84, NW alpha
  p≈0.015, IR ≈ 0.5 (bot_status 2026-06-11b). Momentum is one of the two
  factors Borri–Liu–Tsyvinski–Wu (arXiv 2510.14435v4, 16,468 coins
  2014–2025) find robust post-2020 — but on a universe of thousands of
  coins, weekly, 5-1 long-short.
- (iii) Fails §4.1 by arithmetic: at IR 0.5 the forward test needs ~18
  years (§B). Passes §4.2 only at family DSR 0.69–0.79.
- (iv) Nothing new; `run_trial_queue.py` + paper deploy exists.
- (v) Capacity fine. No tax issue (spot).
- (vi) None needed — §B already kills it *as evidence*. Running it as an
  operational shakedown of the paper stack is fine; calling it validation
  is not.

**Verdict: not a research direction.** Optional as plumbing test only.

### A.2 Long-short cross-sectional factors on a wide perp universe

- (i) Size / momentum / value spread in the alt cross-section; counterparty
  is retail flow into recent losers/winners and liquidity providers.
  Requires the short leg to be market-neutral.
- (ii) Borri et al. 2025: weekly 5-1 long-short returns for CMOM
  +2.6 %/week full sample, +2.1 %/week post-2020, statistically
  significant; CSIZE and CVALUE also robust; MOM12/24, VOL, VOLUME, beta
  factors *not* significant. Universe is ~16k coins incl. dust — those
  spreads are not investable. Negative replication on the investable
  end: Junior (SSRN 6701738, Jul 2022–Apr 2026, **10 large-cap Binance
  perps, long-short, 8h horizon**) rejects exploitable cross-sectional
  alpha from OHLCV + funding signals. Nobody has published the middle
  case (100–300 liquid perps, daily/weekly).
- (iii) §4.1: unknown SR; likely 0.5–1.0 net at the liquid end → borderline
  unvalidatable. §4.2 satisfied if neutral. §4.5 satisfied by the Binance
  UM archive (delistings retained). Costs: weekly rebalance of a 20/20
  book at 0.10 % taker ≈ 4–8 %/yr drag before funding.
- (iv) New engine: cross-sectional long-short perp book with funding
  accrual and margin (`backtest/engine_cs.py`, reuse `perp_simulator`
  math); universe builder with listing/delisting dates; manifest entries.
  Substantial — 2–3 CC chunks.
- (v) Fine at $10–100k. Perp income is taxable in Thailand (already
  decided for 4.B).
- (vi) Kill test: replicate Borri's CMOM/CSIZE sorts on the *investable*
  subset (top-150 by 30-day dollar volume, daily) 2020-01→2022-12 in the
  discovery sandbox (§C.2). If the 5-1 spread is < 0.5 %/week net there,
  the direction is dead before it costs a trial.

**Verdict: candidate, second tier.** Genuine new information (breadth +
short leg) but the prior on net SR at the liquid end is ≤ 1.0, which is
exactly the unvalidatable band.

### A.3 Market-neutral / structural income

Three sub-families, assessed separately because their evidence differs.

**A.3a Single-asset funding carry (long spot / short perp).**
- (i) Leveraged longs pay funding; counterparty is retail leverage demand.
- (ii) Schmeling–Schrimpf–Todorov, *Management Science* 2026 (BIS WP 1087):
  BTC+ETH, 6 venues, 2019-04→2024-07: mean funding ≈ 8 %/yr, Sharpe 6.45
  full sample, 4.06 from 2024, **negative in 2025** (Borri et al. restate
  on Binance BTC 2020-08→2025-05). Cash-and-carry basis compressed to
  ~4 % annualised by Feb 2026 (CoinDesk / Disruption Banking coverage).
  Own evidence: FRH V1/V2b dev keep → holdout retire; 44-month re-test
  Sharpe +0.50, archived 2026-06-11.
- (iii) Fails §4.1 today: current carry ≈ money-market yield; SR on the
  extended window 0.5.
- (vi) Already killed by the project's own data. Do not reopen unless
  funding regime returns (a monitor, not a trial).

**A.3b Cross-sectional funding carry across the alt perp universe.**
- (i) Same counterparty (leveraged longs), but *dispersion* across ~900
  names, not the BTC level. Short the top-decile-funding perps, long the
  bottom decile (or hedge with BTC), rebalanced each 8h/daily. The edge
  is in small/mid caps where institutional carry desks are capacity-
  constrained — the one place retail has a structural advantage.
- (ii) Direct evidence thin. Dispersion exists: a 26-exchange / 749-symbol
  panel (MDPI *Mathematics* 14(2):346, Nov 2025, 8 days) finds 17 % of
  observations with ≥ 20 bp spreads but only ~40 % of top opportunities
  positive after costs and reversal. Own long-only test
  (CrossSectionalFundingRateCarry, Sharpe +1.10, 7 coins) died only to
  the B&H race — the neutral version was never run. Nothing peer-reviewed
  on the liquid-universe long-short version; treat as *unpublished*,
  which per Kanin's framing is a feature.
- (iii) §4.1: plausible net SR 1.5–3 if funding dispersion is persistent
  (each leg earns funding *and* the pair is beta-neutral); this is the
  band forward tests can confirm in 6–12 months. §4.2: neutral → PSR.
  §4.4: high turnover; must model 8h rebalancing at 0.05 % perp taker
  (OKX perp taker is lower than spot) plus funding. §4.5: satisfied.
  Main risk: shorting high-funding names is short-squeeze exposure —
  the sizing/stop rules are the real design.
- (iv) Same `engine_cs.py` as A.2 plus funding-rate ingestion for the
  universe (`data/binance_vision_um.py`).
- (v) Capacity limited by alt liquidity — fine at $100k, not at $10M,
  which is why it can persist. Thai PIT on funding income applies.
- (vi) Kill test in the sandbox: sort perps into funding deciles daily
  2020-01→2022-12, compute the next-day *price* return spread and the
  funding accrual separately. If price reversal wipes out funding
  (i.e. high-funding names keep rising faster than they pay), dead.

**A.3c Term-structure basis (OKX quarterly vs perp / spot).**
- (i) Leverage demand in dated futures; same counterparty as carry.
- (ii) Basis 20–25 % annualised at 2024 peaks, ~10 % mid-2025, ~4 % Feb
  2026 (CME/CoinDesk coverage). Regime-dependent like A.3a.
- (iii) Fails §4.1 in the current regime. Data: Binance Vision has
  delivery-contract klines; OKX quarterlies via CCXT.
- (vi) Monitor only. Same conclusion as A.3a.

**Verdict for A.3:** A.3b is the strongest candidate in the whole report.
A.3a/A.3c are dead in the current regime and should be monitors that
re-arm a hypothesis when funding/basis exceed a pre-written threshold.

### A.4 Extend the two under_tested strategies (NewsSent, AttentionMom)

- (ii) Own evidence: 52-month Sharpe +0.72 / +0.77, MinTRL short by
  7–10 months. NewsSent's "sentiment" is an OHLCV-derived proxy, not
  news; AttentionMom uses Google Trends (pytrends, rate-limited, caused
  a 429 abort on the first holdout attempt).
- (iii) Even when testable, SR ≈ 0.75 sits below the 52-month floor of
  0.79 and far below anything forward-confirmable. Requires a holdout
  regeneration (human-only, shrinks the virgin window for everything).
- **Verdict: not worth a regen.** Park indefinitely; revisit only if the
  regen is triggered by something else.

### A.5 Prediction markets (Polymarket, Phase 5 scaffold)

- (i) Favourite-longshot bias, complementary-contract arbitrage,
  time-decay of near-certain contracts; counterparty is retail
  bettors and slow liquidity.
- (ii) FLB is described in recent reviews as the most robust finding in
  prediction-market research; Clinton & Huang (Vanderbilt, 2025–26,
  as summarised by secondary sources — primary not fetched) report
  negative serial correlation in 58 % of 2024 Polymarket national
  presidential markets; NBA-market arbitrage study (arXiv 2605.00864,
  Feb–Mar 2026) finds risk-free deviations confined to retail scale.
  Own evidence: the 2026-05-08 "PROFITABLE_PROVISIONAL" verdict is 12
  markets with LLM-opinion edges — not evidence.
- (iii) Does not fit the harness at all: no continuous return series,
  no B&H benchmark, capital tied up until resolution, US-access and
  Thai-regulatory questions unresolved. It is a different bot.
- (vi) Cheapest test is a calibration study on *resolved* market history
  (Brier score of market price vs outcome by price bucket) — pure data,
  no trading — which would show whether FLB is exploitable after fees
  on Polymarket specifically.
- **Verdict: out of scope for crypto-bot.** Keep as a separate future
  project; do not spend this project's attention on it now.

### A.6 Accept the null: passive BTC/ETH + rules-based overlay

- (i) Crash-risk reduction via trend/vol overlay; counterparty is nobody —
  it is risk management, not alpha.
- (ii) Widely used (200-day MA, vol targeting); published crypto results
  are mostly practitioner (QuantPedia multi-timeframe study 2018-12→
  2025-11 reports drawdown roughly halved). Under gate v2 an overlay
  will typically *fail* the IR ≥ 0.5 vs B&H test in a bull window and
  should not be scored as alpha.
- (iii) Not a §4 candidate; it is the fallback that lets the bot "run
  properly" with a documented risk profile if nothing else clears.
- (vi) One pre-registered rule, one trial, scored on drawdown and
  Calmar rather than the alpha gate (needs a small verdict-tree option
  for "overlay" designs — schema-stable).
- **Verdict: keep as the floor**, to be implemented only if §C.4's first
  batch produces nothing.

### A.7 What Fable 5.1 changes

Not signal generation. Use it (chat-side, per userMemories routing) for:
mechanism vetting and counterparty identification before code exists;
adversarial review of each pre-registration against §4; statistics
(power, multiplicity accounting for the discovery sandbox, path-CPCV if
a fit/predict design appears); and audits. Sonnet/Opus execute.

---

## B. Power analysis — what a paper test can and cannot prove

Standard error of an annualised Sharpe estimate from daily data over
*T* years is ≈ 1/√T (Lo 2002; the SR²/(2·365) correction is negligible).
Rejecting SR = 0 one-sided at 90 % with 80 % power therefore needs
T ≈ (1.28 + 0.84)² / SR² ≈ **4.5 / SR²** years. Trade count is irrelevant;
calendar time is what counts.

| True SR (or IR vs benchmark) | Years of paper trading needed | P(reject SR=0) after 6 mo / 12 mo / 24 mo |
|---|---|---|
| 0.5 | 18.0 | 18 % / 22 % / 28 % |
| 0.8 | 7.0 | 24 % / 31 % / 44 % |
| 1.0 | 4.5 | 28 % / 39 % / 55 % |
| 1.5 | 2.0 | 41 % / 59 % / 80 % |
| 2.0 | 1.1 | 55 % / 76 % / 94 % |
| 3.0 | 0.5 | 80 % / 96 % / ~100 % |

Backtest side (BLP eq.13 MinTRL at 95 %, units-correct): years ≈ (1.645/SR)²
→ SR 0.5: 10.8 y; 0.8: 4.2 y; 1.0: 2.7 y; 1.5: 1.2 y; 2.0: 0.7 y. Minimum
detectable SR: 29-month window 1.06; current 52-month window 0.79;
Binance UM 2020-01→2025-05 (64 months) 0.71.

Multiplicity (BLP eq.7, V=1): family null sr_zero = 0.52 at N=2, 0.85 at
3, 1.19 at 5, 1.39 at 7, 1.57 at 10. A new family with ≤ 5 confirmation
trials keeps the null ≤ 1.2; a design with true SR 2 still clears DSR
0.95 there. This is the budget §C.4 uses.

Implications: (1) the two IR-0.5 near-misses cannot be confirmed by any
feasible paper test — §A.1 is closed by arithmetic; (2) only SR ≳ 1.5
designs are forward-confirmable inside two years, and only SR ≳ 2 inside
one; (3) the search must therefore be restricted to families where SR 2+
is *plausible* — market-neutral income and event/structural trades —
and the discovery step must produce that number *before* a confirmation
trial is spent.

---

## C. Ranking and the recommended approach

### C.1 Ranking

| Rank | Direction | Why |
|---|---|---|
| 1 | **A.3b cross-sectional funding carry, long-short, wide perp universe** | Only family with a named retail-advantaged counterparty, plausible SR ≥ 2, free survivorship-free data since 2020, confirmable by a 6–12-month paper test, unpublished at the investable scale |
| 2 | **Structural/event mechanisms on the same substrate** (§C.4 family 2–3: deleveraging reversal after OI collapse; listing/delisting flow) | Same data; positioning data (OI, L/S, taker ratio) has never been used here; capacity-limited by nature |
| 3 | A.2 wide-universe long-short factors | Real information, but prior SR ≤ 1 at the liquid end; run its kill test in the sandbox, promote only if the spread is large |
| 4 | A.6 passive + overlay | The floor; implement if 1–3 fail |
| — | A.1, A.4 | Closed by arithmetic / not worth a regen |
| — | A.3a, A.3c | Dead in current regime; monitors only |
| — | A.5 | Different project |

### C.2 Process change: discovery / confirmation split

The root cause of citation dependence is that `backtest.md` treats every
backtest as a counted trial, so no exploratory analysis is permitted and
hypotheses must be imported. Fix, adapted from Harvey–Liu (t > 3 for
multiply-tested claims) and Arnott–Harvey–Markowitz (2019) protocol
items on pre-registration, trial documentation and OOS awareness:

1. **Discovery window** = 2020-01-01 → 2022-12-31 on the new substrate
   (sealed by manifest, never used by any prior trial). Exploratory
   analysis is allowed here **without trials.log rows**, under three
   conditions: (a) every screen run is logged in a discovery ledger
   (`research/discovery/<family>.md`: signal, universe, horizon, result),
   (b) the ledger's count N_disc is carried into the confirmation
   trial's pre-registration and applied as an additional Bonferroni-
   style haircut on the confirmation DSR, (c) discovery never reads
   2023+ data.
2. **Confirmation window** = 2023-01-01 → 2025-05-01 (dev, counted in
   trials.log as today) and **holdout** = 2025-05-01 → 2026-08-31, never
   read until final_gate. For this substrate the holdout is genuinely
   virgin (no prior trial touched Binance UM data); disclose that the
   agents *know* the 2025-10-10 cascade happened.
3. **Pre-registration content** (extends the literature-file template):
   mechanism in one paragraph; counterparty and why they pay; expected
   SR with the discovery number that supports it; turnover and cost at
   OKX perp taker; the kill test and its threshold; N_disc.
4. **Forward stage** only for designs whose dev SR makes §B decisive
   within 12 months (SR ≥ 2). Paper deploy on OKX perps; success = PSR
   ≥ 0.9 after 12 months, or fail-fast if realised SR < 0.5 after 6.

This requires editing `.claude/rules/backtest.md` (and a one-line
pointer in `CLAUDE.md`) — human pre-authorization per the sacred-doc
rule. The harness code does not change.

### C.3 Substrate and engine

- Data: `data/binance_vision_um.py` — klines (1h, 1d), `fundingRate`,
  daily `metrics` (5-min OI / L-S / taker ratios) for **all** UM symbols;
  universe table with first/last kline month (listing / delisting);
  parquet cache under `backtest/cache/binance_um/`. Verified today:
  BTCUSDT funding 2020-01→2026-08, metrics 2020-09-01→2026-08-31,
  LUNAUSDT klines 2021-01→2022-05 (delisted, retained).
- Engine: new `backtest/engine_cs.py` — daily (or 8h) rebalanced
  long-short perp book, equal-notional or vol-scaled, funding accrued at
  settlement, margin/liquidation check per leg using the Phase 4.B risk
  model, OKX perp taker fee + slippage, 2× fee stress. Do **not** modify
  `engine_multi.py` (its long-only contract underlies 21 recorded trials).
- Verdict: `"neutral": true` family → PSR gate; new family in
  `strategy_families.json` (`perp-structural`); manifest rows per §C.2.
- Execution mapping: OKX 443 USDT swaps; universe = Binance-listed ∩
  OKX-listed at signal time (disclosed as a filter, applied ex ante).
- **Recon results (2026-09-02, descriptive only)** — no forward returns,
  no spreads, no Sharpe; full detail in `docs/recon_binance_um_2026-09.md`:
  average 108 symbols/day with funding in 2020–22; cross-sectional
  daily-funding p50 0.065 %/day (~24 %/yr), p90 0.116 %/day, p10
  −0.021 %/day; p90−p10 dispersion 4–39 bp/day, cooling through 2022;
  rank persistence Spearman 0.64/0.47/0.38 at 1/3/7 days, mean
  top-decile stay 1.6 days (→ smooth the signal, do not rebalance raw
  deciles daily); 67 clean OI-collapse (≥20 %/24h) events across 10
  symbols, alt metrics only from 2021-12, a zero-OI feed glitch must be
  masked; 226 listings / 24 delistings with ≥60 d of data.

### C.4 First batch — three mechanism families, ≤ 5 confirmation trials total

| # | Family | Mechanism / counterparty | Discovery kill test (sandbox) | Confirmation design if it survives |
|---|---|---|---|---|
| 1 | Funding-dispersion carry | Leveraged longs in small/mid-cap perps pay funding; desks can't scale into them | Daily decile sort on trailing 3×8h funding, top-150 by dollar volume: is (funding accrued − next-day price spread) > 0 net of 0.05 %×2 per rebalance, 2020–22? Threshold: net ≥ 0.15 %/day on the 10-1 spread | Long bottom / short top decile, vol-scaled, beta-hedged with BTC perp, 8h or daily rebalance, hard stop on any short leg at +15 % — *recon note: control for liquidity, funding vs trailing-30d volume rank is −0.09 (Spearman)* |
| 2 | Deleveraging reversal | Forced liquidations sell at any price; counterparty is the liquidated long (or short) | Event = 24h OI drop ≥ 20 % with price move ≥ 2σ; measure 1–5-day forward return vs unconditional, 2020–22, ≥ 100 events across the universe. Threshold: mean 3-day reversal ≥ 1.5 % with t > 3 | Enter against the move at event close, exit at 3 days or OI recovery; market-neutral variant hedged with BTC — *recon note: alt (non-BTC) OI history only covers 13 months (2021-12→2022-12)* |
| 3 | Listing / delisting flow | Price-insensitive flows around Binance perp listing (attention, index/market-maker inventory) and delisting (forced closure) | ~900 listings + all delistings 2020–22: abnormal return −5…+20 days around the event vs matched names. Threshold: |CAR| ≥ 3 % with t > 3 in a pre-specified window | Trade the window that survived, one rule, universe-wide — *recon note: delisting N is thin (24 qualifying events), treat as indicative not well-powered* |

Budget: family `perp-structural`, at most 5 `full_cpcv` rows across the
three (1 per family + 2 variation slots), 3-consecutive-failure stop,
20-cap irrelevant at this size. Expected outcome if the substrate holds
edge: at least one family shows a discovery spread large enough to imply
SR ≥ 2 net; if none does, that is a clean negative result for retail
structural edge on perps, and A.6 becomes the plan.

### C.5 Definition of "good enough to run"

Stage gate proposal (Kanin to confirm): (G1) confirmation `keep` under
gate v2 on 2023-01→2025-05; (G2) holdout `keep`; (G3) 6–12 months OKX
paper with realised SR consistent with backtest (PSR ≥ 0.9, or fail-fast
at 6 months if SR < 0.5); (G4) small live allocation, sized by Kelly
fraction from paper stats, capital decided then. Nothing before G3 is
"data to back it up".

---

## D. Record-keeping fixes that must land before any new trial

1. Regenerate `repomix-output.xml` (23 commits stale) and push the 8
   Phase 4.E commits (human). `git add` the untracked audit and port-plan
   docs so the gate-v2 rationale is in history.
2. Resolve the two-machine `trials.log`: copy the Mac file's pre-2026-05-05
   rows into the PC file via `backtest.trials` (or document permanently
   that N is understated). Same for `holdout_access.log`.
3. Reconcile tracked `backtest/trial_queue.json` with
   `trial_queue_state.json` (10 items show `queued` but ran); mark sq-031
   and sq-037 explicitly deferred so they do not auto-run.
4. Document the fee-model caveat on all pre-4.E rows (0.04 % vs 0.10 %)
   in `validation_framework.md`'s gate-v2 section (pre-authorized edit).
5. Refresh stale doc sections: `bot_status.md` "Current state",
   `strategies.md` header + 4.E/gate-v2 rows, `open_questions.md`,
   `CLAUDE.md` project-overview paragraph (pre-authorized).
6. Write the discovery-ledger rule into `.claude/rules/backtest.md`
   (§C.2) before the first sandbox screen runs — otherwise the sandbox is
   just unlogged p-hacking.

---

## E. Exit ramp

**Decisions for Kanin (in order):**

1. Approve direction §C (rank-1/2 families on the Binance UM substrate)
   as **Phase 4.F — Perp-structural, mechanism-first**, a new category in
   `MASTER_PLAN.md` (human-only edit).
2. Pre-authorize the `.claude/rules/backtest.md` + `CLAUDE.md` edits for
   the discovery/confirmation rule (§C.2) — `SACRED_OVERRIDE_FILES` on
   the CC invocation.
3. Approve the manifest split for the new substrate (data_start
   2020-01-01, dev_end/holdout_start 2025-05-01, data_end 2026-08-31,
   discovery boundary 2023-01-01 recorded in the manifest notes).
4. Decide on the Mac `trials.log` merge (D.2).
5. Confirm the stage gates in §C.5.

**Recommended path:** ship D.1–D.3 and CC-1 immediately (no sacred edits),
then CC-2 after decision 2, then CC-3. Each is one commit; push stays
human.

### CC-1 — Binance UM data layer (model: Opus; autonomous)

```
Read repomix-output.xml with bash_tool as your FIRST action, then CLAUDE.md,
then docs/research_revival_2026-09.md §C.3 and data/binance_vision.py.
Confirm when done.

TASK: Add a Binance USDT-M perpetual public-archive data layer, modelled on
data/binance_vision.py (which already handles spot monthly 1m zips).

1. data/binance_vision_um.py:
   - list_symbols(): enumerate data/futures/um/monthly/klines/ via the S3
     listing API (https://s3-ap-northeast-1.amazonaws.com/data.binance.vision
     ?delimiter=/&prefix=...&max-keys=1000, follow IsTruncated/NextMarker).
   - universe_table(): per symbol first/last available kline month for 1d
     -> DataFrame [symbol, first_month, last_month, delisted:bool]; cache to
     backtest/cache/binance_um/universe.parquet.
   - fetch_klines(symbol, interval in {"1h","1d"}, start, end) from
     data/futures/um/monthly/klines/<SYM>/<interval>/; parse the 12-column
     futures kline CSV (open_time may be ms or us — detect by magnitude);
     keep taker_buy_base_volume; parquet cache per symbol/interval.
   - fetch_funding(symbol, start, end) from data/futures/um/monthly/
     fundingRate/ (columns calc_time, funding_interval_hours,
     last_funding_rate); index = UTC settlement time.
   - fetch_metrics(symbol, start, end) from data/futures/um/daily/metrics/
     (5-minute rows: sum_open_interest, sum_open_interest_value,
     count_toptrader_long_short_ratio, sum_toptrader_long_short_ratio,
     count_long_short_ratio, sum_taker_long_short_vol_ratio); resample to
     1h (last) and 1d (last) on write.
   - All fetchers: idempotent, resume-able, polite (sleep between zips),
     never read past a caller-supplied `until` (holdout enforcement is
     applied later by backtest/cache.py — do NOT bypass it; do not add any
     load path that skips backtest.holdout).
2. scripts/prefetch_binance_um.py: CLI to fetch universe + 1d + 1h klines,
   funding, metrics for all symbols with >= 6 months of history, bounded
   by --until (default 2026-08-31). Print a coverage table.
3. data/tests/test_binance_vision_um.py: parsing tests on small fixture
   CSVs (klines ms and us timestamps, funding, metrics), universe_table
   logic for a delisted symbol, no-network by default (network tests
   behind a marker).

VERIFIED FACTS (2026-09-02): 986 symbols under monthly/klines, 952 under
monthly/fundingRate; BTCUSDT funding 2020-01 -> 2026-08; BTCUSDT metrics
2020-09-01 -> 2026-08-31; LUNAUSDT 1d klines 2021-01 -> 2022-05 (delisted,
retained). UM liquidationSnapshot is EMPTY (only cm has it) — do not
implement it.

CONSTRAINTS (verbatim): Sacred-harness files (trials.log,
holdout_manifest.json, holdout_access.log, holdout.py schema) and sacred
docs (CLAUDE.md, MASTER_PLAN.md, architecture.md, validation_framework.md):
DO NOT EDIT. No manifest entries in this chunk. No trials. Paper mode
untouched. Runnable artifacts only; bundle shell commands.

AUTONOMY: Proceed without asking for anything under CLAUDE.md "Agent
decides" (tooling/infrastructure/caches). Commit autonomously with a
heredoc message; STOP short of git push.

VERIFY: pytest data/tests -q green; scripts/prefetch_binance_um.py
--symbols BTCUSDT,ETHUSDT,LUNAUSDT --until 2022-12-31 completes and the
coverage table shows LUNAUSDT ending 2022-05.
```

### CC-2 — Discovery/confirmation rule + manifest rows (model: Opus; SACRED, needs pre-authorization)

```
INVOCATION: SACRED_OVERRIDE_FILES=".claude/rules/backtest.md,CLAUDE.md,backtest/holdout_manifest.json" claude

Read repomix-output.xml with bash_tool as your FIRST action, then CLAUDE.md,
.claude/rules/backtest.md, docs/research_revival_2026-09.md §C.2–C.3,
backtest/generate_holdout_manifest.py. Confirm when done.

AUTONOMY (pre-authorization): Kanin pre-authorizes, for this prompt scope
only, (a) adding the "Discovery / confirmation split" section to
.claude/rules/backtest.md, (b) a two-line pointer to it under CLAUDE.md
"Core principles", (c) appending manifest entries for the new substrate
via regenerate_manifest()/the generator's additive path — NOT by hand-
editing JSON, and NOT touching any existing entry. No other sacred edit.

TASK:
1. backtest.md: new section, verbatim content from research_revival §C.2
   items 1–4 (discovery window 2020-01-01..2022-12-31 on substrate
   binance_um only; discovery ledger research/discovery/<family>.md with
   required fields signal/universe/horizon/result/date; N_disc carried
   into the confirmation pre-registration and applied as an extra
   haircut; discovery never reads 2023+ data; forward stage only for
   dev SR >= 2). State explicitly that a discovery screen writes NO
   trials.log row and that a confirmation trial writes exactly one.
2. Manifest: add entries for family perp-structural, substrate
   binance_um, symbols = "universe" (string key, resolved at runtime from
   universe.parquet), timeframe per entry (1d for families 1 and 3, 1h
   for family 2), data_start 2020-01-01, dev_end = holdout_start =
   2025-05-01T00:00Z, data_end 2026-08-31, notes field carrying
   discovery_end 2023-01-01 and the provenance line "Binance UM archive;
   execution OKX; agents aware of 2025-10-10 cascade". Entry ids:
   FundingDispersionCarry, DeleveragingReversal, ListingFlow.
   If the manifest validator rejects a non-list `symbols` value, do NOT
   change the validator (schema is sacred): use symbol "BTCUSDT" as the
   boundary anchor and put `universe_ref: backtest/cache/binance_um/
   universe.parquet` in notes; surface this in the commit message.
3. backtest/strategy_families.json: add "perp-structural" with the three
   ids, "neutral": true for FundingDispersionCarry and the hedged
   variants.
4. Create research/discovery/README.md (ledger format) and three empty
   ledgers.

CONSTRAINTS (verbatim): Only the files pre-authorized above plus new
files under research/discovery/ and strategy_families.json. Do NOT edit
MASTER_PLAN.md, architecture.md, validation_framework.md, trials.log,
holdout_access.log, holdout.py. Manifest additions must be additive —
run the existing manifest tests to prove no existing entry changed.

VERIFY: pytest backtest/tests -q green; python -c "from backtest.holdout
import load_manifest; m=load_manifest(); print(m['FundingDispersionCarry'])"
prints the new entry; git diff shows no change to pre-existing manifest
entries. Commit with heredoc noting the pre-authorization; STOP short of
push.
```

### CC-3 — engine_cs + discovery notebooks (model: Opus; autonomous after CC-1/CC-2)

```
Read repomix-output.xml with bash_tool as your FIRST action, then CLAUDE.md,
.claude/rules/backtest.md (Discovery section), docs/research_revival_2026-09.md
§C.3–C.4, backtest/engine_perp.py, paper_trading/perp_simulator.py,
research/funding-rate-risk-model.md. Confirm when done.

TASK:
1. backtest/engine_cs.py — cross-sectional long-short perp book engine:
   inputs = per-symbol 1d (or 1h) close, funding series, universe mask;
   strategy returns target weights per rebalance (dict symbol->weight,
   sum |w| <= 1, optional beta-hedge leg on BTCUSDT); engine applies
   OKX perp taker fee 0.05% + slippage 0.05% on turnover, accrues funding
   at each settlement (long pays when positive, short receives), marks
   to market daily, enforces per-leg margin/liquidation per
   funding-rate-risk-model.md, exposes per-bar return series in the same
   shape backtest/cpcv_common expects (so run_cpcv_multi/DSR/verdict are
   unchanged). Fee multiplier hook for the 2x stress. Unit tests with a
   3-symbol synthetic panel: funding sign, fee accounting, beta-hedge
   neutrality, liquidation trigger.
2. scripts/discovery_<family>.py for the three §C.4 families: run ONLY on
   data with timestamps < 2023-01-01 (assert this), compute the kill-test
   statistic exactly as written in the §C.4 table, print it with t-stat
   and N, and append a ledger row to research/discovery/<family>.md.
   These scripts must never call backtest.trials.record_trial.
3. Do NOT write strategy classes or confirmation trial scripts in this
   chunk; those come after the ledger results are reviewed chat-side.

CONSTRAINTS (verbatim): schema-stable code — engine_cs is NEW and must
not change cpcv.py/dsr.py/verdict.py/engine.py/engine_multi.py contracts.
Sacred files untouched. Discovery scripts read < 2023-01-01 only.
No trials.log rows. Runnable artifacts only.

AUTONOMY: Proceed without asking. Commit autonomously with heredoc;
STOP short of push. Report the three kill-test statistics verbatim in
your final message.

VERIFY: pytest backtest/tests data/tests -q green; each discovery script
asserts max timestamp < 2023-01-01 and appends exactly one ledger row.
```

### Housekeeping — record-keeping fixes (model: Sonnet; autonomous)

```
Read repomix-output.xml with bash_tool as your FIRST action, then CLAUDE.md,
docs/revival_handoff_2026-09-02.md §2 and docs/research_revival_2026-09.md §D.
Confirm when done.

TASK (one commit): reconcile backtest/trial_queue.json statuses with
backtest/trial_queue_state.json (done/retired/under_tested as recorded;
sq-031 and sq-037 -> "deferred" with a note), refresh docs/bot_status.md
"Current state" (HEAD, working tree, droplet paused, no paper deploy),
docs/strategies.md header + add the 2026-06-11 gate-v2 rows and the
Phase 4.E rows from bot_status.md, docs/open_questions.md (add: Mac/PC
trials.log split; fee-model caveat on pre-4.E rows; holdout regen timing).
git add docs/gate_recalibration_audit_2026-06.md and
docs/playbooks_port_plan_2026-07-03.md.

CONSTRAINTS (verbatim): do NOT edit CLAUDE.md, MASTER_PLAN.md,
architecture.md, validation_framework.md, trials.log, holdout_manifest.json,
holdout_access.log. Doc edits listed are agent-autonomous per CLAUDE.md
"Agent edits documents autonomously".

AUTONOMY: Proceed without asking. Commit with heredoc; STOP short of push.
VERIFY: python scripts/validate_queue.py passes; git diff --stat lists only
the files above.
```

---

## Sources

Literature (with sample periods as stated by the source):
- Schmeling, Schrimpf, Todorov, "Crypto Carry", *Management Science* 2026 / BIS WP 1087 — https://pubsonline.informs.org/doi/10.1287/mnsc.2024.05069 ; https://www.bis.org/publ/work1087.pdf (BTC+ETH, 6 venues, 2019-04→2024-07)
- Borri, Liu, Tsyvinski, Wu, "Cryptocurrency as an Investable Asset Class: Coming of Age", arXiv 2510.14435v4 (2026-03) — https://arxiv.org/abs/2510.14435 (16,468 coins incl. 29,230 delisted, 2013-12→2025-09; carry restated on Binance BTC 2020-08→2025-05)
- Junior, "Failure of Cross-Sectional Alpha Screening on Cryptocurrency Perpetual Futures", SSRN 6701738 (10 Binance perps, 2022-07→2026-04) — https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6701738 (abstract only; SSRN full text not fetched)
- "The Two-Tiered Structure of Cryptocurrency Funding Rate Markets", *Mathematics* 14(2):346 (26 exchanges, 749 symbols, 8 days, Nov 2025) — https://www.mdpi.com/2227-7390/14/2/346
- "Exploring risk and return profiles of funding rate arbitrage on CEX and DEX", *Blockchain: Research and Applications* 2025 — https://www.sciencedirect.com/science/article/pii/S2096720925000818
- Arnott, Harvey, Markowitz, "A Backtesting Protocol in the Era of Machine Learning", *JFDS* 2019 — https://people.duke.edu/~charvey/Research/Published_Papers/P138_A_backtesting_protocol.pdf
- López de Prado, *Causal Factor Investing*, CUP 2023 — https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4205613
- Arbitrage Analysis in Polymarket NBA Markets, arXiv 2605.00864 (Feb–Mar 2026) — https://arxiv.org/html/2605.00864v1
- Basis-trade coverage: CoinDesk 2025-03-21 — https://www.coindesk.com/markets/2025/03/21/what-the-collapse-of-the-u-s-bitcoin-etf-cash-and-carry-trade-means-for-investors ; CME OpenMarkets 2025 — https://www.cmegroup.com/openmarkets/equity-index/2025/Spot-ETFs-Give-Rise-to-Crypto-Basis-Trading.html ; Disruption Banking 2026-02-24 — https://www.disruptionbanking.com/2026/02/24/hedge-funds-dump-bitcoin-etfs-why-smart-money-is-exiting-fast-in-2026/
- QuantPedia BTC multi-timeframe trend study (2018-12→2025-11) — https://quantpedia.com/how-to-design-a-simple-multi-timeframe-trend-strategy-on-bitcoin/
- Prediction-market inefficiency reviews (secondary) — https://predictiontalk.org/d/14-ai-parsed-40-papers-on-pm-inefficiencies-here-are-5-im-going-to-trade/ ; https://www.tradetheoutcome.com/polymarket-accuracy-report-data/

Data verified by direct download 2026-09-02:
- Binance Vision S3 listing (futures/um): 986 klines symbols, 952 fundingRate symbols, 991 metrics symbols (corrected 2026-09-02: the raw S3 listing's top-level echoed `<Prefix>` element was being double-counted alongside the `<CommonPrefixes>` entries; `data/binance_vision_um.py` list_prefixes/list_symbols ignores it — see docs/recon_binance_um_2026-09.md); BTCUSDT-fundingRate-2020-01 … 2026-08; BTCUSDT-metrics-2020-09-01 … 2026-08-31; LUNAUSDT-1d-2021-01 … 2022-05; UM liquidationSnapshot prefix empty — https://data.binance.vision/?prefix=data/futures/um/
- OKX public instruments: 459 swaps, 443 USDT-settled — https://www.okx.com/api/v5/public/instruments?instType=SWAP
- OKX perpetual fees, regular user Lv1: maker 0.02 % / taker 0.05 % — https://www.okx.com/en-us/help/trading-fee-rules-faq

Repo evidence: `backtest/trials.log` (48 rows), `docs/bot_status.md`,
`docs/gate_recalibration_audit_2026-06.md`, `docs/project_diagnosis_2026-07-02.md`,
`paper_trading/simulator.py` fee constants, `backtest/engine_multi.py`.
