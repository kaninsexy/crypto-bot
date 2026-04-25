# Validation Framework (Phase 3b)

Last updated: 2026-04-25

This document describes the statistical validation framework that will gate
every strategy and the portfolio as a whole before anything moves toward
deployment. It is the authoritative reference for what "validated" means on
this project. Modifications require human approval (see `CLAUDE.md`).

## Two-way data split (80 / 20)

Each strategy's full cache window is divided into two non-overlapping halves
by a single calendar cut computed from the actual data bounds in the parquet
cache:

```
holdout_start = data_start + 0.80 × (data_end − data_start)
```

- **Dev window `[data_start, holdout_start)`** — the first 80 % of the
  available history.  Used freely for development, parameter search, bug
  fixing, CPCV path construction, and iterative Sharpe estimation.
- **Holdout window `[holdout_start, data_end)`** — the most recent 20 %,
  chosen deliberately to reflect current market conditions.

For multi-symbol strategies (DualMomentum) the cut is computed on the
intersection window: `data_start = max(per-symbol starts)`,
`data_end = min(per-symbol ends)`.

The holdout is sacred. It is accessed **exactly once per strategy**, at the
end of that strategy's rescue iteration, to produce a final DSR that
determines keep / monitor / retire. Any earlier peek invalidates the split —
if a holdout peek happens, the holdout must be discarded and the split
redrawn (which is expensive and pushes out the schedule).

## Process

### Iterative vs deploy-gate testing

Two test tiers during Phase 3c rescue work:

- **3-month smoke test.** Fast iteration, used to filter obvious failures and
  sanity-check parameter variations. Not a validation signal — purely
  diagnostic. Appends to `trials.log` but smoke-only trials are tagged and
  excluded from DSR multiple-testing correction.
- **3-year full backtest + CPCV + holdout DSR.** The actual deploy gate. A
  strategy that passes 3-month smoke must still pass this before any
  Phase 4 paper deploy.

Agents may freely run 3-month smoke tests during iteration. Running 3-year
CPCV is compute-intensive and should be reserved for candidates that survive
smoke.

## Block Sharpe distribution

The dev window is split into N non-overlapping equal-row blocks. The
strategy runs through each block as a standalone `engine.run()` call
with a fresh `strategy_factory()` instance — no state carryover
between blocks. Each block produces a Sharpe via the same formula as
`backtest/engine.py:_compute_metrics`. The resulting N-element Sharpe
distribution is the input to DSR.

### Why this rather than López de Prado's path-reassembly CPCV

The standard CPCV path machinery generates path variance by fitting
different models on different combinations of training blocks and
producing different predictions on held-out blocks. That is an ML
construct — for rule-based strategies with no fit, the same block
data produces the same returns regardless of which combination it is
nominally part of, so all reconstructed paths collapse to identical
Sharpes. Running the engine on concatenated held-out blocks per
combination would produce non-degenerate variance, but only via
artificial time-adjacency at the gluing boundaries — a leakage
artifact, not a property of the underlying data.

The block Sharpe distribution avoids both failures. Blocks are
independent runs with fresh strategy state, the variance reflects
genuine across-period dispersion of strategy performance, and the
distribution feeds DSR with honest inputs. It is structurally similar
to walk-forward validation but produces N Sharpes per strategy
instead of one, which preserves the multi-sample basis DSR requires.

### Block construction

- Equal-row split into N = 10 blocks (configurable; calibration
  pending in Phase 3b step 6).
- For multi-symbol strategies (DualMomentum), the split is on the
  timestamp intersection across symbols so per-symbol blocks cover
  the same calendar window.
- Each block must clear MIN_TRADES_PER_BLOCK = 5 to contribute. A
  block with fewer trades produces NaN.
- If more than 50 % of blocks are NaN, `run_cpcv` raises CPCVError;
  the result is statistically unreliable.

### Purge and embargo

- `purge_periods` bars are zeroed at the start of each block's return
  series before Sharpe computation, neutralising
  feature-engineering windows that straddle the boundary.
- `embargo_periods` bars are zeroed at the end of each block before
  Sharpe computation, absorbing serial-correlation leakage.
- Defaults are 0 pending Phase 3b empirical calibration.

### Reserved configuration

`CPCVConfig.k_held_out` is preserved in the configuration dataclass
but is unused in block-Sharpe mode. It remains for forward
compatibility if a fit/predict-capable strategy class is added in
the future, at which point true path-CPCV may be re-introduced
alongside this approach.

## Deflated Sharpe Ratio — the accept/reject gate

Formula source: Bailey & López de Prado (2014), "The Deflated Sharpe Ratio".

DSR adjusts an observed Sharpe for:

