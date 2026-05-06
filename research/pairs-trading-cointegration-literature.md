# PairsTradingCointegration — Literature Review

## Pre-trial gates (locked)

1. Single pair: BTC/USDT vs ETH/USDT only (v1 scope).
2. Timeframe: 1H.
3. Formation window: 200 bars rolling Engle-Granger cointegration test
   using statsmodels.tsa.stattools.coint (ADF-based).
4. Hedge ratio: OLS via np.polyfit on the formation window.
5. Spread z-score window: 21 bars.
6. Entry threshold: |z-score| > 2.0.
7. Exit: z-score reverts to 0 (crosses the mean).
8. Long the undervalued leg, short the overvalued leg simultaneously.
9. trial_type = full_cpcv, n_blocks=10.
10. strategy_factory in trial script MUST create a fresh
    PairsTradingCointegration instance per CPCV block (so state resets).
11. DO NOT run the trial — build infra only.

## Hypothesis

A statistical arbitrage strategy that trades the mean-reversion of a
price spread formed by two cointegrated cryptocurrency perpetual
futures, using a z-score for entry and exit signals, will yield positive
risk-adjusted returns. v1 scopes the universe to the BTC/USDT — ETH/USDT
pair (the largest-cap crypto pair with a multi-year cointegrated
relationship per Park 2026 and Carvalho 2021).

## Substrate

BTC/USDT and ETH/USDT 1H spot candles. Manifest entry
`PairsTradingCointegration`:

- timeframe: 1h
- symbols: ["BTC/USDT", "ETH/USDT"]
- data_start: 2023-04-30T00:00:00+00:00
- data_end: 2026-04-19T00:00:00+00:00
- dev_end: 2025-09-13T00:00:00+00:00
- holdout_start: 2025-09-13T00:00:00+00:00
  (sealed; mirrors MeanReversion_BTC_Residual)
- strategy_warmup_candles: 221 (formation_window=200 + zscore_window=21)
- min_tradeable_candles_per_block: 10

Dev window: 2023-04-30 to 2025-09-13 (~2.4 years of 1H bars, ≈21k bars
per leg). Holdout sealed.

## Literature

1. **Taekyung Park (2026).** "Statistical Arbitrage Strategies Using
   Cointegration Analysis in Cryptocurrency Markets." *International
   Journal of Science and Research Archive.* Strategies based on
   cointegrated pairs (especially BTC-ETH) produced significant
   risk-adjusted returns, with Sharpe ratios ranging from 1.58 to 2.45
   and annual alphas of 11–15%. Specifies the 21-bar z-score window and
   |z|>2.0 entry threshold used here.

2. **Masood Tadi & Jiří Witzany (2023).** "Copula-Based Trading of
   Cointegrated Cryptocurrency Pairs." arXiv. A pairs trading strategy
   using copulas to model the dependency of cointegrated crypto pairs
   outperformed buy-and-hold strategies on both profitability and
   risk-adjusted returns. Confirms 2-sigma entry convention and the
   profitability of cointegration-anchored mean reversion in crypto.

3. **Daniel da Silva Carvalho (2021).** "Pairs trading: cointegration-
   based methods, applied to the cryptocurrency market." Master
   Dissertation, Universidade Católica Portuguesa. The Engle-Granger
   approach using the ADF test was the best predictor of cointegration,
   yielding a positive mean return; longer (6-month) formation periods
   performed better than shorter (3-month) ones — motivates the 200-bar
   (≈8.3 trading-day-equivalent) formation window at 1H.

## Variations

| variation_id                  | trial_id | verdict | sharpe | notes |
|-------------------------------|----------|---------|--------|-------|
| btc-eth-engle-granger-v1      | (pending)| (pending)| (pending) | Phase 4.A v1: BTC/ETH 1H, formation=200, zscore=21, entry \|z\|>2.0, exit z=0. Long undervalued / short overvalued. |

## Open questions

- Does the BTC-ETH cointegration relationship persist out-of-sample
  in the holdout window (2025-09-13 onwards), given that 2024-2026 saw
  ETH-specific decoupling around the Pectra upgrade?
- Sensitivity to entry threshold: |z|>1.5 vs >2.0 vs >2.5 — explore
  only after v1 verdict, with multiple-testing correction acknowledged.
- Universe expansion (BTC/SOL, ETH/SOL, BNB/USDT pairs) gated on v1
  passing the verdict tree on the BTC/ETH baseline.

## Trial outcomes

| variation_id | date | verdict | sr_observed | dsr | n_trades |
|---|---|---|---|---|---|
| btc-eth-engle-granger-v1 | (pending) | (pending) | (pending) | (pending) | (pending) |
