# Strategies — Per-Strategy Status and Diagnostic Reference

Last updated: 2026-04-26

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
