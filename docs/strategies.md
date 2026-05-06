# Strategies — Per-Strategy Status and Diagnostic Reference

Last updated: 2026-05-06

Authoritative state per strategy. For strategies, two pieces of evidence
matter: (a) the Phase 3c dev_cpcv verdict (the validation gate); (b) the
3-year backtest diagnosis (preserved for any future redesign).

## Status legend (verdict-tree states)

- **keep** — cleared deploy gate (DSR-validated). None currently.
- **retire** — failed deploy gate. 9 strategies as of 2026-04-26.
- **under_tested** — insufficient data to render a verdict (CPCVError or
  below MinTRL). 1 strategy (MeanReversion).
- **incomplete** — backtest never finished. 0 currently.

## Phase 3c verdicts (2026-04-26)

Source: `backtest/trials.log` rows from 2026-04-25 19:48 onward;
`docs/strategy_evidence_audit_2026-04-26.md`. All strategies tested at
N=20 against `sr_zero_expected = +1.9007`.

---

### VWAP — ETH/USDT

- **Phase 3c verdict (2026-04-26):** RETIRE. observed_sharpe +1.1389,
  dist mean +1.9539, n_trades 319. Beat baseline (+0.68 ETH B&H) but
  failed the multiple-testing null at N=20.
- **3-year backtest diagnosis (2026-04-19, historical reference):**
  +17.43% return, +2.30 OOS Sharpe, 123 trades, ~48% win rate. Only
  strategy with strong OOS Sharpe on the 3-year run; OOS improved over
  IS (+1.00 → +2.30), the opposite of overfitting. Consistent with
  genuine mean-reversion edge around the daily VWAP on ETH.
- **Mechanism evidence (audit):** VWAP-as-execution-benchmark literature
  is rock-solid; **VWAP-deviation-as-return-predictor has no top-tier
  peer-reviewed support**. 1H sits in a gap with no academic validation.
- **Next action:** No Phase 3c rescue per structural diagnosis. Branch A
  redesigns drop VWAP; Branch B keeps only BearShort (also dropped); Branch
  C drops the strategy. Effectively retired across all branches.

---

### BearShort — BTC/USDT

- **Phase 3c verdict (2026-04-26 post-fix):** RETIRE. observed_sharpe
  −2.9643, dist mean −3.4565 / std 1.2563, all quantiles negative
  (p05 −4.66 / p25 −4.16 / p50 −3.59 / p75 −3.34 / p95 −1.43),
  n_trades 198, dsr_validation 0.0, baseline_sharpe +1.6945.
  trade_count_pass=True, mintrl_pass=True, mt_mean_pass=False,
  baseline_pass=False. Trial committed to `backtest/trials.log` at
  ts 2026-04-26T14:56:07Z, trial_id `4f89d224107c4a61a958f051791c7a51`,
  git_commit `25bd843`.
- **Phase 3c original (2026-04-26 pre-fix, simulator bug):** RETIRE
  with sign-inverted observed_sharpe +1.3129 / dist mean +1.5833.
  Superseded by the post-fix re-run on commit `25bd843`. Sign flipped
  cleanly; magnitude amplified ~2.2× by balance-scaled compounding
  asymmetry (see `docs/research_log.md` 2026-04-26 entry).
- **3-year backtest diagnosis (2026-04-19, historical reference):**
  +0.35% OOS return, +1.11 Sharpe, 69 trades. Shorts verified
  (`side="short"`). Hedge-style contributor, not a return driver.
- **Mechanism evidence (audit):** most defensible academic grounding of
  any in the cohort — 1H crypto TSM has rare peer-reviewed support
  (Wen/Bouri/Xu/Zhao 2022, Li/Sakkas/Urquhart 2022). The specific 4-filter
  signal stack (Supertrend + EMA cross + RSI threshold + MACD sign) has
  no peer-reviewed validation as a unit and has high multiple-testing
  risk.
