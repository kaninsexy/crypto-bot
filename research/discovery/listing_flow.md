# Discovery ledger — listing / delisting flow

Family 3 of the §C.4 first batch. Format and hard rules:
`research/discovery/README.md`. Screen:
`scripts/discovery_listing_flow.py`.

## Pre-registered kill test

Copied VERBATIM from the §C.4 table row of
`docs/research_revival_2026-09.md` (row 3). Not editable by a screen
result.

- **Mechanism / counterparty:** Price-insensitive flows around Binance
  perp listing (attention, index/market-maker inventory) and delisting
  (forced closure)
- **Discovery kill test (sandbox):** ~900 listings + all delistings
  2020–22: abnormal return −5…+20 days around the event vs matched
  names.
- **Threshold:** |CAR| ≥ 3 % with t > 3 in a pre-specified window
- **Confirmation design if it survives:** Trade the window that
  survived, one rule, universe-wide

## Ledger

| date | family | signal | universe rule | horizon | statistic | value | t-stat | N | data range used | script + git commit | conclusion |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-09-02 | listing_flow | listing event, abnormal return vs equal-weight listed-cohort benchmark | all UM symbols with 1d klines in window | pre-specified [+0, +20] days | mean CAR over the pre-specified window, % | -7.0475 % | -2.42 | 166 | 2020-01-01 → 2022-11-02 | scripts/discovery_listing_flow.py @ ed87e06 | killed — \|CAR\| 7.048 % / t=-2.42 misses the 3.0 % & \|t\|>3.0 bar |
| 2026-09-02 | listing_flow | listing event, abnormal return vs equal-weight listed-cohort benchmark | all UM symbols with 1d klines in window | pre-specified [+0, +20] days | mean CAR over the pre-specified window, % | -7.0475 % | -2.42 | 166 | 2020-01-01 → 2022-11-02 | scripts/discovery_listing_flow.py @ ed87e06 | DUPLICATE of the row above — same run re-executed, see the note below. Not a second screen. |

### Row provenance note — 2026-09-02

**N_disc for this family = 1, not 2.** The two rows above are byte-identical
because the screen was executed twice with `--append-ledger`: the first run's
headline scrolled off behind the 26-line event-profile table, and it was
re-run to read the statistic. Same script, same commit, same universe, same
window, same numbers.

Recorded rather than deleted, per README rule 4 (rows are append-only). It is
disclosed rather than quietly de-duplicated because N_disc is a
multiple-testing count, and the failure mode it guards against is
*under*-counting screens. Over-counting one repeated execution as two distinct
tests would haircut the confirmation DSR for a test that was never run twice —
wrong in the other direction, and equally a misstatement of what was done.

The operational lesson, worth more than the row: capture a screen's full
output on its first run. A re-run is cheap in compute and expensive in ledger
integrity.

### Result

**Killed.** |CAR| = 7.05 % clears the 3 % effect-size bar, but |t| = 2.42 does
not clear |t| > 3, so the event effect is not statistically distinguishable
from noise at the pre-registered bar. Note also the SIGN: the CAR is
**negative** — new listings underperform the listed cohort over [+0, +20]
days — which is the opposite of the "attention / index inventory buying"
mechanism the kill test was written to detect. A confirmation design built on
this would be trading a different mechanism than the one pre-registered.
