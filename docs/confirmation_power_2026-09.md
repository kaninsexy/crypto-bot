# Can the confirmation stage even conclude? — I2, 2026-09-02

**Script:** `scripts/confirmation_power.py` · **Raw:**
`docs/confirmation_power_2026-09.json` · No `trials.log` row, no holdout read
(only window boundaries are taken from the manifest).

The pre-flight power gate covers **screens**. It has never been pointed
downstream. This asks the same question of the confirmation and holdout
windows: *what true Sharpe would a Phase 4.F strategy need before those
windows are capable of validating it at all?*

Both formulas are **imported from the harness, not reimplemented** —
`backtest.dsr` for the BLP eq.7 Gumbel null, `backtest.families` for the
family trial count and V[{SR_n}].

## The two bars

A confirmation trial must clear both, and the binding one is the larger.

1. **MinTRL** — the minimum track-record length, inverted: given a window of
   `y` years, the smallest annualised Sharpe that can be validated at 95 % is
   `1.645 / √y`. Below it, the record is too short to be significant no matter
   how good the strategy is.
2. **The family multiple-testing null** — `sr_zero = √V[{SR_n}] × Gumbel(N)`.
   A candidate must beat the *expected maximum of N draws under the null*
   before deflation leaves anything behind.

## Windows and the MinTRL floor

| strategy | tf | dev years | dev bars | **dev MinTRL SR** | holdout years | holdout bars | **holdout MinTRL SR** |
|---|---|---|---|---|---|---|---|
| FundingDispersionCarry | 1d | 2.33 | 850 | **1.08** | 1.33 | 486 | **1.42** |
| DeleveragingReversal | 1h | 2.33 | 20,410 | **1.08** | 1.33 | 11,680 | **1.42** |
| ListingFlow | 1d | 2.33 | 850 | **1.08** | 1.33 | 486 | **1.42** |

The floor depends only on the window's LENGTH IN TIME, not on bar count — which
is why the 1h strategy, with 24× the bars, faces exactly the same bar as the
daily ones. More bars of the same calendar period buy resolution, not
significance. This is a standard result and it is worth stating because the
intuition runs the other way.

## The family null

`perp-structural` currently has **0 finite-Sharpe trials**, so V[{SR_n}] takes
the conservative **1.0 fallback**.

| N trials | sr_zero | min SR clearing BOTH bars (dev) |
|---|---|---|
| 1 | 0.000 | 1.08 |
| 2 | 0.520 | 1.08 |
| 3 | 0.853 | 1.08 |
| 4 | 1.052 | 1.08 |
| 5 | 1.193 | **1.19** |

MinTRL binds up to N = 4; past that the multiple-testing null takes over. The
first batch budget is 5 `full_cpcv` rows, so a batch run to its limit ends with
the null, not the window, as the binding constraint.

*Caveat, stated because it cuts the other way:* V[SR] = 1.0 is a fallback. Once
real trials exist the measured variance would likely be below 1.0, lowering
`sr_zero`. The MinTRL floor of 1.08 / 1.42 is unaffected — it depends only on
calendar time and is the harder number.

---

## Verdict

**The confirmation stage cannot validate anything below an annualised Sharpe
of ≈ 1.1 on dev, and the holdout cannot confirm anything below ≈ 1.4.**

Say that plainly, because it reframes the phase. A market-neutral perp design
— funding carry, cross-sectional reversal, event flow — is a **0.5 to 1.0
Sharpe** proposition when it works, net of costs, at daily-to-hourly rebalance.
Published cross-sectional crypto carry results sit in that band, and that is
before the OKX taker fee and the 2× stress this project applies.

So the structure is this: **the confirmation stage was built to validate
strategies roughly twice as good as the ones the discovery stage was looking
for.** A design could be genuinely profitable at SR 0.8, survive every
discovery bar, and still be unable to return `keep` — not because it failed,
but because 2.33 years cannot resolve 0.8 from 0 at 95 % confidence.

This is not a bug in the harness. MinTRL is correct, the Gumbel null is
correct, and both are doing exactly what they are for. It is a **statement
about the substrate**: the Binance UM archive's usable confirmation window is
too short for the effect sizes this class of strategy produces.

### It compounds with the run-2 finding, in the same direction

Run 2 established that the discovery window cannot resolve effects at the 1.5–3 %
scale. This establishes that the confirmation window cannot validate Sharpes
below ~1.1. **Both ends of the pipeline are calibrated for effects larger than
the ones being hunted.** A strategy would have to be simultaneously large
enough to clear discovery and exceptional enough to clear confirmation — and
the band where both hold is narrow enough that it is fair to ask whether it
contains anything real.

### And §C.5 sets the bar higher still

The forward-test plan requires **dev SR ≥ 2** to make a 12-month paper test
decisive. Against a 1.08 MinTRL floor and a 1.19 null at N = 5, the effective
requirement to reach paper deploy is not 1.1 — it is 2.0. That is
top-decile-hedge-fund territory sustained out-of-sample, from a two-year
backtest on a substrate whose discovery half already came back untestable.

### What follows

Three options, and this document deliberately does not choose between them —
it is a measurement, and the choice is BK-0015:

1. **Accept it.** Run only designs with a credible prior of SR > 1.5. That is
   a small set, and honest about what the harness can certify.
2. **Lengthen the window.** The confirmation window is short because the
   substrate starts in 2020 and the holdout was drawn at 2025-05-01. Waiting
   extends both — 2.33 years becomes 3.33 in a year, dropping the floor from
   1.08 to 0.90. Cheap, and the only lever that needs no methodological change.
3. **Change what `keep` means for this family.** Score on drawdown/Calmar as
   the §A.6 overlay is scored, rather than on a Sharpe the window cannot
   resolve. That is a real methodology change and would need its own
   pre-registration, not a footnote.

**This belongs in MASTER_PLAN, not in a footnote** — it constrains every
future strategy on this substrate, not just Phase 4.F.
