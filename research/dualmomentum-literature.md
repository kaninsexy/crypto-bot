# DualMomentum — Phase 4.A resurrection hypothesis-of-record

**Date:** 2026-04-29
**Phase:** 4.A drop-in batch
**Status:** Starting hypothesis (pre-trial)

## Phase 3c failure (2026-04-26)

RETIRE, observed_sharpe −2.3906 / dist mean −2.2850, n_trades 1095 on BTC/USDT (rotating BTC/ETH/BNB) at 1H. The 1H + 3-asset config contracts the Antonacci/Liu/Tsyvinski/Wu framework by ~720× (12-month formation → 21-hour formation). See `docs/strategy_evidence_audit_2026-04-26.md`.

## Starting hypothesis

**Weekly timeframe with 3-week formation period, applied to a ≥5-major basket.**

Source: Liu, Tsyvinski, Wu (2022) "Common Risk Factors in Cryptocurrency", *Journal of Finance* 77(2). Establishes weekly momentum factor in crypto cross-section using 3-week formation — closest peer-reviewed analog to dual-momentum in crypto, and the only one at a frequency the bot can implement.

Antonacci (2014) provides the dual-momentum framework (relative-strength rotation + absolute-momentum filter). Liu/Tsyvinski/Wu establishes the relative-strength leg empirically in crypto; the absolute-momentum filter (asset return must beat cash/risk-free) carries over from Antonacci directly.

## Basket specification

Top-5 by market capitalisation at evaluation time, refreshed monthly. Current snapshot (2026-04-29): BTC, ETH, BNB, SOL, XRP. The mcap-rank rule prevents lookahead bias from a fixed historical pick.

## Source citations

- Liu, Tsyvinski, Wu (2022), JF 77(2) — weekly crypto momentum factor
- Antonacci (2014) — dual-momentum framework
- Asness, Moskowitz, Pedersen (2013), JF 68(3) — cross-asset momentum generality
- `docs/strategy_evidence_audit_2026-04-26.md` — empirical basis for 1H/3-asset rejection

## Variation discipline

Per `CLAUDE.md`:
- 20-variation cap; this is variation #1 (post-Phase-3c)
- 3-consecutive-failure escalation
- Variations beyond #1 require their own source-cited justification before trial
- Pre-justified batch authority covers this starting hypothesis only

## Open implementation question (BLOCKS TRIAL)

The current holdout manifest entry for DualMomentum covers BTC/ETH/BNB. Adding SOL and XRP changes the manifest symbol set. The manifest is sacred-harness; per `CLAUDE.md` this requires explicit human approval before the trial runs. **This stub is committable now; the manifest update is a separate gate that must clear before the dev_cpcv runs.**

(Resolved 2026-04-29: manifest regenerated to 5-symbol basket
[BTC, ETH, BNB, SOL, XRP] before trial #1 ran.  XRP / DOGE / ADA /
TRX / LINK 1h × 36mo caches were pre-loaded via the existing
`load_or_download_ohlcv` + `download_history` path.  Dev window
shrunk from 125.14 weeks (3-symbol) to 124.01 weeks (5-symbol),
0.90% — well under the 10% pre-flight gate.  See
`backtest/holdout_access.log` regen event.)

## Trial #1 outcome (2026-04-29)

**Status:** Retired. No variation #2 queued.

### Hypothesis tested as written

Five-major basket `[BTC/USDT, ETH/USDT, BNB/USDT, SOL/USDT, XRP/USDT]`
on the manifest's 1h frame, candle-count approximation of weekly TF:
- `lookback = 504` (3 weeks × 7 days × 24 hours, equivalent to a
  3-week formation window per Liu/Tsyvinski/Wu 2022).
- `rebalance_every = 168` (1 week × 7 days × 24 hours, weekly
  re-rank cadence).
