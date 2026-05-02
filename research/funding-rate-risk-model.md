# Funding-Rate Harvest — risk model design

**Date:** 2026-04-29
**Phase:** 4.B kickoff (Track D)
**Status:** Design doc for the gated perp simulator (Track F).
**Dependencies:** `data/okx_perp.py`, `data/okx_funding.py`,
`research/funding-rate-literature.md`.

This document specifies the risk model the perp simulator must
implement before the first dev_cpcv trial of `FundingRateHarvest`
runs. Implementation is gated on approval gates G1 (engine multi-leg
path) and G2 (perp simulator). Numbers and conventions here should
be considered binding for the gated implementation; deviations
require a doc-update before code lands.

The model assumes:

- Single venue: OKX USDT-M perp + USDT spot.
- One trade unit at a time per `(spot, perp)` pair (no DCA/scale-in).
- Settlement cadence on USDT-M majors: 8 hours, verified empirically
  by `data/okx_funding.py:detect_funding_cadence` against BTC and
  ETH SWAP at Phase 4.B kickoff.
- Initial position is delta-neutral by equal-notional construction.

## Variation scope

Per-leg margin, liquidation calculation, funding-payment math,
and exit triggers in this document apply to BOTH Variation #1
(single-pair) AND Variation #2+ (multi-pair selection) — the
leg-level risk model is variation-agnostic.

Universe-selection layer (which pairs are eligible at any
evaluation, refresh cadence, lookahead-bias avoidance) is
EXPLICITLY OUT OF SCOPE for this document and is deferred to
the Variation #2 hypothesis-of-record entry in research/
funding-rate-literature.md. Variation #1 runs single-pair
(BTC) per pre-trial gate #8.

## 1. Per-leg margin: cross vs isolated

OKX USDT-M offers both **cross margin** (one collateral pool funds
multiple positions; liquidation evaluated per-position but draws on
the shared pool) and **isolated margin** (each position has dedicated
collateral; liquidation affects only that position).

**Recommendation (default for the simulator): cross margin.**

Rationale:

- A delta-neutral cash-and-carry holds long spot + short perp at
  equal notional. The spot leg's mark-to-market gain offsets the
  perp leg's mark-to-market loss when price rises, and vice versa.
  Under cross margin the long-spot equity counts toward the perp
  leg's collateral, raising the price move required to trigger
  liquidation by roughly 2x compared to isolated.
- Funding is paid/received on perp notional regardless of margin
  mode, so the cash-flow side is unchanged. The difference is
  entirely in liquidation distance.
- Failure mode of cross margin: a catastrophic spot venue outage or
  spot-perp basis dislocation can drain the pool faster than
  expected because liquidation sees only the perp side. The Track F
  simulator must surface this asymmetry explicitly in its trade
  logs so verdict-tree forensics can detect it.

**Trade-off the doc surfaces:** isolated margin caps the maximum
loss per position to its dedicated collateral, which is friendlier
for risk auditing but materially shortens the price-move tolerance
on the short-perp leg before liquidation. For variation #1 the
simulator default is cross; isolated mode is reserved as a
parameter for variation #2+ if the dev_cpcv outcome surfaces a
liquidation-driven loss pattern that cross-margin construction
should have absorbed.

## 2. Liquidation calculation (short-perp leg)

OKX USDT-M maintenance margin is tiered by position size; the
simulator uses the lowest tier (≤ several million USDT notional)
for variation #1 since the bot's deployable capital is well within
that tier.

### 2.1 Symbols

| symbol | meaning |
|---|---|
| `S0` | entry mark price |
| `S` | current mark price (variable) |
| `Q` | position size (in coins; equal notional means `Q × S0` matches the spot leg) |
| `L` | leverage (= notional ÷ initial margin posted) |
| `mr` | maintenance-margin ratio (e.g. 0.005 = 0.5% for the lowest BTC/ETH tier) |
| `IM` | initial margin = `Q × S0 ÷ L` |
| `MM` | maintenance margin = `Q × S × mr` (mark-based) |

### 2.2 Liquidation price (short)

Short-perp PnL at price `S` is `Q × (S0 − S)` (positive when price
falls, negative when price rises). Liquidation triggers when
remaining equity ≤ maintenance margin:

```
IM + Q × (S0 − S_liq) = Q × S_liq × mr
S_liq                  = (IM + Q × S0) / (Q × (1 + mr))
                        = S0 × (1 + 1/L) / (1 + mr)
```

For `L = 5x` and `mr = 0.005`, `S_liq ≈ S0 × 1.194` — i.e. a
~19.4% adverse upward move from entry triggers liquidation on the
short-perp leg in isolation.

### 2.3 Cross-margin extension

