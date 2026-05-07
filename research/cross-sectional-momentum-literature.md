# CrossSectionalMomentum -- literature stub

Strategy id: `CrossSectionalMomentum`
Substrate: 10-symbol crypto basket at 1D
(BTC, ETH, SOL, BNB, XRP, ADA, AVAX, DOT, LINK, LTC)
Trial queue id: sq-020

## Hypothesis-of-record

A long-only cross-sectional portfolio of cryptocurrencies that
performed best over the prior 30-day formation window (the "winner"
tail) generates positive risk-adjusted returns versus BTC
buy-and-hold across the subsequent 7-day holding period.  No short
positions are taken in losers because Han et al. (2024) document
that crypto losers tend to rebound and inflict significant losses
on shorts, making the long-only winner portfolio the dominant
configuration.

## Sources

- Drogen, L.; Hoffstein, C.; Otte, K. (2023). "Cross-sectional
  Momentum in Cryptocurrency Markets." SSRN. Key finding: a
  long-only strategy buying the top quintile of crypto assets
  ranked on 30-day prior returns and holding for 7 days
  consistently delivered excess returns relative to Bitcoin from
  2018-2022.
- Han, C.; Kang, B.; Ryu, J. (2024). "Time-Series and
  Cross-Sectional Momentum in the Cryptocurrency Market: A
  Comprehensive Analysis under Realistic Assumptions." SSRN. Key
  finding: the momentum effect in cryptocurrencies is concentrated
  among winners; losers often rebound and inflict significant
  losses on shorts, so a long-only winner portfolio dominates a
  traditional long-short portfolio.
- Borgards, O. (2021). "Dynamic time series momentum of
  cryptocurrencies." North American Journal of Economics and
  Finance. Key finding: a dynamic time-series momentum strategy
  significantly outperforms buy-and-hold for cryptocurrencies,
  yielding higher risk-adjusted returns and lower downside risk.

## Pre-trial gates (locked)

1. Long-only basket; equal weight across the held top-N winners.
2. Top-N rotation (default N=2 of 10 = top quintile).
3. 30-day prior-return lookback for the cross-sectional ranking.
4. 7-day holding period before re-ranking (weekly rebalance).
5. No short positions in losers; flat exposure outside the winners.
6. Baseline = BTC/USDT B&H over the same dev window.

## Variation table

| # | id | params | trial_type | sharpe | dsr | verdict | notes |
|---|----|--------|------------|--------|-----|---------|-------|
| 1 | cs-momentum-long-winners | lookback=30, top_n=2, hold=7 | full_cpcv | TBD | TBD | TBD | initial dev_cpcv trial |

## Trial outcomes

<!-- Trial outcomes will be appended here by the orchestrator -->

| variation_id | date | verdict | sr_observed | dsr | n_trades |
|---|---|---|---|---|---|
| cs-momentum-long-winners | 2026-05-07 | retire | 1.6423 | 1.0000 | 73 |