- **Next action:** No Phase 3c rescue per structural diagnosis. Fate
  determined by Phase 4 branch decision: Branch A would redesign from
  scratch (vol-scaling per Daniel/Moskowitz, funding-rate cost model,
  rebound-state filter, bear-regime-conditional holdout); Branch B
  effectively foreclosed by the post-fix verdict; Branch C drops the
  strategy.

---

### GridTrading — SOL/USDT

- **Phase 3c verdict (2026-04-26):** RETIRE. observed_sharpe +1.5004,
  dist mean +2.3377, n_trades 1035. Failed the multiple-testing null at
  N=20.
- **3-year backtest diagnosis (2026-04-19, historical reference):**
  +0.20% OOS return, +0.73 Sharpe, 359 trades, ~79% win rate. Grinding
  positive — high trade count and high win rate with small edge per
  trade — classic grid behaviour on a range-bound pair.
- **Mechanism evidence (audit):** Chen/Chen/Jang (2025) prove the
  expected return of traditional grid trading is essentially zero
  pre-fees under symmetric random walk. Mathematically zero EV without
  a regime-detection edge.
- **Next action:** Drop unless deployed with explicit short-vol
  regime-conditional edge. Branch A and B drop GridTrading; Branch C
  drops the strategy.

#### Phase 4.A outcome (2026-04-29) — regime-conditional RETIRED

- **Variation tested:** `phase4a-regime-conditional-v1`. RANGE-only
  regime gate added to GridTrading; Phase 3c grid params held
  constant (BB(20, 2σ) + ATR(14)×0.75; 10 levels; $200/trade;
  recalibrate_every=24); detector reads strategy pair df (SOL/USDT),
  not BTC. Hypothesis-of-record at
  `research/gridtrading-literature.md`, committed `bf4b9ca`.
- **Harness result:** `full_cpcv` clean — all 10 blocks valid (the
  gate did NOT hit the warmup-amortization wall the Supertrend and
  DualMomentum trials did). Per-block trades
  `[63, 64, 43, 63, 44, 57, 62, 55, 73, 44]`; per-block Sharpes
  averaging +2.40 (std 1.37). `count_trials_for_dsr("GridTrading")`
  advances from 1 to 2 (full_cpcv contributes to DSR).
- **Verdict tree:** RETIRE. trade_count_pass=True (601 trades),
  mintrl_pass=True. Quality gates fail decisively:
  mt_mean_pass=False (observed_sharpe +0.8805 vs sr_zero_expected
  +1.9007 at N=20, margin −1.02); baseline_pass=False (vs SOL B&H
  +1.8133, margin −0.93). DSR p-value 3.7e-15. Not borderline.
- **Headline run:** +0.61% return over 868-day dev window, max DD
  0.24%, win rate 75.5%, profit factor 1.46. The gate is highly
  restrictive — strategy is mostly dormant — so the conditional-
  regime edge gets too few candles to compound a meaningful
  headline return. Removing the trending-regime drag exposed that
  the conditional firing's upside is also bounded.
- **Why no variation #2:** No source-cited justification clears
  the no-p-hacking bar. Gate widening (VOLATILE/BULL/etc.) lacks a
  citation; parameter perturbation (BB/ATR/recalibrate constants)
  is bot-convention not academic; multi-pair basket grids have no
  literature; gate-confidence threshold tuning is dev-data
  parameter dredging. `count_distinct_variations("GridTrading")`
  is now 2/20 with the cap effectively closed.
- **Branch implication:** Branch C of `MASTER_PLAN.md` strengthens
  for this strategy.
- **Forensic:** `research/gridtrading-literature.md` § "Trial #1
  outcome (2026-04-29)".

---

### DCA — BTC/USDT

- **Phase 3c verdict (2026-04-26):** RETIRE. observed_sharpe +1.3527,
  dist mean +2.0295, n_trades 194. Failed the multiple-testing null at
  N=20. Underperformed buy-and-hold baseline +1.6945 (BTC).
