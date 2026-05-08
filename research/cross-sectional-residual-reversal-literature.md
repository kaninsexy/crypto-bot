# CrossSectionalResidualReversal -- literature stub

Strategy id: `CrossSectionalResidualReversal`
Substrate: 9-alt crypto basket at 1D with BTC as regressor
(BTC, ETH, SOL, BNB, XRP, ADA, AVAX, DOT, LINK, LTC -- BTC is the
market-factor regressor and is NOT traded; the 9 alts are the
traded universe)
Trial queue id: sq-028

## Hypothesis-of-record

A long-only cross-sectional portfolio of cryptocurrencies whose most
recent 1-day RESIDUAL return (return orthogonalised against the BTC
market factor via a rolling OLS beta) is in the bottom quintile
generates positive risk-adjusted returns versus an ETH/USDT
buy-and-hold baseline over the subsequent daily rebalance period.
Residual orthogonalisation is expected to amplify the cross-sectional
reversal premium by stripping out the common BTC-driven move and
isolating idiosyncratic, liquidity-driven price dislocations
(Blitz et al. 2013; Brogaard et al. 2024; Zaremba 2021).

The original Blitz et al. (2013) and Brogaard et al. (2024)
specification calls for a long-short residual-reversal portfolio
(long bottom quintile, short top quintile). This trial tests only the
LONG leg because (a) `backtest.engine_multi` is structurally long-only
(no short execution path); (b) Han et al. (2024) document that crypto
loser-shorts get punished by rebound moves -- the same precedent
applied to sq-013 (CrossSectionalReversal) and sq-026
(DailyCrossSectionalReversal); and (c) Zaremba (2021) reports the
reversal premium concentrates on the long-loser leg of the cross-
section, which residual orthogonalisation is expected to amplify.

BTC/USDT is intentionally INCLUDED in `symbols` as the regressor leg
(the rolling-beta factor) but is never traded -- `generate_signals`
always emits HOLD for BTC, so the engine never opens a BTC position.
This mirrors the MeanReversion_BTC_Residual (Phase 4.A) construction.

## Sources

- Zaremba, A.; Bilgin, M. H.; Long, H.; Mercik, A.; Szczygielski, J. J.
  (2021). "Up or down? Short-term reversal, momentum, and liquidity
  effects in cryptocurrency markets." International Review of
  Financial Analysis. Key finding: cryptocurrencies with low last-day
  returns significantly outperform those with high last-day returns;
  the effect is attributed to the illiquidity of most coins.
- Blitz, D.; Huij, J.; Lansdorp, S.; Verbeek, M. (2013).
  "Short-term residual reversal." Journal of Financial Markets.
  Key finding: a reversal strategy based on residual stock returns
  (orthogonal to market factors) earns risk-adjusted returns roughly
  twice as large as a conventional reversal strategy. Provides the
  theoretical basis for orthogonalising returns against a market
  factor before ranking the cross-section.
- Brogaard, J.; Han, J.; Kim, H. (2024). "Intraday Residual Reversal
  in the U.S. Stock Market." SSRN. Key finding: a strategy buying
  stocks with negative residual returns and selling those with
  positive residual returns captures the returns to liquidity
  provision on transitory price movements, yielding high annualised
  returns -- supports the residual-loser tail concentration.
- Han, C.; Kang, B.; Ryu, J. (2024). "Time-Series and Cross-Sectional
  Momentum in the Cryptocurrency Market: A Comprehensive Analysis
  under Realistic Assumptions." SSRN. Key finding: crypto
  loser-shorts get punished by rebound moves; long-only baskets
  dominate long-short on a risk-adjusted basis. Justifies dropping
  the short leg of the full long-short specification.
- Fil, M.; Kristoufek, L. (2020). "Pairs Trading in Cryptocurrency
  Markets." IEEE Access 8, 172644-172651. Used in the Phase 4.A
  MeanReversion_BTC_Residual sibling for the rolling-beta
  construction; provides crypto-specific calibration for the OLS
  beta window length.

## Pre-trial gates (locked)

1. Long-only basket; equal weight across the held bottom-N residual
   losers.
2. Bottom-N rotation (default N=2 of 9 alts ~ bottom quintile of the
   traded universe).
3. 1-day residual lookback for the cross-sectional ranking
   (residual_lookback=1).
4. 30-day rolling OLS beta window for the BTC market factor
   (beta_window=30).
5. Daily rebalance frequency.
6. BTC/USDT IS in `symbols` as the regressor leg only; `generate_signals`
   always emits HOLD for BTC. The 9 alts are the traded universe.
7. Baseline = ETH/USDT B&H over the same dev window (largest-cap
   traded alt; BTC is the regressor leg and not traded for this
   strategy).

## Variation table

| # | id | params | trial_type | sharpe | dsr | verdict | notes |
|---|----|--------|------------|--------|-----|---------|-------|
| 1 | cs-residual-reversal-daily | beta_window=30, residual_lookback=1, top_n=2 | full_cpcv | TBD | TBD | TBD | initial dev_cpcv trial |

## Trial outcomes

<!-- Trial outcomes will be appended here by the orchestrator -->

| variation_id | date | verdict | sr_observed | dsr | n_trades |
|---|---|---|---|---|---|
| cs-residual-reversal-daily | 2026-05-08 | retire | -0.3544 | 0.0000 | 935 |
