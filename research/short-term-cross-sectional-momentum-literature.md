# ShortTermCrossSectionalMomentum -- literature stub

Strategy id: `ShortTermCrossSectionalMomentum`
Substrate: 10-symbol crypto basket at 1D
(BTC, ETH, SOL, BNB, XRP, ADA, AVAX, DOT, LINK, LTC)
Trial queue id: sq-024

## Hypothesis-of-record

A long-only cross-sectional portfolio of cryptocurrencies that
performed best over the prior 7-day formation window (the "winner"
tail) generates positive risk-adjusted returns versus BTC
buy-and-hold across the subsequent 7-day holding period.  The
short-horizon variant is motivated by Tzouvanas et al. (2020), who
report the cross-sectional momentum effect is highly significant
for 7-day formation / 7-day holding windows in crypto and
disappears over longer horizons.  No short positions are taken in
losers because Han et al. (2024) document that crypto losers tend
to rebound and inflict significant losses on shorts; the long-only
winner portfolio is the dominant configuration.

## Sources

- Drogen, L.; Hoffstein, C.; Otte, K. (2023). "Cross-sectional
  Momentum in Cryptocurrency Markets." SSRN. Key finding: assets
  performing best over a 30-day period tend to continue to
  outperform over the subsequent 7-day period, with long-only
  strategies consistently delivering excess returns.
- Zaremba, A.; Bilgin, M. H.; Long, H.; Mercik, A.; Szczygielski,
  J. J. (2021). "Up or down? Short-term reversal, momentum, and
  liquidity effects in cryptocurrency markets." International
  Review of Financial Analysis. Key finding: while most
  cryptocurrencies exhibit daily reversal, the handful of largest
  and most liquid coins exhibit daily momentum, outperforming on
  day T+1 after a high return on day T.
- Tzouvanas, P.; Kizys, R.; Tsend-Ayush, B. (2020). "Momentum
  trading in cryptocurrencies: Short-term returns and
  diversification benefits." Economics Letters. Key finding: the
  momentum effect is highly significant for short-term portfolios
  (e.g., 7-day formation and 7-day holding), but it disappears
  over longer terms.

## Pre-trial gates (locked)

1. Long-only basket; equal weight across the held top-N winners.
2. Top-N rotation (default N=2 of 10 = top quintile).
3. 7-day prior-return lookback for the cross-sectional ranking
   (short-horizon variant per Tzouvanas et al. 2020).
4. 7-day holding period before re-ranking (weekly rebalance).
5. No short positions in losers; flat exposure outside the winners.
6. Baseline = BTC/USDT B&H over the same dev window.
7. Universe restricted to the 10-symbol large-cap basket
   (BTC/ETH/SOL/BNB/XRP/ADA/AVAX/DOT/LINK/LTC) -- Zaremba et al.
   (2021) note short-horizon momentum holds for the largest and
   most liquid coins, while smaller-cap coins tend to mean-revert.

## Variation table

| # | id | params | trial_type | sharpe | dsr | verdict | notes |
|---|----|--------|------------|--------|-----|---------|-------|
| 1 | cs-short-term-mom-7d | lookback=7, top_n=2, hold=7 | full_cpcv | TBD | TBD | TBD | initial dev_cpcv trial |

## Trial outcomes

<!-- Trial outcomes will be appended here by the orchestrator -->

| variation_id | date | verdict | sr_observed | dsr | n_trades |
|---|---|---|---|---|---|
| cs-short-term-mom-7d | 2026-05-08 | retire | 0.9605 | 1.0000 | 189 |

