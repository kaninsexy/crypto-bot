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
