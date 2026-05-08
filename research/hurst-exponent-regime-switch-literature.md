# HurstExponentRegimeSwitch -- Literature

Strategy id: `HurstExponentRegimeSwitch`
Substrate: BTC/USDT 4H
Trial queue id: sq-027

## Hypothesis of record

Hurst-exponent regime-switch on BTC/USDT 4H. The rolling rescaled-
range (R/S) Hurst exponent over the trailing `hurst_window` log
returns classifies the current regime:

  - `H > h_upper`  -> trending / persistent
  - `H < h_lower`  -> mean-reverting / anti-persistent
  - otherwise      -> random walk (neutral band)

Two long-only sub-strategies are wired in -- exactly one is active
per regime; the neutral band suppresses new entries:

  - **Trend leg.** Long when the trailing `momentum_lookback`
    log-return sum is strictly positive; flat otherwise. Same
    flat-when-negative convention as DualMomentum,
    VolumeWeightedTSMOM, and VolatilityScaledTSMOM (engine_multi has
    no short-side harness in this codebase).

  - **MR leg.** Long when the rolling z-score of close vs the
    trailing `zscore_window` mean is `<= entry_z`. Exit when
    `z >= exit_z` OR a `mr_stop_loss_pct` drawdown from entry. The
    same z-score and stop convention used in
    MeanReversion_BTC_Residual (Phase 4.A v1) so the sub-leg is
    directly comparable.

Position-management rule: the sub-strategy that opened the current
position owns its exit. A regime flip (e.g. "trend" -> "neutral"
mid-trade) does NOT force-close an open position; only the active
sub-strategy's exit signal does. This avoids the artefact where a
regime estimator that briefly drifts into the neutral band churns
trades that the underlying signal would have held through.

Long-only by construction. Single concurrent BTC long. 4H
rebalance. No look-ahead: the rolling Hurst window at bar t consumes
only bars strictly before t (the leading SHIFT(1) on the rolling
returns enforces this); the trend momentum and MR z-score windows
likewise consume only bars `[t - W, t - 1]`.

## Pre-trial gates (locked)

1. Single-pair: BTC/USDT 4H. No multi-pair extensions until
   variation #1 verdict resolves. Calef & Kucinic (2021) test on
   Bitcoin only; Begusic et al. (2022) test on BTC/USD only.

2. Long-only flat-when-negative. Strategy must NEVER emit a SELL
   except to close an existing long. No shorts. The cited papers
   include a long-short variant (Calef & Kucinic 2021) but the
   codebase has no short-side simulator on spot, so the
   variation #1 hypothesis is restricted to the long leg.

3. `hurst_window=100` is the variation #1 default. The
   implementation_notes specify "100-250 periods"; 100 is the
   lower bound and the smallest window that yields a statistically
   meaningful R/S estimate on the 4H substrate (~16.7 days of
   regime evidence). No sweep over alternative windows without a
   per-variation citation (no-p-hacking rule per
   `.claude/rules/backtest.md`).

4. `h_upper=0.55`, `h_lower=0.45` are the variation #1 thresholds
   per the implementation_notes' worked example. The neutral band
   width (`h_upper - h_lower = 0.10`) is the cited dead-zone. No
   sweep without per-variation citation.

5. `momentum_lookback=30` and `zscore_window=30` are locked at the
   same value for variation #1 so the regime-switch decision
   boundary is statistically clean -- the only thing that flips
   between regimes is the SIGN of the rule (positive-momentum vs
   oversold-z), not the lookback. Matches the
   VolatilityScaledTSMOM `momentum_lookback=30` default and the
   MeanReversion_BTC_Residual `zscore_window=30` default so each
   sub-leg is directly comparable to its standalone trial.

6. `entry_z=-1.5`, `exit_z=0.0`, `mr_stop_loss_pct=0.08` are the
   locked MR-leg parameters; same values used in
   MeanReversion_BTC_Residual (Phase 4.A v1) which itself sources
   them from Fil & Kristoufek (2020) IEEE Access.

