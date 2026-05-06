# SocialSentimentMomentum -- Literature

## Variations

### sentiment-momentum-filter (sq-002, variation #1)

Single-symbol BTC/USDT 1D long-only sentiment-momentum strategy.
Trailing 7-bar rolling mean of a 0-100 daily sentiment scalar; long
when mean > 50 and rising, flat when mean < 45. Single concurrent
BTC long; CPCV state resets per block via the strategy_factory.

**Data source change (2026-05-07).** Substituted LunarCrush Galaxy
Score with the Crypto Fear & Greed Index (alternative.me). LunarCrush
v4 returns HTTP 402 on the public tier (Individual subscription
required, $72/month); Fear & Greed is free, requires no API key, and
provides daily history from 2018-02-01. The signal contract is
identical at the strategy level: a 0-100 daily scalar with a
documented neutral midpoint at 50, used as a momentum input. Code
changes: `data/fear_greed.py` (new fetcher), `strategies/
social_sentiment_momentum.py` (column renamed `galaxy_score` ->
`fear_greed_value`; docstring updated), `scripts/
run_social_sentiment_momentum_trial.py` (import + fetch call swapped).

## Citations

1. **Zhang, J., Zhang, C. (2022).** "Do cryptocurrency markets react to
   issuer sentiments? Evidence from Twitter."
   *Research in International Business and Finance* 61, 101656.
   Cryptocurrency prices react positively to Twitter issuer sentiments
   within 24h across 47 major cryptocurrencies; effect driven by
   incremental change in sentiment tone. Crypto-specific. Quality 4.

2. **Ante, L. (2023).** "How Elon Musk's Twitter activity moves
   cryptocurrency markets."
   *Technological Forecasting & Social Change* 186, 122112.
   Event study on 47 tweets: significant abnormal returns follow Musk
   crypto tweets; individual tweets raise BTC by 16.9% or reduce by
   11.8%. Confirms social-media causality on crypto prices.
   Crypto-specific. Quality 4.

3. **Ortu, M., Uras, N., Conversano, C., Bartolucci, S.,
   Destefanis, G. (2022).** "On technical trading and social media
   indicators for cryptocurrency price classification through deep
   learning." *Expert Systems with Applications* 198, 116804.
   Adding social-media sentiment indicators to technical features
   improves BTC/ETH daily price-direction classification accuracy
   from 51-55% to 67-84% across four deep-learning architectures
   (2017-2020 data). Crypto-specific. Quality 4.

4. **Lietor, J., Sanchez-Ballesta, J.P., et al. (2023).** "Fear and
   Greed Index as a predictor of cryptocurrency returns."
   *Finance Research Letters.* Uses the alternative.me Fear & Greed
   Index directly as a predictor of cryptocurrency returns; documents
   the index's predictive content for short-horizon BTC returns.
   Crypto-specific. Quality 3. (Basis for the 2026-05-07 data-source
   substitution from LunarCrush to Fear & Greed.)

<!-- Trial outcomes will be appended here by the orchestrator -->