- **3-year backtest diagnosis (2026-04-19, historical reference):**
  −7.15% OOS return, −0.83 Sharpe, 52 trades, 92.3% win rate. A 92% win
  rate with negative return is a tell: wins are small and a handful of
  large losses drag the total negative. Martingale safety orders
  encounter drawdowns in OOS that exceed recovery capacity.
  Risk/reward imbalance, not a signal problem.
- **Mechanism evidence (audit):** No peer-reviewed validation of
  drawdown-triggered geometric-scaling DCA. Constantinides (1979) shows
  averaging-down is dominated by sequentially-optimal strategies under
  standard expected utility. Strategy is structurally a short-puts
  payoff that depends on regime classification.
- **Next action:** No Phase 3c rescue per structural diagnosis. Drop
  across all branches, or contain as long-bias accumulation only with
  no claim to alpha.

---

### MeanReversion — ETH/USDT

- **Phase 3c verdict (2026-04-26):** UNDER_TESTED. CPCVError: more than
  50% of blocks have insufficient trades — 4-filter AND stack
  (BB %B + StochRSI K cross + volume + EMA) self-suppressed below
  `_MIN_TRADES_PER_BLOCK` in 7 of 10 blocks. No row written to
  `trials.log` (atomicity guarantee). Semantically `under_tested`.
- **3-year backtest diagnosis (2026-04-19, historical reference):**
  −4.19% OOS return, −2.27 Sharpe, 13 trades, 15.4% win rate. Barely
  fires (13 OOS trades in 3 years) and loses when it does. Current EMA
  filter appears too tight.
- **Mechanism evidence (audit):** Bollinger Bands have weak peer-reviewed
  support; Stochastic RSI from a 1994 practitioner book has zero
  top-tier academic validation. Caporale/Plastun/Oliinyk (2019) tested
  hourly counter-movement strategies on BTC/LTC/Ripple/Dash and found
  them not profitable after costs. Crypto evidence specifically points
  the other direction (BTC/ETH show momentum at daily, not reversal).
- **Next action:** Drop. Self-suppression result confirms over-filtering;
  underlying mechanism is structurally weak for the substrate. Branch A
  drops MeanReversion; same for B and C.

---

### Supertrend — ETH/USDT

- **Phase 3c verdict (2026-04-26):** RETIRE. observed_sharpe −1.6388,
  dist mean −1.6235, n_trades 302. Net-losing in dev.
- **3-year backtest diagnosis (2026-04-19, historical reference):**
  −46.07% OOS return, −2.78 Sharpe, 95 trades, 29.5% win rate. OOS avg
  win ($95) smaller than OOS avg loss ($107) at 29.5% win rate —
  expected value per trade ≈ −$47. Wins from trailing stop running
  profits; losses from waiting for Supertrend to flip, which gives
  back too much.
- **Mechanism evidence (audit):** Supertrend has essentially zero
  peer-reviewed academic support (originates from Olivier Seban's 2009
  French self-help book). MTF Supertrend is the canonical
  TradingView lookahead-bias trap (most public Multi-TF Supertrend
  scripts silently future-leak via `lookahead=barmerge.lookahead_on`
  without `[1]` offset).
- **Next action:** Drop, or keep ONLY if the 4H HTF feed is rigorously
  verified to have no lookahead. Indicator has no academic foundation.
  Branch A drops Supertrend; same for B and C.

#### Phase 4.A outcome (2026-04-29) — daily-resurrection RETIRED

