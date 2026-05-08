# Funding-Rate Harvest — Variation #2 candidate hypotheses

**Date:** 2026-05-08
**Authoring agent:** deep-researcher (Gemini 2.5 Pro deep pass +
WebSearch seed papers)
**Status:** CANDIDATES — not queued. Per `.claude/rules/backtest.md`
proposer-agent rule, the human (or proposer) reviews candidates and
converts a chosen one into a queue entry only after chat-side
selection.

## Background and design constraint

Variation #1 (`phase4b-delta-neutral-singlepair-btc-v1`) passed
dev_cpcv (dsr_validation 0.99999548) but failed final_gate on
holdout (dsr_holdout 0.005407, holdout sharpe +0.3527 vs
sr_zero_expected +0.5198). The dev↔holdout sharpe gap is the
structural-failure signal — V1's mechanism worked but its
economics did not clear the multiple-testing null on out-of-sample
data. The holdout window coincides with the period Schmeling et
al. document as declining-carry (annualized Sharpe 6.45 full-sample,
4.06 from 2024 onward, NEGATIVE in 2025).

Per `funding-rate-literature.md` § "Note on the strategy's
provenance," V2 must be a *structural redesign* (different leg
construction, different instrument family, different rebalancing
rule sourced from a specific paper), not a parameter perturbation
of V1. V2's hypothesis must articulate why its construction would
have replayed on the holdout window where V1's did not.

The pre-existing V2 stub `phase4b-threshold-entry-singlepair-btc-v2`
(funding-rate threshold entry gate) is preserved as the agnostic
baseline; the candidates below are paper-derived structural
alternatives the proposer agent or the human can rank against
that baseline.

## Candidate A — cross-sectional basket carry on OKX USDT-M perp universe

**One-line construction.** At each 8h funding-cadence boundary,
rank all OKX USDT-M perp listings by current funding rate; long
the top decile (or top-N by liquidity-adjusted rate) hedged spot,
short the bottom decile (or short the bottom-N by raw rate) hedged
spot, equal-notional per leg of each name; rebalance at funding
cadence; weight equal-vol within each side.

**Why structurally different from V1.** V1 collected the *level*
of funding on a single name (BTC) on a single venue (OKX). The
basket strategy collects the *cross-section* of funding levels —
funding mean across names is removed by the long/short construction,
leaving the cross-sectional spread as the harvest. The structural
hypothesis is that even when the universe-mean funding is negative
(the 2025 Schmeling et al. regime that broke V1), the
*cross-sectional dispersion* of funding remains exploitable and
positive in expectation. Fan, Jiao, Lu, Tong (SSRN 4666425) is the
load-bearing citation: their cross-sectional carry strategy is
explicitly documented as "resistant to market crashes in 2018 and
2021," which is the specific empirical claim that addresses the
dev-vs-holdout regime collapse.

**Primary citation.** Fan, Jiao, Lu, Tong (2023). "The Risk and
Return of Cryptocurrency Carry Trade." SSRN 4666425. Annualized
return 43.4-46.71%, Sharpe 0.74-0.77, robust to the 2018 and 2021
crash regimes.

**Supporting citations.** (i) Schmeling, Schrimpf & Todorov (BIS WP
1087) — establishes that majors-only carry is regime-fragile,
motivating the basket diversification. (ii) Inan (SSRN 5576424) —
funding rates are out-of-sample forecastable; the forecast can
serve as the cross-sectional ranking signal if raw rate is too
noisy.

**Expected pre-trial gates (locked).**
1. Universe specification: OKX USDT-M perp listings with >=$10M
   24h volume averaged over the dev window; refresh universe
   monthly with point-in-time eligibility (no lookahead).
2. Selection rule: top-N=3, bottom-N=3 by raw 8h annualized
   funding rate at the most recent settlement, or by Inan-style
   forecast if probe shows materially better dev Sharpe.
3. Rebalance cadence: 8h funding boundary (matches harvest
   cadence per Schmeling et al. table convention).
4. Construction: equal-notional spot+perp hedge per name, equal
   notional across names within a side.
5. Manifest schema extension required: `basket: [...]` field —
   sacred-harness change requiring explicit chat approval before
   queue.

**Forensic risk and dev-vs-holdout mitigation.** Risk: cross-
sectional carry can collapse in joint-liquidation regimes (Schmeling
et al. document a 22%-of-OI sell-liquidation pulse following high
standardised carry — basket exposure inherits this). Mitigation:
the long+short construction means the basket hedges its own
universe-mean funding; the dev-vs-holdout gap collapses to the
gap in *cross-sectional dispersion* between dev and holdout, which
Fan et al. document as more stable than levels.

---

## Candidate B — volatility-regime-conditional delta-neutral harvest

**One-line construction.** Same legs as V1 (single-pair
delta-neutral long-spot + short-perp on BTC), but harvest only when
realized 30-day volatility on BTC is below the dev-window median
(LV regime); stay flat in HV regime.