- **Number of trials** — how many variations have been tried (from `trials.log`).
- **Skewness** of the returns distribution (negative skew penalises).
- **Kurtosis** of the returns distribution (fat tails penalise).
- **Sample size** — short track records penalise.

The output is a probability that the observed Sharpe is not a false positive
given the multiple-testing context. This probability is the keep / reject
gate.

## Thresholds

To be empirically calibrated in Phase 3b. Plan: run DSR against known-signal
and known-noise synthetic cases at thresholds 0.80, 0.85, 0.90, 0.95, and
pick the level that correctly separates signal from noise on that test bench.

Default tiers, pending calibration:

- DSR ≥ 0.95 — production sizing (Kelly active, full allocation within caps).
- 0.90 ≤ DSR < 0.95 — keep-for-monitoring (sized at a floor, paper only).
- DSR < 0.90 — retire.

These defaults may be tightened or loosened after calibration. Calibration
itself is an agent-autonomy item (see `CLAUDE.md` — "empirically calibrate
thresholds").

## `trials.log`

An append-only record of every experiment that touches validation data. One
row per experiment, capturing at minimum:

- Timestamp.
- Strategy name and variation ID.
- Parameters used.
- CPCV path count and CPCV Sharpe distribution summary (mean, std, quantiles).
- DSR on validation and, where applicable, DSR on holdout.

Every variation counts toward multiple-testing correction via the trial
count in the DSR formula. This is why the file is append-only and why it
**must not be edited or rewritten by agents**. It binds agent enthusiasm
to statistical reality: if an agent wants to try a 21st variation, the DSR
bar is already higher than it was at variation 1, so the variation has to
work harder to clear it. That is the intended behaviour.

The schema for `trials.log` is part of the sacred harness; schema changes
require human approval (per `CLAUDE.md`).

## Minimum Track Record Length (MinTRL)

Secondary sanity check, also from Bailey & López de Prado. MinTRL estimates
how many observations are required for an observed Sharpe to be statistically
distinguishable from zero at a given confidence level. If a strategy has
fewer observations than MinTRL, it is flagged as *under-tested* rather than
passed or failed. An under-tested strategy can be kept on paper for further
data collection, but it does not clear the deploy gate on its own.

## Baseline comparison

Every strategy's Sharpe must beat a buy-and-hold Sharpe on its trading pair
over the same period. This is a floor, not a ceiling. A strategy that merely
matches buy-and-hold is not adding value relative to the passive alternative.

For portfolio-level validation (Phase 3d), the baseline is an equal-weighted
passive portfolio of the same instruments. The combined strategy portfolio
must beat that passive basket on Sharpe and DSR both.

## Regime attribution

Any strategy that survives holdout DSR gets per-regime Sharpe analysis across
BULL / BEAR / CHOP / CRASH. Those per-regime Sharpes feed into
`REGIME_PRIORS`, which is the input to regime-aware Kelly sizing built in
Phase 2c. `REGIME_PRIORS` is currently empty — this step is what fills it.

## Infrastructure

The holdout split is managed by a small set of dedicated modules.  Do not
replicate their logic elsewhere; go through these interfaces.

| File | Role |
|------|------|
| `backtest/holdout_manifest.json` | Source of truth for every strategy's `data_start`, `dev_end`, `holdout_start`, `data_end`, and `timeframe`. Generated once; updated only via `regenerate_manifest()`. |
| `backtest/holdout_access.log` | Append-only audit log.  Every call to `load_holdout` appends one access event; every manifest regeneration appends one `regenerated=true` event. |
| `backtest/holdout.py` | Public accessors: `load_dev(strategy_id)` (unrestricted), `load_holdout(strategy_id, caller=…, reason=…)` (single-access enforced). Also exports `load_manifest()`. |
| `backtest/generate_holdout_manifest.py` | `generate_initial()` for first-time setup; `regenerate_manifest(strategies=None)` to redraw the split after cache refresh. CLI: `python -m backtest.generate_holdout_manifest init \| regen`. |
| `backtest/cache.py` | `load_or_download_ohlcv` enforces the holdout boundary at read time.  Pass `until_ts=get_symbol_dev_cutoff(symbol)` to restrict to the dev window.  Raises `HoldoutBypass` if holdout rows are returned outside an authorised context. |

Modifying `holdout.py`, `holdout_manifest.json`, or `holdout_access.log`
schema requires human approval (see `CLAUDE.md`).

## What this framework is not

It is not a feature store, not a research notebook, and not a general-purpose
analytics layer. It is a gate. Every surface it exposes is in service of
answering one question per strategy: *does this clear holdout DSR, yes or no?*
Keeping the surface narrow is intentional; it makes the harness simple to
audit and hard for agents to accidentally invalidate.
