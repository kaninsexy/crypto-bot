# Strategy Evidence Audit — 2026-04-26

**Status:** One-time reference document. Synthesizes the academic, practitioner, and platform evidence for each of the 10 strategy classes in this bot's library, interpreted alongside the dev_cpcv 9 retired + 1 under_tested empirical result.

**Companion data:** `logs/dev_cpcv_all_20260425_194818.log`, `backtest/trials.log` (rows from 2026-04-25 19:48 onward).

---

## Empirical result — the data this audit interprets

dev_cpcv all-strategies run, 2026-04-25 (sr_zero_expected = +1.9007 throughout, baseline = ETH/USDT B&H over dev window unless noted):

| Strategy | observed_sharpe | baseline_sharpe | beats_baseline | Verdict |
|---|---|---|---|---|
| DCA | +1.3527 | +1.6945 | No | RETIRE |
| Supertrend | -1.6388 | +0.6836 | No | RETIRE |
| GridTrading | +1.5004 | +1.8570 | No | RETIRE |
| Breakout | -1.3337 | +0.2035 | No | RETIRE |
| TrendFollowing | -1.7708 | +1.6945 | No | RETIRE |
| BearShort | +1.3129 (pre-fix) † | +1.6945 | No | RETIRE |
| VWAP | +1.1389 | +0.6836 | Yes | RETIRE |
| VolatilityBreakout | -3.6221 | +1.6945 | No | RETIRE |
| DualMomentum | -2.3906 | +1.6945 | No | RETIRE |
| MeanReversion | (CPCVError) | — | — | UNDER_TESTED* |

*MeanReversion raised `CPCVError: more than 50% of blocks have insufficient trades` — 4-filter AND stack self-suppressed below `_MIN_TRADES_PER_BLOCK` in 7 of 10 blocks. No row written to trials.log (atomicity). Semantically `under_tested`.

† The BearShort row reflects the simulator's pre-fix sign-inverted short PnL. The post-fix re-run on commit `25bd843` (trial_id `4f89d224107c4a61a958f051791c7a51`, ts 2026-04-26T14:56:07Z, identical `params_hash`) produced `observed_sharpe = -2.9643` with a block-Sharpe distribution mean −3.46 (p05 −4.66 / p95 −1.43); verdict still RETIRE. The table's column-level conclusion (0/10 cleared the null, 1/10 beat baseline) is unchanged. See `docs/open_questions.md` "Phase 4 implications of the 2026-04-26 short-pnl fix" for full detail.

**Pattern:** 0 of 10 strategies cleared the multiple-testing null at N=20. 1 of 10 beat baseline (VWAP, by a thin margin in a trending dev window). 4 of 10 had negative observed Sharpe — these are net-losing in the dev window, not "almost edge."

---

## Why this is structural, not bad luck

López de Prado's first prescription in *Advances in Financial Machine Learning* (2018) Ch. 12: "Develop models for entire asset classes or investment universes, rather than for specific securities. If you find mistake X only on security Y, no matter how apparently profitable, it is likely a false discovery."

When 10 strategies on the same single-asset substrate all fail CPCV, that's overdetermined: every strategy inherits the same noise process, regime exposure, and fee structure. The Bailey/Borwein/López de Prado Probability of Backtest Overfitting (PBO) literature establishes there is no Sharpe threshold safe under multiple-trial selection on one substrate. CPCV is doing its job; the substrate is the problem.

The 1H bar specifically sits in a documented academic dead zone. HFT/microstructure alpha is gone by 1H (arbitraged sub-second by Jump/Wintermute/Cumberland). Trend signals don't materialize until daily-to-monthly horizons (Moskowitz/Ooi/Pedersen 2012, *JFE*; Hurst/Ooi/Pedersen 2017 century-of-evidence). On-chain and attention signals operate at weekly+ (Liu/Tsyvinski 2018, 2022). What's left at 1H is mostly noise — and round-trip costs (~0.1–0.13% per trade) compound into 20–40% annual drag for any strategy with frequent rebalance.

