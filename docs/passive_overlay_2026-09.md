# Passive BTC/ETH + rules-based overlay — §A.6 fallback report

**Date:** 2026-09-02 · **Pre-registration:**
`research/passive-overlay-literature.md` (committed `cca9b24`, **before**
`scripts/run_passive_overlay.py` existed) · **Script:**
`scripts/run_passive_overlay.py` · **Raw output:**
`docs/passive_overlay_2026-09.json`

---

## Read this before the table

**This is not alpha, and it is not presented as a finding.** It is a
configuration: a rule that reduces exposure to a regime it cannot predict but
can recognise late. Its counterparty is nobody, because it extracts nothing
from anybody.

Three consequences, all deliberate:

- **No `backtest/trials.log` row was written**, and none should be. A row is a
  claim entered into the multiple-testing correction; counting a non-claim
  would make the DSR haircut harsher for every real candidate. A test
  (`test_module_never_touches_the_trials_log`) parses the script's AST and
  fails if anyone ever adds one.
- **Scored outside the verdict tree**, on drawdown and Calmar, per §A.6(vi).
- **It would fail gate v2, by construction.** Gate v2 wants IR ≥ 0.5 versus
  buy-and-hold. A rule that sits flat through part of a bull market must
  underperform buy-and-hold over a mostly-bull window. Its annualised return
  here is **less than half** buy-and-hold's. That is the design working, not
  the design failing.

**Why this exists at all:** the Phase 4.F perp-structural batch produced
**0 of 3 testable families**. Not three kills — three families that the
sealed discovery window could not resolve at the effect sizes their kill tests
pre-registered (`research/discovery/*.md`). §A.6 is the floor that lets the
bot run with a documented risk profile when nothing clears.

## Results

Window **2021-05-30 → 2025-05-01** (3.92 years, 1433 daily bars).
50/50 BTC/ETH, OKX spot, 0.10 % per side.

| metric | overlay | buy & hold | |
|---|---:|---:|---|
| Annualised return | +5.19 % | **+11.92 %** | overlay gives up 57 % of the return |
| Annualised volatility | **15.55 %** | 61.98 % | |
| **Max drawdown** | **−15.97 %** | −76.26 % | **79 % relative reduction** |
| **Calmar** | **0.33** | 0.16 | **2.1× better** |
| Worst rolling 12m | **−12.25 %** | −75.93 % | |
| Time in market | 63.8 % | 100 % | the cost of the insurance |
| Total cost paid | 0.63 % | 0.10 % | 47 months of rebalancing |
| Total return | +21.96 % | +55.49 % | |

![overlay vs buy and hold](passive_overlay_2026-09.svg)

### Pre-committed failure checks

Written into the pre-registration **before** the run, so the outcome could not
be narrated afterwards:

| condition | threshold | actual | |
|---|---|---|---|
| drawdown materially reduced | ≥ 20 % relative | 79.1 % | **PASS** |
| Calmar improved | > buy & hold | 0.33 vs 0.16 | **PASS** |
| not market timing in disguise | ≥ 50 % time in market | 63.8 % | **PASS** |

All three pass.

## What this means, stated carefully

The overlay **quartered the drawdown** and roughly **doubled Calmar**, at the
cost of **57 % of the return**. Whether that is a good trade is a preference,
not a result — it depends entirely on whether a −76 % peak-to-trough is
something the owner would actually sit through. Most people discover the
answer to that during the drawdown rather than before it, which is the honest
argument for the overlay.

Two numbers deserve emphasis because they are the ones an owner feels:

- **Worst rolling 12-month return: −12.25 % vs −75.93 %.** Buy-and-hold had a
  twelve-month window that lost three quarters of the account.
- **Annualised volatility 15.55 % vs 61.98 %.** The vol target was 20 %; the
  realised figure is below it because the rule is flat 36 % of the time and
  never levers above 1.0×.

The result is better than the practitioner benchmark §A.6(ii) cites
(QuantPedia: drawdown roughly *halved*; here roughly *quartered*). **Treat
that gap as a warning, not a win** — it is the sort of margin that usually
indicates a favourable window rather than a better rule. See below.

## What this cannot tell us — the limitations that matter

