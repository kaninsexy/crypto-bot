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

**One drawdown episode.** The whole drawdown result rests on the 2022 bear.
A 3.92-year window contains one major decline, so "79 % drawdown reduction" is
a statement about 2022, not an estimate of what the rule does in general. The
sample size for the headline metric is effectively **one**.

**The window excludes the case this rule is worst at.** Trend overlays fail on
sharp V-shaped crashes: they exit after the fall and re-enter after the
recovery, converting a round trip into a realised loss. March 2020 is exactly
that case, and it is outside this window because the OKX cache starts
2020-11-11. The 2022 bear was slow and trending — the case trend rules suit.
So the window is, by accident, favourable to the rule being tested.

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
4. **The V-shaped-crash case.** The first sharp single-week crash after
   deployment is the real test, because this window never contained one.
   Record what the rule did; it is the most informative observation available.

## Provenance

- Pre-registration `cca9b24`, before the script existed.
- Run at the commit recorded in `docs/passive_overlay_2026-09.json`.
- No `trials.log` row; no `load_holdout` call; `paper_mode` unchanged.
- Reproduce: `python scripts/run_passive_overlay.py --json out.json --chart out.svg`
