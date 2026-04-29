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

## Phase 3c context

Funding-rate harvest was not in the Phase 3c portfolio — it requires a
substrate (perpetual futures + funding-rate settlement) that the bot
does not have today. The Phase 4.B scoping (committed `bf4b9ca` and
extended in the venue resolution at commit `241983f`) added the
substrate decision: OKX USDT-M perp + USDT spot, accept Thai PIT on
funding income (Branch 1 of three branches surfaced at scoping).

## Starting hypothesis (variation #1)

**Delta-neutral cash-and-carry: long spot + short perpetual on the
highest-funding-rate USDT-M majors. Position is held while the per-
settlement funding rate is positive and the leg-margin / liquidation
cushion is intact.**

Mechanism: when funding > 0, longs on the perp pay shorts at each
8-hour settlement (OKX USDT-M cadence — verified by
`data/okx_funding.py:detect_funding_cadence`, see Phase 4.B Track B
HALT-AND-CONSULT check). The bot holds the short perp leg; an equal-
notional long spot leg cancels first-order delta exposure. Positive
funding is collected as cash on the perp leg; the spot leg
contributes only a small basis P&L from spot↔perp price drift, which
is bounded by the funding cadence × volatility within a settlement
period.

Pre-tax baseline funding APY ~10.95% per the 2026-04-29 chat synthesis
(`docs/research_log.md` § "AI/algo trading viability"). This is the
*pool* APY before edge selection — picking only the top-funding majors
at any moment increases this. The selection layer is itself a
parameter that variation #1 fixes (top-1, rebalanced at each
settlement) so the multi-testing surface stays bounded.

### Source citations (variation #1)

**Primary peer-reviewed citation — TO BE FILLED BEFORE FIRST TRIAL:**

> *<author / paper / year / venue — must be one of the "multiple
> 2024–2025 funding-rate-harvest papers" referenced in
> `docs/research_log.md` § "AI/algo trading viability". Pre-trial
> verification gate: confirm authors, exact title, journal, year,
> and methodology section so this hypothesis cites a specific
> empirical result, not a synthesis pointer.>*

Supporting:

- `docs/research_log.md` § "AI/algo trading viability" — synthesis
  ranks funding-rate harvest #1 among retail-accessible crypto
  edges; baseline ~10.95% pre-tax APY anchor.
- `docs/research_log.md` § "Thai SEC venue / derivatives status
  (logged 2026-04-29)" — venue / tax substrate.
- OKX exchange documentation — funding mechanics, 8h settlement
  cadence on USDT-M majors (verified empirically via the Track B
  cadence detector against BTC/ETH SWAP).

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

## Variation #1 — full specification

| field | value |
|---|---|
| `strategy_id` | `FundingRateHarvest` (final name TBD; manifest schema is gated G3) |
| variation_id | `phase4b-delta-neutral-top1-v1` |
| Universe | OKX USDT-M perp + USDT spot, top-N majors by liquidity (initial: BTC, ETH; expand once Track A history depth + Track B cadence both verified for additional majors) |
| Selection | At each 8h settlement, rank candidates by trailing 24-hour funding rate; long the spot + short the perp on the top-1 (winner-take-all) |
| Sizing | Equal notional on long-spot + short-perp legs (delta-neutral at entry) |
| Entry trigger | Top-1 funding rate > threshold (parameter; default = expected execution + financing cost band, sourced from the to-be-locked primary citation) |
| Hold rule | Maintained while funding remains positive and maintenance-margin cushion ≥ threshold |
| Exit trigger A | Funding flips to negative for N consecutive settlements (parameter) |
| Exit trigger B | Maintenance-margin cushion breach (parameter) |
| Rebalance | Re-rank at each settlement; rotate to new top-1 only on rank change |
| Timeframe (manifest) | Settlement-aligned (8h native). Manifest entry shape is pending approval gate G3 — out of this prompt's scope |

The threshold and N parameters are NOT free variables here; they will
be set from the to-be-locked primary citation's reported execution-cost
band and post-flip persistence statistics.

## Variations #2+ (stubs)

These slots are RESERVED for later trial entries. Each requires its
own source-cited justification appended below before any trial runs,
per the no-p-hacking rule. **Stubs are not pre-justified hypotheses —
they are placeholders so the variation accounting in
`count_distinct_variations` is auditable.**

### Variation #2 — TBD

> *Citation slot:* <author / paper / year / venue>
> *Hypothesis:* (to be filled — must specify exactly what changes
> from variation #1: selection layer, sizing rule, exit trigger,
> universe expansion, etc.)
> *Why this and not random sweep:* (must answer with a peer-
> reviewed source — generic appeals to "more parameters" or "different
> threshold" do not clear the bar.)

### Variation #3+ — TBD (slots open up to 20-cap minus prior-trial slots)

Same shape as variation #2.

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
