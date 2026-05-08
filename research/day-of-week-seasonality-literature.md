# DayOfWeekSeasonality -- literature stub

Strategy id: `DayOfWeekSeasonality`
Substrate: 10-symbol crypto basket at 1D
(BTC, ETH, SOL, BNB, XRP, ADA, AVAX, DOT, LINK, LTC)
Trial queue id: sq-025

## Hypothesis-of-record

Cryptocurrency returns are not uniform across the days of the week.
A long-only cross-sectional portfolio that, on each trading day,
ranks the universe by each asset's mean return on that same weekday
over a trailing window and holds the top quintile (winners) earns
positive risk-adjusted returns vs BTC buy-and-hold over the
subsequent daily rebalance period.

## Sources

- Long, H.; Zaremba, A.; Demir, E.; Szczygielski, J. J.; Vasenin, M.
  (2020). "Seasonality in the Cross-Section of Cryptocurrency
  Returns." Finance Research Letters. Key finding: a significant
  seasonal pattern exists where average past same-weekday returns
  positively predict future cryptocurrency performance in the
  cross-section.
- Caporale, G. M.; Plastun, A. (2019). "The day of the week effect
  in the cryptocurrency market." Finance Research Letters. Key
  finding: cryptocurrency returns tend to be higher on Mondays, and
  a simple trading strategy exploiting this anomaly generates
  substantial profits in the time-series formulation.
- Shanaev, S.; Ghimire, B. (2022). "A generalised seasonality test
  and applications for cryptocurrency and stock market seasonality."
  Quarterly Review of Economics and Finance. Key finding: robust
  evidence for the day-of-the-week effect across most analyzed
  crypto assets.
- Padysak, M.; Vojtko, R. (2022). "Seasonality, Trend-following, and
  Mean reversion in Bitcoin." SSRN. Key finding: examines daily
  return distributions; primary focus on intraday seasonality but
  acknowledges daily components.

## Pre-trial gates (locked)

1. Long-only basket; equal weight across the held top-N winners.
2. Top-N rotation (default N=2 of 10 = top quintile per Long 2020).
3. Trailing 56-day window for the cross-sectional same-weekday
   ranking (8 observations per weekday).
4. Minimum 4 same-weekday observations per symbol to be eligible
   for ranking on a given bar (rejects symbols still in warmup).
5. Daily rebalance frequency (every bar evaluates the rotation).
6. Baseline = BTC/USDT B&H over the same dev window.

## Variation table

| # | id | params | trial_type | sharpe | dsr | verdict | notes |
|---|----|--------|------------|--------|-----|---------|-------|
| 1 | cross-sectional-weekday-effect | lookback=56, min_obs=4, top_n=2 | full_cpcv | TBD | TBD | TBD | initial dev_cpcv trial |

## Trial outcomes

<!-- Trial outcomes will be appended here by the orchestrator -->
