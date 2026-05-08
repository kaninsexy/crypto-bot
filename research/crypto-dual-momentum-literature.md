# CryptoDualMomentum -- literature stub

Strategy id: `CryptoDualMomentum`
Substrate: 10-symbol crypto basket at 1D
(BTC, ETH, SOL, BNB, XRP, ADA, AVAX, DOT, LINK, LTC)
Trial queue id: sq-032

## Hypothesis-of-record

A dual-momentum strategy that conditions cross-sectional rotation
on a market-wide time-series momentum filter delivers positive
risk-adjusted returns versus BTC buy-and-hold over the dev window.

The TSMOM filter is the "absolute momentum" leg: BTC/USDT close >
200-day SMA must hold for the strategy to take any position.  When
the filter is OFF, the strategy liquidates all held positions and
stays flat, avoiding the cross-sectional rotation's vulnerability
to bear-market drawdowns where the "best of a falling basket" is
still falling.

When the TSMOM filter is ON, the strategy ranks the 10-symbol
universe by prior 180-day (6-month) return and longs the top
quintile (top_n=2 of 10) equal-weight, holding for 30 days
before re-ranking.  Long-only -- no shorts on losers, because
Han et al. (2024) document that crypto losers tend to rebound
and inflict losses on shorts.

## Sources

- Han, C.; Kang, B.; Ryu, J. (2024).  "Time-Series and
  Cross-Sectional Momentum in the Cryptocurrency Market: A
  Comprehensive Analysis under Realistic Assumptions." SSRN.
  Key finding: after accounting for realistic costs and
  liquidation risk, time-series momentum remains strong and
  profitable, especially for past winners; cross-sectional
  momentum on its own is found to be weak.  Long-only winners
  dominate long-short configurations.
- Borgards, O. (2021).  "Dynamic time series momentum of
  cryptocurrencies." North American Journal of Economics and
  Finance.  Key finding: a dynamic TSMOM trading strategy on
  cryptocurrencies significantly outperforms a buy-and-hold
  strategy, yielding higher risk-adjusted returns and lower
  downside risk.
- Huang, Z.-C.; Sangiorgi, I.; Urquhart, A. (2024).
  "Cryptocurrency Volume-Weighted Time Series Momentum." SSRN.
  Key finding: a volume-weighted TSMOM strategy generates
  significant returns, with a winner-minus-loser portfolio
  achieving 0.94% per day and an annualised Sharpe ratio of
  2.17.

## Pre-trial gates (locked)

1. Long-only basket; equal weight (1 / top_n) across the held
   top-N winners.
2. Top-N rotation (default N=2 of 10 = top quintile per Han
   et al. 2024).
3. 200-day SMA TSMOM filter on BTC/USDT (the market index).
   Filter OFF -> liquidate held + stay flat; filter ON ->
   rotate.
4. 180-day prior-return lookback for the cross-sectional
   ranking (6-month formation per Borgards 2021).
5. 30-day holding period (monthly rebalance) before re-ranking.
6. No short positions in losers; flat exposure outside the
   winner set or whenever the TSMOM filter is OFF.
7. Baseline = BTC/USDT B&H over the same dev window (the
   absolute counterfactual for "did the dual-momentum rotation
   beat just holding the market index?").
8. Single-pair filter symbol fixed at BTC/USDT; the filter
   symbol is also part of the tradeable basket -- when BTC is
   ranked top-N it is held alongside the other winners.

## Variation table

| # | id | params | trial_type | sharpe | dsr | verdict | notes |
|---|----|--------|------------|--------|-----|---------|-------|
| 1 | tsmom-filter-cs-rotation | tsmom_lookback=200, cs_lookback=180, top_n=2, holding_period=30 | full_cpcv | TBD | TBD | TBD | initial dev_cpcv trial |

## Trial outcomes

<!-- Trial outcomes will be appended here by the orchestrator -->

| variation_id | date | verdict | sr_observed | dsr | n_trades |
|---|---|---|---|---|---|
| tsmom-filter-cs-rotation | 2026-05-08 | under_tested | 1.9006 | 1.0000 | 15 |