The CTA diversification multiplier is the second structural finding. AQR's "Trends Everywhere" (Hurst/Ooi/Pedersen 2017) reports median individual-asset gross Sharpe of TSMOM at 0.27 vs diversified-portfolio Sharpe ~1.4 — a ~5× multiplier from going wide. Sharpe scales by √N for uncorrelated strategies. But crypto correlation collapses effective N: BTC/ETH 2-year correlation 0.83, ETH/BNB similar. With ρ ≈ 0.85, effective independent assets in a 3-token universe is ~1.3, multiplier ~1.14×. **Multi-pair on crypto majors is largely pseudo-diversification.**

---

## Per-strategy verdicts

### 1. DCA (3Commas-style price-deviation safety orders)

**Mechanism evidence.** No peer-reviewed validation of drawdown-triggered geometric-scaling DCA. Constantinides (1979, *JFQA*) shows averaging-down is dominated by sequentially-optimal strategies under standard expected utility. Mean-reversion support comes only from Bouri et al. (2019, *IRFA*) on asymmetric BTC mean reversion — regime-dependent and unstable post-2018. Time-based dollar-cost-averaging has unrelated literature; the price-deviation variant is martingale, not classical DCA.

**Practitioner reality.** 3Commas markets case studies showing "100% win rate" — mechanical artifact, since open positions never realize losses until forced. Documented operational risk: 2022 API breach class-action (Freeman v. 3Commas, 9th Cir. revived March 2026) and ≥$22M user losses. CFTC Press Release 8854-24 explicitly characterizes "AI/automated bot above-average returns" claims as a fraud red-flag.

**Empirical (this run).** observed_sharpe +1.35, baseline_sharpe +1.69. Strategy underperformed buy-and-hold in a trending dev window. Classic short-volatility profile result.

**Timeframe.** 15m–1h is approximately optimal for the mechanism (such as it is); 1H is fine.

**Asset structure.** Single-pair acceptable (signal is exogenous price-deviation).

**Verdict: drop or contain as long-bias accumulation only.** Mechanism has no robust academic support. The strategy is structurally a short-puts payoff that depends on regime classification you'd need an independent edge to do well. **TF/pair is not the issue — the strategy itself doesn't carry true alpha.**

### 2. GridTrading (Bitsgap-style adaptive)

**Mechanism evidence.** **Chen, Chen & Jang (2025, arXiv 2506.11921) prove the expected return of traditional grid trading is essentially zero pre-fees** under symmetric random walk. This is the cleanest theoretical result against grid strategies in the literature. Range-bound mean reversion has hourly support in BTC (Bouri et al. 2019; Naeem et al. 2021), but Arda (2025, SSRN 5775962) shows Bollinger-based MR fails in sustained declines. Cong/Li/Tang/Yang (2023, *Management Science*) on crypto wash trading: >70% of unregulated-exchange volume is fake, overstating achievable round-trips.

**Practitioner reality.** Pionex aggressively markets grids because its revenue is fee-turnover — directly incentivized to recommend high-frequency configs whose net-of-fee edge is marginal. Bitsgap's UI redesign (their own admission) revealed prior "bot profit" metric excluded inventory mark-to-market. Stevens FSC (2024) found ML grid optimization "did not offer significant improvements over traditional grid trading."

**Empirical (this run).** observed_sharpe +1.50, baseline_sharpe +1.86. Lost to buy-and-hold; the +1.50 is below the multiple-testing null by 0.40 SR.

**Timeframe.** 15m–4h on confirmed sideways regime; 1H is among the better choices.

**Asset structure.** Single-pair is the standard deployment.

**Verdict: drop unless deployed with explicit short-vol regime-conditional edge.** Mathematically zero EV without a regime-detection edge. **TF/pair is not the issue — the strategy itself is fee-negative without external regime alpha.**

