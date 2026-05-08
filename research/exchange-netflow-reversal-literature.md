# ExchangeNetflowReversal -- literature stub

Strategy id: `ExchangeNetflowReversal`
Substrate: 10-symbol crypto basket at 1D
(BTC, ETH, SOL, BNB, XRP, ADA, AVAX, DOT, LINK, LTC)
Trial queue id: sq-034

## Hypothesis-of-record

A long-only cross-sectional portfolio of cryptocurrencies that
experience the most extreme negative exchange netflow z-score
(largest net outflows from centralised exchanges, the
"accumulation" tail) generates positive risk-adjusted returns vs
BTC buy-and-hold over the subsequent daily rebalance period.

The published hypothesis is long-short -- short the top quintile
of z-scored netflow (largest inflows = selling pressure) and long
the bottom quintile (largest outflows = accumulation). The Phase
4 backtest harness (`backtest.engine_multi`) is long-only, so the
short leg is dropped and the long leg of the cross-sectional
spread is tested in isolation. This mirrors the long-only
adaptation used for `CrossSectionalReversal` (sq-013) and
`CrossSectionalMomentum`.

## Sources

- Fantazzini, D.; Li, S. (2024). "On-Chain Data and Cryptocurrency
  Market Predictability." SSRN. Key finding: exchange net position
  change is a significant predictor for Bitcoin returns; an
  increase in exchange balances (net inflow) negatively impacts
  future returns.
- Kim, T.-m.; Ahn, J.-w. (2023). "Unveiling the Predictive Power
  of On-Chain Data on Cryptocurrency Returns." Finance Research
  Letters. Key finding: a portfolio strategy longing coins with
  the lowest exchange inflows and shorting those with the highest
  inflows yielded significant excess returns, confirming exchange
  flow as a strong contrarian signal.
- Chen, Y.; Li, Z.; Li, L. (2023). "What Factors Drive
  Cryptocurrency Returns? A Comprehensive Analysis Using On-Chain
  and Off-Chain Data." SSRN. Key finding: using LASSO regression,
  exchange net flow was identified as one of the top predictors,
  with negative coefficients indicating that higher inflows
  correlate with lower future returns.

## Pre-trial gates (locked)

1. Long-only basket; equal weight across the held bottom-N
   netflow-z-score names (no shorts; engine_multi is long-only).
2. Bottom-N rotation (default N=2 of 10 = bottom quintile per
   Kim/Ahn 2023 quintile-spread design).
3. 30-bar rolling z-score normalisation of the daily netflow
   series (Fantazzini/Li 2024 monthly-cycle style; Chen et al.
   2023 LASSO uses comparable rolling-window normalisation).
4. Daily rebalance frequency.
5. Baseline = BTC/USDT B&H over the same dev window (BTC is the
   natural counterfactual for a long-only crypto-basket overlay).
6. Netflow data is pre-fetched from a CryptoQuant / Glassnode-
   style on-chain provider and stored in
   `data/onchain/<symbol>_netflow.parquet` with a `netflow_native`
   column. Trial script aborts with `TRIAL_ERROR_TYPE:
   missing_data` if the cache is absent, mirroring sq-001.

## Variation table

| # | id | params | trial_type | sharpe | dsr | verdict | notes |
|---|----|--------|------------|--------|-----|---------|-------|
| 1 | cs-exchange-netflow-reversal | zscore_window=30, top_n=2 | full_cpcv | TBD | TBD | TBD | initial dev_cpcv trial; requires netflow parquet cache |

## Trial outcomes

<!-- Trial outcomes will be appended here by the orchestrator -->

| variation_id | date | verdict | sr_observed | dsr | n_trades |
|---|---|---|---|---|---|