Under cross margin the long-spot leg's mark-to-market gain
(`Q × (S − S0)` when price rises) adds to the collateral pool,
extending `S_liq` upward. To first order, equal-notional spot+perp
construction *doubles* the price move tolerance — `S_liq ≈ S0 ×
(1 + 2/L) / (1 + mr)`, ~38.8% upward move at 5x.

**Caveat:** the simulator must subtract any spot-leg trading-fee
drag and basis-decoupling effects from the cross-margin offset.
Phase-1 implementation tracks these as separate ledger entries so
forensic logs can attribute liquidation-distance erosion to a
specific cause.

## 3. Funding payment math

### 3.1 Per-settlement cash flow

At each funding settlement timestamp `t`:

```
funding_cash(t) = funding_rate(t) × mark_price(t) × position_size_perp
```

Sign convention:

- `funding_rate > 0`: longs pay shorts. Bot is short the perp, so
  it **receives** `+funding_cash` into the perp-leg cash balance.
- `funding_rate < 0`: shorts pay longs. Bot **pays** `−funding_cash`
  out of the perp-leg cash balance.

`mark_price` and `funding_rate` are taken from
`data/okx_funding.py:load_or_fetch_funding_history` — the DataFrame
columns `[funding_rate, mark_price]` are joined by snap-merge of
the 1h mark-OHLCV `open` at the settlement timestamp. The
simulator must use the *same* mark price for funding accounting
that the live OKX exchange uses; mismatching mark-source between
backtest and live introduces unbacktested slippage.

### 3.2 Realised P&L composition

Total realised P&L over a hold of `K` settlements:

```
total_pnl = spot_pnl(entry → exit)
          + perp_pnl(entry → exit)
          + Σ_{i=1..K} funding_cash(t_i)
          − fees_total
```

For an equal-notional delta-neutral construction:

```
spot_pnl + perp_pnl  ≈  0  (first-order; cancels exactly at the
                              same mark price for both legs at exit
                              when entry and exit are at identical
                              spot ↔ perp basis)
```

so the net edge concentrates in `Σ funding_cash − fees_total`. The
hypothesis-of-record's required-funding threshold is the level at
which expected `Σ funding_cash` exceeds expected fees + financing
costs by enough to clear the verdict-tree multiple-testing bar.

### 3.3 Basis-decoupling tracking error

Spot↔perp basis (perp_mark − spot_mid) is normally bounded by
arbitrage but can dislocate during stress (forced liquidations on
either leg, exchange-specific issues, cross-venue spread blowouts).
The simulator must:

- Compute basis at every 1h tick (using the spot-leg OHLCV from
  `data/okx_perp.py` ingestion plus the spot OHLCV manifest path).
- Flag in the trade log any tick where `|basis| > basis_threshold`
  (parameter; default 0.5% of spot mid for variation #1).
- Compute exit P&L using the *actual* basis at exit, not the
  entry-basis assumption.

Variation #1 uses the same `mark_price` series for both spot-equiv
and perp valuation; this is correct under the equal-notional /
small-basis assumption and surfaces the basis-decoupling case as a
flag rather than as a P&L attribution. Variation #2+ may need a
two-mark accounting if dev_cpcv shows basis-decoupling P&L is
material.

## 4. Exit triggers

Two exit triggers, both parameterised. Both must be implemented in
the gated simulator.

### 4.1 Funding-flip exit

```
if (last N consecutive funding_rate values are < flip_exit_threshold):
    close position
```

Parameters:

- `N` (consecutive settlements): default 3 (24 hours of negative funding
  before exit; tunable per-variation with a citation).
- `flip_exit_threshold`: default 0.0 (exit on any negative funding for
  N consecutive settlements). Variation #2+ may use a stricter
  threshold (e.g. −0.005% per settlement) sourced from a citation.

Rationale: funding rates are persistent at the daily-to-weekly horizon
in normal markets but mean-revert over longer windows. A single
negative settlement is noise; N consecutive is signal. `N = 3` is
the conservative default — a strategy paying out for 24 hours of
negative funding before flipping has surrendered roughly half a day
of positive carry, which is small relative to a typical entry-to-
exit hold of 7–14 days.

### 4.2 Maintenance-margin breach

```
if maintenance_margin_cushion < cushion_threshold:
    close position
```

Where `cushion = (current_equity − maintenance_margin) ÷ maintenance_margin`
expressed as a multiple. Default `cushion_threshold = 0.5` — exit when
remaining cushion drops below 50% of the maintenance bar (i.e. when an
additional adverse move of ~mr × position_notional would liquidate).

Rationale: closing voluntarily before the exchange liquidates avoids
the liquidation fee (OKX charges a clearance fee on liquidations) and
preserves the spot leg for a clean re-entry once funding conditions
re-emerge.

### 4.3 Composite exit semantics

Both triggers are independent and either fires the exit. The
simulator's exit-reason logging must record which trigger fired so
post-trial forensics can attribute exits to funding-flip vs margin-
breach.

## 5. Combined-position sanity at entry

Equal-notional means `position_perp_notional == position_spot_notional`
at entry, so the net delta is zero by construction.

Tracking error within a settlement period is bounded by:

```
|Δ(basis)| × notional   ≤   K_basis × σ_per_8h × notional
```

where `K_basis ≈ 1` for liquid majors in normal markets and grows
during stress. The simulator must compute and log this within-period
tracking error so the verdict-tree forensics can detect periods where
delta-neutrality broke down.

**Sanity check the simulator must run on every trade:** at exit,
`abs(spot_qty × spot_exit_price + perp_qty × perp_exit_price)` must
equal the entry notional within **±5%** (basis + fees). Violations
indicate a leg-construction bug and the trial row should be tagged
with `superseded_by` per the existing trials.log invalidation
policy (`backtest/trials.py` § "superseded_by" field, established at
commit `6f3c0bf`).

**Tolerance calibration history (chat 2026-05-02).** The original
±1% tolerance was set at risk-model authoring time without dev-data
calibration.  The Phase 4.B Variation #1 smoke trial (HEAD
`735055c`) tripped the violation flag on 16/17 closes.  Gate-2
audit (`scripts/phase_4b_gate2_audit.py`) on the dev-window smoke
(BTC-USDT-SWAP, 16 funding_flip exits) found:

  - max basis-at-exit: **2.67%** (single outlier at a 2025
    regime-transition close)
  - p95 basis-at-exit: **1.32%**
  - p75: 0.69%, p50: 0.45%
  - aggregate `funding_cash_share`: **93.22%** (basis_pnl_share 6.78%)

