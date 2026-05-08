# LiquidityConditionedReversal -- literature stub

Strategy id: `LiquidityConditionedReversal`
Substrate: 11-symbol crypto basket at 1D
(BTC, ETH, SOL, BNB, XRP, ADA, AVAX, DOT, LINK, LTC, UNI)
Trial queue id: sq-033

## Hypothesis-of-record

A long-only cross-sectional short-term reversal portfolio is
profitable only when applied to a low-liquidity subset of the crypto
universe -- high-liquidity coins exhibit momentum, not reversal. Each
day, restrict the universe to the bottom quintile by 30-day mean
dollar volume (the illiquid tail), then long the worst 1-day
performer within that subset; rebalance daily. The variation predicts
positive risk-adjusted returns vs BTC buy-and-hold over the dev
window.

## Sources

- Ficura, M.; Colak, G. (2023). "Impact of Size and Volume on
  Cryptocurrency Momentum and Reversal." SSRN. Key finding: weekly
  return reversal is statistically significant only for small and
  illiquid coins (t-stat = -7.31), while large and liquid coins
  exhibit a weekly momentum effect.
- Zaremba, A.; Bilgin, M. H.; Long, H.; Mercik, A.; Szczygielski, J. J.
  (2021). "Up or down? Short-term reversal, momentum, and liquidity
  effects in cryptocurrency markets." International Review of
  Financial Analysis. Key finding: a significant daily reversal where
  cryptocurrencies with low last-day returns outperform; the authors
  argue this pattern is driven by the illiquidity of most coins.
- Wen, Z.; Bouri, E.; Xu, Y.; Zhao, Y. (2022). "Intraday Return
  Predictability in the Cryptocurrency Markets: Momentum, Reversal,
  or Both." SSRN. Key finding: patterns of return predictability
  (both momentum and reversal) flip as a function of liquidity, among
  other factors.

## Pre-trial gates (locked)

1. Long-only basket; equal weight across the held bottom-N losers.
2. Two-stage filter: first liquidity (bottom-N by 30-day mean dollar
   volume), then reversal (bottom-N by prior return) within the
   illiquid subset.
3. 30-day rolling mean of close x volume defines the liquidity rank.
4. 1-day prior-return lookback for the reversal ranking.
5. Daily rebalance frequency.
6. Baseline = BTC/USDT B&H over the same dev window.
7. 11-symbol universe (matches the AltcoinSeasonRotation /
   CryptoSectorRotation manifest universe). Bottom-quintile
   liquidity_bottom_n=2 (2/11 ~= 18%).
8. top_n=1 inside the illiquid subset (long the single biggest loser
   among the 2 illiquid coins).

## Variation table

| # | id | params | trial_type | sharpe | dsr | verdict | notes |
|---|----|--------|------------|--------|-----|---------|-------|
| 1 | liquidity-conditioned-reversal-daily | liq_lookback=30, liq_bottom_n=2, rev_lookback=1, top_n=1 | full_cpcv | TBD | TBD | TBD | initial dev_cpcv trial |

## Trial outcomes

<!-- Trial outcomes will be appended here by the orchestrator -->

| variation_id | date | verdict | sr_observed | dsr | n_trades |
|---|---|---|---|---|---|
| liquidity-conditioned-reversal-daily | 2026-05-08 | retire | 0.1288 | 1.0000 | 325 |

