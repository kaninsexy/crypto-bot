# PROPOSAL — Discovery / confirmation split (2026-09-02)

**Status: PROPOSED. Nothing in this file has been applied.**

Three sacred documents need edits to implement
`docs/research_revival_2026-09.md` §C.2 items 1–4. Sacred-doc edits
require explicit human pre-authorization per `CLAUDE.md` → "Human only"
→ Pre-authorization exception. This file holds the **exact text** to
insert, marked with its target file and section, so the human can review
the wording before authorising anything. The agent that wrote this file
did not edit `.claude/rules/backtest.md`, `CLAUDE.md`, or
`docs/MASTER_PLAN.md`.

Companion artifacts already landed (all non-sacred):

- `backtest/engine_cs.py` + `backtest/tests/test_engine_cs.py` — the
  long-short perp book engine (§C.3).
- `research/discovery/` — ledger format + three empty ledgers with
  their pre-registered kill tests.
- `scripts/discovery_funding_dispersion.py`,
  `scripts/discovery_deleveraging_reversal.py`,
  `scripts/discovery_listing_flow.py` — the three §C.4 screens.
  **Run with `--selftest` only until the rule below is approved.**
- `backtest/proposed_manifest_entries_binance_um.json` — the three
  manifest rows, staged OUTSIDE `holdout_manifest.json`.
- `backtest/strategy_families.json` — `perp-structural` family (this
  file is explicitly non-sacred and additive, so it is already applied).

---

## Proposal 1 — `.claude/rules/backtest.md`

**Target file:** `.claude/rules/backtest.md`
**Target section:** insert as a new top-level section immediately
BEFORE `## No p-hacking rule` (so the p-hacking rule reads as the
constraint that still binds inside the discovery window).

**Invocation line to authorise the edit:**

```bash
SACRED_OVERRIDE_FILES=".claude/rules/backtest.md" claude
```

**Text to insert, verbatim:**

```markdown
## Discovery / confirmation split

Adopted from `docs/research_revival_2026-09.md` §C.2, itself adapted
from Harvey–Liu (t > 3 for multiply-tested claims) and
Arnott–Harvey–Markowitz (2019) on pre-registration, trial documentation
and OOS awareness. Applies to substrates whose manifest entry declares a
`discovery_end`; every other strategy is unaffected and every existing
rule in this file continues to bind.

**1. Discovery window.** For a substrate with a declared discovery
window (Binance UM: 2020-01-01 → 2022-12-31, sealed by manifest, never
read by any prior trial), exploratory analysis is permitted WITHOUT a
`trials.log` row, under three conditions:

  (a) every screen run is logged in a discovery ledger,
      `research/discovery/<family>.md` (signal, universe rule, horizon,
      statistic, value, t-stat, N, data range, script + commit,
      conclusion);
  (b) the ledger's row count `N_disc` is carried into the confirmation
      trial's pre-registration and applied as an additional
      Bonferroni-style haircut on the confirmation DSR;
  (c) discovery never reads 2023+ data. Screens hard-assert this and
      abort on violation.

A discovery screen writes NO trials.log row; the ledger row is its
record. A confirmation trial writes exactly one full_cpcv row and
carries N_disc from the ledger into its pre-registration, and the
confirmation DSR is additionally haircut by N_disc.

**2. Confirmation window.** Confirmation = 2023-01-01 → 2025-05-01
(dev, counted in `trials.log` exactly as today) and holdout =
2025-05-01 → 2026-08-31, never read until `final_gate`. For the Binance
UM substrate the holdout is genuinely virgin — no prior trial touched
Binance UM data — with the standing disclosure that the agents KNOW the
2025-10-10 cascade happened and that this knowledge is not removable.

**3. Pre-registration content.** Extends the literature-file template.
Before a confirmation trial runs, `research/<strategy>-literature.md`
must state: the mechanism in one paragraph; the counterparty and why
they pay; the expected SR with the discovery number that supports it;
turnover and cost at the OKX perp taker fee; the kill test and its
threshold; and `N_disc`.

**4. Forward stage.** Only designs whose dev SR makes the 12-month
forward test decisive (SR ≥ 2) proceed. Paper deploy on OKX perps;
success = PSR ≥ 0.9 after 12 months; fail-fast if realised SR < 0.5
after 6.

**What this does NOT relax.** The 20-variation cap, the
3-consecutive-failure escalation, the no-p-hacking rule for
confirmation-stage variations, the archive-by-default rule, the
compute-budget circuit breaker, and the human-only push/deploy boundary
all bind unchanged. Discovery screens may not be used to select a
confirmation hypothesis after the fact by re-reading a screen's output
window: the kill test, its threshold, and any pre-specified event
window are frozen in the ledger's header before the screen runs, copied
from the batch table in `docs/MASTER_PLAN.md`. Moving a threshold or a
window after seeing the statistic is p-hacking regardless of which
window the data came from.

**Budget for the first batch.** Family `perp-structural`, at most 5
`full_cpcv` rows across the three families (1 per family + 2 variation
slots), 3-consecutive-failure stop.
```

---

## Proposal 2 — `CLAUDE.md` pointer

**Target file:** `CLAUDE.md`
**Target section:** `## Core principles`, appended as a new bullet
immediately AFTER the existing **Every experiment counts** bullet (it
is the qualification to that bullet and must be read with it).

**Invocation line to authorise the edit:**

```bash
SACRED_OVERRIDE_FILES="CLAUDE.md" claude
```

**Text to insert, verbatim (the two-line pointer):**

