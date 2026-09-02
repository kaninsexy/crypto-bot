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

| date | family | signal | universe rule | horizon | statistic | value | t-stat | N | MDE | data range used | script + git commit | conclusion |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-09-02 | listing_flow | listing event, abnormal return vs equal-weight listed-cohort benchmark | all UM symbols with 1d klines in window | pre-specified [+0, +20] days | mean CAR over the pre-specified window, % | -7.0475 % | -2.42 | 166 | 8.7330 % | 2020-01-01 → 2022-11-02 | scripts/discovery_listing_flow.py @ ed87e06 | killed — \|CAR\| 7.048 % / t=-2.42 misses the 3.0 % & \|t\|>3.0 bar |
| 2026-09-02 | listing_flow | listing event, abnormal return vs equal-weight listed-cohort benchmark | all UM symbols with 1d klines in window | pre-specified [+0, +20] days | mean CAR over the pre-specified window, % | -7.0475 % | -2.42 | 166 | 8.7330 % | 2020-01-01 → 2022-11-02 | scripts/discovery_listing_flow.py @ ed87e06 | DUPLICATE of the row above — same run re-executed, see the note below. Not a second screen. |

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

### Retrospective power — 2026-09-02, and it changes the classification

The pre-flight power gate (`.claude/rules/backtest.md`, discovery split item 5)
was written after this screen ran. Applied retrospectively:

| quantity | value |
|---|---|
| sample sd of event CAR | 37.51 % |
| N (listing events) | 166 |
| **MDE at \|t\| > 3** | **8.73 %** |
| pre-registered effect threshold | 3.0 % |
| N required for MDE ≤ threshold | 1407 events |

(sd recovered exactly from the reported triple, sd = mean·√N / t. Caveat
stated rather than buried: this is the sd of the EVENT CARs, i.e. conditional,
where the gate asks for the unconditional dispersion of 20-day cumulative
returns. For a 20-day crypto horizon the two are of similar magnitude, so the
conclusion below is not sensitive to the distinction — but the number is a
proxy and is labelled as one.)

**MDE 8.73 % is nearly 3× the 3 % bar, so this screen was badly UNDERPOWERED
for its own pre-registered threshold.** A TRUE 3 % CAR effect would have
returned **t ≈ 1.03** — nowhere near the bar. And the shortfall is not
closeable: the window contains 166 listing events in total, against the 1407
needed. Even screening every listing that exists leaves the test ~8× short.

**So "killed" overstates what was learned here too, and the correct
classification is `untestable on the available discovery data`.** The
pre-registered kill test anticipated "~900 listings"; the archive yields 166
inside the sealed window. That gap, not the substrate, is what the row
measured.

One observation survives the power problem, because it is about SIGN rather
than significance, and is recorded as a lead rather than a result:

**Killed.** |CAR| = 7.05 % clears the 3 % effect-size bar, but |t| = 2.42 does
not clear |t| > 3, so the event effect is not statistically distinguishable
from noise at the pre-registered bar. Note also the SIGN: the CAR is
**negative** — new listings underperform the listed cohort over [+0, +20]
days — which is the opposite of the "attention / index inventory buying"
mechanism the kill test was written to detect. A confirmation design built on
this would be trading a different mechanism than the one pre-registered.


### Dependence correction — 2026-09-02

Decision rule pre-committed at `a410b68`, before these numbers existed.
Method: CR1 cluster-robust SE, `scripts/cluster_robust_check.py`. **Not a new
screen** — same signal, universe, horizon, window and point estimate; only the
standard error changes. **`N_disc` unchanged at 1.**

| variant | G | mean CAR | t ordinary | **t clustered** | design effect | robust MDE |
|---|---|---|---|---|---|---|
| cluster by listing date | 154 | −7.05 % | −2.42 | **−2.43** | 1.00× | 8.71 % |
| cluster by listing month | 32 | −7.05 % | −2.42 | **−1.86** | 1.30× | 11.39 % |
| non-overlapping (one CAR per month) | 32 | −8.16 % | −1.92 | **−1.92** | 1.00× | 12.73 % |

**Clustering by DATE barely moves anything (1.00×), and that is a real finding
rather than a null result.** 166 listings fall on 154 distinct dates: Binance
rarely listed several perps on the same day in this window, so same-day
clustering has almost nothing to group. The dependence that matters here is
not same-day — it is the **overlapping [+0, +20] windows** of listings a few
weeks apart, which share a market path.

The two variants that address overlap both weaken the result: clustering by
month gives **1.30×** and pushes `t` to −1.86, and the strictly
non-overlapping construction (one CAR per calendar month, so no two
observations share a window) gives 32 observations at `t` = −1.92.

*Small-G caveat: both month-level variants have G = 32, only just above the
G = 30 threshold; treat them as indicative.*

**Conclusion unchanged, and strengthened.** The family was already untestable
at MDE 8.73 % against a 3 % bar; under every dependence-corrected variant the
robust MDE is **8.71 %–12.73 %**, still multiples of the bar, and `|t|` never
approaches 3.

The negative SIGN observed in run 2 — new listings *underperforming* the
listed cohort, opposite to the pre-registered "attention buying" mechanism —
survives every variant, but so does its insignificance. It remains an
observation, not a finding.