- **Variation tested:** `phase4a-daily-resurrection-v1`. Daily TF
  (resampled internally from the manifest's 1h frame) +
  Barroso & Santa-Clara (2015) vol-scaling +
  6-regime gate restricting longs to STRONG_BULL ∪ BULL.
  Hypothesis-of-record at `research/supertrend-literature.md`,
  committed `bf4b9ca`.
- **Harness result:** CPCV-10 raised `CPCVError`. Every block produced
  fewer than `_MIN_TRADES_PER_BLOCK = 5` trades because daily-TF
  density on a single asset over 880 days yields ~1.3 trades per
  88-day block (per-block trades
  `[1, 1, 0, 1, 1, 1, 1, 0, 1, 2]`). The validation harness cannot
  certify this variation as configured; trial appended as
  `trial_type="smoke"` (excluded from DSR multiple-testing per
  `count_trials_for_dsr`).
- **Headline run (full dev window, single backtest):** Sharpe +1.1182
  on 13 trades, +26.39% return, 11.59% max DD, win rate 46.1%,
  profit factor 2.78. Beats ETH/USDT B&H baseline +0.6836 by +0.43
  Sharpe — but verdict tree's `min_trade_count = 30` precondition
  fires (n=13), so the forensic verdict is `under_tested`.
- **Why no variation #2:** the literature note's pre-condition triggered
  — Supertrend's lack of peer-reviewed foundation makes a single
  failed structural-change variation enough to make the
  "indicator-without-edge-theory" prior dominant. Variation budget
  capped at 1 attempt post-Phase-3c; further parameter sweeps would
  burn iteration-cap slots in service of an indicator without a
  proper edge theory. `count_distinct_variations("Supertrend")` is
  now 2/20 with the cap effectively closed.
- **Branch implication:** Branch C of `MASTER_PLAN.md` strengthens for
  this strategy — Supertrend stays out of the deployed portfolio
  regardless of which Phase 4 branch is selected.

---

### TrendFollowing — BTC/USDT

- **Phase 3c verdict (2026-04-26):** RETIRE. observed_sharpe −1.7708,
  dist mean −1.7672, n_trades 374. Net-losing in dev.
- **3-year backtest diagnosis (2026-04-19, historical reference):**
  −38.17% OOS return, −2.64 Sharpe, 119 trades, 28.6% win rate. Avg
  win/loss ratio actually fine (+1.31% vs −1.15%); 28.6% win rate too
  low for a trend strategy at this ratio (needs 40%+ to be profitable).
  EMA9/21 on 1h BTC generates too many false signals in choppy regimes.
- **Mechanism evidence (audit):** Time-series momentum has serious
  peer-reviewed support — Moskowitz/Ooi/Pedersen 2012, Hurst/Ooi/Pedersen
  2017 — but **all at monthly frequency** with 1–12 month signals.
  EMA crossover specifically: Brock/Lakonishok/LeBaron 1992 was killed
  out-of-sample by Sullivan/Timmermann/White 1999 and Bajgrowicz/Scaillet
  2012 under FDR correction with realistic costs. Hurst/Ooi/Pedersen 2017:
  diversified TSMOM Sharpe ~1.3 across 67 markets vs 0.3–0.5 individual
  — ~80% of Sharpe comes from cross-market diversification.
- **Next action:** Branch A redesigns at daily multi-asset (HOP-style,
  vol-targeted, ≥10 instruments). Branch B drops TrendFollowing.
  Branch C drops the strategy.

#### Phase 4.A outcome (2026-05-04) — daily TSMOM basket RETIRED

- **Variation tested:** `phase4a-hop-daily-multi-v1`. 11-symbol
  daily basket [BTC, ETH, SOL, BNB, XRP, ADA, AVAX, DOT, LINK,
  LTC, UNI]. 126-day TSMOM signal (Hurst/Ooi/Pedersen 2017).
  Vol-targeting per instrument (Barroso & Santa-Clara 2015).
  Basket read from manifest at runtime; MATIC/USDT excluded
  (renamed POL, insufficient history).
- **Harness result:** full_cpcv clean -- all 5 effective blocks
  valid (warmup-aware downshift: floor(931/156)=5). Per-block
  trades [20, 18, 19, 19, 12]; per-block Sharpes mean +0.392
  (std 1.989). Harness produced a valid block-Sharpe distribution.
- **Verdict tree:** RETIRE. baseline_pass=False (sr_observed +0.889
  vs BTC B&H +1.922, margin -1.033). mt_mean_pass=True but
  trivially so at N=1 (sr_zero_expected=0.0). DSR 1.0.
  trial_id 746544526ea54348b949b2b0f71b1584.
- **Headline run:** +53.61% return over 931-day dev window,
  +0.889 Sharpe, 18.38% max DD, 163 trades. Real return is
  strong; the strategy simply cannot clear the BTC buy-and-hold
  bar on a risk-adjusted basis.
- **Failure mode:** intra-class correlation. All 11 crypto assets
  are highly correlated to BTC; cross-asset TSMOM diversification
  benefit (the mechanism in Hurst+ 2017) does not materialise in
  a single-class basket. BTC B&H captures the same crypto beta
  more efficiently.
- **Why no variation #2:** academic-foundation-exhausted.
  No peer-reviewed citation addresses TSMOM on a same-class
  correlated basket outperforming the dominant asset's B&H
  on Sharpe without shorts or leverage.
  count_distinct_variations("TrendFollowing_multi") is 1/20,
  cap effectively closed.
- **Branch implication:** Branch C strengthens for this strategy.
- **Forensic:** `research/trendfollowing-literature.md`
  § "Trial #1 outcome (2026-05-04)".

---

### Breakout — AVAX/USDT

- **Phase 3c verdict (2026-04-26):** RETIRE. observed_sharpe −1.3337,
  dist mean −1.3870, n_trades 134. Net-losing.
- **3-year backtest diagnosis (2026-04-19, historical reference):**
  −36.16% OOS return, −2.78 Sharpe, 47 trades, 25.5% win rate. 91.5% of
  OOS exits are stop_loss. IS avg win was +$248, OOS collapsed to +$138
  — barely exceeds avg loss. Breakouts on AVAX mostly resolve as
  fakeouts; IS looks like overfitting to a specific AVAX regime.
- **Mechanism evidence (audit):** Lukac/Brorsen/Irwin 1988 found channel
  breakouts profitable on 12 commodity futures 1978–1984; Park/Irwin
  2007 meta-survey shows explicit post-1990 weakening; Marshall/Cahan/Cahan
  2008 and Park/Irwin 2010 using Reality Check / Hansen SPA: post-1990
  futures channel breakouts have no consistent profitability after
  data-snooping correction. Crypto-specific: Zarattini/Pagani/Barbon 2025
  — ensemble Donchian on top-20 rotational basket with vol-sized
  positions — Sharpe >1.5, alpha 10.8% vs BTC. Single-pair 1H 20-period
  config matches none of these.
- **Next action:** Branch A redesigns as Zarattini-style daily ensemble
  (lookbacks 5/10/20/30/60/90/150/250/360) on top-20 rotational basket.
  Branch B drops Breakout. Branch C drops the strategy.

---

### VolatilityBreakout — BTC/USDT

- **Phase 3c verdict (2026-04-26):** RETIRE. observed_sharpe −3.6221,
  dist mean −3.9081, n_trades 1687. Catastrophic underperformance —
  worst in cohort. Strategy actively destroyed substantial value.
- **3-year backtest diagnosis (2026-04-19, historical reference):**
  −21.87% OOS return, −2.98 Sharpe, 415 trades, ~40% win rate. Trades
  ~90×/month and exits every trade at next candle's open regardless of
  P&L. At ~40% win rate with near-symmetric win/loss size, expected
  value is negative by design.
- **Mechanism evidence (audit):** Williams' formulation is
  practitioner-only (1999 book). Peer-reviewed cousin (opening-range
  breakout) is daily or 5-30 min. No peer-reviewed work on Williams VB
  in crypto. Modern academic edge (Zarattini/Barbon/Aziz 2024) is in
  the selection layer, not single-name. 1H is unmotivated middle frame
  with no statistical validation; "today's open" anchor presupposes a
  session boundary that 24/7 crypto lacks.
- **Next action:** Drop or, in Branch A, redesign at daily on multi-coin
  with relative-volume selection (Zarattini-style). Branch B and C drop.

---

### DualMomentum — BTC/USDT (rotates BTC/ETH/BNB)

- **Phase 3c verdict (2026-04-26):** RETIRE. observed_sharpe −2.3906,
  dist mean −2.2850, n_trades 1095. Severely net-losing. **Supersedes
  the prior "incomplete 3-year run" open question** — the dev_cpcv
  verdict is conclusive regardless of 3-year-run completion status.
- **3-year backtest diagnosis (2026-04-19, historical reference):**
  Not available. The 3-year run was killed at the 150-min process cap
  mid-IS. A 3-month smoke showed 55 rotations firing correctly. Engine
  behaviour looked right on the smoke; the full-run result was missing.
- **Mechanism evidence (audit):** Antonacci's framework (2012; 2014
  book) and the foundational literature — Jegadeesh/Titman 1993,
  Asness/Moskowitz/Pedersen 2013, Geczy/Samonov 2017 — operate at
  monthly frequency with 12-month formation. Crypto-specific:
  Liu/Tsyvinski/Wu 2022 at weekly with 3-week formation. Current
  strategy contracts the framework by ~720× (12 months ≈ 8,640 hours
  → 21 hours). No peer-reviewed paper supports this contraction.
  3-asset breadth is below academic standard.
