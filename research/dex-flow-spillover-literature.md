# DEXFlowSpillover -- literature stub

Strategy id: `DEXFlowSpillover`
Substrate: BTC/USDT at 1H
Trial queue id: sq-039

## Hypothesis-of-record

Statistically significant order flow imbalances (OFI) on major
decentralized exchanges (e.g. Uniswap V3 WBTC/USDC) predict the
direction of near-term price changes for the same asset on
centralized exchanges. Concretely: when the cumulative DEX OFI
exceeds a statistical threshold (z-score > +2 sigma), the CEX price
follows in the direction of the imbalance over the subsequent
minutes-to-hours window. This trial tests the long-only directional
spillover reaction to extreme positive OFI z-scores on BTC/USDT 1H.

## Sources

- Makarov, I. & Schoar, A. (2023). "Price Discovery in Decentralized
  Exchanges." SSRN. Order flow on Uniswap V3 significantly predicts
  CEX (Binance) prices: a 1-sigma DEX OFI predicts a 4.6 bps CEX
  price move over the next 5 minutes. The directional-spillover
  effect is the central empirical claim this strategy targets.
- Lehar, A., St-Pierre, L. M., Moallemi, C. C., & Rizk, R. G.
  (2024). "The Role of Decentralized Exchanges in Crypto-Asset
  Price Discovery." SSRN. For ETH-USDC, Uniswap V3 contributes
  40-50% of price discovery, often leading CEX prices -- supports
  the lead-lag premise that motivates trading the spillover.
- Cong, L. W., Wang, Y., Tang, K., & Wang, J. (2022). "Arbitrage
  Opportunities in Decentralized Exchanges." SSRN. Persistent
  DEX/CEX dislocations average 0.29% daily profit potential --
  basis for treating extreme z-score flow as a tradeable signal
  rather than noise.
- Han, C., Kang, B., & Ryu, J. (2024). "Time-Series and Cross-
  Sectional Momentum in the Cryptocurrency Market: A Comprehensive
  Analysis under Realistic Assumptions." SSRN. Crypto loser-shorts
  are punished by rebound moves -- precedent for the long-only
  adaptation in this trial.

## Pre-trial gates (locked)

1. Single-pair: BTC/USDT 1H.
2. Long-only on spot. The literal hypothesis-of-record includes a
   short leg on extreme negative OFI z-scores (i.e. directional
   spillover down); it is dropped here for the same reason as
   sq-013 / sq-016 / sq-018 / sq-020 / sq-035 / sq-036:
   ``backtest.engine`` is structurally long-only on spot, and Han,
   Kang & Ryu (2024) report that crypto loser-shorts get punished
   by rebound moves.
3. OFI data source: no Uniswap V3 / DEX swap feed is currently
   wired into this repo. The trial uses a deterministic OHLCV-
   derived proxy -- a rolling 5-bar sum of Lee-Ready signed volume
   (sign(close - open) * volume) -- documented in
   ``scripts/run_dex_flow_spillover_trial.py``
   (``_compute_ofi_proxy``). Swapping in real Uniswap V3 swap-
   volume OFI (Makarov & Schoar 2023, Lehar et al 2024) requires
   changing only that helper; the strategy class is data-source
   agnostic.
4. OFI proxy window = 5 hourly bars (matches the 5-hour scaling
   of the 5-minute Makarov & Schoar 2023 lead-lag horizon to 1H
   sampling cadence).
5. Z-score lookback = 60 hourly bars (~2.5 days). Conventional
   intraday-stats normalization window used in jump / OFI z-score
   studies and matches sq-036 PCR contrarian's 60-bar choice.
6. Entry threshold z > +2.0 (Makarov & Schoar 2023 1-sigma effect
   at +4.6 bps over 5 min; +2.0 targets the cleaner extreme tail
   and matches sq-036 PCR contrarian's 2-sigma convention).
7. Exit: time-based after holding_period=5 bars (5 hours, mid-
   range of the lead-lag horizon scaled from the Makarov & Schoar
   2023 5-minute horizon to 1H sampling), OR mean-reversion exit
   when z reverts to <= 0.0, whichever comes first.
8. Baseline = BTC/USDT B&H over the same dev window.

## Variation table

| # | id | params | trial_type | sharpe | dsr | verdict | notes |
|---|----|--------|------------|--------|-----|---------|-------|
| 1 | dex-cex-flow-imbalance-spillover | zscore_lookback=60, entry=+2.0, exit=0.0, hold=5, ofi_window=5 | full_cpcv | TBD | TBD | TBD | first run |

## Trial outcomes

<!-- Trial outcomes will be appended here by the orchestrator -->

| variation_id | date | verdict | sr_observed | dsr | n_trades |
|---|---|---|---|---|---|
| dex-cex-flow-imbalance-spillover | 2026-05-08 | retire | -0.7421 | 0.0000 | 355 |

