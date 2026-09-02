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

| date | family | signal | universe rule | horizon | statistic | value | t-stat | N | MDE | data range used | script + git commit | conclusion |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-09-02 | funding_dispersion_carry | trailing 3x8h funding sum, decile 10 minus decile 1 (short top / long bottom) | top-150 by trailing 30d quote volume | next 1 day | mean daily net 10-1 spread, %/day (net of 0.05%x2) | 0.2748 %/day | 2.94 | 1043 | 0.2805 %/day | 2020-02-22 → 2022-12-30 | scripts/discovery_funding_dispersion.py @ ed87e06 | ~~survives — net 0.2748 %/day >= 0.15 %/day threshold~~ **SUPERSEDED 2026-09-02 by the row below — the script's `verdict()` ignored the pre-registered t-stat bar** |
| 2026-09-02 | funding_dispersion_carry | trailing 3x8h funding sum, decile 10 minus decile 1 (short top / long bottom) | top-150 by trailing 30d quote volume | next 1 day | mean daily net 10-1 spread, %/day (net of 0.05%x2) | 0.2748 %/day | 2.94 | 1043 | 0.2805 %/day | 2020-02-22 → 2022-12-30 | scripts/discovery_funding_dispersion.py @ ed87e06 (verdict fix) | **killed — \|t\|=2.94 <= 3.0.** Same numbers, corrected conclusion: the effect-size bar clears (0.2748 >= 0.15 %/day) but the pre-registered t > 3 bar does not. Supersedes the row above (README rule 4). |

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
"close to the bar" is what the bar exists to refuse.

**This family is CLOSED within the discovery window.** The paragraph above
originally went on to muse that a wider universe or a longer window "could
plausibly settle it" as a new N_disc = 2 screen. That was wrong and is
retracted, because neither lever exists:

- **Universe** — the screen already ran the pre-registered top-150 rule
  against the ~166 symbols with klines in the window. There is no wider
  universe to widen to; the rule is already at the substrate's ceiling.
- **Window** — the only remaining lever is calendar, and more calendar means
  2023+, which is the CONFIRMATION window. Discovery is forbidden to read it
  (`.claude/rules/backtest.md`: "discovery never reads 2023+ data", hard-
  asserted by the screen itself).

So there is no admissible re-run. Leaving the invitation standing would let a
later session read it as an open door and re-screen the family looking for a
different number, which is the exact behaviour N_disc exists to price. If this
family is ever revisited it is as a confirmation-stage decision on fresh
grounds, not as another discovery row.

### Retrospective power — 2026-09-02, and it changes the classification

The pre-flight power gate (`.claude/rules/backtest.md`, discovery split item 5)
was written after this screen ran. Applied retrospectively:

| quantity | value |
|---|---|
| realised sample sd of the daily net spread | 3.0197 %/day |
| N (rebalance days) | 1043 |
| **MDE at t > 3** | **0.2805 %/day** |
| pre-registered effect threshold | 0.15 %/day |
| N required for MDE ≤ threshold | 3648 days (~10 years) |

(sd is recovered exactly from the reported triple, sd = mean·√N / t. Here the
outcome variable is the daily net spread itself — every rebalance day is an
observation and none is an "event" — so the sample sd *is* the unconditional
dispersion the gate asks for.)

**MDE 0.2805 %/day exceeds the 0.15 %/day bar, so this screen was
UNDERPOWERED for its own pre-registered threshold.** A TRUE effect of exactly
0.15 %/day would have returned **t ≈ 1.60** and been logged as a null. The
screen could only ever have confirmed effects ≥ 0.28 %/day; the observed
0.2748 %/day sits just under its own detection floor, which is why t landed at
2.94 rather than clearing 3.

**So "killed" overstates what was learned, and the correct classification is
`untestable on the available discovery data`.** The distinction matters and is
not pedantic: "killed" says *the effect is not there*, and this run cannot
support that. What it supports is *this window cannot resolve an effect of the
size we pre-registered*.

Under item 5 the run would now be REFUSED rather than concluded, and the
remedy — widen until MDE ≤ threshold — is unavailable here for the reasons
already given above: the universe is at the substrate ceiling (top-150 of ~166)
and reaching N = 3648 rebalance days needs ~10 years, where the sealed
discovery window offers 3. **The family is closed as untestable, not as
disproved.** N_disc remains 1; this note re-reads an existing row, it does not
add one.
