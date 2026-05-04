# Funding-Rate Harvest — Phase 4.B hypothesis-of-record

**Date:** 2026-04-29 (Variation #1 citation gap closed 2026-05-02;
empirical-baseline correction 2026-05-02; parameter-drift revert
2026-05-02 morning; Tracks 1-bundle continuation citation-anchored
fill 2026-05-02 afternoon)
**Phase:** 4.B kickoff — data layer locked (Path 5 hybrid ingestion);
Variation #1 citation gap closed; Variation #1 parameters fully set
(5 of 5 with named-source backing).
**Status:** Variation #1 RETIRED post-holdout 2026-05-02.
dev_cpcv passed (dsr_validation 0.99999548); final_gate
retired (dsr_holdout 0.005407, sharpe +0.3527 vs
sr_zero_expected +0.5198). The dev↔holdout sharpe gap is
the structural-failure-mode signal per the extended
provenance pre-commit (see § "Note on the strategy's
provenance" below). Variations #2+ remain stubs;
structural-redesign hypothesis-of-record + paper-derived
citation required before queue.

## Pre-trial citation gate — CLOSED 2026-05-02

**Citation slot — CLOSED.** Variation #1's primary peer-reviewed
citation is Schmeling, Schrimpf & Todorov "Crypto Carry" (BIS WP
1087 / CEPR DP20719 / SSRN 4268371; forthcoming Management Science)
— sample April 2019 – July 2024 across BTC + ETH on six venues,
exactly the delta-neutral long-spot / short-perp construction in
Variation #1. See § "Variation #1 → Source citations" below for the
full citation set including supporting papers (Christin/Routledge/
Soska/Zetlin-Jones working paper; recent CEX/DEX comparison work)
and the empirical calibration probe behind
`exit_funding_flip_n_settlements`.

**Parameter slots — CLOSED.** All five Variation #1 parameter
values now carry named-source backing (paper, risk-model section,
exchange-spec, or empirical probe with chat-side review of which
statistic anchors the structural hypothesis). The 2026-05-02
morning first-pass filled three slots by judgment and was reverted
after chat-side audit; the afternoon Tracks 1-bundle continuation
re-filled all three with citation-derived values, and the audit
caveat is preserved in the corresponding rows so the drift trail
is auditable.

**Net status of the no-p-hacking gate.** CLOSED for Variation #1.
Variation #1 may queue `record_trial(trial_type="full_cpcv")`
once the harness extensions land (Tracks E-I + Track 2
`signal_event_count` plumbing). Future variations still need
their own source-cited justification per the same rule.

The empirical anchor from Schmeling et al. — full-sample mean
funding ~8% APY with annualized Sharpe 6.45, declining to 4.06 from
2024 onward and turning negative in 2025 — replaces the earlier
"~10.95% baseline" framing that appeared in chat 2026-04-29 drafts.
The 10.95% figure was a theoretical/design floor (0.01% per 8h
settlement × 1095 settlements/year), not an empirical observation;
mixing the two led to drift across earlier docs. The corrected
post-tax economic baseline is in § "Post-tax economic baseline"
below; the historical drift correction is logged in
`docs/research_log.md` § "Thai SEC venue / derivatives status".

## Pre-trial gates (locked)

Constraints persisted from Phase 4.B venue scoping (chat
2026-04-29) and Tracks A-D drift correction (2026-04-30). These
are hard constraints on Variation #N hypothesis design.
Deviation requires updating this section explicitly with
rationale, not silent deviation in a Variation row.

1. **Gate #8 — single-pair-first**: Variation #1 is single-pair
   (legs: spot BTC/USDT + perp BTC-USDT-SWAP via OKX). Multi-
   pair selection (top-N from basket) is Variation #2 with its
   own hypothesis row. Source: chat 2026-04-29 venue scoping
   pre-trial gates list.
2. **Gate (no-p-hacking)**: Each variation requires a primary
   peer-reviewed source citation before record_trial(trial_
   type='full_cpcv'). Variation #1 citation gap CLOSED
   2026-05-02 (Schmeling/Schrimpf/Todorov "Crypto Carry");
   future variations still need their own source-cited
   justification before queue. Source: CLAUDE.md no-p-hacking
   rule.
3. **Gate (post-tax economics)**: Per-strategy expected Sharpe
   / required-funding-rate threshold derives from the post-tax
   *empirical* baseline (~5.2–6.0% APY after Thai PIT 25-35%
   on Section 40(4)(h) classification, anchored on the
   Schmeling et al. 2019–2024 ~8% empirical mean), not the
   theoretical ~10.95% design-floor figure that appeared in
   chat 2026-04-29 drafts. Source: docs/research_log.md
   § "Thai SEC venue / derivatives status (logged 2026-04-29;
   empirical-baseline correction 2026-05-02)".

