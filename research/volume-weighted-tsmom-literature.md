# VolumeWeightedTSMOM -- Literature

Strategy id: `VolumeWeightedTSMOM`
Substrate: BTC/USDT 1D
Trial queue id: sq-012

## Hypothesis of record

Volume-weighted time-series momentum: the TSMOM signal is the
volume-weighted average of daily log-returns over a `lookback_window`
window. For each bar t the signal is

    signal_t = sum(log_ret_i * volume_i) / sum(volume_i)
             for i in [t - lookback_window, t - 1]

Long when `signal_t > 0`; flat when `signal_t <= 0`. Long-only by
construction (engine_multi does not support unconditional shorts
without a dedicated short-side harness; this strategy uses the same
flat-when-negative convention as DualMomentum). Single concurrent
BTC long. Rebalance daily.

## Pre-trial gates (locked)

1. Single-pair: BTC/USDT 1D. No multi-pair extensions until
   variation #1 verdict resolves.
2. Long-only flat-when-negative. Strategy must NEVER emit a SELL
   except to close an existing long. No shorts.
3. lookback_window=20 is the variation #1 default. No sweep over
   alternative lookbacks without a per-variation citation
   (no-p-hacking rule per `.claude/rules/backtest.md`).
4. trial_type = full_cpcv. n_blocks=10, k_held_out=2, purge=0,
   embargo=0.
5. strategy_factory in the trial script MUST construct a fresh
   VolumeWeightedTSMOMStrategy per CPCV block so `_position_open`
   resets to False at every block boundary.
6. Manifest holdout_start = 2025-09-22T00:00:00+00:00 (matches
   peer 1d entries: ContrarianSearchVolume, SocialSentimentMomentum,
   IlliquidityPremium, TrendFollowing_multi).
7. No look-ahead: the rolling window at bar t consumes only bars
   strictly before t (the leading SHIFT(1) on the volume-weighted
   ratio enforces this).

## Substrate

BTC/USDT 1D spot candles. Manifest entry `VolumeWeightedTSMOM`:

- timeframe: 1d
- symbol: BTC/USDT
  (existing schema convention; the OHLCV cache is keyed by
  spot-style symbols even when the trade is logically a perp)
- data_start: 2023-03-06T00:00:00+00:00
- data_end: 2026-04-19T00:00:00+00:00
- dev_end: 2025-09-22T00:00:00+00:00
- holdout_start: 2025-09-22T00:00:00+00:00 (sealed)
- strategy_warmup_candles: 22 (lookback_window=20 + 1 leading
  log-return NaN + 1 buffer bar)
- min_tradeable_candles_per_block: 10

## Citations

1. **Huang, Z.-C., Sangiorgi, I., Urquhart, A. (2024).**
   "Volume-weighted time-series momentum." *SSRN.*
   Reports a winner-minus-loser TSMOM portfolio achieving 0.94%
   per day with annualized Sharpe 2.17 -- substantially
   outperforming simple TSMOM benchmarks. Provides the explicit
   volume-weighting formulation used as the variation #1 signal.
   Crypto-specific. Quality 4.

2. **Shen, D., Urquhart, A., Wang, P. (2022).** "High-volume
   periods and intraday return predictability in cryptocurrency
   markets." *Financial Review.* Documents that high-volume
   regimes carry the bulk of intraday return predictability:
   first-half-hour return positively predicts last-half-hour
   return, and trading the effect yields significant economic
   gains. Motivates volume as the weighting variable rather than
   uniform weighting. Crypto-specific. Quality 4.

3. **Li, X., Zhang, X. (2023).** "Time-series momentum on
   cryptocurrency daily returns." *Proceedings of the 2nd
   International Conference on Business and Policy Studies*
   (Springer). Daily TSMOM with short lookback horizons (10-30
   bars) generates significant positive profits and higher Sharpe
   ratios than passive holding even with 0.1% transaction costs.
   Justifies the 20-bar lookback default. Crypto-specific.
   Quality 3.

## Variation table

| # | id | params | trial_type | sharpe | dsr | verdict | notes |
|---|----|--------|------------|--------|-----|---------|-------|
| 1 | volume-weighted-tsmom-v1 | lookback=20 | full_cpcv | TBD | TBD | TBD | pending |

## Trial outcomes

<!-- Populated by the orchestrator after the trial completes. -->
