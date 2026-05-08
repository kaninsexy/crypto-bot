# ExchangeListingDrift -- literature stub

Strategy id: `ExchangeListingDrift`
Substrate: BTC/USDT at 1D
Trial queue id: sq-037

## Hypothesis-of-record

Cryptocurrencies experience a significant positive price drift in the
days immediately following a listing announcement on a top-tier
exchange (Coinbase, Binance), an effect which can be captured via
perpetual futures if the asset is already listed on a derivatives
venue. A long position is initiated to capture the pre-listing run-up
and exited at or near the actual listing date to time the documented
"sell the news" reversal immediately post-listing. This trial tests
the long-only event-driven drift on BTC/USDT 1D using an OHLCV-derived
event-score proxy (no announcement feed wired into this repo).

## Sources

- Le, H., Nguyen, T., & Park, D. (2021). "The Coinbase Effect: An
  Analysis of Cryptocurrency Listing Announcements." Finance Research
  Letters. Significant average abnormal return of approximately 29%
  for the first five days after a Coinbase listing announcement.
- Mazabel, F., & Sciandra, A. (2022). "The Crypto-Listing Pop: An
  Empirical Analysis of the 'Binance Effect'." SSRN. Distinguishing
  between announcement and listing dates, the paper finds a
  cumulative average abnormal return of around 41% in the 10 days
  surrounding the announcement, with the run-up beginning before the
  actual listing date.
- Corbet, S., Meegan, A., Larkin, C., Lucey, B., & Yarovaya, L.
  (2020). "What Moves Crypto Prices?" Review of Financial Studies.
  News of exchange listings is one of the most significant specific
  events that can explain jumps and extreme price movements in
  cryptocurrencies.
- Han, C., Kang, B., & Ryu, J. (2024). "Time-Series and Cross-
  Sectional Momentum in the Cryptocurrency Market: A Comprehensive
  Analysis under Realistic Assumptions." SSRN. Crypto loser-shorts
  are punished by rebound moves -- precedent for the long-only
  adaptation in this trial.

## Pre-trial gates (locked)

1. Single-pair: BTC/USDT 1D.
2. Long-only on spot. The literal hypothesis-of-record describes a
   pre-listing long capturing the run-up; no short leg is
   implemented. Project conventions also drop short legs because
   ``backtest.engine`` is structurally long-only on spot, and Han,
   Kang & Ryu (2024) report that crypto loser-shorts get punished by
   rebound moves (same precedent as sq-013 / sq-016 / sq-018 /
   sq-020 / sq-035 / sq-036).
3. Event-score data source: no Coinbase / Binance announcement feed
   is currently wired into this repo. The trial uses a deterministic
   OHLCV-derived proxy -- a rolling 30-day abnormal-volume z-score
   gated on positive-return bars -- documented in
   ``scripts/run_exchange_listing_drift_trial.py``
   (``_compute_event_score_proxy``). Swapping in a real announcement
   feed (Coinbase blog scrape, Binance announcement RSS, Twitter API
   on @binance / @coinbase) requires changing only that helper; the
   strategy class is data-source agnostic.
4. Event-score baseline window = 30 daily bars (conventional
   event-study estimation window of one trading month per Le et al.
   2021).
5. Entry threshold score > +2.0 (Corbet et al. 2020 use a 2-sigma
   jump filter to identify listing-news-driven jumps; +2.0 targets
   the right tail of the abnormal-volume distribution where
   announcement-like events concentrate).
6. Holding period = 5 daily bars (Le et al. 2021: cumulative abnormal
   return of ~29% over the first 5 days post-announcement; the
   "sell the news" exit is timed to the end of the documented post-
   announcement drift window).
7. Baseline = BTC/USDT B&H over the same dev window.
8. CPCV harness: 10 blocks, k_held_out=2, purge=0, embargo=0,
   warm_up_candles=50 (default). CPCVError is caught and a
   verdict=retire trial row is appended (event-score signal may be
   too sparse to meet the per-block trade-count floor).

## Variation table

| # | id | params | trial_type | sharpe | dsr | verdict | notes |
|---|----|--------|------------|--------|-----|---------|-------|
| 1 | major-exchange-listing-announcement | entry_threshold=+2.0, holding_period=5, baseline_window=30 | full_cpcv | TBD | TBD | TBD | first run |

## Trial outcomes

<!-- Trial outcomes will be appended here by the orchestrator -->
