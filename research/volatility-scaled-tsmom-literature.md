# VolatilityScaledTSMOM -- Literature

Strategy id: `VolatilityScaledTSMOM`
Substrate: BTC/USDT 1D
Trial queue id: sq-021

## Hypothesis of record

Volatility-scaled time-series momentum on BTC/USDT 1D. The signal at
bar t is the sign of the trailing `momentum_lookback`-bar log-return
sum:

    momentum_t = sum(log_ret_i)  for i in [t - momentum_lookback, t - 1]

Long when `momentum_t > 0`; flat when `momentum_t <= 0`. Long-only by
construction (engine_multi has no short-side harness; same
flat-when-negative convention as DualMomentum and
VolumeWeightedTSMOM). Single concurrent BTC long. Daily rebalance.

Position size at entry follows Barroso & Santa-Clara (2015):

    realized_vol_annual = std(log_ret_i for i in [t - vol_window, t - 1])
                          * sqrt(365)
    vol_factor          = min(target_vol_annual / realized_vol_annual,
                              vol_factor_cap)
    amount_usdt         = notional_capital * vol_factor

The simulator clamps `amount_usdt` to available balance, so
`vol_factor_cap = 1.0` plus the spot-only constraint means the
strategy never leverages.

## Pre-trial gates (locked)

1. Single-pair: BTC/USDT 1D. No multi-pair extensions until
   variation #1 verdict resolves.
2. Long-only flat-when-negative. Strategy must NEVER emit a SELL
   except to close an existing long. No shorts.
3. `momentum_lookback=30` and `vol_window=30` are the variation #1
   defaults. No sweep over alternative windows without a
   per-variation citation (no-p-hacking rule per
   `.claude/rules/backtest.md`).
4. `target_vol_annual=0.15` (15% annualised) and `vol_factor_cap=1.0`
   are locked. The cap = 1.0 is non-negotiable on spot BTC -- the
   codebase has no leverage path for spot. Raising the cap requires
   moving to a perp-margin substrate, which is a different manifest
   entry and a different trial.
5. trial_type = full_cpcv. n_blocks=10, k_held_out=2, purge=0,
   embargo=0.
6. strategy_factory in the trial script MUST construct a fresh
   `VolatilityScaledTSMOMStrategy` per CPCV block so `_position_open`
   resets to False at every block boundary.
7. Manifest holdout_start = 2025-09-22T00:00:00+00:00 (matches
   peer 1d entries: VolumeWeightedTSMOM, ContrarianSearchVolume,
   SocialSentimentMomentum, IlliquidityPremium, TrendFollowing_multi).
8. No look-ahead: the rolling momentum window at bar t consumes only
   bars strictly before t (the leading SHIFT(1) on the rolling sum
   enforces this); the realized-vol window at entry also uses only
   bars [t - vol_window, t - 1].

## Substrate

BTC/USDT 1D spot candles. Manifest entry `VolatilityScaledTSMOM`:

- timeframe: 1d
- symbol: BTC/USDT
  (existing schema convention; the OHLCV cache is keyed by
  spot-style symbols even when the trade is logically a perp)
- data_start: 2023-03-06T00:00:00+00:00
- data_end: 2026-04-19T00:00:00+00:00
- dev_end: 2025-09-22T00:00:00+00:00
- holdout_start: 2025-09-22T00:00:00+00:00 (sealed)
- strategy_warmup_candles: 32 (momentum_lookback=30 + 1 leading
  log-return NaN + 1 buffer bar; the realized-vol window of 30
  reaches validity at the same bar)
- min_tradeable_candles_per_block: 10

## Citations

1. **Grobys, K., Kolari, J. W., Sandretto, D., Shahzad, S. J. H.,
   Aijoe, J. (2025).** "Cryptocurrency momentum has (not) its
   moments." *Financial Markets and Portfolio Management.*
   Volatility-management techniques applied to cryptocurrency
   momentum portfolios substantially increase payoffs and produce
   statistically significant alphas by mitigating severe crashes.
   Crypto-specific. Quality 4.

2. **Kumar, M. P., Jenefer, B. M. (2026).** "Time-series momentum in
   cryptocurrency markets: a pre and post spot Bitcoin ETF
   analysis." *Zenodo.* A volatility-scaled TSMOM portfolio
   generated a Sharpe ratio of 0.82 pre-ETF and 1.22 post-ETF,
   outperforming Buy-and-Hold in regime-switching conditions.
   Crypto-specific. Quality 3.

3. **Catalin (2026).** "BTC volatility-aligned momentum engine."
   *finaur.com.* Practitioner framework aligning daily Bitcoin
   momentum signals with volatility regimes -- ATR-based risk
   scaling, defined entry/exit conditions. Crypto-specific.
   Quality 2.

4. **Barroso, P., Santa-Clara, P. (2015).** "Momentum has its
   moments." *Journal of Financial Economics.* Foundational
   equity-market formulation of constant-volatility-target scaling
   that the crypto adaptations above import: position size =
   target_vol / realized_vol, computed on a rolling window of
   trailing returns. Not crypto-specific but cited as the
   methodological backbone by Grobys et al. (2025) and Kumar &
   Jenefer (2026).

## Variation table

| # | id | params | trial_type | sharpe | dsr | verdict | notes |
|---|----|--------|------------|--------|-----|---------|-------|
| 1 | volatility-scaled-tsmom | mom_lb=30, vol_w=30, target_vol=0.15, cap=1.0 | full_cpcv | TBD | TBD | TBD | pending |

## Trial outcomes

<!-- Populated by the orchestrator after the trial completes. -->
