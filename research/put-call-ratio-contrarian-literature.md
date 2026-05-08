# PutCallRatioContrarian — literature stub

Strategy id: `PutCallRatioContrarian`
Substrate: BTC/USDT at 1D
Trial queue id: sq-036

## Hypothesis-of-record

Extreme levels of the crypto options Put/Call Ratio (PCR), indicating
crowded one-sided sentiment, predict an imminent price reversal that
can be profitably traded contrarian. A high PCR (puts dominating
calls) marks bearish-sentiment saturation and precedes a bounce; a
low PCR marks bullish-sentiment saturation and precedes a pullback.
This trial tests the long-only contrarian reaction to extreme high
PCR z-scores on BTC/USDT 1D.

## Sources

- Kyriazis, N. A., Papakyriakou, P., & Rozas, G. (2022). "Investor
  sentiment in the Bitcoin market: An analysis of the put/call
  ratio." Global Finance Journal. A one-standard-deviation increase
  in the Bitcoin PCR is associated with a +1.69% next-day return.
- Akyildirim, E., et al. (2024). "Forecasting Bitcoin prices: The
  role of the options market." Finance Research Letters. The open-
  interest put-call ratio has significant contrarian predictive
  power for Bitcoin returns.
- Chen, Z., et al. (2023). "The information content of the Bitcoin
  options market." Journal of International Financial Markets,
  Institutions and Money. The OI-based PCR is a strong contrarian
  predictor of Bitcoin spot returns over 1-5 day horizons.
- Han, C., Kang, B., & Ryu, J. (2024). "Time-Series and Cross-
  Sectional Momentum in the Cryptocurrency Market: A Comprehensive
  Analysis under Realistic Assumptions." SSRN. Crypto loser-shorts
  are punished by rebound moves -- precedent for the long-only
  adaptation in this trial.

## Pre-trial gates (locked)

1. Single-pair: BTC/USDT 1D.
2. Long-only on spot. The literal hypothesis-of-record includes a
   short leg on extreme negative z-scores; it is dropped here for
   the same reason as sq-013 / sq-016 / sq-018 / sq-020 / sq-035:
   ``backtest.engine`` is structurally long-only, and Han, Kang &
   Ryu (2024) report that crypto loser-shorts get punished by
   rebound moves.
3. PCR data source: no Deribit options feed is currently wired into
   this repo. The trial uses a deterministic OHLCV-derived proxy --
   a rolling 14-day ratio of down-day volume to up-day volume --
   documented in ``scripts/run_put_call_ratio_contrarian_trial.py``
   (``_compute_pcr_proxy``). Swapping in real Deribit OI-based PCR
   (Kyriazis 2022, Akyildirim 2024, Chen 2023) requires changing
   only that helper; the strategy class is data-source agnostic.
4. PCR proxy window = 14 daily bars (matches the typical PCR
   reporting cadence in the cited literature).
5. Z-score lookback = 60 daily bars (Chen 2023, Akyildirim 2024).
6. Entry threshold z > +2.0 (Kyriazis 2022 1-sigma effect at
   +1.69% next-day; +2.0 targets the cleaner extreme tail).
7. Exit: time-based after holding_period=3 bars (mid of the 1-5
   day predictive horizon in Chen 2023), OR mean-reversion exit
   when z reverts to <= 0.0, whichever comes first.
8. Baseline = BTC/USDT B&H over the same dev window.

## Variation table

| # | id | params | trial_type | sharpe | dsr | verdict | notes |
|---|----|--------|------------|--------|-----|---------|-------|
| 1 | pcr-contrarian-zscore-reversal | zscore_lookback=60, entry=+2.0, exit=0.0, hold=3, proxy_window=14 | full_cpcv | TBD | TBD | TBD | first run |

## Trial outcomes

<!-- Trial outcomes will be appended here by the orchestrator -->

| variation_id | date | verdict | sr_observed | dsr | n_trades |
|---|---|---|---|---|---|
| pcr-contrarian-zscore-reversal | 2026-05-08 | retire | None | None | 0 |