The strategy is funding-cash-dominated; the original ±1% was
miscalibrated for regime-transition closes where basis legitimately
widens above 1% without indicating a leg-construction bug.  Widened
to **±5%** to cover the dev-window p95 + max with headroom while
preserving the gate's purpose: catching order-of-magnitude
leg-construction errors (e.g. 50-50 spot-perp notional swap, sign-
flipped quantity, missing-spot leg).  ±5% is well below the
detection floor for those structural bugs, which would produce
deviations in the 50%+ range.

## 6. Open implementation questions (deferred to gated work)

These are tracked here so the Track D doc is the single source of
truth on risk-model design questions before the gated G1/G2 work
begins.

1. **Borrow rate on the spot leg.** Holding spot ties up USDT that
   could otherwise earn the OKX simple-earn rate (~3–5% APY).
   This is an opportunity cost that reduces net edge. The
   risk-model accounting should subtract this from
   `Σ funding_cash` for honest expected-Sharpe estimation. The
   simple-earn APY is a parameter; default = current OKX simple-
   earn USDT rate, refreshed at trial-run time.

2. **Slippage at 8h settlement boundaries.** Funding settlements
   are public events; spot↔perp basis can spike briefly at
   settlement as participants reposition. Variation #1 assumes
   zero slippage at settlement (mark-price valuation only); if
   dev_cpcv shows abnormally large basis flags around settlement
   timestamps, variation #2 should add a slippage haircut sourced
   from a paper measuring this effect.

3. **Cross-vs-isolated margin parameter test.** Variation #1 uses
   cross margin. A potential variation #2 toggles isolated-only
   to test whether the cross-margin shared-pool offset is what's
   carrying the edge or whether the funding-rate harvest works
   purely on isolated terms. This requires a paper that
   distinguishes the two empirically — citation slot in
   `research/funding-rate-literature.md` § "Variation #2".

4. **Tier-aware maintenance-margin handling.** Variation #1 uses
   the lowest-tier `mr = 0.005`. If the bot's deployable capital
   grows beyond that tier in production, the simulator must
   handle tier transitions; this is a Phase 5 concern, not 4.B.

## Cross-document references

- `research/funding-rate-literature.md` — strategy hypothesis-of-
  record; cites this doc for the risk-model conventions.
- `data/okx_funding.py` — `[funding_rate, mark_price]` source for the
  funding-cash math in §3.
- `data/okx_perp.py` — perp OHLCV source for basis calculation
  in §3.3.
- `backtest/trials.py` — `superseded_by` field used in §5 leg-
  construction sanity guard.
- `docs/MASTER_PLAN.md` § "Phase 4.B — Funding-Rate Harvest" —
  scoping authority and approval-gate map (G1 / G2 / G3) for the
  gated implementation work that consumes this doc.
