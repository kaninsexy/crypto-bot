# IntradayJumpReversal -- literature stub

Strategy id: `IntradayJumpReversal`
Substrate: BTC/USDT at 1H
Variation: intraday-jump-reversal-zscore

## Hypothesis-of-record

Trading against large, statistically significant intraday price
moves (jumps), as defined by a z-score relative to recent rolling
volatility, generates positive risk-adjusted returns due to
overreaction and transient liquidity imbalances.  On BTC/USDT 1H
candles, a 1H simple return whose magnitude exceeds a z-score
threshold (z = r_t / rolling_std_24h, |z| >= 3.0) signals a
transient overreaction that mean-reverts within ~24 hours.

Long-only by construction: the long-only-leg precedent that
applied to sq-013 (CrossSectionalReversal), sq-016
(CrossSectionalSkewness), sq-018 (AttentionMomentum), sq-019
(IntradayMomentumReversal), and sq-020 (CrossSectionalMomentum)
carries over here because (a) backtest.engine is structurally
long-only on spot, and (b) Han et al. (2024) document that crypto
loser-shorts are punished by rebound moves.

The cited Alexzap (2026) BTC-USD JMR backtest specifies a
long/short rule (short upward jumps, long downward jumps).  This
trial tests only the LONG leg of the fade rule:

  * z_t <= -3.0 AND not _position_open  -> BUY (fade down jump)
  * _position_open AND z_t >= +3.0      -> SELL (opposite jump)
  * _position_open AND bars_held >= 24  -> SELL (time stop)
  * otherwise                           -> HOLD

The cited evidence in Wen et al. (2022) reports positive economic
value for intraday timing strategies that include reversal signals
following large price jumps in cryptocurrency markets, which is
what the LONG-leg test isolates.

## Sources

- Wen, Z.; Bouri, E.; Xu, Y.; Zhao, Y. (2022).  "Intraday return
  predictability in the cryptocurrency markets: Momentum, reversal,
  or both."  The North American Journal of Economics and Finance.
  Key finding: a timing strategy based on intraday predictors,
  which includes reversal signals following large price jumps,
  produces higher economic value than a buy-and-hold benchmark in
  cryptocurrency markets.  Crypto-specific.  Quality 4.
- Zaremba, A.; Bilgin, M.H.; Long, H.; Mercik, A.; Szczygielski,
  J.J. (2021).  "Up or down? Short-term reversal, momentum, and
  liquidity effects in cryptocurrency markets."  International
  Review of Financial Analysis.  Key finding: on a daily frequency,
  cryptocurrencies with low last-day returns significantly
  outperform those with high last-day returns; this reversal
  effect is strongest in illiquid coins and is the multi-asset
  analogue of the single-asset z-score fade tested here.
  Crypto-specific.  Quality 4.
- Alexzap (2026).  "Intraday Volatility Jump Mean-Reversion (JMR)
  Trading Strategy for BTC-USD in Python."  Medium / DEV
  Community.  Key finding: a backtest of a strategy on BTC-USD
  from 2022-2024 that shorts upward volatility jumps and longs
  downward volatility jumps, defined by a z-score threshold,
  generated a positive Sharpe Ratio.  Crypto-specific, BTC-
  specific, and the direct empirical precedent for the parameter
  choice (24h rolling window, z = 3.0).  Quality 2.
- Han, C.; Kang, B.; Ryu, J. (2024).  "Time-Series and
  Cross-Sectional Momentum in the Cryptocurrency Market: A
  Comprehensive Analysis under Realistic Assumptions."  SSRN.
  Key finding: crypto loser-shorts are punished by rebound moves;
  long-only baskets dominate long-short on a risk-adjusted basis.
  Justifies dropping the short leg of the full long-short JMR
  specification.

## Pre-trial gates (locked)

1. Single-pair: BTC/USDT (manifest notation) on 1H timeframe only.
2. Long-only: strategy MUST NEVER emit Signal.SELL when
   self._position_open == False.  No short entries under any
   condition.
3. Z-score definition: z_t = r_t / sigma_t where r_t is the latest
   completed 1H simple return (close_t / close_{t-1} - 1) and
   sigma_t is the standard deviation of the prior `vol_window`
   (default 24) 1H returns excluding r_t (no look-ahead).
4. Entry rule: BUY when z_t <= -z_threshold (default -3.0) and no
   position is currently open.
5. Exit rule: SELL when an open position observes either
   z_t >= +z_threshold (opposite-direction jump) OR
   bars_held >= max_hold_bars (default 24, the documented intraday-
   reversal half-life from Wen et al. 2022 / Shen et al. 2022).
6. trial_type = full_cpcv.  n_blocks=10, k_held_out=2, purge=0,
   embargo=0.
7. Manifest holdout_start must equal the IntradaySeasonalityEffects
   / IntradayMomentumReversal peer 1H entry:
   2025-09-12T14:12:00+00:00.  data_start =
   2023-04-20T15:00:00+00:00, data_end = 2026-04-19T14:00:00+00:00.
8. strategy_factory in the trial script MUST construct a fresh
   IntradayJumpReversalStrategy per CPCV block so `_position_open`
   resets to False and `_bars_held` resets to 0 at every block
   boundary.
9. No look-ahead: the volatility reference (sigma_t) is computed
   from the prior vol_window completed 1H returns, exclusive of
   the bar whose return is being z-scored.  The entry decision
   uses only data observable at the close of bar t.
10. Baseline = BTC/USDT B&H over the same dev window.

## Substrate

BTC/USDT 1H spot/perpetual candles (OKX cache, spot-style symbol
notation per project convention).  Manifest entry
`IntradayJumpReversal`:

- timeframe: 1h
- symbol: BTC/USDT
- data_start: 2023-04-20T15:00:00+00:00
- data_end: 2026-04-19T14:00:00+00:00
- dev_end: 2025-09-12T14:12:00+00:00
- holdout_start: 2025-09-12T14:12:00+00:00 (sealed)

The 1H single-symbol manifest schema (see IntradaySeasonalityEffects
and IntradayMomentumReversal peer entries) does not require
`strategy_warmup_candles` or `min_tradeable_candles_per_block`;
the sacred 50-candle engine warmup plus the strategy's internal
HOLD-on-insufficient-history guard (returns HOLD until
vol_window + 2 candles accumulate) cover the warmup period.

## Variation table

| # | id | params | trial_type | sharpe | dsr | verdict | notes |
|---|----|--------|------------|--------|-----|---------|-------|
| 1 | intraday-jump-reversal-zscore | vol_window=24, z_threshold=3.0, max_hold_bars=24 | full_cpcv | TBD | TBD | TBD | initial dev_cpcv trial |

## Trial outcomes

<!-- Trial outcomes will be appended here by the orchestrator -->

| variation_id | date | verdict | sr_observed | dsr | n_trades |
|---|---|---|---|---|---|
| intraday-jump-reversal-zscore | 2026-05-07 | retire | -1.0974 | 0.0000 | 241 |
