# Discovery ledger — deleveraging reversal

Family 2 of the §C.4 first batch. Format and hard rules:
`research/discovery/README.md`. Screen:
`scripts/discovery_deleveraging_reversal.py`.

## Pre-registered kill test

Copied VERBATIM from the §C.4 table row of
`docs/research_revival_2026-09.md` (row 2). Not editable by a screen
result.

- **Mechanism / counterparty:** Forced liquidations sell at any price;
  counterparty is the liquidated long (or short)
- **Discovery kill test (sandbox):** Event = 24h OI drop ≥ 20 % with
  price move ≥ 2σ; measure 1–5-day forward return vs unconditional,
  2020–22, ≥ 100 events across the universe.
- **Threshold:** mean 3-day reversal ≥ 1.5 % with t > 3
- **Confirmation design if it survives:** Enter against the move at
  event close, exit at 3 days or OI recovery; market-neutral variant
  hedged with BTC

## Universe rule and power — recorded EX ANTE, 2026-09-02

Written and committed **before the screen was run**, so the universe size is
on record as a design input rather than something chosen after seeing a
statistic.

**Universe rule (from the pre-registered kill test above):** "≥ 100 events
across the universe." The rule is stated in EVENTS, not symbols — so the
symbol count is whatever it takes to reach the event floor with power to
resolve the pre-registered effect size, and is not itself a free parameter.

**Data window:** 2021-12-01 → 2022-12-31. Not a choice and not a narrowing of
the pre-registered "2020–22": per `docs/recon_binance_um_2026-09.md` §4, the
5-minute metrics archive carries **BTCUSDT alone from 2020-09**, and the alts
only begin **2021-12**. A CROSS-SECTIONAL open-interest study therefore has
~13 usable months no matter how much is downloaded. Stopping at 2022-12-31
rather than 2023-01-01 is deliberate: three cached symbols hold a row stamped
exactly on the boundary instant, and while the screens' strict `<` assertion
catches it, the ambiguity should not exist in the first place.

**Power.** Unconditional 3-day return σ over the window, computed from the
cached daily klines (212 symbols with ≥ 60 bars): **median σ = 9.69 %**
(p25 8.08 %, p75 11.20 %). This is a design input measured from unconditional
returns — it is NOT the event statistic and says nothing about the result.

Minimum detectable effect at the pre-registered `t > 3` bar,
MDE = 3σ/√N:

| symbols | ~events | MDE at t > 3 |
|---|---|---|
| 30 | ~189 | **2.11 %** |
| 60 | ~378 | 1.50 % |
| 100 | ~630 | **1.16 %** |
| 166 | ~1046 | 0.90 % |

The pre-registered bar is a mean 3-day reversal of **1.5 %**. So:

- at **30 symbols**, a TRUE effect of exactly 1.5 % returns **t ≈ 2.13** and
  would be logged "killed";
- at **100 symbols**, the same true effect returns **t ≈ 3.88** and is
  detectable.

**A 30-symbol screen would therefore manufacture a null.** It cannot resolve
the effect the kill test was written to detect, so a "killed" row from it
would record the sample size, not the substrate. The screen runs at **≥ 100
symbols**.

This is COMPLETING the pre-registered test, not replacing it. The earlier
30-symbol bound was a download-cost decision (the metrics archive is one zip
per day per symbol), never a design choice, and no screen was run under it —
so **N_disc for this family stays 1**. Had a 30-symbol screen been run and
ledgered, this would instead be a second row and N_disc = 2.

## Ledger

| date | family | signal | universe rule | horizon | statistic | value | t-stat | N | MDE | data range used | script + git commit | conclusion |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