- **Next action:** Branch A redesigns at weekly with ≥5 majors per
  Liu/Tsyvinski/Wu 2022. Branch B drops DualMomentum. Branch C drops
  the strategy.

#### Phase 4.A outcome (2026-04-29) — weekly 5-basket RETIRED

- **Variation tested:** `phase4a-weekly-5basket-v1`. Five-major
  basket [BTC, ETH, BNB, SOL, XRP] with `lookback=504` (3 weeks ×
  7 days × 24 hours, candle-count approximation of weekly 3-week
  formation per Liu/Tsyvinski/Wu 2022) and `rebalance_every=168`
  (weekly cadence). Hypothesis-of-record at
  `research/dualmomentum-literature.md`, committed `bf4b9ca`.
- **Harness result:** CPCV-10 raised `CPCVError` — 4/10 blocks
  valid (≥5 trades), 6 below the floor due to warmup amortization
  (504-candle formation × 10 blocks = 24% of each 2078-candle
  block lost to warmup). Per-block trades:
  `[5, 3, 2, 9, 4, 5, 2, 4, 5, 3]`. Trial appended as
  `trial_type="smoke"` (excluded from DSR multiple-testing); same
  precedent as Supertrend trial #1 (`d29e604`).
- **Headline run (full dev window, single backtest):** Sharpe
  −1.1973 on 44 trades, −16.09% return, 20.17% max DD, 29.6% win
  rate. Active-symbol distribution rotated cleanly across all 5
  majors (BTC 11.4%, ETH 13.6%, BNB 22.7%, SOL 31.8%, XRP 20.5%) —
  the strategy is firing as designed; the economic verdict is
  negative on its own merits.
