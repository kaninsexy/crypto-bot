# IntradayMomentumReversal -- literature stub

Strategy id: `IntradayMomentumReversal`
Substrate: BTC/USDT at 1H
Trial queue id: sq-019

## Hypothesis-of-record

The return of the first hour of a fixed UTC trading session
predicts the return of the last hour for cryptocurrency perpetual
futures.  The direction (momentum or reversal) is conditional on
the realized-volatility regime: low-vol days exhibit momentum
(closing-hour return follows the opening hour), high-vol days
exhibit reversal (closing-hour return inverts the opening hour).

Long-only by construction: the long-only-leg precedent that
applied to sq-013 (CrossSectionalReversal), sq-016
(CrossSectionalSkewness), sq-018 (AttentionMomentum), and sq-020
(CrossSectionalMomentum) carries over here because (a) backtest.
engine is structurally long-only on spot, and (b) Han et al. (2024)
document that crypto loser-shorts are punished by rebound moves.

The original sq-019 implementation specification calls for a
long/short baseline (long on positive open, short on negative). 
This trial tests only the LONG leg of each conditional rule:

  * high_vol AND opening_return < 0  -> BUY (reversal, long leg)
  * low_vol  AND opening_return > 0  -> BUY (momentum, long leg)
  * otherwise                        -> HOLD

The cited evidence in Wen et al. (2022) and Shen et al. (2022)
reports positive Sharpes on the long leg of the intraday timing
sort in BTC perpetual futures, which is exactly what this
implementation tests.

## Sources

- Wen, Z.; Bouri, E.; Xu, Y.; Zhao, Y. (2022).  "Intraday return
  predictability in the cryptocurrency markets: Momentum, reversal,
  or both."  The North American Journal of Economics and Finance.
  Key finding: a timing strategy based on intraday momentum and
  reversal predictors produces higher economic value than buy-and-
  hold or always-long benchmarks across 1H crypto perpetuals.
  Crypto-specific.  Quality 4.
- Shen, D.; Urquhart, A.; Wang, P. (2022).  "Bitcoin intraday time-
  series momentum."  Financial Review.  Key finding: the first
  half-hour return of a defined trading session positively
  predicts the last half-hour return, yielding substantial
  economic gains, especially in market downturns (high-vol
  regime).  Provides the conditional logic for the high-vol
  reversal rule.  Crypto-specific.  Quality 4.
- Zaremba, A.; Bilgin, M.H.; Long, H.; Mercik, A.; Szczygielski,
  J.J. (2021).  "Up or down? Short-term reversal, momentum, and
  liquidity effects in cryptocurrency markets."  International
  Review of Financial Analysis.  Key finding: the largest, most
  liquid cryptocurrencies exhibit momentum while smaller, illiquid
  ones exhibit reversal.  The realized-vol regime (vol_short >
  vol_long) is the BTC-specific proxy for the liquidity/dispersion
  factor -- high-vol regimes correspond to stressed/illiquid
  markets where reversal dominates.  Crypto-specific.  Quality 4.
- Han, C.; Kang, B.; Ryu, J. (2024).  "Time-Series and
  Cross-Sectional Momentum in the Cryptocurrency Market: A
  Comprehensive Analysis under Realistic Assumptions."  SSRN.
  Key finding: crypto loser-shorts are punished by rebound moves;
  long-only baskets dominate long-short on a risk-adjusted basis.
  Justifies dropping the short leg of the full long-short
  intraday specification.

## Pre-trial gates (locked)

1. Single-pair: BTC/USDT (manifest notation) on 1H timeframe only.
2. Long-only: strategy MUST NEVER emit Signal.SELL when
   self._position_open == False.  No short entries under any
   condition.
3. Fixed UTC session: 08:00 - 16:00 UTC.  opening_hour = 08:00,
   entry_hour = 14:00 (BUY fills at 14:00 close = 15:00 open),
   exit_hour = 15:00 (SELL fills at 15:00 close = 16:00 open).
   Position holds for exactly the closing 1H bar.
4. Conditional rule on the realized-vol regime: vol_short =
   std(daily returns, last 7 days), vol_long = std(daily returns,
   last 30 days), high_vol = vol_short > vol_long.  high_vol days
   apply reversal logic (long if open return < 0); low_vol days
   apply momentum logic (long if open return > 0).
5. trial_type = full_cpcv.  n_blocks=10, k_held_out=2, purge=0,
   embargo=0.
6. Manifest holdout_start must equal IntradaySeasonalityEffects'
   peer 1H entry: 2025-09-12T14:12:00+00:00.  data_start =
   2023-04-20T15:00:00+00:00, data_end = 2026-04-19T14:00:00+00:00.
7. strategy_factory in the trial script MUST construct a fresh
   IntradayMomentumReversalStrategy per CPCV block so
   `_position_open` resets to False at every block boundary.
8. No look-ahead: the opening-bar return at entry_hour is computed
   from a fully-completed earlier 1H bar in the same UTC day
   (08:00 candle, observable at 09:00 UTC; entry decision at
   14:00 UTC).  The vol regime uses only daily returns up to the
   most recently completed daily bar at entry-decision time.
9. Baseline = BTC/USDT B&H over the same dev window.

## Substrate

BTC/USDT 1H spot/perpetual candles (OKX cache, spot-style symbol
notation per project convention).  Manifest entry
`IntradayMomentumReversal`:

- timeframe: 1h
- symbol: BTC/USDT
- data_start: 2023-04-20T15:00:00+00:00
- data_end: 2026-04-19T14:00:00+00:00
- dev_end: 2025-09-12T14:12:00+00:00
- holdout_start: 2025-09-12T14:12:00+00:00 (sealed)

The 1H single-symbol manifest schema (see IntradaySeasonalityEffects
peer entry) does not require `strategy_warmup_candles` or
`min_tradeable_candles_per_block`; the sacred 50-candle engine
warmup plus the strategy's internal HOLD-on-insufficient-history
guard cover the warmup period (vol_lookback_long_days = 30 daily
returns ~= 720 1H candles per block before first signal fires).

## Variation table

| # | id | params | trial_type | sharpe | dsr | verdict | notes |
|---|----|--------|------------|--------|-----|---------|-------|
| 1 | intraday-conditional-momentum-reversal | open=8, entry=14, exit=15, vol_s=7, vol_l=30 | full_cpcv | TBD | TBD | TBD | initial dev_cpcv trial |

## Trial outcomes

<!-- Trial outcomes will be appended here by the orchestrator -->
