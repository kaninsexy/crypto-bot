# CrossSectionalReversal -- literature stub

Strategy id: `CrossSectionalReversal`
Substrate: 10-symbol crypto basket at 1D
(BTC, ETH, SOL, BNB, XRP, ADA, AVAX, DOT, LINK, LTC)
Trial queue id: sq-013

## Hypothesis-of-record

A long-only cross-sectional portfolio of cryptocurrencies that
performed worst over the prior 1-day lookback (the "loser" tail)
generates positive risk-adjusted returns vs BTC buy-and-hold over
the subsequent daily rebalance period.

## Sources

- Zaremba, A.; Bilgin, M. H.; Long, H.; Mercik, A.; Szczygielski, J. J.
  (2021). "Up or down? Short-term reversal, momentum, and liquidity
  effects in cryptocurrency markets." International Review of
  Financial Analysis. Key finding: cryptocurrencies with low returns
  on the previous day significantly outperform those with high
  returns, especially among less-liquid coins.
- Nakagawa, K.; Sakemoto, R. (2024). "Cross-sectional reversal
  portfolios in the cryptocurrency market and market uncertainty."
  SSRN. Key finding: cross-sectional reversal portfolios generate
  higher returns than conventional momentum and reversal strategies.
- Han, C.; Kang, B.; Ryu, J. (2023). "Time-Series and Cross-Sectional
  Momentum in the Cryptocurrency Market: A Comprehensive Analysis
  under Realistic Assumptions." SSRN. Key finding: loser
  cryptocurrencies often rebound and inflict significant losses on
  short positions, implying a reversal effect for the loser
  portfolio.

## Pre-trial gates (locked)

1. Long-only basket; equal weight across the held bottom-N losers.
2. Bottom-N rotation (default N=2 of 10 = bottom quintile).
3. 1-day prior-return lookback for the cross-sectional ranking.
4. Daily rebalance frequency.
5. Baseline = BTC/USDT B&H over the same dev window.

## Variation table

| # | id | params | trial_type | sharpe | dsr | verdict | notes |
|---|----|--------|------------|--------|-----|---------|-------|
| 1 | cs-short-term-reversal-loser-portfolio | lookback=1, top_n=2 | full_cpcv | TBD | TBD | TBD | initial dev_cpcv trial |

## Trial outcomes

<!-- Trial outcomes will be appended here by the orchestrator -->

| variation_id | date | verdict | sr_observed | dsr | n_trades |
|---|---|---|---|---|---|
| cs-short-term-reversal-loser-portfolio | 2026-05-07 | retire | -0.1077 | 0.0006 | 1432 |