## Phase 3c context

Funding-rate harvest was not in the Phase 3c portfolio — it requires a
substrate (perpetual futures + funding-rate settlement) that the bot
does not have today. The Phase 4.B scoping (committed `bf4b9ca` and
extended in the venue resolution at commit `241983f`) added the
substrate decision: OKX USDT-M perp + USDT spot, accept Thai PIT on
funding income (Branch 1 of three branches surfaced at scoping).

## Strategy archetype

Delta-neutral cash-and-carry: long spot + short perpetual at equal
notional. Income source: positive funding rate paid to shorts at
each 8-hour OKX USDT-M settlement.

Mechanism: when funding > 0, longs on the perp pay shorts at each
settlement (cadence verified live 2026-04-29 via
`scripts/phase_4b_halt_consult_check.py` — median/min/max all
8.00h over 90 settlements on BTC and ETH SWAP). The bot holds the
short perp leg; an equal-notional long spot leg cancels first-
order delta exposure. Positive funding is collected as cash on
the perp leg; the spot leg contributes only a small basis P&L
from spot↔perp price drift, bounded by the funding cadence ×
volatility within a settlement period.

**Empirical baseline (anchor for hypothesis testing).** Schmeling,
Schrimpf & Todorov "Crypto Carry" (2019-04 → 2024-07 sample;
BTC + ETH on six venues): full-sample mean funding ~8% APY with
vol ~0.8%, annualized Sharpe 6.45 over the full sample, 4.06
from 2024 onward, turning negative in 2025. The 2024-onward
decline is a regime feature; Variation #1's expected-Sharpe
direction is framed conservatively as a result.

**Theoretical/design baseline (reference only).** The exchange-
design floor for funding is 0.01% per 8h settlement × 1095
settlements/year ≈ 10.95% APY. Earlier drafts (chat 2026-04-29)
treated this as the empirical anchor; that was incorrect — the
0.01% × 1095 figure is the design ceiling implied by exchange
funding-rate clamps and is not what BTC/ETH have realized on
average. Hypothesis testing uses the empirical anchor (~8%
pre-tax) above; the theoretical floor is recorded here only so
the prior drift across docs is auditable.

The pair-selection layer (single-pair vs multi-pair top-N) is
NOT part of the strategy archetype — see "Pre-trial gates
(locked)" above and the per-Variation entries below for the
locked design choice per variation.

## Post-tax economic baseline

**Pre-tax empirical APY:** ~8% (Schmeling/Schrimpf/Todorov full-
sample mean, BTC + ETH, April 2019 – July 2024). 2024-onward
realized has been lower with the 2025 sub-window negative; the
~8% number is the long-run anchor, not a forward forecast.

**Thai PIT classification:** funding income is realised as a financial
benefit on a non-licensed venue and falls under Section 40(4)(h) of
the Revenue Code (per `docs/research_log.md` § "Thai SEC venue"). The
2025–2029 PIT exemption (Ministerial Regulation 399) does NOT apply
because OKX is not on the Thai-SEC-licensed digital-asset operator
list as of 2026-04-29.

