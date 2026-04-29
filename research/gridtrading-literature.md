# GridTrading — Phase 4.A resurrection hypothesis-of-record

**Date:** 2026-04-29
**Phase:** 4.A drop-in batch
**Status:** Starting hypothesis (pre-trial)

## Phase 3c failure (2026-04-26)

RETIRE, observed_sharpe +1.5004 / dist mean +2.3377, n_trades 1035 on SOL/USDT 1H. Beat zero but failed multiple-testing null at N=20. The unconditional grid configuration is consistent with Chen/Chen/Jang (2025): essentially-zero EV pre-fees under symmetric random walk.

## Starting hypothesis

**Gate firing on range / low-trend / mid-vol regimes only; otherwise dormant.**

Chen/Chen/Jang (2025) establishes that *unconditional* grid trading has zero EV under symmetric-random-walk price dynamics. Real markets are not symmetric random walks; they exhibit regime-dependent dynamics that the unconditional model averages over:

- **Range regimes** are mean-reverting by construction; grid orders systematically buy weakness and sell strength against a mean-reverting drift — a positive-edge configuration that random-walk grids cannot capture.
- **Low-trend regimes** suppress the directional drift that bleeds grid PnL during sustained trends.
- **Mid-vol regimes** provide enough movement to fill grid levels (grid EV is zero in dead-quiet markets) without enough movement to break range bounds (grid EV is negative in breakouts).

The hypothesis: this conditional configuration captures regime-specific edge that the unconditional grid averages out. The strategy is *dormant* outside these regimes — no positions, no grid maintenance — so the multiple-testing surface is the conditional firing only.

## Source citations

- Chen, Chen, Jang (2025) — zero EV in unconditional case (negative result motivating the conditional design)
- Existing 6-regime detector — implementation already in place; gate is the only new code
- `docs/strategy_evidence_audit_2026-04-26.md` — empirical basis for unconditional retire

## Variation discipline

Per `CLAUDE.md`:
- 20-variation cap; this is variation #1 (post-Phase-3c)
- 3-consecutive-failure escalation
- Variations beyond #1 require their own source-cited justification before trial
- Pre-justified batch authority covers this starting hypothesis only

## Implementation note

Genuinely drop-in: existing GridTrading code remains; the only change is wrapping entry logic in a regime-conditional gate. No harness change. Holdout manifest entry for SOL/USDT is already present, no manifest update required.
