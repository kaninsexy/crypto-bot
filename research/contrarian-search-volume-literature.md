# ContrarianSearchVolume — literature stub

Strategy id: `ContrarianSearchVolume`
Substrate: BTC/USDT at 1D
Trial queue id: sq-011

## Hypothesis-of-record

Abnormally high Google Trends search volume for "bitcoin" predicts
negative subsequent returns. Staying flat during search-volume spikes
and long during normal/low-search periods outperforms BTC
buy-and-hold.

## Sources

- Chemkha, R., Ben Jabeur, S., & Naifar, N. (2023). "Search-volume
  effects in cryptocurrency markets." Research in International
  Business and Finance.
- Dastgir, S., et al. (2019). "Search volume index and Bitcoin
  returns." Research in International Business and Finance.
- Salisu, A.A., Gupta, R., & Bouri, E. (2021). "Predicting Bitcoin
  with search-volume data." PLOS ONE.

## Pre-trial gates (locked)

1. Single-pair: BTC/USDT 1D.
2. Long-only (engine is long-only); flat during high-search regimes.
3. ASVI = (current - rolling_median_window) / (rolling_std_window
   + 1e-9); window=4 (weekly Trends, ~4 weeks of context).
4. Trends weekly data resampled to daily via forward-fill.
5. Baseline = BTC/USDT B&H over the same dev window.

## Variation table

| # | id | params | trial_type | sharpe | dsr | verdict | notes |
|---|----|--------|------------|--------|-----|---------|-------|
| 1 | contrarian-search-volume-spike | window=4, threshold=1.0 | full_cpcv | TBD | TBD | TBD | first trial |