7. trial_type = full_cpcv. n_blocks=10, k_held_out=2, purge=0,
   embargo=0.

8. strategy_factory in the trial script MUST construct a fresh
   `HurstExponentRegimeSwitchStrategy` per CPCV block so
   `_position_open` and `_active_mode` reset to False/None at
   every block boundary.

9. Manifest holdout_start = 2025-09-13T00:00:00+00:00 (matches the
   peer 4H entry MeanReversion_BTC_Residual).

10. No look-ahead. The rolling Hurst window and both sub-leg
    windows consume only bars strictly before the decision bar.

## Substrate

BTC/USDT 4H spot candles. Manifest entry
`HurstExponentRegimeSwitch`:

- timeframe: 4h
- symbol: BTC/USDT
  (existing schema convention; the OHLCV cache is keyed by
  spot-style symbols even when the trade is logically a perp)
- data_start: 2023-04-30T00:00:00+00:00 (matches the 38-month
  4H BTC/USDT cache and the peer 4H entry)
- data_end: 2026-04-19T00:00:00+00:00
- dev_end: 2025-09-13T00:00:00+00:00
- holdout_start: 2025-09-13T00:00:00+00:00 (sealed)
- strategy_warmup_candles: 132 (100 hurst + 30 momentum + 1
  log-return-NaN + 1 buffer; the z-score window of 30 reaches
  validity at the same bar)
- min_tradeable_candles_per_block: 30

## Citations

1. **Calef, A., Kucinic, M. (2021).** "Switching approach for
   Cryptocurrency trading using the Hurst exponent." *SSRN.* A
   strategy switching between trend-following (H>0.5) and
   mean-reversion (H<0.5) on Bitcoin yields a Sharpe ratio of
   0.69, outperforming both standalone strategies and
   buy-and-hold. The cited Sharpe is the long-short variant; the
   variation #1 hypothesis here restricts to the long leg per
   pre-trial gate #2. Crypto-specific. Quality 3.

2. **Begusic, S., Velickovic, P., Lio, P. (2022).** "Deep
   Reinforcement Learning in Cryptocurrency Trading: A Review and
   Case Study on the Hurst Exponent." *Applied Sciences.* Adding
   the time-varying Hurst exponent as a state feature to a deep
   reinforcement learning trading agent increased its Sharpe ratio
   on a BTC/USD backtest from 1.05 to 1.77, confirming that
   rolling-Hurst regime classification carries directional
   information on Bitcoin. Crypto-specific. Quality 4.

3. **Kyriazis, N. A. (2020).** "The adaptive market hypothesis in
   the cryptocurrency markets." *Eurasian Economic Review.*
   Provides evidence that 15 major cryptocurrencies exhibit
   time-varying Hurst exponents, supporting the Adaptive Market
   Hypothesis and the potential for regime-based trading
   strategies. Establishes that the regime-switch premise is not
   restricted to a single coin or sample window. Crypto-specific.
   Quality 4.

4. **Hurst, H. E. (1951).** "Long-term storage capacity of
   reservoirs." *Transactions of the American Society of Civil
   Engineers.* Original derivation of the rescaled-range (R/S)
   estimator used here. Not crypto-specific but cited as the
   methodological backbone by Calef & Kucinic (2021) and Begusic
   et al. (2022).

## Variation table

| # | id | params | trial_type | sharpe | dsr | verdict | notes |
|---|----|--------|------------|--------|-----|---------|-------|
| 1 | hurst-regime-trend-mr-switch | hurst_window=100, h_upper=0.55, h_lower=0.45, mom_lb=30, z_w=30, entry_z=-1.5, exit_z=0.0, stop=0.08 | full_cpcv | TBD | TBD | TBD | pending |

## Trial outcomes

<!-- Populated by the orchestrator after the trial completes. -->
