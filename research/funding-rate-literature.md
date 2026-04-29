# Funding-Rate Harvest — Phase 4.B hypothesis-of-record

**Date:** 2026-04-29
**Phase:** 4.B kickoff — data layer ready, harness extension still gated
**Status:** Starting hypothesis (pre-trial). Variations #2+ are stubs
with named source slots, not filled hypotheses.

## Pre-trial citation gate (READ FIRST)

Per `docs/research_log.md` § "AI/algo trading viability and strategy-
archetype evidence (consolidated 2026-04-29)" closing note: *"The
funding-rate-harvest citations should be re-verified specifically
before Phase 4.B begins — that strategy depends most heavily on a
specific empirical literature, and the exact paper set should be in
`research/funding-rate-literature.md` per the no-p-hacking rule before
the first 4.B trial appends to `trials.log`."*

**This stub establishes the hypothesis structure, the post-tax economic
baseline, and the variation-#1 design. The primary-source verification
(authors / journals / years for the 2024–2025 funding-rate-harvest
papers referenced in the synthesis) is the explicit pre-trial gate
that must clear before any `record_trial(trial_type="full_cpcv")` call
for funding-rate-harvest. Variation #1 cannot run until the citation
slot below is filled with a specific peer-reviewed reference.**

This is the same discipline `research/supertrend-literature.md` and
`research/dualmomentum-literature.md` followed — primary citations
are specific (Barroso & Santa-Clara 2015 JFE; Liu/Tsyvinski/Wu 2022
JF) and named at the time the literature stub is committed, not after
trial results land. Funding-rate-harvest is the one Phase-4 entry
where the synthesis acknowledged the citation slot as not-yet-locked;
locking it is a pre-trial deliverable.

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
   type='full_cpcv'). Variation #1 currently has a citation
   gap (placeholder <author / paper / year / venue>) — must be
   filled before queue. Source: CLAUDE.md no-p-hacking rule.
3. **Gate (post-tax economics)**: Per-strategy expected Sharpe
   / required-funding-rate threshold derives from the post-tax
   baseline (~7.1-8.2% APY after Thai PIT 25-35% per Section
   40(4)(h) classification), not the pre-tax ~10.95% baseline.
   Source: docs/research_log.md § "Thai SEC venue / derivatives
   status (logged 2026-04-29)".

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

Pre-tax baseline funding APY ~10.95% per the 2026-04-29 chat
synthesis (`docs/research_log.md` § "AI/algo trading viability").
The pair-selection layer (single-pair vs multi-pair top-N) is
NOT part of the strategy archetype — see "Pre-trial gates
(locked)" above and the per-Variation entries below for the
locked design choice per variation.

## Post-tax economic baseline

**Pre-tax pool APY:** ~10.95% (synthesis anchor).

**Thai PIT classification:** funding income is realised as a financial
benefit on a non-licensed venue and falls under Section 40(4)(h) of
the Revenue Code (per `docs/research_log.md` § "Thai SEC venue"). The
2025–2029 PIT exemption (Ministerial Regulation 399) does NOT apply
because OKX is not on the Thai-SEC-licensed digital-asset operator
list as of 2026-04-29.

**Post-tax APY:** at marginal Thai PIT 25–35% on Section 40(4)(h)
benefit, pool APY post-tax = ~10.95% × (1 − 0.25..0.35) ≈ **7.1–8.2%
APY**.

**Implication for hypothesis testing:** every variation in this file
must derive its expected Sharpe / required-funding-rate threshold from
the post-tax APY baseline, not the pre-tax number. The 7.1–8.2%
post-tax baseline is the gross-of-execution-and-financing-costs
ceiling; net-of-fees net-of-borrow Sharpe is bounded below this.

## Variation #1 — `phase4b-delta-neutral-singlepair-btc-v1`

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
- <author / paper / year / venue> — primary funding-rate-
  harvest empirical paper, baseline ~10.95% pre-tax annualised
  APY before edge selection. CITATION GAP: must be filled
  before the first trial appends a full_cpcv row to trials.log
  per CLAUDE.md no-p-hacking rule.
- docs/research_log.md § "Thai SEC venue / derivatives status
  (logged 2026-04-29)" — post-tax baseline ~7.1-8.2% after
  Thai PIT 25-35% on Section 40(4)(h) classification.
- docs/MASTER_PLAN.md § "Phase 4.B" — Branch 1 venue resolution.

**Parameters.** [TBD before trial-queue per Variation #1
parameter design; below are placeholder slots, fill before
record_trial(trial_type='full_cpcv'):]
- timeframe: <TBD per timeframe-per-strategy principle; OHLCV
  resolution affects liquidation monitoring + slippage
  modeling, not the funding signal itself which fires at 8h>
- target_vol_annual: <TBD>
- notional_capital_per_leg: <TBD; must be equal across legs
  for delta-neutrality at entry>
- exit_funding_flip_n_settlements: <TBD; consecutive negative
  funding settlements that trigger close>
- exit_margin_breach_threshold: <TBD; cushion above
  maintenance margin that triggers de-risk>

**Expected Sharpe direction.** Positive but materially below
the pre-tax-APY-implied bound. Post-tax baseline ~7.1-8.2%
before perp fees, spot fees, slippage, and long-leg vol drag
(the long-spot leg's price-PnL is hedged by the short-perp
leg, but tracking error from price moves is bounded by funding
cadence × volatility — see research/funding-rate-risk-model.md
§ "Combined-position sanity").

**Verdict-tree precondition.** trade-count floor: continuous
hold means n_trades may be ~1 per dev block (one open + one
close per regime period). The verdict tree's
min_trade_count=30 floor is at risk; the strategy's CPCV
interpretability depends on funding-payment-event count, not
trade count. Surface this as an open question before queuing
CPCV — may require harness adaptation or a different signal
boundary definition.

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
