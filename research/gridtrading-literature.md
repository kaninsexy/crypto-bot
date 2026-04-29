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

## Trial #1 outcome (2026-04-29)

**Status:** Retired. No variation #2 queued.

### Trial run

- **Driver:** `/tmp/grid_phase4a_trial1.py` (mirror of
  `backtest/runner.py:_run_strategy_dev_cpcv` with explicit
  `variation_id="phase4a-regime-conditional-v1"`, explicit
  `params` dict, and explicit hypothesis text).
- **trials.log row:** `trial_id=eb18277d7970402da6c0a942b199be6a`,
  `params_hash=7d58b0fbc7f2d5efeeb478151668bed341682037c5c0ba30cd31653441c7d67f`,
  `git_commit=5dc593e`, `trial_type=full_cpcv`,
  `n_trials=RESCUE_TRIAL_BUDGET=20` (Phase 3c convention; symmetric
  with the rescue-default row's DSR threshold).
- **Pair / config:** SOL/USDT 1H, manifest entry unchanged. Phase 3c
  factory params held constant (bb_period=20, bb_std=2.0,
  atr_period=14, atr_step_mult=0.75, atr_trend_threshold=2.5,
  grid_levels=10, usdt_per_trade=200, recalibrate_every=24,
  btd_mode=False, trailing_grid=False). Regime gate is the sole
  structural change.
- **Detector:** `portfolio.regime_detector.RegimeDetector` reading
  the strategy's own SOL/USDT df (asset-specific regime per
  Chen/Chen/Jang 2025 interpretation), warmup floor 210 candles,
  no confidence threshold.

### Validation harness outcome

| metric | value |
|---|---:|
| observed_sharpe | +0.8805 |
| sr_zero_expected (N=20) | +1.9007 |
| dsr_validation | 3.71e-15 |
| mintrl_required | 1218.72 bars |
| t_observed | 20278 bars |
| baseline_sharpe (SOL/USDT B&H) | +1.8133 |
| n_trades | 601 |
| dist mean | +2.3955 |
| dist std | 1.3716 |

Per-block trades: `[63, 64, 43, 63, 44, 57, 62, 55, 73, 44]`.
All 10 blocks valid — no CPCVError, no NaN blocks. The gate did
its job: total trade count fell from Phase 3c's 1035 to 601
(−42%), and crucially every block stayed above
`_MIN_TRADES_PER_BLOCK = 5` so the harness produced a clean
verdict.

Per-block Sharpes:
`[+2.87, +2.39, +1.85, +3.36, +2.96, +1.65, +4.92, +2.26, −0.78, +2.46]`.
Distribution mean +2.40 looks strong but the headline single-pass
Sharpe of +0.88 tells a different story: block-isolated runs
(fresh strategy state per block, no cross-block position carry,
favorable boundary effects) inflate per-block Sharpes versus what
a continuous run achieves. The headline number is what gates the
verdict tree.

### Verdict tree breakdown

| precondition | result |
|---|---|
| trade_count_pass (n_trades=601 ≥ 30) | **True** |
| mintrl_pass (1218 < 20278) | **True** |

| quality gate | result | margin |
|---|---|---:|
| mt_mean_pass (sharpe > sr_zero_expected) | **False** | −1.02 |
| baseline_pass (sharpe > B&H) | **False** | −0.93 |

**Verdict: RETIRE.** Both quality gates fail decisively — well
outside the ±0.05-of-threshold borderline band that would warrant
a chat consult.

### Headline single-pass run forensic

Total return +0.61% over the 868-day dev window (annualised ≈
+0.26%). Max DD 0.24% — the gate is highly restrictive, leaving
the strategy mostly dormant. Win rate 75.5% but profit factor
only 1.46 (wins are small relative to losses, consistent with
grid mechanics: many small wins on grid-step bounces, rarer
larger losses when range is broken).

The economic story is clean: the gate keeps GridTrading out of
trending and crash regimes (Phase 3c's −46% loss territory) but
in doing so it leaves the strategy active for too short a fraction
of the dev window to compound a meaningful headline return. The
strategy was held up by its random-walk-EV-zero ceiling: removing
the trending-regime drag exposed that the conditional firing's
upside is also bounded.

### Why no variation #2

Per the no-p-hacking rule, variation #2 needs a source-cited
justification. Candidate variations and their citation status:

- **Different gate (e.g., add VOLATILE or BULL):** No source
  supports a multi-regime gate; Chen/Chen/Jang 2025's analysis is
  RANGE-specific.
- **Different lookback/recalibrate cadence:** No source justifies
  perturbing the BB(20) / ATR(14) / recalibrate(24) constants
  from this run; they are crypto-trading-bot conventions, not
  academically optimised.
- **Different basket (multi-pair grids):** No source supports
  grid trading on a basket; Chen/Chen/Jang single-asset frame.
- **Different gate confidence threshold:** Possible, but the
  6-regime detector's confidence is a free parameter without
  external calibration source. Tuning it on dev would be
  parameter-dredging.

None clear the no-p-hacking bar. The strategy is retired for
Phase 4.A. Branch C of `MASTER_PLAN.md` strengthens for this
strategy — GridTrading stays out of any deployed portfolio.

`count_distinct_variations("GridTrading")` is now 2 / 20 with the
cap effectively closed; `count_trials_for_dsr("GridTrading")`
advances from 1 to 2 (this trial is `full_cpcv`, not smoke,
unlike the prior two Phase 4.A trials).