### 3. VWAP (rolling 24-bar mean reversion)

**Mechanism evidence.** VWAP-as-execution-benchmark literature (Berkowitz/Logue/Noser 1988, *J. Finance*; Madhavan 2002) is rock-solid but **VWAP-deviation-as-return-predictor has no top-tier peer-reviewed support**. Adjacent intraday reversal work (Heston/Korajczyk/Sadka 2010, *J. Finance*; Avramov/Chordia/Goyal 2006, *J. Finance*) finds sub-hour reversal is dominated by bid-ask bounce and unprofitable after costs. Khandani & Lo (2011, *JFM*) document equity stat-arb contrarian Sharpe collapsed ~10× from 1995 to 2007.

**Practitioner reality.** No major retail crypto bot platform offers a flagship VWAP-MR template — meaningful red flag.

**Empirical (this run).** observed_sharpe +1.14, baseline_sharpe +0.68 (vs ETH B&H during VWAP's symbol). Beat baseline in a relatively sideways segment of dev. Did not clear N=20 null. 319 trades — sample size fine.

**Timeframe.** 1H is in a gap with no peer-reviewed support. Reversal evidence concentrates at sub-hour (microstructure-dominated, unprofitable retail) or daily-to-monthly (cross-sectional).

**Asset structure.** Lo & MacKinlay (1990, *RFS*): most reversal is cross-sectional (lead-lag across stocks), not own-name. Single-pair time-series VWAP-MR is the weakest reversal variant.

**Verdict: drop.** Misappropriates execution-benchmark literature as predictive evidence. The +1.14 observed in dev is consistent with normal mean-reversion noise in a flat sub-period; OOS holdout window is unlikely to replicate. **Strategy itself is weak; TF/pair makes it worse but isn't the root.**

### 4. MeanReversion (BB %B + StochRSI K/D + EMA filter)

**Mechanism evidence.** Bollinger Bands have weak peer-reviewed support: Lento/Gradojevic/Wright (2007); Fang/Jacobsen/Qin (2014, Auckland UT WP) explicitly conclude "trading on Bollinger Bands may no longer be profitable." Stochastic RSI is from Chande & Kroll's 1994 practitioner book — *zero* top-tier peer-reviewed validation. Crypto-specific: Caporale/Plastun/Oliinyk (2019, *FMPM*) tested hourly counter-movement strategies on BTC/LTC/Ripple/Dash and found them **not profitable** after costs. Liu/Tsyvinski/Wu (2022) establish the cross-sectional crypto factor structure as market+size+**momentum** — no validated short-term reversal factor for liquid majors.

**Empirical (this run).** CPCVError — 4-filter AND stack self-suppressed. Only 3 of 10 CPCV blocks had `_MIN_TRADES_PER_BLOCK` trades. The strategy didn't even produce enough events for statistical evaluation. Variance reduction by stacking correlated weak signals taken to the point of self-suppression.

**Timeframe.** 1H is in the academic dead zone. Zaremba et al. (2021, *IRFA*) on 3,600+ coins: large/liquid coins show daily *momentum*, not reversal — daily reversal exists only in illiquid alts where costs kill it.

**Asset structure.** Same as VWAP — single-pair time-series MR is the weakest reversal variant.

**Verdict: drop.** ≥8 hyperparameters across BB, StochRSI, EMA filter, cooldown — near-textbook in-sample inflation under DSR. The crypto evidence specifically points the wrong direction (BTC/ETH show momentum at daily, not reversal). Self-suppression result confirms over-filtering. **Strategy itself is structurally weak; TF/pair compounds the issue.**

### 5. TrendFollowing (EMA 9/21 + MACD)

**Mechanism evidence.** Time-series momentum has serious peer-reviewed support — Moskowitz/Ooi/Pedersen (2012, *JFE*); Hurst/Ooi/Pedersen (2017, *JPM*); Asness/Moskowitz/Pedersen (2013, *J. Finance*) — but **all at monthly frequency** with 1–12 month signals. EMA crossover specifically: Brock/Lakonishok/LeBaron (1992) was killed out-of-sample by Sullivan/Timmermann/White (1999, *J. Finance*) and Bajgrowicz/Scaillet (2012, *JFE*) under FDR correction with realistic costs. MACD has no standalone peer-reviewed validation.

Crypto-specific: Detzel et al. (2021, *Financial Management*) validated 5–100 day MA rules on Bitcoin OOS — at **daily**. Liu/Tsyvinski (2021, *RFS*) document strong TS momentum at daily/weekly on BTC/ETH. Shen/Urquhart/Wang (2022, *Financial Review*) is the only peer-reviewed Bitcoin intraday TSMOM paper, and it's session-anchored, not continuous 1H.

**Empirical (this run).** observed_sharpe **−1.77**, baseline +1.69. Net-losing in dev. EMA(9,21) on 1H ETH whipsawed substantially.

**Timeframe.** Academic evidence is monthly futures or daily crypto. 1H unsupported.

**Asset structure (decisive).** Hurst/Ooi/Pedersen (2017): diversified TSMOM Sharpe ~1.3 across 67 markets vs 0.3–0.5 individual — **~80% of Sharpe comes from cross-market diversification**. Single-pair ETH/USDT is the weakest possible deployment.

**Verdict: redesign at 4H or daily as multi-pair portfolio, or drop.** TF/pair IS the structural issue here — but the strategy class fundamentally requires diversification. Even single-pair daily would capture only ~20% of documented Sharpe. **TF/pair is the issue, but fixing it requires multi-pair, which crypto correlation structure makes only modestly helpful.**

### 6. Supertrend (ATR-band + BTC filter + 4H HTF agreement)

**Mechanism evidence.** **Supertrend has essentially zero peer-reviewed academic support.** Originates in Olivier Seban's 2009 French self-help book. Closest academic relatives — Donchian channels, Wilder ATR/ADX, Chande volatility-based stops — are practitioner constructs absorbed into the >5,000-rule universes that fail Marshall/Cahan/Cahan (2008, *JBF*) and Park/Irwin (2010) data-snooping tests.

**TradingView lookahead trap.** MTF Supertrend (the "4H HTF agreement" feature) is the canonical TradingView lookahead-bias trap — pulling 4H Supertrend onto 1H chart with `lookahead=barmerge.lookahead_on` and no `[1]` offset is a textbook future leak. Most public "Triple Supertrend" / "Multi-TF Supertrend" scripts have this bug.

**Empirical (this run).** observed_sharpe **−1.64**, baseline +0.68. Strategy net-lost despite a positive baseline — actively destroyed value.

**Timeframe.** No peer-reviewed support at any frequency for ATR-band channels.

**Asset structure.** Single-pair forfeits the diversification premium. BTC filter is primitive multi-asset awareness, not true cross-sectional diversification.

**Verdict: drop, or keep ONLY if you can rigorously verify the 4H HTF feed has no lookahead.** Indicator has no academic foundation; most common implementation pattern of its strongest feature silently future-leaks. **Strategy is weakly grounded; TF stack is the surface-level problem but the indicator's foundation is the deeper one.**

### 7. Breakout (20-period channel + volume 2× + ADX + MTF Supertrend)

**Mechanism evidence.** Lukac/Brorsen/Irwin (1988, *Applied Economics*): channel-breakout systems profitable on 12 commodity futures 1978–1984. Park/Irwin (2007, *J. Economic Surveys*) meta-survey: 56/95 positive but **explicit post-1990 weakening**. Marshall/Cahan/Cahan (2008, *JBF*) and Park/Irwin (2010) using White's Reality Check and Hansen SPA: **post-1990 futures channel breakouts have no consistent profitability after data-snooping correction**.

Crypto-specific: **Zarattini/Pagani/Barbon (2025, SSRN 5209907)** is the strongest evidence — ensemble of Donchian breakouts (lookbacks 5/10/20/30/60/90/150/250/360 days) on a survivorship-free top-20 crypto rotational portfolio with vol-sized positions reports Sharpe >1.5, alpha 10.8% vs BTC. **Note: ensemble + portfolio + daily lookbacks + vol-sizing — none of these match your single-pair 1H 20-period config.**

**Empirical (this run).** observed_sharpe **−1.33**, baseline +0.20 (AVAX dev window). Net-losing.

**Timeframe.** Academic evidence is daily. Marshall/Cahan/Cahan (2008b, *J. Empirical Finance*) found intraday equity TA not profitable after snooping.

**Asset structure (decisive).** Original Turtles required 20+ uncorrelated futures. Zarattini et al. used top-20 crypto rotational. Single-pair Donchian breakout has no academic support.

**Verdict: redesign at daily as ensemble multi-coin portfolio (Zarattini-style), or drop.** This is the strategy where the user's hypothesis about TF/pair lock is most directly correct — but the redesign required is substantial (ensemble lookbacks, top-20 universe, vol-sizing). **TF/pair IS the structural issue, and a credible peer-reviewed redesign exists.**

### 8. VolatilityBreakout (Larry Williams: open + K × prev range)

**Mechanism evidence.** Williams' formulation is practitioner-only (1999 book, 1987 World Cup audited but anecdotal). Peer-reviewed cousin is opening-range breakout: Holmberg/Lönnbark/Lundström (2013, *Finance Research Letters*) on crude oil daily; Wu/Syu/Lin/Ho (2019, *IEEE Access*) on intraday index futures. **Zarattini/Barbon/Aziz (2024, SFI 24-98)** tested 5-min ORB on >7,000 US stocks 2016–2023: diversified universe Sharpe ~0.48; **the edge is in selection (Stocks in Play with relative volume + news catalyst), not the breakout primitive**. No peer-reviewed work on Williams VB in crypto.

**Empirical (this run).** observed_sharpe **−3.62** (worst in cohort), baseline +1.69. Catastrophic underperformance — the strategy actively destroyed substantial value relative to passive holding.

**Timeframe.** Williams: daily. Crabel/ORB: 5–30 min. 1H is unmotivated middle frame with no statistical validation. The "today's open" anchor presupposes a session boundary that 24/7 crypto lacks.

**Asset structure.** Williams was multi-futures concentrated; modern academic edge (Zarattini/Barbon/Aziz) is in the selection layer, not single-name.

**Verdict: drop.** Single-pair 1H combines worst features: not Williams' daily, not Crabel's intraday-session, no selection layer. The −3.62 Sharpe is consistent with the audit prediction — the strategy is structurally broken for this substrate. **TF/pair is part of the issue but the strategy needs structural redesign, not parameter tuning.**

### 9. DualMomentum (BTC/ETH/BNB, 21-bar, rebalance every 5)

**Mechanism evidence.** Antonacci's framework (2012, SSRN 2042750; 2014 book) and the foundational literature — Jegadeesh/Titman (1993, *J. Finance*); Asness/Moskowitz/Pedersen (2013, *J. Finance*); Geczy/Samonov (2017) — operate at **monthly frequency with 12-month formation**. Crypto-specific: Liu/Tsyvinski/Wu (2022) at weekly with 3-week formation.

**Your strategy contracts the framework by ~720× (12 months ≈ 8,640 hours → 21 hours).** No peer-reviewed paper supports this contraction. The only hourly cross-sectional crypto study (Chu/Chan/Zhang 2020, *RIBF*) is 6-month single-regime in-sample on top-7 coins — wholly inadequate.

Daniel/Moskowitz (2016, *JFE*) on momentum crashes is critical: 14 of 15 worst momentum returns occurred when past 2-year market return was negative AND contemporaneous month was positive (panic-state rebound). 3-asset breadth is below academic standard. With BTC/ETH/BNB at correlations 0.7–0.9, cross-sectional component is mostly noise; strategy collapses toward absolute momentum.

**Empirical (this run).** observed_sharpe **−2.39**, baseline +1.69. Severely net-losing. Consistent with audit prediction: 720× frequency contraction breaks the mechanism.

**Asset structure.** Antonacci uses 6–10 asset classes; Geczy/Samonov used 6+ across 215 years. 3-asset crypto rotation has no academic precedent.

**Verdict: redesign at weekly with ≥5 majors, or drop.** The 720× frequency contraction is the dominant problem; even at weekly, 3-asset breadth is borderline. **TF is the explicit structural issue, but breadth is also too narrow.**

### 10. BearShort (Supertrend bearish + EMA 20<50 + RSI<55 + MACD<0)

**Mechanism evidence.** Of all 10, this has the most defensible time-series momentum theoretical grounding. Moskowitz/Ooi/Pedersen (2012) and Hurst/Ooi/Pedersen (2017) document TSM is symmetric — short-side trend-following in down-trending regimes is documented at monthly. Daniel/Moskowitz (2016, *JFE*): short-side momentum is structurally a short call on losers, exposed to panic-rebound crashes — but they demonstrate **dynamically vol-scaled momentum approximately doubles Sharpe** vs static, providing a clear improvement path.

Crypto intraday TSM has the strongest peer-reviewed support of any 1H signal: Wen/Bouri/Xu/Zhao (2022, *NAJEF*) and Li/Sakkas/Urquhart (2022, *J. Financial Markets*). However, the specific 4-filter signal stack (Supertrend + EMA cross + RSI threshold + MACD sign) has no peer-reviewed validation as a unit and has high multiple-testing risk.

**Empirical (this run).** observed_sharpe +1.31, baseline +1.69 (BTC dev window). Did not beat baseline (BTC was strongly trending up in dev — short strategy in long regime). Did not clear N=20 null.

The dev-window context matters here: BearShort is regime-allocated to BEAR/CRASH only, but the dev window contained substantial BULL/STRONG_BULL periods. Trade frequency was low and most opportunities were against strategy direction. The result is more a "didn't trade enough relevant opportunities" than "no edge in mechanism" — but that's a thesis CPCV correctly cannot distinguish without bear-regime-specific holdout.

**Timeframe.** Wen et al. and Li/Sakkas/Urquhart provide rare peer-reviewed support for hourly TSM on BTC. Daily/4H still has stronger evidence.

**Asset structure.** TSM works on single instruments per MOP — single-pair is canonical here.

**Verdict: keep, but redesign with vol-scaling and explicit rebound-state filter.** Most defensible academically. Add Daniel/Moskowitz dynamic vol scaling; model funding-rate cost (~10% annualized drag at -0.01%/8h); add explicit rebound-state filter (post-2-year-decline + contemporaneous-positive month per Daniel/Moskowitz 2016). Stress-test against May 2021 capitulation, March 2020 COVID, November 2022 FTX. **TF/pair is not the issue — the strategy is structurally sound; the implementation needs hardening, and the dev-window result is not informative without bear-regime evaluation.**

---

## Cross-cutting findings

### Platform reliability

Bitsgap/3Commas/Pionex ship execution, not edge. None publishes walk-forward, CPCV, DSR, or holdout protocols. Business models decouple platform revenue from user PnL — 3Commas/Bitsgap collect SaaS subscription regardless of outcome; Pionex earns turnover fees that scale with bot trades regardless of user PnL. The "demo bot" iteration economy on Bitsgap (one user feedback: "*it takes about 5 demo bots to find one good coin pair for a real bot*") is a literal description of multiple-testing without DSR adjustment.

Closest thing to independent retail PnL data: BIS Working Paper No. 1049 estimates **73–81% of retail crypto investors have likely incurred losses**. Bot-specific cohort data does not exist publicly; anecdotal Trustpilot/CoinCentral 2026 cite >80% bot-user underperformance vs buy-and-hold.

### Timeframe × strategy class meta-pattern

| Strategy class | Native peer-reviewed frequency | 1H position |
|---|---|---|
| Cross-sectional momentum | 3–12 month formation, monthly rebalance | Far below |
| Time-series momentum | 1–12 month, monthly | Far below |
| Trend following / CTA | Daily-monthly | Far below |
| Cross-sectional reversal | Daily-weekly | Above |
| Pairs / stat arb | Daily | Above |
| Microstructure / market making | Sub-second | Far above |
| Donchian / channel breakout | Daily (futures) | Far below |
| Opening range breakout | 5–30 min | Below |
| Mean reversion / Bollinger | Daily | Far below |
| **Intraday TSM crypto** | **1H BTC/majors** | **At 1H** |

**1H crypto TSM is essentially the only strategy class with peer-reviewed hourly support — and that's exactly what BearShort is.** Everything else in the cohort is being deployed at a frequency the published evidence does not validate.

### Hypothesis verdict

User Hypothesis A: 1H/single-pair lock is the structural problem.
Counter-Hypothesis B: validation correctly identifies that retail templates lack edge regardless of TF.

**Evidence supports B as the dominant explanation, with A correct in specific cases (5 TrendFollowing, 7 Breakout, 9 DualMomentum — and partly 8 VolatilityBreakout).** Even if Hypothesis A were 100% true and you moved to weekly bars on a 10-coin basket, you would still face Hypothesis B because the templates themselves don't carry edge. Cakici et al. (2024, *IRFA*) shows sophisticated ML on 40 features at weekly barely survives transaction costs; naive RSI/MACD/Supertrend/Bollinger stacks cannot plausibly outperform that.

Multi-pair would help marginally: ~1.1× Sharpe scaling on crypto majors (not √10 due to correlation), plus larger N for DSR variance. Best case, this rescues 1–2 borderline cases. It does not "unlock" the cohort.

---

## Recommendations

**Drop entirely (mechanism doesn't carry edge at any retail-accessible TF):** VWAP, MeanReversion, DCA, GridTrading, Supertrend.

**Redesign at higher TF and as multi-pair portfolio (TF/pair IS the issue, but redesign is substantial):** TrendFollowing (daily multi-asset per HOP); Breakout (Zarattini-style daily ensemble + top-20 rotational); DualMomentum (weekly with ≥5 majors per LTW).

**Redesign at daily or with selection layer:** VolatilityBreakout (daily Williams on multi-coin with Zarattini-style relative-volume filter).

**Keep, harden:** BearShort. Add Daniel/Moskowitz dynamic vol scaling, model funding-rate cost, explicit rebound-state filter, stress-test bear-regime rebounds.

---

## What this audit does and does not establish

**Does establish:** the dev_cpcv 9 retired + 1 under_tested result is not a tuning problem. The substrate (1H, single-pair) plus the strategy library (mostly retail templates) cannot produce edge that survives proper validation. Adding parameter variations under N=20 budget will not change this.

**Does not establish:** whether moving to daily multi-pair would produce edge. The academic evidence supports that *some* strategy classes (TSM, breakout, dual momentum) work at higher TF on diversified portfolios. It does not guarantee that *your* implementation of those strategies at higher TF would clear DSR. That's a Phase 4 empirical question requiring fresh validation.

**Open question for Phase 4 deliberation:** given the structural finding, does the project (a) rebuild around daily/multi-pair frameworks (substantial scope, 2–3 month redesign), (b) accept BearShort-only with hardened implementation (narrow but defensible), or (c) pivot off systematic crypto entirely toward different markets or strategy categories?

This audit informs that decision but does not make it.