- `regime_filter = False` (no external regime feeder in this trial;
  the strategy's intrinsic absolute-momentum filter still applies —
  HOLD when the top-ranked asset's lookback return is negative).

The candle-count approximation is mathematically equivalent to literal
weekly TF: `(close[t] − close[t−504]) / close[t−504]` on 1h data is
identical to `(close[w] − close[w−3]) / close[w−3]` on weekly data for
any aligned `t` / `w`. Liu/Tsyvinski/Wu 2022 does not require a
calendar-Monday anchor.

### Validation harness outcome

CPCV-10 (Phase 3c match: `n_blocks=10, k_held_out=2, purge=0,
embargo=0`) raised `CPCVError`: *"more than 50% of blocks have
insufficient trades; CPCV unreliable (valid 4/10 blocks)"*.

Per-block trade counts:

| block | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| trades | **5** | 3 ✗ | 2 ✗ | **9** | 4 ✗ | **5** | 2 ✗ | 4 ✗ | **5** | 3 ✗ |
| Sharpe | −0.379 | −3.241 | +2.020 | −0.689 | +0.575 | −2.477 | +5.293 | −1.750 | −0.909 | +2.702 |

Six of ten blocks fell under `_MIN_TRADES_PER_BLOCK = 5`; the
harness's `> 50% NaN` check (`backtest/cpcv.py:564`) aborts. Valid-
block Sharpes (where trades ≥ 5): blocks 0, 3, 5, 8 → mean ≈ −1.11.

Total trades across all blocks: 42 (vs 44 on the single-pass
headline run — the 2 missing reflect block-edge boundary effects
where a position is force-closed at `backtest_end` of one block and
not re-opened in the next).

### Headline single-pass dev backtest (full window, before CPCV)

| metric | value |
|---|---|
| Sharpe | −1.1973 |
| n_trades | 44 |
| return | −16.09% |
| max DD | 20.17% |
| win rate | 29.6% |
| profit factor | 0.430 |

Active-symbol distribution (BUY signals from smoke v2): BTC 11.4%,
ETH 13.6%, BNB 22.7%, SOL 31.8%, XRP 20.5% — strategy rotates as
designed, no single-symbol dominance.

### trials.log row

Appended as `trial_type="smoke"` with `variation_id =
"phase4a-weekly-5basket-v1"` because the harness could not produce a
valid CPCV block-Sharpe distribution (full_cpcv schema requires
`cpcv.sharpe_distribution`). Same precedent as Supertrend trial #1
(commit `d29e604`): when CPCV raises `CPCVError` for reasons
intrinsic to the variation, smoke captures the headline data point
without inflating multiple-testing accounting. Smoke is excluded
from `count_trials_for_dsr` per Phase 3b Chunk 5;
`count_distinct_variations("DualMomentum")` advanced from 1 to 2,
consuming one slot of the 20-variation cap.

### Why no variation #2

Academic-foundation-exhausted precondition. Liu/Tsyvinski/Wu 2022 is
the strongest peer-reviewed source for crypto weekly momentum on a
multi-major basket; Antonacci 2014 is the canonical dual-momentum
framework. The trial's parameters (3-week formation, weekly
rebalance, top-1 from a top-5-by-mcap basket) are at the academic
standard from these sources. Any variation that perturbs lookback,
rebalance cadence, or basket composition would lack a citation —
parameter sweeps without per-variation justification are exactly
what CLAUDE.md's no-p-hacking rule forecloses, since each variation
inflates the DSR multiple-testing correction.

The strategy is retired for Phase 4.A. Branch C of `MASTER_PLAN.md`
strengthens for this strategy.

### Structural finding (separate from the strategy's economic verdict)

Block-isolated CPCV pays the strategy's formation/warmup period in
*every* block. This trial's `lookback = 504` candles is paid against
each block's ~2078 candles (10 blocks of the 20,788-candle dev
window), so 24% of every block is consumed by warmup before the
strategy can re-rank for the first time. Long-formation strategies
on the current dev-window length structurally face this density
floor regardless of how many trades the strategy fires on a single
full-dev-window pass.

This is the second occurrence of the pattern (Supertrend trial #1
also failed for warmup-amortization reasons, though with a more
extreme density issue). Cross-referenced as an open
harness-design question in `docs/open_questions.md` § "Block-
isolated CPCV warmup amortization (structural)". Tracked
separately from per-strategy retirement decisions.
