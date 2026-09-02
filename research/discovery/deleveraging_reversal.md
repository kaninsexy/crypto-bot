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

> **The event projection in the table above was wrong, and the direction
> matters.** It assumed ~6.3 events per symbol (30 symbols → ~189 events).
> The realised rate is **~1.2 per symbol**: 220 events from 166 perp symbols,
> not the ~1046 projected. The event definition — a 24 h OI drop ≥ 20 % AND a
> ≥ 2σ price move on the same day — is roughly five times rarer than the
> estimate assumed.
>
> The conclusion the table drew (30 symbols is underpowered) was right; the
> arithmetic behind it was optimistic, and being optimistic about N is the
> failure mode the gate exists to catch. Left uncorrected, this table would
> read as a validated projection method. It is not one: an event-rate estimate
> should be measured on a sample of the actual event definition before it is
> used to size a universe, not inferred from symbol counts. See the Result
> section below for the realised figures, which supersede these.

## Ledger

| date | family | signal | universe rule | horizon | statistic | value | t-stat | N | MDE | data range used | script + git commit | conclusion |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-09-02 | deleveraging_reversal | 24h OI drop <= -20% AND |r| >= 2x trailing 30d sigma | all UM symbols with klines AND metrics in window | +3 days | mean 3-day reversal, % (sign-adjusted against the event move) | -1.7122 % | -1.78 | 220 | 2.6235 % | 2020-01-01 → 2022-12-31 | scripts/discovery_deleveraging_reversal.py @ ba52939 | **REFUSED — UNDERPOWERED, not killed.** MDE 2.6235 % exceeds the pre-registered 1.5 % bar at N=220: a TRUE 1.5 % effect would return t = 1.72, below the 3.0 bar. N required 674; the whole substrate yields 220. The observed -1.7122 % / t=-1.78 is therefore NOT evidence about the effect and must not be read as a kill. |


### Run provenance — 2026-09-02

**N_disc for this family = 1.** The screen was executed three times; the
ledger carries one row, and that is correct.

| # | universe | events | MDE | outcome |
|---|---|---|---|---|
| A | 118 perp symbols with cached metrics | 145 | 3.34 % | refused, no row written |
| B | 182 cached / 166 perp — the whole substrate | 220 | 2.62 % | refused, no row written |
| C | identical to B, script patched to record refusals | 220 | 2.62 % | **the row above** |

A → B is the universe widening the power gate demanded, which
`.claude/rules/backtest.md` item 5 defines as COMPLETING the pre-registered
test, not a new screen. B → C changed no data and no parameter: the first two
runs wrote nothing at all, which was a defect — a later session finding an
empty ledger cannot tell "never screened" from "screened and refused", and
would burn the compute again to rediscover the same refusal. The screen now
records a refusal instead of vanishing.

### Result — REFUSED as untestable, NOT killed

At the full available universe the test cannot resolve the effect it was
written to detect:

| quantity | value |
|---|---|
| events (24h OI drop ≥ 20 % AND \|r\| ≥ 2σ) | 220 |
| unconditional 3-day return σ | 12.97 % |
| **MDE at t > 3** | **2.62 %** |
| pre-registered threshold | 1.5 % |
| N required | 674 events |
| N available in the entire substrate | 220 |

The remedy the rule prescribes — widen until MDE ≤ threshold — is exhausted:

- **Universe:** all 166 perp symbols listed before 2023 now have metrics. The
  event rate is ~1.2 per symbol, so reaching 674 events would need ~590
  symbols. They do not exist in this window.
- **Window:** alt metrics begin 2021-12; earlier is BTC-only, and later is the
  confirmation window, which discovery may not read.
- **Horizon:** pre-registered at 3 days. Changing it after seeing the numbers
  is the p-hacking the split exists to prevent.

So the family closes as **untestable on the available discovery data**, the
same classification as the other two — and for the same underlying reason: the
sealed window is too small for the effect sizes the kill tests pre-registered.

**The observed −1.71 % / t = −1.78 is not a kill and must not be cited as
one.** At MDE 2.62 % that number carries no information about whether a 1.5 %
reversal exists.

### A powered observation, recorded as a LEAD — not a result

The per-horizon profile contains something the headline hides. Re-running the
power check at each horizon:

| horizon | mean | t | MDE | powered? |
|---|---|---|---|---|
| **+1 day** | **−6.28 %** | **−7.78** | 2.42 % | **YES** |
| +2 days | −1.72 % | −2.45 | ~2.8 % | no |
| +3 days (pre-registered) | −1.71 % | −1.78 | 2.89 % | no |
| +5 days | **−5.85 %** | **−5.77** | 3.04 % | **YES** |

The +1 day effect is large, highly significant, and adequately powered — and
its SIGN is the opposite of the hypothesis. `rev_h` is defined so that
positive = price reverses against the event move, so −6.28 % means price
**continues** in the event direction for another day after a 20 % open-interest
collapse plus a 2σ move. Forced-liquidation cascades kept going; they did not
bounce.

This is recorded as a **lead, not a finding**, and nothing in this run acts on
it. It is not the pre-registered statistic, and adopting a horizon because its
number looked better is exactly the substitution the discovery/confirmation
split exists to forbid. Testing it properly means a NEW pre-registered screen —
its own kill test, its own threshold frozen before the run, and **N_disc = 2**
for this family. That is a decision for a human, not a pivot inside a run whose
pre-registered question just came back untestable.

One caveat that must travel with the lead: a −6 % one-day continuation after a
liquidation cascade is the shape of an effect that is real but untradeable. The
entry is at the close of a day that just moved ≥ 2σ on a 20 % OI collapse —
precisely when spreads are widest, depth is thinnest and a taker fill is worst.
Any confirmation design would have to clear that cost bar before the sign
matters, and this substrate's ~0.05 % taker + slippage assumption is calibrated
to normal conditions, not cascade conditions.
