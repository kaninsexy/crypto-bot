# Kill report — FundingRateHarvest_BTC (retired 2026-06-11)

Archived per CLAUDE.md archive-by-default (no deletions). Retirement is
agent-autonomous under the "data clearly negative" rule; the deciding
evidence is the 2026-06-11 extended-window re-test under gate spec v2.

## What the strategy was

Delta-neutral BTC carry: long spot BTC/USDT + short perp BTC-USDT-SWAP,
equal notional, income = positive funding paid to shorts (8h cadence,
OKX). Strongest peer-reviewed support of any candidate at scoping
(Schmeling, Schrimpf & Todorov, BIS WP 1087 "Crypto Carry"; Almeida et
al. 2024 arXiv 2410.15195v2 for the V2b vol-regime gate).

## Trial history

| variation | window | result |
|---|---|---|
| V1 `phase4b-deltaneutral-singlepair-btc-v1` | dev 2023-05→2025-09, holdout 2025-09→2026-04 | dev KEEP (dev_cpcv mean +5.17, dsr_validation ~1.0 — units-invalid per gate spec v2); holdout RETIRE 2026-05-02 (holdout sharpe +0.35, dsr_holdout 0.0054). Mechanism worked (funding collected as designed); economics failed out-of-window. |
| V2b `phase4b-volregime-conditional-singlepair-btc-v2b` | dev 2023-05→2025-09, holdout 2025-09→2026-05 | structural redesign (LV-vol-regime gate). Dev KEEP (sharpe +2.90, gates pre-units-fix); holdout RETIRE 2026-05-08 (sharpe −1.14 vs B&H −0.90, n_trades 14). |
| V2b extended-window re-test (gate spec v2) | dev 2021-08-31→2025-05-01 (44.0 months, funding-archive-bound) | trial 2567dbd3eb9a442986a5f83a1ceddd7e, 2026-06-11: dev sharpe **+0.5007**, CPCV p50 +0.056, family-scaled DSR **0.044** vs ≥0.95 gate, forensic neutral PSR ~0.76 < 0.95, MinTRL at realized SR ≈ 179k bars (~20.5y). Verdict under_tested by precondition; every quality signal negative. |

## Why retired

The carry edge is a regime artifact. Both dev KEEPs were earned on
2023–2024 bull-heavy windows whose funding pools were rich; both
holdout windows and the extended dev window (which adds the
2021-09→2022 carry-pool collapse) remove the edge. The vol-regime gate
(V2b) did not rescue it: the LV-regime filter still collects nothing
over the longer window. Consistent with Schmeling et al.'s own
sub-sample decay (Sharpe 6.45 full-sample → negative in 2025) and with
the 2026-06 gate-recalibration audit (the original dsr_validation
values that justified the dev KEEPs were units-invalid).

Per the literature's structural-redesign gate, a V3 would need a NEW
structural mechanism from a specific paper. Two structural designs have
now failed across three windows; no further variation is queued. 17 of
20 variation-cap slots unused — this is an evidence-based retire, not a
cap exhaustion.

## What is archived here

- `funding_rate_harvest.py` — strategy implementation (moved from
  `strategies/`).
- `test_funding_rate_harvest.py` — its unit tests (moved from
  `backtest/tests/`; not collected by the main suite).

Still in place elsewhere (shared harness, NOT strategy-specific):
`backtest/engine_perp.py`, `backtest/cpcv_perp.py`,
`paper_trading/perp_simulator.py`, `data/okx_perp.py`,
`data/okx_funding.py` — these serve any future two-leg strategy.
Trial scripts `scripts/phase_4b_*.py` remain with imports updated to
the archive path (runnable for forensics).

## Re-test cooldown

Per MASTER_PLAN Phase 4.C loop rules: 30d initial cooldown before any
re-test proposal; a revival needs a new structural hypothesis + 
citation, not a parameter change.
