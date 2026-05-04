# TrendFollowing -- Phase 4.A resurrection hypothesis-of-record

**Date:** 2026-05-04
**Phase:** 4.A resurrection batch
**Status:** Hypothesis-of-record. Not yet queued.

## Phase 3c failure (2026-04-26)

RETIRE, observed_sharpe -1.7708, n_trades 374 on BTC/USDT 1H.
Root cause: EMA 9/21 crossover on a single instrument at 1H
generates too many false signals in choppy regimes. 28.6% win
rate -- below profitability threshold for this avg win/loss
ratio. Single-asset and 1H timeframe are both structural
mismatches vs the academic evidence base.

## Starting hypothesis

Time-series momentum (TSMOM) on an 11-instrument daily crypto
basket.

**Signal:** sign of the trailing 126-day return for each
instrument (6-month formation period per Hurst/Ooi/Pedersen
2017). Go long when trailing-126d return is positive. Close
when trailing-126d return turns negative.

**Vol-targeting per instrument (Barroso & Santa-Clara 2015):**
position size = (target_vol_annual / realized_vol_126d) x
(1 / N) of portfolio capital, where N = number of active
instruments (read from manifest symbols list at runtime --
never hardcoded). Long-only on spot substrate.

**Rebalance:** daily.

**Basket:** read from manifest entry TrendFollowing_multi
symbols list at runtime. Current list (11 instruments, all
OKX USDT spot with 3+ years history): BTC/USDT, ETH/USDT,
SOL/USDT, BNB/USDT, XRP/USDT, ADA/USDT, AVAX/USDT, DOT/USDT,
LINK/USDT, LTC/USDT, UNI/USDT. Future variations
may use a different basket by updating the manifest symbols
list with documented rationale -- code reads manifest, never
hardcodes symbols.

MATIC/USDT removed -- renamed to POL Sept 2024, insufficient
OKX 1d history for 38-month window.

## Key structural differences from Phase 3c

| Dimension | Phase 3c | This hypothesis |
| --- | --- | --- |
| Timeframe | 1H | Daily |
| Instruments | 1 (BTC/USDT) | 11-asset basket |
| Signal | EMA 9/21 crossover | 126-day trailing return |
| Sizing | Fixed notional | Vol-targeted per instrument |
| Citation | None | Moskowitz+ 2012, Hurst+ 2017, Barroso+ 2015 |

## Harness changes required

- backtest/cpcv_common.py: new optional CPCVConfig fields
  strategy_warmup_candles (default 0) and
  min_tradeable_candles_per_block (default 30). When
  strategy_warmup_candles > 0, effective block count =
  min(n_blocks, total_candles // (strategy_warmup_candles +
  min_tradeable_candles_per_block)). For this strategy:
  warmup=126, min_tradeable=30, ~880 daily dev candles ->
  floor(880/156) = 5 effective blocks.
- holdout_manifest.json: new entry TrendFollowing_multi
  (additive only).
- backtest/engine_multi.py: new multi-asset daily engine.
- backtest/cpcv_multi.py: run_cpcv_multi with synchronised
  block splits across all symbols, portfolio-level block
  Sharpe.
- strategies/trend_following_multi.py: new strategy class.
- scripts/phase_4a_trendfollowing_smoke.py: smoke trial script
  (authored here, run separately after human review).

## Source citations

- Moskowitz, Ooi & Pedersen (2012) "Time Series Momentum"
  Journal of Financial Economics 104 -- foundational TSMOM,
  long-only variant, multi-asset
- Hurst, Ooi & Pedersen (2017) "A Century of Evidence on
  Trend-Following Investing" Journal of Portfolio Management
  -- multi-asset daily execution, 6-month formation period
- Barroso & Santa-Clara (2015) "Momentum has its moments"
  Journal of Financial Economics 116 -- 126-day realized vol
  for per-instrument position scaling

## Variation discipline

Per CLAUDE.md:
- 20-variation cap; this is variation #1 (post-Phase-3c)
- 3-consecutive-failure escalation
- Variations beyond #1 require their own source-cited
  justification appended here before trial
- Different basket or timeframe = different variation with
  its own citation
- Pre-justified batch authority covers this starting
  hypothesis only
