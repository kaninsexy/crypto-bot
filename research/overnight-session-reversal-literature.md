# OvernightSessionReversal -- literature stub

Strategy id: `OvernightSessionReversal`
Substrate: BTC/USDT at 1H
Trial queue id: sq-023

## Hypothesis-of-record

Returns during the US stock-market trading session (NYSE 09:30-
16:00 ET, approximated on the 1H UTC grid as 14:00-21:00 UTC) are
negatively correlated with the preceding overnight-session return
(NYSE-closed window, ~21:00 UTC prior day to 14:00 UTC current
day).  When the overnight return is negative, the day session
tends to revert positive; we BUY at the day-session start and
exit at the day-session end.

The original sq-023 specification calls for a long/short baseline:
short the day session when overnight return > 0, long the day
session when overnight return < 0.  This trial tests only the LONG
leg of the conditional rule:

  * overnight_return < 0  -> BUY at day-session start (15:00 UTC fill)
  * overnight_return >= 0 -> HOLD (would-be short, dropped)

Long-only by construction: the long-only-leg precedent that
applied to sq-013 (CrossSectionalReversal), sq-016
(CrossSectionalSkewness), sq-018 (AttentionMomentum), sq-019
(IntradayMomentumReversal), sq-020 (CrossSectionalMomentum), and
sq-024 (ShortTermCrossSectionalMomentum) carries over here because
(a) backtest.engine is structurally long-only on spot, and
(b) Han et al. (2024) document that crypto loser-shorts are
punished by rebound moves, making the long-leg-only test the
right prior for crypto perp markets.

The cited evidence in Ham, Ryu, Webb (2022) reports statistically
significant positive returns on the long leg of the overnight-
to-day reversal sort in BTC, which is exactly what this
implementation tests.

## Sources

- Ham, H.; Ryu, D.; Webb, R.I. (2022).  "The effects of overnight
  events on daytime trading sessions."  International Review of
  Financial Analysis.  Key finding: a strategy that shorts (longs)
  a cryptocurrency during the NYSE daytime session following a
  positive (negative) return in the prior overnight session
  generates statistically significant positive returns.  Crypto-
  specific.  Quality 4.
- Lou, D.; Polk, C.; Skouras, S. (2019).  "A tug of war:
  Overnight versus intraday expected returns."  Journal of
  Financial Economics.  Key finding: in equities, price-momentum
  strategies primarily earn their premium overnight while
  reversing intraday, revealing a 'tug of war' between different
  investor clienteles dominant at different times.  Equity
  evidence; cited as theoretical foundation for the overnight-
  to-day reversal channel.  Quality 5.
- Zaremba, A.; Bilgin, M.H.; Long, H.; Mercik, A.; Szczygielski,
  J.J. (2021).  "Up or down? Short-term reversal, momentum, and
  liquidity effects in cryptocurrency markets."  International
  Review of Financial Analysis.  Key finding: cryptocurrencies
  with low returns on the previous day significantly outperform
  those with high returns, an effect concentrated in less liquid
  coins.  Crypto-specific reversal effect; supports a long-leg
  prior on negative-overnight days.  Quality 4.
- Barardehi, Y.H.; Bogousslavsky, V.; Muravyev, D. (2023).
  "What Drives Momentum and Reversal? Evidence from Day and
  Night Signals."  SSRN.  Key finding: portfolios formed on past
  intraday returns exhibit short-term reversal, consistent with
  investor underreaction to information from trades.  Equity
  evidence; cited as cross-asset support for the short-horizon
  reversal channel.  Quality 3.
- Han, C.; Kang, B.; Ryu, J. (2024).  "Time-Series and
  Cross-Sectional Momentum in the Cryptocurrency Market: A
  Comprehensive Analysis under Realistic Assumptions."  SSRN.
  Key finding: crypto loser-shorts are punished by rebound
  moves; long-only baskets dominate long-short on a risk-
  adjusted basis.  Justifies dropping the short leg of the full
  long-short overnight-reversal specification.

## Pre-trial gates (locked)

1. Single-pair: BTC/USDT (manifest notation) on 1H timeframe only.
2. Long-only: strategy MUST NEVER emit Signal.SELL when
   self._position_open == False.  No short entries under any
   condition.
3. Fixed UTC session boundaries: entry_hour = 14 (BUY at the close
   of the 14:00 UTC bar = 15:00 UTC fill, ~30 min after NYSE open
   under no-DST baseline), exit_hour = 20 (SELL at the close of
   the 20:00 UTC bar = 21:00 UTC fill, ~NYSE close).
4. Overnight return computed at decision time as
   (open_today_entry_bar - close_yesterday_exit_bar) /
   close_yesterday_exit_bar.  Both boundary bars must be present
   in the dataframe; if either is missing (block boundary, first
   day after warmup) the strategy HOLDs.
5. Long-only reversal rule: BUY when overnight_return < 0 and
   _position_open is False; HOLD otherwise.  The short branch
   (overnight > 0) is HOLD on the long-only spot engine.
6. trial_type = full_cpcv.  n_blocks=10, k_held_out=2, purge=0,
   embargo=0.
7. Manifest holdout_start must equal IntradaySeasonalityEffects'
   peer 1H entry: 2025-09-12T14:12:00+00:00.  data_start =
   2023-04-20T15:00:00+00:00, data_end = 2026-04-19T14:00:00+00:00.
8. strategy_factory in the trial script MUST construct a fresh
   OvernightSessionReversalStrategy per CPCV block so
   `_position_open` resets to False at every block boundary.
9. No look-ahead: the prior day's exit_hour close (21:00 UTC
   yesterday) and the current day's entry_hour open (14:00 UTC
   today) are both fully observable at decision time (close of
   bar 14:00 UTC = 15:00 UTC).
10. Baseline = BTC/USDT B&H over the same dev window.

## Substrate

BTC/USDT 1H spot/perpetual candles (OKX cache, spot-style symbol
notation per project convention).  Manifest entry
`OvernightSessionReversal`:

- timeframe: 1h
- symbol: BTC/USDT
- data_start: 2023-04-20T15:00:00+00:00
- data_end: 2026-04-19T14:00:00+00:00
- dev_end: 2025-09-12T14:12:00+00:00
- holdout_start: 2025-09-12T14:12:00+00:00 (sealed)

The 1H single-symbol manifest schema (see IntradaySeasonalityEffects
peer entry) does not require `strategy_warmup_candles` or
`min_tradeable_candles_per_block`; the sacred 50-candle engine
warmup plus the strategy's internal HOLD-on-missing-boundary-bar
guard cover the warmup period (one full UTC day = 24 1H candles
before the first decision can fire).

## Variation table

| # | id | params | trial_type | sharpe | dsr | verdict | notes |
|---|----|--------|------------|--------|-----|---------|-------|
| 1 | us-session-overnight-reversal | entry=14, exit=20 | full_cpcv | TBD | TBD | TBD | initial dev_cpcv trial |

## Trial outcomes

<!-- Trial outcomes will be appended here by the orchestrator -->