```markdown
- **Discovery screens are the one exception, and they are ledgered.**
  On a substrate whose manifest declares a `discovery_end`, exploratory
  screens inside the sealed discovery window write a
  `research/discovery/<family>.md` row instead of a `trials.log` row,
  and their count `N_disc` haircuts the confirmation DSR. See
  `.claude/rules/backtest.md` § "Discovery / confirmation split".
```

---

## Proposal 3 — `docs/MASTER_PLAN.md` Phase 4.F category

**Target file:** `docs/MASTER_PLAN.md`
**Target section:** a new `#### Phase 4.F — Perp-structural batch` under
`### Phase 4`, placed immediately after
`#### Phase 4.E — Microstructure / Order-Flow batch`.

**Authority note:** this is a NEW strategy CATEGORY, which `CLAUDE.md`
holds Human-only regardless of pre-authorization scope for outcome
rows. The paragraph below is a proposal for the human to paste, not an
agent edit.

**Invocation line to authorise the edit:**

```bash
SACRED_OVERRIDE_FILES="docs/MASTER_PLAN.md" claude
```

**Text to insert, verbatim:**

```markdown
#### Phase 4.F — Perp-structural batch (proposed 2026-09-02, awaiting human pre-authorization)

**Full design: `docs/research_revival_2026-09.md` §C (canonical for
this phase). Summary:**

**Scope.** A new strategy family `perp-structural` on a genuinely new
substrate: the Binance USDT-M perpetual public archive (klines,
`fundingRate`, 5-minute `metrics` OI / long-short / taker ratios, and a
listing/delisting universe table including delisted symbols). Three
pre-registered mechanism families, enumerated with their kill tests in
§C.4: FundingDispersionCarry, DeleveragingReversal, ListingFlow. Each
names its counterparty and why that counterparty pays — the
mechanism-first path, replacing the citation-first path that produced
zero holdout passers across ~44 designs.

**Statistical rationale.** Every prior batch was scored against a
`trials.log` count inflated by exploration that had nowhere else to go.
The discovery / confirmation split (§C.2) gives exploration a ledger of
its own and charges it as an explicit `N_disc` haircut on the
confirmation DSR, instead of either hiding it or paying for it twice.
Budget is deliberately small: at most 5 `full_cpcv` rows across the
three families (1 per family + 2 variation slots), 3-consecutive-failure
stop; the 20-variation cap is irrelevant at this size.

**Data substrate.** Binance UM archive via `data/binance_vision_um.py`,
cached under `backtest/cache/binance_um/`. Research substrate is
Binance; execution venue remains OKX (443 USDT swaps; universe =
Binance-listed ∩ OKX-listed at signal time, disclosed as a filter and
applied ex ante). Cross-venue provenance disclosure per the 2026-06-11
BNB-backfill precedent. Discovery window 2020-01-01 → 2022-12-31;
confirmation dev 2023-01-01 → 2025-05-01; holdout 2025-05-01 →
2026-08-31, genuinely virgin for this substrate, with the standing
disclosure that the agents know the 2025-10-10 cascade happened.

**Engine.** `backtest/engine_cs.py` — a long-short perp book:
per-bar target weights, funding accrued at 8h settlements (archive
timestamps floored to the hour before alignment), per-leg
maintenance-margin / liquidation checks, OKX perp taker fee +
slippage, and forced closure on delisting. `engine_multi.py` is NOT
modified — its long-only contract underlies 21 recorded trials.

**Batch-specific gate (locked).** Every trial runs at standard taker
fees + slippage AND at 2× fees; edge must survive both or the verdict
is retire. `FundingDispersionCarry` is registered `"neutral": true`, so
its verdict baseline is PSR vs 0 rather than alpha/IR vs
same-instrument B&H; the other two are directional.

**Discipline.** Existing rules unchanged except where §C.2 explicitly
carves out discovery: 3 enumerated starting hypotheses, ≤5 confirmation
trials, 3-consecutive-failure batch stop, every CONFIRMATION trial
appends to `trials.log`, no grid searches. Discovery screens append to
`research/discovery/<family>.md` and never to `trials.log`.

**Sequencing.** (1) Data layer (`data/binance_vision_um.py`) — DONE
(commit 38e1c32). (2) Engine + discovery scaffolding + proposals —
DONE. (3) Human pre-authorization of the `.claude/rules/backtest.md`
and `CLAUDE.md` edits in `docs/proposed_backtest_rule_discovery_2026-09.md`,
and of the three manifest rows in
`backtest/proposed_manifest_entries_binance_um.json`. (4) Discovery
screens on real data → ledger rows. (5) Confirmation trials, for
surviving families only, through the unchanged harness.

**Exit ramp.** If no family shows a discovery spread large enough to
imply SR ≥ 2 net, that is a clean negative result for retail structural
edge on perps and §A.6 becomes the plan. That outcome is a success
condition of this batch, not a failure of it.
```

---

## Review checklist for the human

1. Does the discovery ledger's `N_disc` haircut belong in `dsr.py`
   (code) or in the literature file (documentation)? This proposal
   puts it in the pre-registration text and leaves `dsr.py` untouched;
   wiring it into the DSR computation is a schema-stable-code change
   and would need its own approval.
2. Is `2023-01-01` the right discovery/confirmation boundary given the
   Binance UM archive starts 2020-01 and `metrics` only starts
   2020-09-01? (`DeleveragingReversal` loses its first ~8 months of OI
   history.)
3. Phase 4.F is a new CATEGORY — Human-only under `CLAUDE.md`
   regardless of any other pre-authorization in the same prompt.