**~~One drawdown episode.~~ CORRECTED 2026-09-02 — see Robustness below.**
This paragraph claimed the result rested on the 2022 bear and that the
effective sample was one episode. Both were wrong. There are **8 distinct
drawdown episodes deeper than 5 %** on this window and **12** on the longer
Binance one, and the deepest is **2023-03-10**, not 2022. The original claim
was an assumption stated as a finding.

**~~The window excludes the case this rule is worst at.~~ TESTED 2026-09-02 —
see Robustness below.** This correctly identified March 2020 as the missing
case. It has now been run on Binance spot, which reaches it: the rule
**survived**, taking −11.6 % against buy-and-hold's −58.0 %. The reason is not
the one this paragraph assumed — the vol target had already cut the position
to ~10 % of notional, so the trend filter was barely the operative part.

**Publication bias applies even though no search was run.** The 200-day MA is
among the most published rules in existence. This run fitted nothing, but the
*choice* of a 200-day MA inherits decades of aggregate selection.

**The return sacrifice is real and is not noise.** 5.19 % annualised over 3.92
years, against a 12 % risk-free-ish alternative in parts of that period, is not
obviously worth running a bot for. The case for the overlay is the drawdown
profile, and only that.

**Not a forward test.** Nothing here has been run out-of-sample. The window
ends at the dev/holdout boundary and the holdout was not touched.

## Paper-mode configuration

The bot can run this today. Values are the frozen pre-registered ones; nothing
here is tunable without a new pre-registration.

```yaml
# passive_overlay — §A.6 fallback. Risk management, not alpha.
strategy: passive_overlay
paper_mode: true                 # live deploy and live capital are Human-only
assets: [BTC/USDT, ETH/USDT]     # OKX spot
weighting: equal                 # 50/50 at each rebalance
trend_filter:
  type: sma
  window: 200                    # Faber (2007)
  rule: hold_only_when_close_above_sma
volatility_scaling:
  lookback_days: 30              # Barroso & Santa-Clara (2015)
  target_annual: 0.20
  max_leverage: 1.0              # never above 1.0
rebalance:
  schedule: monthly              # first UTC day of the month
  signal_as_of: prior_close      # no lookahead
costs:
  taker_per_side: 0.0010         # OKX spot
reporting:
  score_against: buy_and_hold_50_50
  headline_metrics: [max_drawdown, calmar]
  excluded_from: [trials_log, verdict_tree, gate_v2]
```

**Monthly monitoring — what a human should actually look at.** The failure
mode is not a bad month; it is the rule ceasing to do its one job.

1. **Realised max drawdown since inception vs buy-and-hold over the same
   period.** If the gap narrows toward zero, the overlay is not protecting and
   the return sacrifice buys nothing.
2. **Time in market over the trailing 12 months.** Persistently below ~50 %
   means it has drifted into market timing and should be judged as such;
   persistently near 100 % means it is buy-and-hold with extra steps and extra
   fees.
3. **Number of round trips per year.** A rise signals whipsaw — repeated
   exit-then-re-enter — which is how a trend overlay bleeds. More than ~6
   round trips in a year deserves investigation.
4. **The V-shaped-crash case.** Updated 2026-09-02: this window did not
   contain one, but the longer Binance window does (March 2020) and the rule
   handled it — via the vol target, not the trend filter. So watch the
   POSITION SIZE going into a crash, not just the flat/long flag: if realised
   vol has not risen before the drop, the overlay enters it at full weight and
   the March 2020 result will not repeat.

## Provenance

- Pre-registration `cca9b24`, before the script existed.
- Run at the commit recorded in `docs/passive_overlay_2026-09.json`.
- No `trials.log` row; no `load_holdout` call; `paper_mode` unchanged.
- Reproduce: `python scripts/run_passive_overlay.py --json out.json --chart out.svg`

---

## Robustness — I3, 2026-09-02. **The rule survives March 2020.**

The original report above rested on one bear market and started 2021-05-30
because the OKX spot cache begins 2020-11-11. It carried a caveat that the
window "by accident excludes March 2020 — the sharp V-shaped crash trend
overlays handle WORST".

That caveat has now been tested rather than asserted, on the **Binance spot**
archive (same instrument class, different venue; the cross-venue precedent is
the 2026-06-11 BNB backfill), which reaches back to 2017. **Not one parameter
of the rule changed** — same 200-day MA, 20 % vol target, 1.0× cap, monthly
rebalance, 0.10 % per side. Only the price source, and therefore the window.

