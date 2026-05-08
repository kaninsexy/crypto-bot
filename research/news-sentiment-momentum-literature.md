# NewsSentimentMomentum -- literature stub

Strategy id: `NewsSentimentMomentum`
Substrate: 7-symbol crypto basket at 1D
(BTC, ETH, SOL, BNB, XRP, ADA, AVAX)
Variation id: news-sentiment-momentum

## Hypothesis-of-record

Cryptocurrencies with high recent (24-hour) news-sentiment scores
outperform those with low scores over the subsequent daily holding
period. A long-only cross-sectional portfolio of the top-tercile
"news winners" -- ranked daily and held one day before re-ranking
-- generates positive risk-adjusted returns versus BTC buy-and-hold
across the dev window.

The original specifications in Chen/Hafner/Weber (2023) and
Kalamara et al. (2022) call for a dollar-neutral long-short
portfolio (long top tercile, short bottom tercile / decile). This
trial tests only the LONG leg because (a) `backtest.engine_multi`
is structurally long-only (no short execution path); (b) Han, Kang
& Ryu (2024) document that crypto loser-shorts are punished by
rebound moves -- the same precedent applied to sq-013
(CrossSectionalReversal), sq-016 (CrossSectionalSkewness), sq-018
(AttentionMomentum), and sq-020 (CrossSectionalMomentum); and (c)
Burggraf (2022) reports significant out-of-sample annualised
returns on the long high-sentiment leg for Bitcoin, supporting the
long-only adaptation.

## News-sentiment data source

No paid news-sentiment API is currently wired into this repo. The
trial uses a deterministic, OHLCV-derived proxy: the
volume-weighted log-return per daily bar.

    log_ret[t]    = log(close[t] / close[t-1])
    vol_baseline  = mean(volume over 30 prior bars)
    vol_ratio[t]  = clip(volume[t] / vol_baseline, 0.1, 10.0)
    news_score[t] = log_ret[t] * vol_ratio[t]

Volume-weighted return is the standard equity-finance proxy for
news-driven price moves (Engelberg & Parsons 2011 JF, Tetlock
2007 JF): high-volume up moves coincide with positive news,
high-volume down moves with negative news, while low-volume drift
is treated as noise. Burggraf (2022) explicitly uses
volume-amplified return signals as a news-sentiment proxy when
extracting the directional component for the long-only Bitcoin
trading simulation.

The strategy class itself is data-source agnostic: the trial
script computes the score series and hands it in via the
`sentiment_data` constructor argument. Swapping in a paid
news-sentiment feed (e.g. CryptoCompare News, CoinDesk Sentiment,
RavenPack) only requires changing
`_compute_news_score_for_symbol` in the trial script; the
rebalance / sizing / verdict logic is unaffected.

## Sources

- Chen, Y.; Hafner, C.M.; Weber, W. (2023). "Sentiment-driven
  cryptocurrency returns." Journal of International Financial
  Markets, Institutions and Money. Key finding: a long-short
  strategy sorting cryptocurrencies into terciles based on news
  sentiment generates a daily return of 0.17% with a Sharpe ratio
  of 1.15.
- Kalamara, E.; Papadimitriou, A.D.; Tziamprias, T.A.;
  Androulidakis, G.S. (2022). "News sentiment and crypto
  cross-section." Journal of International Financial Markets,
  Institutions and Money. Key finding: a cross-sectional trading
  strategy going long the top decile and short the bottom decile
  of cryptos sorted by news sentiment generates a monthly Sharpe
  ratio of 0.44.
- Burggraf, T. (2022). "News-based sentiment and Bitcoin returns."
  Finance Research Letters. Key finding: a trading simulation
  using news sentiment to predict Bitcoin returns confirms
  economic value by yielding significant annualised returns in an
  out-of-sample test.
- Han, C.; Kang, B.; Ryu, J. (2024). "Time-Series and
  Cross-Sectional Momentum in the Cryptocurrency Market: A
  Comprehensive Analysis under Realistic Assumptions." SSRN. Key
  finding: crypto loser-shorts are punished by rebound moves;
  long-only baskets dominate long-short on a risk-adjusted basis.
  Justifies dropping the short leg of the long-short news
  sentiment specification.

## Pre-trial gates (locked)

1. Long-only basket; equal weight across the held top-N news
   winners.
2. Top-N rotation default N=2 of 7 = top tercile of the 7-symbol
   traded universe (matches the long leg of the Chen/Hafner/Weber
   2023 tercile sort).
3. News-sentiment score per bar = mean of the volume-weighted
   log-return proxy over the last `sentiment_window` daily bars
   (default `sentiment_window=1`, the "past 24 hours" aggregation
   in the hypothesis-of-record).
4. Daily rebalance (`holding_period=1`) before re-ranking, per the
   strategy description and Chen/Hafner/Weber (2023) tercile sort.
5. News-sentiment proxy is computed inside the trial script from
   OHLCV (volume-weighted log return, 30-bar baseline volume,
   vol-ratio clip [0.1, 10.0]). The strategy class is
   data-source agnostic; swapping in a paid feed touches only
   `_compute_news_score_for_symbol` in the trial script.
6. No short positions in losers; flat exposure outside the held
   top-N basket.
7. Baseline = BTC/USDT B&H over the same dev window.
8. Manifest substrate is locked: 1D timeframe, 7-symbol basket
   (BTC, ETH, SOL, BNB, XRP, ADA, AVAX), `strategy_warmup_candles=31`
   (30-day baseline + 1 for the score), `min_tradeable_candles_per_block=30`.

## Variation table

<!-- Trial outcomes will be appended here by the orchestrator -->
