# DailyCrossSectionalReversal -- literature stub

Strategy id: `DailyCrossSectionalReversal`
Substrate: 9-alt crypto basket at 1D
(ETH, SOL, BNB, XRP, ADA, AVAX, DOT, LINK, LTC -- BTC excluded by design)
Trial queue id: sq-026

## Hypothesis-of-record

A long-only cross-sectional portfolio of cryptocurrencies that
performed worst over the prior 1-day lookback (the "loser" tail)
generates positive risk-adjusted returns versus an ETH/USDT
buy-and-hold baseline over the subsequent daily rebalance period,
with the effect concentrated on less-liquid alts.  BTC is
intentionally excluded from the traded universe because the
short-term reversal premium is documented to be weak or to flip to
momentum on the most-liquid asset (Zaremba 2021; Ficura 2023).

The original sq-026 implementation specification calls for a
long-short portfolio (long bottom quintile, short top quintile).
This trial tests only the LONG leg because (a) `backtest.engine_multi`
is structurally long-only (no short execution path); (b) Han et al.
(2024) document that crypto loser-shorts get punished by rebound
moves -- the same precedent applied to sq-013 (CrossSectionalReversal)
and sq-016 (CrossSectionalSkewness); and (c) Zaremba (2021) and
Ficura (2023) report that the reversal premium concentrates on the
long-loser leg of the cross-section, especially among less-liquid
coins, which is exactly what this implementation tests.

## Sources

- Zaremba, A.; Bilgin, M. H.; Long, H.; Mercik, A.; Szczygielski, J. J.
  (2021). "Up or down? Short-term reversal, momentum, and liquidity
  effects in cryptocurrency markets." International Review of
  Financial Analysis. Key finding: a cross-sectional portfolio of
  cryptocurrencies with low last-day returns significantly
  outperforms one with high last-day returns; the effect is driven
  by illiquid coins.
- Caporale, G. M.; Plastun, A. (2019). "Price overreactions in the
  cryptocurrency market." Journal of Economic Studies. Key finding:
  the cryptocurrency market exhibits significant price overreactions
  consistent with behavioural biases, leading to subsequent price
  corrections (reversals) -- the behavioural foundation of the
  short-term reversal premium.
- Ficura, M. (2023). "Impact of size and volume on cryptocurrency
  momentum and reversal." FFA Working Papers. Key finding: size and
  volume are critical factors determining the direction of return
  predictability; reversal effects are prominent in smaller,
  less-traded coins -- justifies excluding BTC from the traded
  universe.
- Han, C.; Kang, B.; Ryu, J. (2024). "Time-Series and Cross-Sectional
  Momentum in the Cryptocurrency Market: A Comprehensive Analysis
  under Realistic Assumptions." SSRN. Key finding: crypto
  loser-shorts get punished by rebound moves; long-only baskets
  dominate long-short on a risk-adjusted basis. Justifies dropping
  the short leg of the full long-short specification.

## Pre-trial gates (locked)

1. Long-only basket; equal weight across the held bottom-N losers.
2. Bottom-N rotation (default N=2 of 9 ~ bottom quintile of the
   9-alt traded universe).
3. 1-day prior-return lookback for the cross-sectional ranking.
4. Daily rebalance frequency.
5. BTC/USDT is NOT in the traded universe -- it is excluded by
   design per the hypothesis-of-record's less-liquid focus.  The
   manifest entry contains only the 9 traded alts; engine_multi
   never sees BTC for this strategy.
6. Baseline = ETH/USDT B&H over the same dev window (largest-cap
   traded alt; the natural counterfactual when BTC is excluded
   from the universe).

## Variation table

| # | id | params | trial_type | sharpe | dsr | verdict | notes |
|---|----|--------|------------|--------|-----|---------|-------|
| 1 | cs-daily-reversal | lookback=1, top_n=2 | full_cpcv | TBD | TBD | TBD | initial dev_cpcv trial |

## Trial outcomes

<!-- Trial outcomes will be appended here by the orchestrator -->

| variation_id | date | verdict | sr_observed | dsr | n_trades |
|---|---|---|---|---|---|