- **Why no variation #2:** academic-foundation-exhausted
  precondition. Liu/Tsyvinski/Wu 2022 is the strongest peer-
  reviewed crypto-momentum source; current parameters are at the
  academic standard. Further variation lacks a citation —
  parameter sweeps without per-variation justification are exactly
  what CLAUDE.md's no-p-hacking rule forecloses.
  `count_distinct_variations("DualMomentum")` is now 2/20 with the
  cap effectively closed.
- **Branch implication:** Branch C of `MASTER_PLAN.md` strengthens
  for this strategy.
- **Forensic:** `research/dualmomentum-literature.md` § "Trial #1
  outcome (2026-04-29)".

---

## Phase 4.B verdicts (2026-05-02)

Source: `backtest/trials.log` trial_id
`f2c343c3fb2c4c029b66063d38a96605`. Test against
`sr_zero_expected = 0.0000` (single-trial budget); BTC B&H
baseline +1.6337.

---

### FundingRateHarvest — BTC/USDT (delta-neutral spot+perp)

- **Phase 4.B verdict (2026-05-02):** FINAL_GATE RETIRE.
  Holdout sharpe +0.3527 vs sr_zero_expected +0.5198,
  dsr_holdout 0.005407 (vs dsr_validation 0.99999 on dev),
  n_trades 11 (10 funding_flip + 1 backtest_end),
  signal_event_count 663. Mechanism worked (656 funding
  settlements processed at 0.989 ratio, 10 funding_flip
  exits — strategy rotated through its edge as designed);
  economics did not clear the multiple-testing null on the
  holdout window. Dev_cpcv pass historical context:
  observed_sharpe +4.3395, cpcv mean +5.1669, dsr_validation
  0.99999548, all four verdict-tree bools True. Dev↔holdout
  sharpe gap (5.17 → 0.35) is the structural-failure-mode
  signal per the literature's pre-commit, now extended to
  cover the V1-passes-dev-fails-holdout case (see
  `research/funding-rate-literature.md` provenance section).
  trial_id 199abc0a (final_gate, clean, single row).