**Why structurally different from V1.** V1's logic was always-on
once funding > 0; it had no concept of regime. Almeida, Grith,
Miftachov, Wang (arXiv 2410.15195) demonstrate that Bitcoin
risk-premia decompose distinctly across two volatility regimes
(LV BVRP=0.17, HV BVRP=0.12), with the LV regime carrying the
larger share of the upside-return premium. The V2 hypothesis is
that the carry-pool collapse Schmeling et al. document in 2025
coincides with HV-regime-dominant funding (long-side leverage
unwinds, longs no longer overpay shorts), and a regime-gated
construction would have stayed flat through the holdout-killing
sub-window. This is structurally different from V1 because the
strategy's *exposure* (not its parameters) is now a function of
an exogenous regime variable.

**Primary citation.** Almeida, Grith, Miftachov, Wang (2024).
"Risk Premia in the Bitcoin Market." arXiv 2410.15195v2.
Documents two distinct option-implied volatility regimes with
materially different risk-premium decomposition.

**Supporting citations.** (i) Schmeling, Schrimpf & Todorov —
provides the time-varying-carry empirical anchor the regime
filter operationalizes. (ii) Ruan & Streltsov (SSRN 4218907) —
spot-market spreads widen at funding settlement; provides the
exit-timing input.

**Expected pre-trial gates (locked).**
1. Regime variable: realized 30-day BTC return volatility,
   computed at each 8h boundary on a rolling window. Source:
   Almeida et al.'s LV/HV partition is option-implied; the
   realized-vol proxy must be calibrated on dev data only to
   recover a partition that maps to the same economic regime.
2. Threshold: dev-window median of the regime variable; held
   fixed for holdout (no in-holdout recalibration).
3. Legs and parameters from V1 unchanged: single-pair BTC,
   leverage 5x cross-margin, equal-notional, flip_exit_n=4,
   exit_mr_ratio_threshold=0.01.
4. Calibration probe: `scripts/phase_4b_v2_volregime_probe.py`
   computes the regime-variable distribution on dev only and
   writes the threshold to a probe-output JSON.

**Forensic risk and dev-vs-holdout mitigation.** Risk: the
realized-vol proxy may not recover the option-implied LV/HV
partition — if so, the regime gate fires on a different signal
than the paper validates. Mitigation: the calibration probe
must include a sanity check comparing the proxy's regime
membership against an option-implied baseline if available
(Deribit options data are public). The dev-vs-holdout gap
predicted by this construction is structurally smaller than V1's
because the strategy is flat in the holdout-killing HV sub-window
that broke V1.

---

## Candidate C — basis-conditional funding harvest

**One-line construction.** Same legs as V1 (single-pair BTC
delta-neutral long-spot + short-perp), but enter only when (i)
funding > min_entry threshold AND (ii) current log basis
(perp_mark / spot_mid - 1) > basis_entry threshold; exit when
either condition fails or a flip-exit fires.

**Why structurally different from V1.** V1 conditioned only on
funding rate. Cao, Luo, Cheng, Dong (SSRN 6365329) decompose
expected perpetual-futures returns into three orthogonal
components: current log basis, spot-price misperceptions, and
expected futures-spot spreads. The structural claim: funding
rate alone is a single component of the expected-return surface;
basis carries marginal forward information. A basis-gated
construction harvests carry only when both signals align,
which structurally filters out the "funding > 0 but basis is
collapsing" sub-regime that drives the worst V1 holdout sharpe
contributions.

**Primary citation.** Cao, Luo, Cheng, Dong (2026). "Anatomy of
Cryptocurrency Perpetual Futures Returns." SSRN 6365329.
Cost-of-carry decomposition into log basis + spot
misperceptions + expected spread.

**Supporting citations.** (i) Ackerer, Hugonnier, Jermann (Math
Finance 2024) — equilibrium pricing of perpetual futures; the
no-arbitrage basis-funding relationship that grounds the basis
entry gate. (ii) Schmeling, Schrimpf & Todorov — full-sample
basis statistics on majors.

**Expected pre-trial gates (locked).**
1. Basis variable: log(perp_mark / spot_mid) computed at each
   1h tick from the existing data layer (no schema change).
2. Funding threshold: `min_funding_rate_entry` per the existing
   V2-stub threshold-entry hypothesis (33rd percentile of dev
   positive-funding sessions, annualized).
3. Basis threshold: dev-window median of log basis when funding
   > min_funding_rate_entry; calibrated by probe on dev only.
4. All other parameters from V1 unchanged.

**Forensic risk and dev-vs-holdout mitigation.** Risk: basis
and funding are mechanically related (the funding rule clamps
basis), so the marginal information content of basis over
funding alone may be small. Mitigation: the dev probe must
report the conditional Sharpe gain of (funding > th AND basis >
th) over (funding > th alone) — if the gain is < 20%, abandon
this candidate before queue. The dev-vs-holdout gap predicted
by this construction is smaller than V1's only if dev probe
confirms basis carries marginal information.

---

## Candidate D — cross-exchange funding-rate-dispersion strategy

**One-line construction.** At each 8h boundary, when BTC perp
funding rate on Exchange A exceeds the funding rate on Exchange B
by more than a cost-adjusted threshold, open short on Exchange A
+ long on Exchange B (both perp, equal-notional, no spot leg);
close when the spread reverts or autocorrelation-decay window
elapses.

