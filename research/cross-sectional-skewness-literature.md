# CrossSectionalSkewness -- literature stub

Strategy id: `CrossSectionalSkewness`
Substrate: 10-symbol crypto basket at 1D
(BTC market factor + ETH, SOL, BNB, XRP, ADA, AVAX, DOT, LINK, LTC traded)
Trial queue id: sq-016

## Hypothesis-of-record

A long-only cross-sectional portfolio of cryptocurrencies with the
LOWEST expected idiosyncratic skewness generates positive
risk-adjusted returns versus BTC buy-and-hold.  Expected
idiosyncratic skewness is forecast at each monthly (30-day)
rebalance via the Boyer/Mitton/Vorkink (2009) cross-sectional
predictive regression `is_t = g0 + g1*is_{t-1} + g2*iv_{t-1} + e_i`
fitted across the alt universe, then evaluated forward to predict
`E[is_{t+1}]`.  Idiosyncratic returns are the residuals from
regressing each alt's daily return on BTC's daily return over a
rolling 60-day window.  The bottom quintile (lowest predicted
skewness) is held equal-weight for the next 30 days.

The original sq-016 implementation specification calls for a
long-short portfolio (long bottom quintile, short top quintile).
This trial tests only the LONG leg because (a) backtest.engine_multi
is structurally long-only (no short execution path); (b) Han et
al. (2024) document that crypto loser-shorts are punished by
rebound moves -- the same precedent applied to sq-013
(CrossSectionalReversal) and sq-020 (CrossSectionalMomentum); and
(c) Boyer/Mitton/Vorkink (2009) report the cited Sharpe ~0.94
specifically on the long-only bottom-quintile leg, which is
exactly what this implementation tests.

## Sources

- Liu, Y.; Chen, Y. (2024).  "Skewness risk and the cross-section
  of cryptocurrency returns."  International Review of Financial
  Analysis.  Key finding: a NEGATIVE cross-sectional relationship
  between asymmetry risk (skewness) and future returns in
  cryptocurrencies, driven by idiosyncratic risk.  Direct
  empirical support for ranking on idiosyncratic-skewness and
  longing the LOW-skewness tail.
- Tekulova, P. (2022).  "Skewness/Lottery Trading Strategy in
  Cryptocurrencies."  SSRN.  Key finding: skewness-sorted
  portfolios using a long (360-day) lookback exhibit positive
  performance, including through crisis periods.  Supports the
  cross-sectional skewness-sort approach in crypto.
- Boyer, B.; Mitton, T.; Vorkink, K. (2009).  "Expected
  Idiosyncratic Skewness."  Review of Financial Studies.  Key
  finding: in equities, longing the lowest 5% of expected
  idiosyncratic skewness achieved a Sharpe ratio of 0.947.
  Provides the explicit two-stage methodology this implementation
  ports to crypto: (i) historical idiosyncratic-return residuals
  from a market-factor regression; (ii) cross-sectional
  predictive regression of next-period skewness on lagged
  skewness and lagged idiosyncratic volatility.
- Han, C.; Kang, B.; Ryu, J. (2024).  "Time-Series and
  Cross-Sectional Momentum in the Cryptocurrency Market: A
  Comprehensive Analysis under Realistic Assumptions."  SSRN.
  Key finding: crypto loser-shorts are punished by rebound
  moves; long-only baskets dominate long-short on a risk-
  adjusted basis.  Justifies dropping the short leg of the
  full long-short skewness specification.

## Pre-trial gates (locked)

1. Long-only basket; equal weight across the held bottom-N alts.
2. Bottom-N rotation by predicted E[is_{t+1}] (default N=2
   ~ bottom quintile of the 9-alt traded universe).
3. 60-day residual window for is_t / iv_t and is_{t-1} / iv_{t-1}.
4. 30-day holding period (monthly rebalance per
   Boyer/Mitton/Vorkink 2009 and the sq-016 implementation notes).
5. BTC/USDT is the market factor only -- never traded.  The
   strategy always emits HOLD for BTC; the engine's per-symbol
   universe still includes BTC because its OHLCV is required to
   compute residuals.
6. Cross-sectional regression requires >= 3 alt observations to
   fit (intercept + 2 slopes); falls back to ranking by raw is_t
   when the regression matrix is singular.
7. Baseline = BTC/USDT B&H over the same dev window (per the
   hypothesis-of-record's BTC-counterfactual framing).

## Variation table

| # | id | params | trial_type | sharpe | dsr | verdict | notes |
|---|----|--------|------------|--------|-----|---------|-------|
| 1 | cs-expected-idio-skewness | lookback=60, top_n=2, hold=30, market=BTC/USDT | full_cpcv | TBD | TBD | TBD | initial dev_cpcv trial |

## Trial outcomes

<!-- Trial outcomes will be appended here by the orchestrator -->

| variation_id | date | verdict | sr_observed | dsr | n_trades |
|---|---|---|---|---|---|
| cs-expected-idio-skewness | 2026-05-07 | retire | None | None | 0 |