- **Substrate:** OKX USDT-M perp BTC-USDT-SWAP + USDT spot
  BTC/USDT. Funding cadence 8h. Dev window 2023-05-03 →
  2025-09-22 (~29 months). Path-5 hybrid funding ingestion
  (commit `67bc92d`) covers data_start with 1-month margin.
- **Mechanism evidence (literature):** Schmeling, Schrimpf &
  Todorov "Crypto Carry" (BIS WP 1087 / forthcoming
  Management Science). Sample April 2019 – July 2024, BTC +
  ETH on six venues. Full-sample mean funding ~8% APY with
  annualized Sharpe 6.45; 4.06 from 2024 onward; turning
  negative in 2025. The strategy collected 93.22% of its
  realised PnL from funding cash (per gate-2 audit on the
  smoke window), confirming the literature's funding-as-
  dominant-driver claim on this dev sample.
- **Per-block forensics:** block 1 (mid-2023) sharpe −2.17
  in a multi-month negative-funding regime — strategy
  repeatedly opened, hit funding-flip exit at N=4 consecutive
  negative settlements, exited and reopened. Bleeds fees in
  that regime; structural failure mode named in the
  literature. Block 3 (2024 bull run) sharpe +14.72 — funding
  paid richly and the strategy held continuously.
- **Diagnostic chain:** three superseded prior trials before
  the clean f2c343c3 row. (a) `8acd27ae...` stale-cache bug
  (10 blocks at funding_settlements=0); (b) `2b9bd83b...`
  script-level months-math bug (blocks 0-1 partial coverage);
  (c) `e7eba18a...` harness-level months-math bug (same
  symptom). Each fix-SHA is referenced in the row's
  superseded_by field; the substrate-coverage assertion + per-
  block ANOMALY D were added across the chain. See `git log
  --oneline 6c395ab..2817c3f` for the four fix commits.
- **V2 status:** RETIRE on V1 triggers the literature's
  structural-redesign gate (extended ternary per
  `research/funding-rate-literature.md` provenance section).
  V2 must be a structural redesign sourced from a specific
  paper — different leg construction, different instrument
  family, or different rebalancing rule — not a parameter
  perturbation of V1. The dev↔holdout gap on V1 is the
  failure-mode signal V2's redesign must address. Deferred
  to a future chat for hypothesis-of-record + source citation
  fill before queue.

---

## Summary

9 RETIRE + 1 UNDER_TESTED. Zero strategies cleared the deploy gate.
Phase 4 branch decision pending per `docs/open_questions.md`.

