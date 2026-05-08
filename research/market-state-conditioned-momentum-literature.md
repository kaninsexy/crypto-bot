# MarketStateConditionedMomentum -- literature stub

Strategy id: `MarketStateConditionedMomentum`
Substrate: 10-symbol crypto basket at 1D
(BTC, ETH, SOL, BNB, XRP, ADA, AVAX, DOT, LINK, LTC)
Trial queue id: sq-031

## Hypothesis-of-record

A long-only basket of liquid crypto perpetuals deploys a per-asset
30-day time-series momentum (TSMOM) signal only when BTC's broader
market state is in a continuation regime (both the prior and current
60-day BTC log returns are positive); during transition states
(prev-UP/curr-DOWN or prev-DOWN/curr-UP) and trending-down states
(both periods negative) the strategy neutralizes exposure. The
hypothesis is that conditioning a static TSMOM tilt on the broader
market regime yields higher risk-adjusted returns than always-on
TSMOM, by skipping the transition windows where Cheema et al.
(2017) document TSMOM underperformance and the trending-down
windows where a long-only spot construction has no edge.

## Sources

- Han, C.; Kang, B.; Ryu, J. (2024). "Time-Series and
  Cross-Sectional Momentum in the Cryptocurrency Market: A
  Comprehensive Analysis under Realistic Assumptions." SSRN. Key
  finding: evidence for time-series momentum in cryptocurrencies is
  strong, whereas evidence for cross-sectional momentum is weak,
  with the effect concentrated among winners. Motivates the
  long-only, winner-only construction on the alt basket.
- Cheema, M. A.; Nartea, G. V.; Man, Y. (2017). "Cross-Sectional
  and Time-Series Momentum Returns and Market States." MPRA Paper.
  Key finding: in equity markets, time-series momentum strategies
  outperform cross-sectional strategies only when the market
  continues in the same state (UP/UP or DN/DN), but underperform in
  market transitions (UP/DN, DN/UP). Motivates the two-period
  market-state classifier and the transition-skip rule.
- Tzouvanas, P.; Kizys, R.; Tsend-Ayush, B. (2019). University of
  Southampton. Key finding: short-term cross-sectional momentum
  strategies (e.g. 7-day formation/holding) are highly profitable
  in crypto, yielding a 19% weekly return, but the effect
  disappears over longer terms. Motivates the choice of a 30-day
  per-asset TSMOM lookback rather than the equity-paper 12-month
  horizon, and the 60-day market-state lookback rather than 6-12
  months.

## Pre-trial gates (locked)

1. Long-only basket; equal weight across the held top-N alts when
   the market state is trending-up. No short positions.
2. BTC/USDT is the market-state benchmark and is **never traded**;
   it always emits HOLD. Baseline for the verdict tree is BTC/USDT
   buy-and-hold over the same dev window.
3. Two-period market-state classifier on BTC's log return:
   - prev_state_ret = log(close[t-L] / close[t-2L])
   - curr_state_ret = log(close[t]   / close[t-L])
   - trending-up = both positive; transition = signs differ;
     trending-down = both non-positive.
4. Deploy TSMOM only on trending-up; neutralize (force-exit and
   skip new entries) on transition and trending-down.
5. Per-alt TSMOM signal = sign(sum of trailing tsmom_lookback log
   returns). Long when positive; flat when non-positive.
6. Max concurrent positions capped at 5 of 9 alts (~half the
   basket); rank entries by TSMOM strength descending.
7. CPCV uses warmup-aware downshift: strategy_warmup_candles=121
   (= 2 * market_state_lookback + 1), min_tradeable_candles_per_block=30.
8. First full_cpcv for the strategy -- the count_trials_for_dsr
   monkeypatch (max(N, 1)) is the standard first-trial convention.

## Variation table

| # | id | params | trial_type | sharpe | dsr | verdict | notes |
|---|----|--------|------------|--------|-----|---------|-------|
| 1 | market-state-conditioned-momentum | market_state_lookback=60, tsmom_lookback=30, max_positions=5 | full_cpcv | TBD | TBD | TBD | initial dev_cpcv trial |

## Trial outcomes

<!-- Trial outcomes will be appended here by the orchestrator -->
