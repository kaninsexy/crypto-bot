# Validation Framework (Phase 3b)

Last updated: 2026-04-25

This document describes the statistical validation framework that will gate
every strategy and the portfolio as a whole before anything moves toward
deployment. It is the authoritative reference for what "validated" means on
this project. Modifications require human approval (see `CLAUDE.md`).

## Three-way data split

- **Training (50%)** — used freely for development, parameter search, bug
  fixing, feature engineering.
- **Validation (30%)** — used for CPCV path construction and for early
  rejection of strategies that do not survive cross-validation.
- **Holdout (20%)** — the most recent slice of the data, chosen deliberately
  to reflect current market conditions.

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

## CPCV — Combinatorial Purged Cross-Validation

CPCV is the primary cross-validation method. Instead of producing a single
Sharpe number for a strategy, it produces a *distribution* of Sharpe values
across many reconstructed backtest paths.

Standard CPCV mechanics:

- Split the training+validation span into N non-overlapping blocks.
- Form all combinations where k blocks are held out and N−k blocks are used
  for training.
- Purge overlapping periods around the held-out blocks to prevent leakage
  from lookahead in feature engineering.
- Embargo a short period after each held-out block to prevent leakage from
  serial correlation.

Research finding from the López de Prado family of papers: CPCV materially
outperforms walk-forward validation in false-discovery prevention, because
walk-forward produces a single path and a single Sharpe that is easy to
cherry-pick.

The number of paths N and the held-out count k are parameters — Phase 3b
will calibrate both empirically (see Thresholds).

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

## What this framework is not

It is not a feature store, not a research notebook, and not a general-purpose
analytics layer. It is a gate. Every surface it exposes is in service of
answering one question per strategy: *does this clear holdout DSR, yes or no?*
Keeping the surface narrow is intentional; it makes the harness simple to
audit and hard for agents to accidentally invalidate.