**Post-tax empirical APY:** at marginal Thai PIT 25–35% on Section
40(4)(h) benefit, pool APY post-tax = ~8% × (1 − 0.25..0.35) ≈
**5.2–6.0% APY**.

**Theoretical-baseline reference (not the trading anchor).** The
0.01%-per-settlement × 1095-settlement design ceiling implies a
10.95% pre-tax / 7.1–8.2% post-tax band. That band has appeared in
earlier drafts of this file and `docs/research_log.md` as if it were
the empirical baseline; it is not. It remains here only as the
exchange-design ceiling reference, against which the empirical
~8% / 5.2–6.0% figure can be cross-checked.

**Implication for hypothesis testing:** every variation in this file
must derive its expected Sharpe / required-funding-rate threshold from
the **post-tax empirical** baseline (~5.2–6.0% APY), not from the
pre-tax number and not from the theoretical-design ceiling. That
band is the gross-of-execution-and-financing-costs ceiling; net-of-
fees net-of-borrow Sharpe is bounded below this.

## Variation #1 — `phase4b-delta-neutral-singlepair-btc-v1`

**Status (2026-05-02):** RETIRED post-holdout.
final_gate trial_id 199abc0a; dsr_holdout 0.005407;
holdout sharpe +0.3527 vs sr_zero_expected +0.5198. See
"Note on the strategy's provenance" below for the V2
entry gate.

**Pre-trial gate #8 (locked, persisted from Phase 4.B venue
scoping 2026-04-29):** Variation #1 must be single-pair. The
legs are spot BTC/USDT (translated to OKX BTC-USDT at the
data-layer boundary) and perp BTC-USDT-SWAP. Multi-pair
selection (top-N by funding rate across a basket) is reserved
for Variation #2 with its own hypothesis row.

**Hypothesis.** Equal-notional long-spot + short-perp on
BTC-USDT-SWAP + BTC/USDT, held continuously while funding rate
is positive. Income source: positive funding paid to shorts at
each 8h settlement on OKX USDT-M.

**Substrate.** OKX (USDT-M perp + USDT spot). Funding cadence
8h verified live 2026-04-29 via scripts/phase_4b_halt_consult_
check.py (median/min/max all 8.00h over 90 settlements). Data
layer: data/okx_perp.py + data/okx_funding.py with separate
cache namespaces.

**Source citations.**

