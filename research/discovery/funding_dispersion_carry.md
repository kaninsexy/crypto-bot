# Discovery ledger — funding-dispersion carry

Family 1 of the §C.4 first batch. Format and hard rules:
`research/discovery/README.md`. Screen:
`scripts/discovery_funding_dispersion.py`.

## Pre-registered kill test

Copied VERBATIM from the §C.4 table row of
`docs/research_revival_2026-09.md` (row 1). Not editable by a screen
result.

- **Mechanism / counterparty:** Leveraged longs in small/mid-cap perps
  pay funding; desks can't scale into them
- **Discovery kill test (sandbox):** Daily decile sort on trailing 3×8h
  funding, top-150 by dollar volume: is (funding accrued − next-day
  price spread) > 0 net of 0.05 %×2 per rebalance, 2020–22?
- **Threshold:** net ≥ 0.15 %/day on the 10-1 spread
- **Confirmation design if it survives:** Long bottom / short top
  decile, vol-scaled, beta-hedged with BTC perp, 8h or daily rebalance,
  hard stop on any short leg at +15 %

## Ledger

| date | family | signal | universe rule | horizon | statistic | value | t-stat | N | data range used | script + git commit | conclusion |
|---|---|---|---|---|---|---|---|---|---|---|---|