**Why structurally different from V1.** V1 traded the
funding-rate *level* on one venue against zero. This candidate
trades the *dispersion* across venues. The structural hypothesis
is that even when the level collapses across all venues
(2025 Schmeling et al. regime), the *spread* remains exploitable
because the cross-CEX information flow is one-directional and
slow (Two-Tiered Structure paper: CEX-to-DEX, autocorrelation
> 0.96). The dev-vs-holdout level collapse on V1 is replaced by
a dev-vs-holdout *spread persistence* check, which the paper's
8-day high-frequency sample documents as stable in regime.

**Primary citation.** "The Two-Tiered Structure of Cryptocurrency
Funding Rate Markets" (MDPI Mathematics 14(2):346, 2026).
Documents persistent (autocorrelation > 0.96), economically
significant (>=20bp in 17% of observations) cross-exchange
funding spreads; 40% of top opportunities net positive after
costs.

**Supporting citations.** (i) van Rij et al. (Blockchain:
Research and Applications, 2025) — recent CEX/DEX funding-rate
arbitrage empirical study (115.9% return / 1.92% max DD over
6 months, but caveat: short sample). (ii) Gornall, Rinaldi,
Xiao (SSRN 5036933, AEA 2026) — economic theory: cross-venue
funding premia are scarce-arbitrage-capital phenomena.

**Expected pre-trial gates (locked).**
1. Substrate gate (LOAD-BEARING): the bot today runs OKX-only
   per Phase 4.B Branch 1 venue resolution
   (`docs/research_log.md` § "Thai SEC venue / derivatives
   status"). This candidate requires multi-venue substrate
   (OKX + at least one of Binance/Bybit). The substrate
   decision is OUT OF SCOPE for the data layer Phase 4.B
   landed; this gate must clear before any trial-design
   work proceeds.
2. Cost model: per-venue maker/taker fee + spread-reversal
   haircut per the Two-Tiered paper's 40%-of-top-opportunities
   number; entry threshold = 2× round-trip cost.
3. Universe: BTC + ETH initially (highest liquidity); expand
   only if dev shows insufficient signal density.
4. Holding window: ETH 20-min mean-reversion (per the paper)
   suggests sub-cadence holding; conflicts with 8h funding
   collection. Resolve before queue.

**Forensic risk and dev-vs-holdout mitigation.** Risk: only 40%
of top opportunities net positive after costs (per the paper);
cost model error is the dominant out-of-sample failure mode.
Mitigation: the dev probe must include a holdout-style cost
robustness check (vary spread-reversal haircut by ±50%, confirm
Sharpe sign stability). Substrate gate is a hard gate — without
multi-venue infrastructure this candidate cannot queue.

---

## Counter-finding tracked: funding-rate-as-momentum-signal candidate REJECTED

The deep pass surfaced a candidate (e) "use funding rate as a
forward-momentum signal for the underlying" but He, Manela, Ross,
von Wachter (arXiv 2212.06888v5) is the load-bearing counter-finding:
"Past return momentum significantly explains the futures-spot gap
with a time-series regression R² of more than 50% ... funding rate
is the OUTCOME of momentum, not a predictor of forward returns."
A V2 candidate that treats funding rate as a forward-signal is
reverse-causality and is rejected at the literature stage. The
funding-rate-as-carry interpretation (V1 + Candidates A/B/C/D) is
not contradicted by He et al. and is the structurally correct
framing.

---

## Open gaps surfaced by the deep pass (paper-derivable but
unresolved at this stage)

1. Long-run stationarity of cross-sectional carry premia is
   under-tested; Fan et al. sample ends 2023, post-2025 robustness
   unknown.
2. Capacity constraints on funding-rate arbitrage (decay as
   capital enters) are largely unmeasured. Gornall/Rinaldi/Xiao
   theory, no empirical decay curve.
3. No published out-of-sample regime-switching forecasting model
   for the LV/HV partition Almeida et al. identify; realized-vol
   proxy validation is the V2-internal probe burden.
4. Term structure of perpetual-vs-dated futures basis is
   under-studied for crypto; a true term-structure carry trade
   has weak literature support today and was not surfaced as a
   load-bearing candidate.
5. Cross-exchange arbitrage cost models in the literature
   typically assume single-account collateralization; the
   multi-account margin/collateral implications are unmodeled.

---

## Cross-document references

- `research/funding-rate-literature.md` — V1 hypothesis-of-record,
  Variation #1 retirement note, pre-trial gate #8 (single-pair-
  first), existing V2 stub `phase4b-threshold-entry-singlepair-
  btc-v2` (threshold entry gate — agnostic baseline).
- `research/funding-rate-risk-model.md` — leg-level risk model
  (variation-agnostic).
- `.memory/T2_semantic/_pending_review/citations/` — full per-
  citation provenance for each paper referenced above.
- `docs/research_log.md` § "Thai SEC venue / derivatives status"
  — single-venue (OKX) substrate constraint relevant to
  Candidate D.
