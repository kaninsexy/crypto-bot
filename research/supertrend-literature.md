# Supertrend — Phase 4.A resurrection hypothesis-of-record

**Date:** 2026-04-29
**Phase:** 4.A drop-in batch
**Status:** Starting hypothesis (pre-trial)

## Phase 3c failure (2026-04-26)

RETIRE, observed_sharpe −1.6388 / dist mean −1.6235, n_trades 302 on ETH/USDT 1H. Net-losing in dev. See `docs/strategy_evidence_audit_2026-04-26.md`.

## Starting hypothesis

Three structural design changes applied jointly:

1. **Daily timeframe** in place of 1H. Per `docs/MASTER_PLAN.md` § "Timeframe-per-strategy principle", 1H is a Phase 3c artifact, not a project commitment. Daily reduces signal density and removes the chop regime where Supertrend's flip-then-give-back loss pattern dominates (3-year audit: 29.5% win rate, avg loss > avg win on 1H).

2. **Barroso & Santa-Clara (2015) vol-scaling**: position size scaled by `target_vol / rolling_vol_30d`. Source: Barroso & Santa-Clara, "Momentum has its moments", *Journal of Financial Economics* 116 (2015). Phase 3d also adopts this; Supertrend's resurrection is a direct application.

3. **Regime-gated to trending only** using the existing 6-regime detector. Supertrend is a trend-following indicator; firing in range/chop is structural mismatch.

## Source citations

- Barroso & Santa-Clara (2015), JFE — vol-scaling
- `docs/strategy_evidence_audit_2026-04-26.md` — empirical basis for 1H rejection
- Per-strategy timeframe principle (`docs/MASTER_PLAN.md`)

## Variation discipline

Per `CLAUDE.md`:
- 20-variation cap; this is variation #1 (post-Phase-3c)
- 3-consecutive-failure escalation
- Variations beyond #1 require their own source-cited justification appended here before trial
- Pre-justified batch authority covers this starting hypothesis only

## Note on the indicator's provenance

The Supertrend indicator has no peer-reviewed foundation (Olivier Seban, 2009). The hypothesis is that vol-scaled, regime-gated, daily-TF execution can produce positive edge *despite* the indicator's weak provenance, by removing the structural failure modes the Phase 3c result diagnosed. If this fails, the lack of academic foundation becomes the dominant prior and further Supertrend variations should be capped well below 20.

## Trial #1 outcome (2026-04-29)

**Status:** Retired. No variation #2 queued.

**Hypothesis tested as written.** All three structural changes from the
starting hypothesis were applied jointly: daily TF (resampled internally
from the manifest's 1h frame), Barroso & Santa-Clara (2015) vol-scaling
on position size, and 6-regime gate restricting long entries to
STRONG_BULL ∪ BULL.

### Validation harness outcome

CPCV-10 (the Phase 3c block count, retained for trials.log
comparability) raised `CPCVError` because every one of the 10 dev-window
blocks fell under the harness's `_MIN_TRADES_PER_BLOCK = 5` floor:

```
per-block trades:  [1, 1, 0, 1, 1, 1, 1, 0, 1, 2]   total = 9
per-block Sharpe:  [+0.44, -3.43,  0,  +5.17, -0.87, +0.06, +2.70,  0, +3.29, -0.02]
blocks ≥ 5 trades:  0 / 10  →  >50% NaN, harness aborts
```

The structural cause is daily-TF trade density: ~13 trades over ~880 dev
days produces 88-day blocks with ~1.3 trades each.  This is intrinsic to
daily-TF Supertrend on a single-asset dev window — not a bug or
mis-tuning.

### Headline run (single full dev-window backtest)

| metric | value |
|---|---|
| Sharpe | +1.1182 |
| baseline (ETH B&H) Sharpe | +0.6836 |
| n_trades | 13 |
| return | +26.39% |
| max DD | 11.59% |
| win rate | 46.1% |
| profit factor | 2.78 |
| avg win / avg loss | +25.2% / −7.3% |

The headline result clears the buy-and-hold floor by +0.43 Sharpe.
However, the verdict tree's `min_trade_count = 30` precondition fires
on n=13: the strategy is `under_tested` regardless of headline-Sharpe
margin.  The trade-count floor exists because per-bar MinTRL alone
under-detects low-trade-frequency strategies (see
`backtest/verdict.py` § "Why MinTRL gate is paired with a trade-count
floor").

### trials.log row

Appended as `trial_type="smoke"` with `variation_id =
"phase4a-daily-resurrection-v1"` because the harness could not
produce a valid CPCV block-Sharpe distribution (full_cpcv schema
requires it).  Smoke is excluded from `count_trials_for_dsr` per
docs/validation_framework.md, so this trial does not inflate the
multiple-testing correction for any later Supertrend variation;
`count_distinct_variations("Supertrend")` did increment from 1 to 2,
consuming one slot of the 20-variation cap.

### Why no variation #2

The hypothesis-of-record's pre-condition (above, "Note on the
indicator's provenance") triggered: Supertrend has no peer-reviewed
foundation, so a single failed structural-change variation is enough
to make the lack-of-academic-foundation prior dominant.  Further
parameter sweeps would burn iteration-cap slots and (if any reached
full_cpcv) DSR multiple-testing inflation in service of an indicator
without a proper edge theory.

The variation budget for Supertrend is therefore capped at 1 attempt
post-Phase-3c.  The strategy is retired for Phase 4.A; Branch C of the
MASTER_PLAN strengthens for this strategy.

### Reusable lesson

Daily-TF strategies on the current single-asset dev window face a
structural density floor: 880 days × 1 asset ÷ 10 CPCV blocks × ≥5
trades/block ⇒ minimum signal cadence of one trade per ~17.6 days for
the harness to validate.  Strategies whose theory implies daily-or-
slower entry cadence on a single asset cannot pass CPCV-10 without
either (a) multi-asset breadth multiplying trades-per-block, or (b) a
manifest re-cut that reduces dev breadth in favour of harness fit
(sacred-harness change, not casually invokable).  This constraint
applies to any future single-asset, daily-TF resurrection candidate;
candidates pre-flagged as low-cadence should consider multi-asset
designs from the start.