Reproduce: `python scripts/run_passive_overlay.py --substrate binance`

| metric | **Binance, 2019-12-18 → 2025-05-01 (5.37 y)** | | OKX, 2021-05-30 → 2025-05-01 (3.92 y) | |
|---|---:|---:|---:|---:|
| | overlay | buy & hold | overlay | buy & hold |
| Annualised return | +17.91 % | +67.06 % | +5.19 % | +11.92 % |
| Annualised volatility | 17.29 % | 70.70 % | 15.55 % | 61.98 % |
| **Max drawdown** | **−15.98 %** | −76.25 % | **−15.97 %** | −76.26 % |
| **Calmar** | **1.12** | 0.88 | **0.33** | 0.16 |
| Worst rolling 12m | −12.26 % | −75.93 % | −12.25 % | −75.93 % |
| Time in market | 69.8 % | 100 % | 63.8 % | 100 % |
| **Drawdown reduction** | **79.0 %** | | **79.1 %** | |

All three pre-committed conditions pass on the longer window too.

### It survived — and NOT for the reason you would guess

The overlay was **not** flat through March 2020. It was invested, and it took
the hit — a small one:

| date | BTC weight | ETH weight | total |
|---|---|---|---|
| 2020-02-01 | 0.171 | 0.131 | 0.302 |
| 2020-03-01 | **0.000** | 0.105 | **0.105** |
| 2020-04-01 | 0.000 | 0.000 | 0.000 |

Drawdown through 2020-02-15 → 2020-04-15: **overlay −11.61 %, buy-and-hold
−57.95 %.** Worst single overlay day in March 2020: −4.66 %.

**The vol target did the work, not the 200-day MA.** By February 2020 realised
volatility had already cut the position to ~30 % of notional, and to ~10 % by
1 March. The trend filter had only removed BTC. So the protection in the one
episode this rule was theoretically worst at came from the Barroso &
Santa-Clara component, not the Faber component — which is worth knowing,
because it means the two parts are not interchangeable and dropping the vol
scaling to "simplify" would remove the half that mattered here.

### Two corrections to the original report

**1. "The result rests on ONE episode" was wrong.** On the longer window there
are **12 distinct drawdown episodes deeper than 5 %** (−16.0, −13.1, −12.9,
−12.3, −11.6, −11.1, −11.0, −8.8, −8.1, −6.2, −5.9, −5.0 %), and 8 on the
original window. The headline is not a single-episode artifact.

**2. The deepest drawdown is not in 2022, and not in March 2020 — it is
2023-03-10, on BOTH windows.** That is why the two max-drawdown figures are
almost identical (−15.98 % vs −15.97 %): the binding episode lies in the
overlap, so extending the window backwards did not find anything worse. The
original report attributed the drawdown result to "the 2022 bear"; that was an
assumption, and it was wrong.

### What got worse, and it is the honest headline

**The return sacrifice is far larger on the longer window: +17.91 % vs
+67.06 % — the overlay gives up 73 % of buy-and-hold's return**, against 57 %
on the shorter one. The extra years are the 2020–21 bull run, exactly the
regime a trend-and-vol overlay is worst-positioned for, and the vol target in
particular caps participation precisely when returns are highest.

Read the two windows together: the overlay's drawdown protection is stable
(79 % on both), and its cost is not — it depends heavily on how much bull
market the window contains. **Anyone extrapolating the Calmar of 1.12 should
notice it is flattered by a start date near the December 2019 low.**

### Whipsaw: zero

**0 whipsaws** (flat → long → flat inside 30 days) across 5.37 years, with 6
round trips total. The monthly rebalance suppresses the failure mode that
usually bleeds trend overlays. Total cost paid over the whole period: 0.83 %.

### What still cannot be claimed

- One venue pair, two assets, one rule, one parameterisation.
- 200-day MA overlays remain among the most published rules in existence, so
  aggregate publication bias applies even though this ran no search.
- 2019-12 → 2025-05 contains one full cycle. Two would be better.
- Still **not** an alpha claim, still **no** `trials.log` row, and it would
  still fail gate v2 by construction — more decisively on this window, since
  it gives up 73 % of the return.
