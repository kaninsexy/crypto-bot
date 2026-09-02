# Passive + rules-based overlay — pre-registration

citation_key: faber2007-tactical-allocation

**Written and committed BEFORE the overlay was run.** Everything below —
rule, parameters, window, scoring metrics, and what would count as failure —
is frozen at this commit. Nothing here may be edited after seeing a number;
a change is a new pre-registration with its own document.

**Status:** the §A.6 FALLBACK deliverable, implemented because the §C.4
perp-structural batch produced 0 of 3 testable families
(`research/discovery/*.md`). Per `docs/research_revival_2026-09.md` §A.6:
*"Not a §4 candidate; it is the fallback that lets the bot 'run properly'
with a documented risk profile if nothing else clears."*

---

## THIS IS NOT AN ALPHA CLAIM

Stated first because everything else depends on it.

The overlay makes **no claim of edge**. Its counterparty is nobody — there is
no one on the other side paying for this, because it is not extracting
anything. It is **risk management**: a rule that reduces exposure to a
regime it cannot predict but can recognise late.

Consequences, all deliberate:

- **It writes NO `backtest/trials.log` row.** A trials.log row is a claim
  submitted to the multiple-testing correction. This makes no claim, so
  counting it would inflate `N` for every strategy that does — making the
  DSR haircut harsher for real candidates on the strength of something that
  never competed. `.claude/rules/backtest.md` says every experiment counts;
  this is not an experiment, it is a configuration.
- **It is scored OUT of the verdict tree**, on drawdown and Calmar, never on
  the gate-v2 IR test.
- **It would FAIL gate v2, by construction, and that is not a defect.**
  Gate v2 asks for IR ≥ 0.5 versus buy-and-hold. A rule that is flat during
  part of a bull market must underperform buy-and-hold over a window that is
  mostly bull. Measuring it against that gate would be using the wrong
  instrument and reporting the result as a verdict.

If the report reads as "we found something", the report is wrong.

## The rule — ONE rule, no variants, no search

Per §A.6(vi): *one pre-registered rule, one trial.*

1. **Assets:** BTC/USDT and ETH/USDT spot, equal weight (50/50) at each
   rebalance.
2. **Trend filter:** hold an asset only while its close is **above its own
   200-day simple moving average**. Below, that asset's allocation is flat
   (cash, 0 % return).
3. **Volatility scaling:** scale each held position so its trailing 30-day
   realised volatility maps to a **20 % annualised target**, capped at
   **1.0×** (no leverage, ever).
4. **Rebalance:** monthly, on the first UTC day of each month, using the
   signal as of the prior close.
5. **Costs:** 0.10 % per side on turnover actually traded (OKX spot taker),
   charged at each rebalance on the absolute weight change.

**Parameter provenance — none of these is fitted.** 200-day MA: Faber (2007),
*A Quantitative Approach to Tactical Asset Allocation*, the canonical value
and unchanged since. 20 % annualised vol target with a 30-day lookback:
Barroso & Santa-Clara (2015) on volatility-managed portfolios; 20 % is a
round number near crypto's long-run realised vol, not a chosen optimum.
Monthly rebalance: Faber again, and it minimises turnover. Equal weight:
the absence of a choice.

**No parameter search will be run.** Not a 100-day MA, not a 15 % target,
not weekly rebalancing. If the numbers disappoint, that is the answer. The
entire value of a fallback is that it was not tuned.

## Scoring — pre-specified

**Window:** 2021-05-30 → 2025-05-01.

*Deviation from the prescribed 2020-01-01, disclosed per D5.* The OKX daily
spot cache begins **2020-11-11**, and a 200-day MA needs 200 bars of warmup,
so the first date with a valid signal is 2021-05-30. Nothing is extended to
reach the prescribed start: splicing a Binance perp series onto OKX spot
would change the instrument mid-sample, which is worse than a shorter honest
window. The end date is the dev/holdout boundary and is respected even though
this design is scored outside the verdict tree.

**Consequence, stated up front:** the window contains the 2022 bear and the
2023–25 recovery, but *not* the 2020 crash or the early-2021 run-up. It is
~3.9 years — short for drawdown statistics, which are driven by a handful of
episodes. Any drawdown reduction reported here rests on **one** major
episode (2022). That is a weak basis for extrapolation and the report must
say so.

**Benchmark:** 50/50 BTC/ETH buy-and-hold, same window, same costs at entry.

**Metrics** (all reported for both, no cherry-picking):

| metric | why |
|---|---|
| annualised return | context, not the headline |
| annualised volatility | what the overlay directly targets |
| **max drawdown** | **the headline** |
| **Calmar** (ann. return / max DD) | **the headline**, per §A.6(vi) |
| worst rolling 12-month return | the tail an owner actually feels |
| time in market | the cost of the insurance |
| turnover / total cost paid | what the rule costs to run |

**The headline is drawdown reduction.** Return is reported for completeness
and is expected to be *lower* than buy-and-hold.

## What would make this a failure

Pre-committed so the outcome cannot be narrated afterwards:

- **Max drawdown not materially reduced** (< 20 % relative reduction vs
  buy-and-hold) → the overlay does not do the one job it exists for, and
  should not be deployed at all.
- **Calmar not improved** → the drawdown reduction was bought with an
  equal-or-worse return sacrifice, i.e. it is equivalent to just holding
  less. Then the honest recommendation is to hold less, not to run a rule.
- **Time in market below ~50 %** → it is not a "passive + overlay", it is a
  market-timing strategy in disguise, and it would need to be judged as one.

Any of those is reported as a failure of the fallback, not written around.

## What this cannot tell us

- One window, one major drawdown episode. Not evidence the rule generalises.
- 200-day MA overlays are among the most widely published rules in existence
  (§A.6(ii)), so they are heavily subject to aggregate publication bias even
  though *this* application ran no search.
- Trend overlays fail in sharp V-shaped drawdowns — they exit after the fall
  and re-enter after the recovery, converting a round trip into a realised
  loss. The 2022 bear was slow and trending, which is the case they suit.
  A 2020-03-style single-day crash is the case they do not, and it is
  outside this window.

## References

- Faber, M. (2007). *A Quantitative Approach to Tactical Asset Allocation.*
  Journal of Wealth Management. — the 200-day MA rule and monthly rebalance.
- Moskowitz, T., Ooi, Y. H., & Pedersen, L. (2012). *Time Series Momentum.*
  Journal of Financial Economics 104(2). — trend persistence across asset
  classes.
- Barroso, P., & Santa-Clara, P. (2015). *Momentum Has Its Moments.* Journal
  of Financial Economics 116(1). — volatility scaling of a risky position.
- `docs/research_revival_2026-09.md` §A.6 — the project's own framing, incl.
  the QuantPedia practitioner result (drawdown roughly halved) and the
  explicit instruction not to score an overlay as alpha.
