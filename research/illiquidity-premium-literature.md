# IlliquidityPremium — literature stub

Strategy id: `IlliquidityPremium`
Substrate: 10-symbol crypto basket at 1D
(BTC, ETH, SOL, BNB, XRP, ADA, AVAX, DOT, LINK, LTC)
Trial queue id: sq-014

## Hypothesis-of-record

Cryptocurrencies with higher Amihud illiquidity (mean
|daily_return| / daily_volume over a 30-day lookback) generate
positive excess returns vs BTC buy-and-hold on a long-only top-3
basket.

## Sources

- Youssef, M. & El Wajdi, F. (2023). "Illiquidity premium in
  cryptocurrency markets." Research in International Business and
  Finance.
- Dan, X., Wang, P., & Zhou, Y. (2020). "Liquidity and asset prices in
  the cryptocurrency market." Finance Research Letters.

## Pre-trial gates (locked)

1. Long-only basket; equal weight across the held top-N.
2. Top-N rotation (default N=3 of 10).
3. 30-bar Amihud illiquidity window.
4. Baseline = BTC/USDT B&H over the same dev window.

## Variation table

| # | id | params | trial_type | sharpe | dsr | verdict | notes |
|---|----|--------|------------|--------|-----|---------|-------|
| 1 | cs-illiquidity-premium-v1 | window=30, top_n=3 | full_cpcv | None | None | retire | CPCVError; n_trades=0 |

## Trial outcomes

| variation_id | date | verdict | sr_observed | dsr | n_trades |
|---|---|---|---|---|---|
| cs-illiquidity-premium-v1 | 2026-05-06 | retire | None | None | 0 |

