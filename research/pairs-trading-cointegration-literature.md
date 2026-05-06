# PairsTradingCointegration — Literature Review

## Reframe Note (2026-05-06)

Classical pairs trading requires short positions (long the undervalued
leg, short the overvalued leg simultaneously). `backtest/engine_multi.py`
is long-only. After the prior session's two-leg implementation surfaced
that the engine silently drops the short leg — producing incorrect
economics — the human approved a reframe to a long-only **two-asset
rotation** (Option B2):

- Hold **BTC/USDT** when the spread is abnormally low (BTC has
  underperformed relative to ETH).
- Hold **ETH/USDT** when the spread is abnormally high (ETH has
  underperformed relative to BTC).
- At most one asset is held at a time. Never short.
- The cointegration spread (with ADF p < 0.05 filter) is used for
  timing/selection, not for delta-neutral hedging.

This tests whether the cointegration spread has *directional* predictive
power in crypto — a necessary (not sufficient) condition for the full
delta-neutral pairs trade. If this rotation passes the verdict tree, a
proper delta-neutral variation (Option A) becomes worth the engine
investment to support short positions.

## Pre-trial gates (locked)

1. Two-asset rotation: BTC/USDT and ETH/USDT only (v1 scope). At most
   one asset is held at any time; **no short positions**.
2. Timeframe: 1H.
3. Hedge ratio window: 720 bars rolling OLS via covariance/variance
   ratio in log space (no statsmodels needed for the ratio itself).
4. Spread z-score window: 168 bars (= 1 trading week at 1H).
5. Cointegration filter: statsmodels.tsa.stattools.adfuller on the
   168-bar spread series; entries gated on p < 0.05.
6. Entry threshold: |z-score| > 2.0.
   - z < -2.0 -> BUY symbol_a (BTC), HOLD symbol_b (ETH)
   - z > +2.0 -> BUY symbol_b (ETH), HOLD symbol_a (BTC)
7. Exit threshold: |z-score| < 0.5 against the position side.
8. trial_type = full_cpcv, n_blocks=10.
9. strategy_factory in trial script MUST create a fresh
   PairsTradingCointegrationStrategy instance per CPCV block (so
   `_current_position` resets to None at each block boundary).
10. position_fraction returns 1.0 (full capital in the held asset; only
    one asset is held at a time so this is structurally safe).
11. DO NOT run the trial in this session — build infra only. The trial
    runs on Windows via the Task Scheduler.

## Hypothesis

The BTC/USDT vs ETH/USDT cointegration spread has directional predictive
power on a 1H timeframe: when the spread is more than 2 standard
deviations from its 168-bar mean, rotating into the underperforming leg
captures positive risk-adjusted returns relative to passive BTC B&H. The
ADF cointegration filter avoids entries during regimes where the
relationship has broken down.

## Substrate

BTC/USDT and ETH/USDT 1H spot candles. Manifest entry
`PairsTradingCointegration`:

- timeframe: 1h
- symbols: ["BTC/USDT", "ETH/USDT"]
- data_start: 2023-03-06T07:00:00+00:00
- data_end: 2026-05-05T06:00:00+00:00
- dev_end: 2025-09-16T01:24:00+00:00
  (computed via the standard 80/20 split on the BTC+ETH 1H cache
  intersection per `backtest/generate_holdout_manifest.py`)
- holdout_start: 2025-09-16T01:24:00+00:00 (sealed)
- strategy_warmup_candles: 888 (hedge_ratio_window=720 + zscore_window=168)
- min_tradeable_candles_per_block: 168

Dev window: ~2.5 years of 1H bars (~21k bars per leg). Holdout sealed.

## Literature

1. **Taekyung Park (2026).** "Statistical Arbitrage Strategies Using
   Cointegration Analysis in Cryptocurrency Markets." *International
   Journal of Science and Research Archive.* Sec.3 specifies the
   |z|>2.0 entry threshold and ADF p<0.05 cointegration filter; reports
   Sharpe 1.58–2.45 and annual alphas of 11–15% on cointegrated crypto
   pairs (especially BTC-ETH).

2. **Daniel da Silva Carvalho (2021).** "Pairs trading: cointegration-
   based methods, applied to the cryptocurrency market." Master
   Dissertation, Universidade Católica Portuguesa. Sec.5 finds the
   Engle-Granger ADF approach is the best predictor of cointegration on
   crypto, with longer (~6-month) formation periods outperforming
   shorter ones — motivates the 720-bar (~30-day at 1H) hedge ratio
   window. Sec.5.2 motivates the |z|<=0.5 partial-reversion exit used
   here for the rotation variant.

3. **Masood Tadi & Jiří Witzany (2023).** "Copula-Based Trading of
   Cointegrated Cryptocurrency Pairs." arXiv. Sec.4 confirms the
   2-sigma entry convention and the profitability of cointegration-
   anchored mean reversion in crypto vs buy-and-hold baselines.

## Variations

| variation_id                 | trial_id | verdict | sharpe | notes |
|------------------------------|----------|---------|--------|-------|
| stat-arb-coint-rotation-v1   | (pending)| (pending)| (pending) | Phase 4.C v1: BTC/ETH 1H rotation, hedge=720, zscore=168, entry \|z\|>2.0, exit \|z\|<0.5, ADF p<0.05 filter. Long-only. |

## Open questions

- Does the BTC-ETH cointegration relationship persist out-of-sample in
  the holdout window (2025-09-16 onwards), given that 2024–2026 saw
  ETH-specific decoupling around the Pectra upgrade?
- If the rotation passes the verdict tree, the natural follow-on is an
  engine_multi extension to support shorts so the delta-neutral pairs
  trade (Option A) can be tested. The rotation variant validates the
  cointegration *signal*; the delta-neutral variant validates the
  *trade structure*.
- Sensitivity sweeps (entry |z| in {1.5, 2.0, 2.5}; exit |z| in
  {0.0, 0.5, 1.0}) are deferred until v1 verdict is in, with
  multiple-testing correction acknowledged in advance.

## Trial outcomes

| variation_id | date | verdict | sr_observed | dsr | n_trades |
|---|---|---|---|---|---|
| stat-arb-coint-rotation-v1 | (pending) | (pending) | (pending) | (pending) | (pending) |