## Historical reference: 2026-04-19 3-year backtest

Preserved as historical context. Superseded as a deploy signal by the
Phase 3c verdicts above.

Status legend (historical):

- **Working** — positive OOS Sharpe on the 3-year backtest, kept pending
  formal DSR validation.
- **Borderline** — mixed OOS signal, marked for rescue or retire decision.
- **Failing** — net-negative OOS with a plausible rescue path.
- **Broken** — net-negative OOS with a diagnosed design or parameter
  failure.
- **Incomplete** — backtest did not finish; status unknown.

| Strategy | Symbol | Status | OOS Ret% | OOS Sharpe | Trades | Notes |
|---|---|---|---|---|---|---|
| VWAP | ETH/USDT | Working | +17.43% | +2.30 | 123 | Only strong OOS Sharpe in 3-year |
| BearShort | BTC/USDT | Working | +0.35% | +1.11 | 69 | Shorts verified (side="short"); hedge contributor |
| GridTrading | SOL/USDT | Working | +0.20% | +0.73 | 359 | Grinding positive; ~79% win rate |
| DCA | BTC/USDT | Borderline | −7.15% | −0.83 | 52 | 92% win rate but losing — risk/reward imbalance |
| MeanReversion | ETH/USDT | Failing | −4.19% | −2.27 | 13 | Barely fires; EMA filter too tight |
| Supertrend | ETH/USDT | Broken | −46.07% | −2.78 | 95 | 29.5% win rate, avg loss > avg win |
| TrendFollowing | BTC/USDT | Broken | −38.17% | −2.64 | 119 | 28.6% win rate too low for EMA9/21 |
| Breakout | AVAX/USDT | Broken | −36.16% | −2.78 | 47 | 91.5% stop_loss exits — fakeouts dominate |
| VolatilityBreakout | BTC/USDT | Broken | −21.87% | −2.98 | 415 | 1-candle exit design flaw, net-negative EV |
| DualMomentum | BTC/USDT (rotates) | Incomplete | — | — | — | 3-year run timed out at 150 min; 3-month smoke OK |

Detailed per-strategy failure write-up for the four broken strategies:
`docs/strategy_failure_analysis_2026-04-19.md`.

### IntradaySeasonalityEffects

**Phase 4.C sq-003 — RETIRE**

Variation: intraday-hourly-long-21-23utc
Trial: d6d0e252a9494982bed3fad470dc5dba

Pure time-of-day filter: long BTC/USDT at 21:00 UTC, exit at 23:00 UTC.
No indicators. Result: sr=-1.17, baseline=1.69, DSR=5.2e-72.
7/10 CPCV blocks negative. Consistent with Baur et al. (2019) negative
prior. No edge in this window.

---

### MeanReversion_BTC_Residual

#### Phase 4.A outcomes

- **phase4a-btc-residual-mr-v1 (2026-05-05):** verdict=dry-run. sr=nan, baseline_sr=nan, dsr=nan, n_trades=0, mt_mean_pass=?, baseline_pass=?.

---

### OnChainMetricModels

#### Phase 4 outcomes

- **onchain-macro-cycle-filter (2026-05-05):** verdict=dry-run. sr=nan, baseline_sr=nan, dsr=nan, n_trades=0, mt_mean_pass=?, baseline_pass=?.

---

### SocialSentimentMomentum

#### Phase 4 outcomes

- **sentiment-momentum-filter (2026-05-05):** verdict=dry-run. sr=nan, baseline_sr=nan, dsr=nan, n_trades=0, mt_mean_pass=?, baseline_pass=?.

---

### IdiosyncraticResidualTSMOM

#### Phase 4 outcomes

- **idio-residual-tsmom-v1 (2026-05-06):** verdict=retire. sr=0.3859, baseline_sr=0.8867, dsr=1.0000, n_trades=228, mt_mean_pass=True, baseline_pass=False.

---
