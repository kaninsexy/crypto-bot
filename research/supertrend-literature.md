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
