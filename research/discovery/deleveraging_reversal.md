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

## Ledger

| date | family | signal | universe rule | horizon | statistic | value | t-stat | N | data range used | script + git commit | conclusion |
|---|---|---|---|---|---|---|---|---|---|---|---|
