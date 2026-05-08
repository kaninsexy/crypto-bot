# CrossSectionalFundingRateCarry -- literature stub

Strategy id: `CrossSectionalFundingRateCarry`
Substrate: 7-symbol crypto basket at 1D
(BTC, ETH, SOL, BNB, XRP, ADA, AVAX)
Variation id: cs-funding-rate-carry-v1
Trial queue id: sq-038

## Hypothesis-of-record

Cryptocurrencies whose perpetual futures carry the lowest (most-
negative) funding rates outperform those with the highest funding
rates over the subsequent daily holding period. A long-only
cross-sectional portfolio of the bottom-tercile "carry winners" --
ranked daily by their daily-aggregated OKX USDT-M perpetual funding
rate and held one day before re-ranking -- generates positive
risk-adjusted returns versus BTC buy-and-hold across the dev
window.

The original specifications in Bianchi et al. (2022) and Abedifar et
al. (2023) call for a dollar-neutral long-short portfolio (long the
bottom funding-rate quintile, short the top). This trial tests only
the LONG (lowest-funding) leg because (a) `backtest.engine_multi`
is structurally long-only (no short execution path); (b) Han, Kang
& Ryu (2024) document that crypto loser-shorts are punished by
rebound moves -- the same precedent applied to sq-013
(CrossSectionalReversal), sq-016 (CrossSectionalSkewness), sq-018
(AttentionMomentum), sq-020 (CrossSectionalMomentum), and
news-sentiment-momentum; and (c) Bianchi et al. (2022) report that
the long leg of the funding-rate carry portfolio carries the bulk
of the unconditional alpha, supporting the long-only adaptation.

## Funding-rate data source

The 8-hour OKX USDT-M perpetual funding history is fetched per
symbol via `data.okx_funding.load_or_fetch_funding_history` (cache-
wrapped; per-month archive parquets under
`backtest/cache/perp_funding/archive/{instId}/`). The 8-hour rate
series is then mean-aggregated to a daily series by grouping the
three intra-day settlements (00:00 / 08:00 / 16:00 UTC) to their
UTC date and reindexing onto each symbol's OHLCV daily index.
Missing-day forward-fill absorbs rare gaps so the strategy receives
a value at every trading bar.

The strategy class itself is data-source agnostic: the trial script
computes the daily series and hands it in via the `funding_data`
constructor argument. Swapping in a different exchange (Binance,
Bybit) or a different aggregation rule (last-rate, sum,
predicted-rate) requires changing only `_load_funding_for_symbol`
in the trial script; the rebalance / sizing / verdict logic is
unaffected.

## Sources

- Bianchi, D.; Babiak, M.; Ciner, C. (2022). "Carry trades in
  cryptocurrency markets." Journal of International Financial
  Markets, Institutions and Money. Key finding: a dollar-neutral
  strategy sorting perpetual futures on their funding rates yields
  significant monthly alphas of 2.19% and an annualized Sharpe
  ratio of 1.34.
- Abedifar, P.; Fica, O.; Imbierowicz, B. (2023). "Taming the
  Basis: A Cross-Sectional Perspective on Cryptocurrency Carry."
  SSRN. Key finding: a dollar-neutral strategy going long
  perpetuals with a low basis and shorting those with a high basis
  generates significant risk-adjusted returns of up to 2.5% per
  week, unexplained by common risk factors.
- ryanczm (2024). "Crypto Stat Arb."
  https://github.com/ryanczm/Crypto-Stat-Arb. Key finding:
  practitioner research outlines a cross-sectional statistical
  arbitrage strategy on perpetual futures that explicitly includes
  a 'carry' factor derived from funding rates.
- Han, C.; Kang, B.; Ryu, J. (2024). "Time-Series and
  Cross-Sectional Momentum in the Cryptocurrency Market: A
  Comprehensive Analysis under Realistic Assumptions." SSRN. Key
  finding: crypto loser-shorts are punished by rebound moves;
  long-only baskets dominate long-short on a risk-adjusted basis.
  Justifies dropping the short leg of the long-short funding-rate
  carry specification.

## Pre-trial gates (locked)

1. Long-only basket; equal weight across the held top-N carry
   winners.
2. Top-N rotation default N=2 of 7 = bottom tercile of the 7-symbol
   traded universe (matches the long leg of the Bianchi et al.
   (2022) tercile / quintile sort).
3. Funding-rate score per bar = mean of the daily-aggregated
   funding-rate proxy over the last `funding_window` daily bars
   (default `funding_window=1`, the literal "prior-day funding
   rate" in the hypothesis-of-record).
4. Daily rebalance (`holding_period=1`) before re-ranking, per the
   trial-queue implementation note ("rebalance at the chosen
   frequency, e.g. daily").
5. Funding-rate proxy is fetched in the trial script from
   `data.okx_funding.load_or_fetch_funding_history` and aggregated
   to a daily series by mean of the three 8h settlements per UTC
   day. The strategy class is data-source agnostic; swapping in a
   different exchange / aggregation touches only
   `_load_funding_for_symbol` in the trial script.
6. Ranking is ASCENDING (lowest / most-negative funding first). The
   long leg of the cross-section is the carry-receiver tail.
7. No short positions in the high-funding leg; flat exposure
   outside the held bottom-N basket.
8. Baseline = BTC/USDT B&H over the same dev window.
9. Manifest substrate is locked: 1D timeframe, 7-symbol basket
   (BTC, ETH, SOL, BNB, XRP, ADA, AVAX),
   `strategy_warmup_candles=2` (1-day funding window + 1 buffer),
   `min_tradeable_candles_per_block=10`.

## Variation table

| # | id | params | trial_type | sharpe | dsr | verdict | notes |
|---|----|--------|------------|--------|-----|---------|-------|
| 1 | cs-funding-rate-carry-v1 | funding_window=1, top_n=2, hold=1 | full_cpcv | TBD | TBD | TBD | initial dev_cpcv trial |

## Trial outcomes

<!-- Trial outcomes will be appended here by the orchestrator -->

| variation_id | date | verdict | sr_observed | dsr | n_trades |
|---|---|---|---|---|---|
| cs-funding-rate-carry-v1 | 2026-05-08 | retire | 1.0964 | 1.0000 | 846 |

