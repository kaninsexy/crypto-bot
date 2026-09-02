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
| 2026-09-02 | funding_dispersion_carry | trailing 3x8h funding sum, decile 10 minus decile 1 (short top / long bottom) | top-150 by trailing 30d quote volume | next 1 day | mean daily net 10-1 spread, %/day (net of 0.05%x2) | 0.2748 %/day | 2.94 | 1043 | 2020-02-22 → 2022-12-30 | scripts/discovery_funding_dispersion.py @ ed87e06 | ~~survives — net 0.2748 %/day >= 0.15 %/day threshold~~ **SUPERSEDED 2026-09-02 by the row below — the script's `verdict()` ignored the pre-registered t-stat bar** |
| 2026-09-02 | funding_dispersion_carry | trailing 3x8h funding sum, decile 10 minus decile 1 (short top / long bottom) | top-150 by trailing 30d quote volume | next 1 day | mean daily net 10-1 spread, %/day (net of 0.05%x2) | 0.2748 %/day | 2.94 | 1043 | 2020-02-22 → 2022-12-30 | scripts/discovery_funding_dispersion.py @ ed87e06 (verdict fix) | **killed — \|t\|=2.94 <= 3.0.** Same numbers, corrected conclusion: the effect-size bar clears (0.2748 >= 0.15 %/day) but the pre-registered t > 3 bar does not. Supersedes the row above (README rule 4). |

### Correction note — 2026-09-02

**This family does NOT pass the batch's pass rule.** Both rows above report the
same single screen run; only the conclusion changed.

`verdict()` in `scripts/discovery_funding_dispersion.py` checked only the
%/day threshold and never looked at the t-statistic, so a run at t = 2.939 was
reported as "survives". Three independent places show the t > 3 bar was
pre-registered rather than invented afterwards:

- `.claude/rules/backtest.md` § "Discovery / confirmation split" cites
  Harvey–Liu, *t > 3 for multiply-tested claims*, as the basis of the split;
- this screen's own `--selftest` already asserts `t_stat > 3.0` on its
  synthetic positive control;
- the sibling screen `scripts/discovery_listing_flow.py` enforces `|t| > 3.0`
  alongside its effect-size bar — the two family screens in one batch were
  applying different rules.

The fix was made **after** seeing 2.939, which is exactly the circumstance the
no-p-hacking rule polices, so the direction matters and is stated plainly: it
makes the test **stricter**, and it makes a bar that was already written down
actually bind. Moving a threshold to admit a result is p-hacking; repairing a
check that failed to apply a pre-registered threshold is its opposite. Nothing
about the threshold, the window, or the statistic changed.

**N_disc for this family = 1.** The two rows are one screen run, not two.

For the record, 2.939 is close to 3.0, and that is not a reason to pass it —
"close to the bar" is what the bar exists to refuse. It is a reason to note
that a longer window or a wider universe could plausibly settle it, which is a
*new* pre-registered screen (a new row and N_disc = 2), not a re-reading of
this one.