PRIMARY:
- Schmeling, M., Schrimpf, A., & Todorov, K. (2023/2025).
  "Crypto Carry." BIS Working Papers No. 1087; CEPR DP20719;
  SSRN 4268371; forthcoming Management Science. Sample
  April 2019 – July 2024 covering BTC + ETH across six venues.
  Key findings load-bearing for Variation #1:
  - Carry strategy = short perp + long spot (matches
    Variation #1 construction exactly).
  - Funding rate is the dominant return driver: full-sample
    mean ~8% APY with vol ~0.8%.
  - Annualized Sharpe 6.45 full-sample, 4.06 from 2024
    onward, turning negative in 2025. The regime decline is
    the load-bearing reason Variation #1's expected-Sharpe
    direction is framed conservatively (rather than relying
    on the full-sample headline number).
  - Liquidation risk: a 10% increase in standardised carry
    predicts a ~22% increase in sell liquidations relative
    to total open interest over the following month —
    directly motivates the `exit_margin_breach_threshold`
    parameter setting below.

SUPPORTING (FOUNDATIONAL):
- Christin, N., Routledge, B. R., Soska, K., & Zetlin-Jones,
  A. (~2022, working paper). "The Crypto Carry Trade."
  Carnegie Mellon University. Foundational decomposition of
  crypto carry returns into funding-rate spread vs basis
  components; Schmeling et al. extends the empirical work
  with a longer/multi-venue sample.

SUPPORTING (RECENT VENUE COMPARISON):
- van Rij, S. N. C. *et al.* (2025). "Exploring Risk and
  Return Profiles of Funding Rate Arbitrage on CEX and DEX."
  *Blockchain: Research and Applications*, ScienceDirect
  article PII S2096720925000818. Sample August 2023 –
  February 2024 across Binance, BitMEX, ApolloX, Drift on
  BTC/ETH/XRP/SOL/BNB. Useful as a recent venue-comparison
  reference; the 6-month sample is short, so do NOT anchor
  baseline expectations on this paper alone — it
  complements rather than replaces the Schmeling et al.
  long-run anchor. (Lead-author surname pending verification
  against the published author list; the SSRN/journal
  metadata listed `S. N. C. van Rij` at retrieval time.)

SUPPORTING (EMPIRICAL CALIBRATION, INTERNAL):
- Negative-funding regime duration distribution probe
  (computed 2026-05-02, persisted as
  `scripts/phase_4b_funding_regime_probe.py` for
  reproducibility — DO NOT execute as part of CI; it is a
  one-time calibration probe whose output is committed into
  the `exit_funding_flip_n_settlements` row above). The
  probe's p95 statistic (= 4 settlements = 32 h) is the
  citation-aligned anchor for `exit_funding_flip_n_settlements`;
  the probe's `recommended_N` field (= round(p50) = 1) is
  superseded chat-side because the median measures intra-day
  noise, not the structural regime Schmeling et al. describe.

CROSS-REFERENCES:
- docs/research_log.md § "Thai SEC venue / derivatives status
  (logged 2026-04-29; empirical-baseline correction
  2026-05-02)" — post-tax baseline ~5.2-6.0% after Thai PIT
  25-35% on Section 40(4)(h) classification.
- docs/MASTER_PLAN.md § "Phase 4.B" — Branch 1 venue resolution.
- research/funding-rate-risk-model.md — per-leg margin,
  liquidation, funding payment math, exit triggers.

**Parameters.** All five slots filled as of 2026-05-02 (Tracks
1-bundle continuation prompt) with named-source backing per the
no-p-hacking rule. Each value below cites the gate, paper, or
empirical probe that justifies it. Five slots: `signal_cadence`,
`timeframe`, `target_vol_annual`, `notional_capital_per_leg`,
`exit_funding_flip_n_settlements`. Audit-trail note: the earlier
first-pass (2026-05-02 morning) filled three of these by judgment
and was reverted to placeholders after chat-side audit; this fill
is the post-audit citation-anchored version.

- `signal_cadence: "8h"` — funding-payment cadence on OKX
  USDT-M majors. The strategy's edge is collected at this
  cadence regardless of which OHLCV grid the manifest uses;
  it is the unit at which carry returns are measured in the
  empirical literature.
  Citation: Schmeling, Schrimpf & Todorov (2023, BIS WP 1087,
  forthcoming Management Science) — carry strategy returns
  reported at funding-payment cadence (annualized aggregate
  over the sample). OKX BTC-USDT-SWAP cadence verified live
  2026-04-29 by `scripts/phase_4b_halt_consult_check.py`
  (median/min/max all 8.00h over 90 settlements).

- `timeframe: "1h"` — OHLCV resolution for liquidation-cushion
  monitoring + intra-settlement basis-spike modeling. NOT
  derived from infrastructure convenience (the audit on
  2026-05-02 explicitly rejected the "1h is what the rest of
  the manifest uses" rationale).
  Citation: `research/funding-rate-risk-model.md` § 2.2
  (liquidation-cushion checks between settlements require
  sub-settlement OHLCV) + § 3.3 (basis tracking error
  computed at every 1h tick). Risk-model-derived structural
  requirement: a coarser grid would miss adverse mark-price
  spikes between 8h settlements; a finer grid would inflate
  the per-block bar count without commensurate signal gain
  given that funding fires at 8h.

- `target_vol_annual: 0.05` (5%) —
  Citation: Schmeling, Schrimpf & Todorov (2023). Derivation
  chain (each step traceable):
    forward Sharpe target = 1.0  (conservative discount of
      Schmeling et al.'s 2024-onward Sharpe 4.06 for the
      documented 2025 regime decline, Thai PIT post-tax
      shrinkage from ~8% pre-tax APY to ~5.2-6.0% post-tax,
      and execution costs per `funding-rate-risk-model.md`
      § 6.1-§ 6.2 open questions).
    target vol  = post-tax edge / target Sharpe
                ≈ 0.055 / 1.0
                = 0.055,  rounded down to 0.05.
  Floor: Schmeling et al. realized full-sample carry vol
    0.8% — target vol must exceed this or the budget bites
    on normal-regime variance.
  Ceiling: absorbs liquidation-tail premium (Schmeling et al.
    document a ~22%-of-OI sell-liquidation pulse following
    high standardised carry) + basis-decoupling stress per
    `funding-rate-risk-model.md` § 3.3.

- `notional_capital_per_leg: 10000` (USDT, trial-normalized)
  — equal-notional locked per gate #8. Sharpe is scale-
  invariant, so the absolute level is a trial constant rather
  than a signal-affecting parameter. Backing: gate #8 single-
  pair-first + scale-invariance of the Sharpe statistic; no
  per-paper citation required because the value does not
  affect the verdict.

- `exit_funding_flip_n_settlements: 4` — close the position
  after 4 consecutive negative 8h settlements (= 32h of
  sustained negative funding).
  Citation: Schmeling, Schrimpf & Todorov (2023) frame
  negative-funding regimes as sustained multi-event phenomena,
  not single-settlement noise. Numerical N calibrated to the
  95th percentile of negative-funding-rate run durations on
  BTC-USDT-SWAP across the dev window 2023-05-03 →
  2025-09-22 (probe output
  `/tmp/funding_negative_regime_distribution.json` →
  `scripts/phase_4b_funding_regime_probe.py`: p95 = 4
  settlements = 32 h). Alternatives rejected:
    p50 = 1   — noise-anchored (single-settlement blip,
                fires on intra-day funding noise);
    max = 8   — extremum-anchored (one observation, no
                statistical content);
    risk-model § 4.1 default N = 3 — engineering convenience
                that lands at dev-window ~p85, below the
                citation-aligned p95 cutoff.
  Probe-design caveat: the probe's `recommended_N` field
  (= round(p50)) is *superseded* by this row. The structural
  analog of Schmeling et al.'s "regime" is the upper-tail of
  the run-length distribution, not the median; future
  variations relying on the probe's `recommended_N` field
  require chat-side review of which statistic anchors the
  structural hypothesis.

- `exit_margin_breach_threshold: 0.01` (1.0% account margin
  ratio) — 2× cushion above the lowest-tier maintenance margin
  (mr = 0.005) per `research/funding-rate-risk-model.md` § 2.1.
  Backing: OKX exchange-spec lowest-tier maintenance-margin
  ratio + 2× buffer for execution-lag absorption at
  settlement-time price spikes (consistent with the Schmeling
  et al. 22%-of-OI sell-liquidation pulse following high
  standardised carry). The exchange-spec component is hard;
  the 2× buffer is the variation's risk-tolerance choice and
  may be revisited if dev_cpcv shows the cushion is the
  binding exit driver.

**Expected Sharpe direction.** Positive but materially below
the empirical-APY-implied ceiling. Post-tax empirical baseline
~5.2–6.0% before perp fees, spot fees, slippage, and long-leg
vol drag. The Schmeling et al. 2024-onward decline (annualized
Sharpe 6.45 full sample → 4.06 from 2024 → negative in 2025)
is the load-bearing reason this direction statement is
conservative — *not* the post-tax framing alone, which only
re-scales the level rather than raising regime risk. The
long-spot leg's price-PnL is hedged by the short-perp leg,
but tracking error from price moves is bounded by funding
cadence × volatility (see
`research/funding-rate-risk-model.md` § "Combined-position
sanity").

**Verdict-tree precondition.** trade-count floor: continuous
hold means n_trades may be ~1 per dev block (one open + one
close per regime period). The verdict tree's
min_trade_count=30 floor is at risk; the strategy's CPCV
interpretability depends on funding-payment-event count, not
trade count. Surface this as an open question before queuing
CPCV — may require harness adaptation or a different signal
boundary definition.

## Variation #2 -- `phase4b-threshold-entry-singlepair-btc-v2`

**Date:** 2026-05-04
**Status:** Hypothesis-of-record. Not yet queued.

**Structural failure mode addressed.** Variation #1 passed
dev_cpcv (dsr_validation 0.99999) but failed final_gate on
holdout (dsr_holdout 0.0054, sharpe +0.35 vs sr_zero_expected
+0.52). The holdout window coincides with the period Schmeling
et al. document as declining-carry (Sharpe 4.06 from 2024
onward, negative in 2025). V1 always-on logic entered and held
regardless of current carry level -- bleeding fees and negative
settlements in low-carry regimes. V2 structural redesign is a
minimum funding-rate entry gate that keeps the strategy flat
when carry is insufficient to cover costs.

**Hypothesis.** Delta-neutral BTC single-pair (same legs as V1:
spot BTC/USDT + perp BTC-USDT-SWAP on OKX). Entry gate added:
only open or reopen the position when the current 8h annualised
funding rate exceeds `min_funding_rate_entry`. Hold until either
a funding-flip exit fires (same N=4 rule as V1) or the current
rate falls below `exit_funding_rate_threshold` (hysteresis
exit). Stay flat otherwise.

**Threshold calibration rule (pre-specified, probe-executed
before first trial).** `min_funding_rate_entry` = 33rd
percentile of all positive-funding sessions in the dev window
(annualised rate, where annualised = rate_per_8h x 1095).
`exit_funding_rate_threshold` = 50% of `min_funding_rate_entry`.
Calibration probe runs on dev window funding history only;
holdout data is never accessed during calibration.

**All other parameters unchanged from V1:** leverage 5.0x
cross-margin, equal notional legs, flip_exit_n=4,
exit_mr_ratio_threshold=0.01.

**Source citations.**
- Schmeling, Schrimpf & Todorov "Crypto Carry" (BIS WP 1087 /
  forthcoming Management Science) -- regime-conditional
  performance observation (Sharpe 6.45 full-sample, 4.06 from
  2024, negative in 2025) is the direct empirical grounding for
  the threshold filter. The threshold operationalises "only
  harvest carry when carry is present."
- V1 per-block forensics (trials.log trial_id f2c343c3): Block
  1 Sharpe -2.17 in negative-funding regime, Block 3 Sharpe
  +14.72 in positive-funding regime -- within-dev evidence
  confirming the regime-conditional hypothesis.

**Harness changes required.** None to sacred-harness files.
`FundingRateHarvestStrategy` gains two optional parameters:
`min_funding_rate_entry: float = 0.0` (default preserves V1
behaviour exactly) and `exit_funding_rate_threshold: float =
0.0`. Calibration probe is
`scripts/phase_4b_v2_threshold_probe.py`. The full_cpcv script
reads the probe output file to set the parameters.

**Variation slot.** `count_distinct_variations
("FundingRateHarvest")` will increment from 1 to 2 when the
first trial row is appended.

## Variation #2 — `phase4b-delta-neutral-top-N-funding-v1` (STUB, NOT QUEUED)

Multi-pair selection layer: rank OKX USDT-M perps by current
funding rate, pick top-N by some criterion (top-1 by raw rate,
top-N by liquidity-adjusted rate, threshold-based, etc.),
refresh at funding-cadence boundaries. Equal-notional two-leg
position on each selected pair.

**Status: STUB, not queued.** Variation #1 must run first per
pre-trial gate #8. This variation gets its own hypothesis-of-
record fill before queue, including:
- Universe specification (which pairs are eligible; refresh
  cadence; lookahead-bias avoidance)
- Selection rule (top-N, threshold, liquidity-adjusted)
- Citation: <author / paper / year / venue> — must name the
  primary peer-reviewed source for cross-sectional funding-
  rate selection edge before queue
- Manifest schema extension: Variation #1's single-pair
  manifest entry doesn't support multi-pair basket. New
  manifest schema (e.g., `basket: [...]` field) is sacred-
  harness change and requires explicit chat approval.
- CPCV path: Variation #1 uses run_cpcv_perp on a single leg
  pair. Variation #2's basket adds per-pair leg state — the
  block-Sharpe contract preservation needs verification.

## Variation discipline

Per `CLAUDE.md`:

- 20-variation cap; variation #1 above is the first slot.
- 3-consecutive-failure escalation.
- Variations beyond #1 require their own source-cited justification
  appended above before trial. Generic parameter sweeps (tweak
  threshold by 0.01, change N from 1 to 2, etc.) do NOT clear the
  no-p-hacking gate.
- Pre-justified batch authority covers variation #1 starting
  hypothesis only — and even that authority is gated on the primary
  citation slot above being filled before the first
  `record_trial(trial_type="full_cpcv")` call.

## Note on the strategy's provenance

Funding-rate harvest is the one Phase-4 candidate where the
peer-reviewed support is generally considered strongest among
retail-accessible crypto strategies (see synthesis ranking #1 in
`docs/research_log.md`). That makes the no-p-hacking discipline more
load-bearing here, not less: a strategy with a clearer literature
also has a more demanding evidence bar — the variations should
correspond to specific paper-derived design choices, not exploratory
parameter sweeps.

If variation #1 fails its dev_cpcv verdict, the structural-failure-
mode question (delta-neutral execution costs vs funding pool APY,
borrow rate on spot leg, slippage at 8h settlement boundaries)
becomes the dominant prior. Variation #2 must be a *structural*
redesign (different leg construction, different instrument family,
different rebalancing rule sourced from a specific paper), not a
parameter perturbation of variation #1.

If variation #1 *passes* dev_cpcv with a margin clearly outside the
borderline band (±0.05 of the multiple-testing threshold), the
hypothesis-of-record is satisfied and the strategy proceeds to
holdout / final-gate per the verdict tree. The 20-variation cap
becomes irrelevant in that path.

If variation #1 *passes* dev_cpcv but *fails* final_gate on the
holdout window (the actual 2026-05-02 outcome — dev sharpe +5.17,
dsr_validation 0.99999; holdout sharpe +0.35, dsr_holdout 0.0054),
the dev↔holdout sharpe gap is the structural-failure-mode signal
— the strategy's mechanism worked (funding settlements processed
cleanly, funding_flip exits fired as designed) but its economics
did not clear the multiple-testing null on out-of-sample data.
This is the same V2 standard as the V1-fails-dev-cpcv branch: V2
must be a structural redesign (different leg construction,
different instrument family, different rebalancing rule sourced
from a specific paper), not a parameter perturbation of V1. The
dev↔holdout gap itself is V2's design constraint — V2's
hypothesis must articulate why its construction would have
replayed on the holdout window where V1's did not.

## Cross-document references

- `docs/research_log.md` § "AI/algo trading viability and strategy-
  archetype evidence (consolidated 2026-04-29)" — synthesis ranking
  and the citation re-verification note.
- `docs/research_log.md` § "Thai SEC venue / derivatives status
  (logged 2026-04-29)" — venue, tax substrate, post-tax APY math.
- `research/funding-rate-risk-model.md` — risk model design
  (per-leg margin, liquidation, funding payment math, exit triggers,
  delta-neutral sanity) for the gated-G1 perp simulator.
- `data/okx_perp.py` — OHLCV ingestion (Phase 4.B Track A).
- `data/okx_funding.py` — funding-rate ingestion + cadence detector
  (Phase 4.B Track B).
