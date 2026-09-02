# Validation Framework (Phase 3b; gate spec v2 as of 2026-06-11)

Last updated: 2026-06-11 (gate spec v2 — pre-authorized edit per the
2026-06-11 work order implementing
`docs/gate_recalibration_audit_2026-06.md`)

This document describes the statistical validation framework that will gate
every strategy and the portfolio as a whole before anything moves toward
deployment. It is the authoritative reference for what "validated" means on
this project. Modifications require human approval (see `CLAUDE.md`).

## Gate spec v2 (2026-06-11)

> **Fee-model caveat on pre-Phase-4.E trials (recorded 2026-09-02).** Every
> trial recorded before Phase 4.E was costed with `paper_trading/simulator.py`
> defaults — `FEE_MARKET = 0.04%`, which is a Binance *futures* taker rate,
> plus 0.05% slippage — while running on a *spot* substrate whose real taker
> fee is 0.10% (OKX / Binance spot). Phase 4.E corrected this to 0.10% plus a
> 2× stress, and Phase 4.F uses the OKX perp taker rate (0.05%) + 0.05%
> slippage with the same 2× stress. Consequence: high-turnover pre-4.E rows
> (e.g. CrossSectionalReversal at 1432 trades, NewsSent at 2071 trades) are
> **fee-optimistic**, and their verdicts should be read with that caveat until
> re-checked at the corrected rate. This paragraph records the caveat; it does
> not re-score any recorded trial. Tracked in `docs/open_questions.md`
> § "Fee-model caveat on pre-Phase-4.E trials" and
> `docs/revival_handoff_2026-09-02.md` §2 item 4.

The 2026-06 gate-recalibration audit
(`docs/gate_recalibration_audit_2026-06.md`) found five defects in the
v1 gates; this section is the canonical record of the corrections.
Sections below are amended in place where v1 text was wrong; where v1
behaviour is described for historical context it is marked SUPERSEDED.

**1. Units fix (BLP eq. 9/13).** The eq.9 SR-estimator variance and
eq.13 MinTRL formulas operate in per-bar units; v1 plugged the
engine's ANNUALISED Sharpe into them, inflating the deflation z-score
by ~√(bars-per-year) (≈19× at 1d, ≈94× at 1h). Consequence: every
positive-Sharpe trial recorded `dsr_validation = 1.0` (saturated) and
absurdly small `mintrl` values (6–286 bars). v2:
`deflated_sharpe` / `min_track_record_length` take a REQUIRED
`bars_per_year` argument and convert internally
(`sr_pb = sr_ann / √bars_per_year`); result dataclasses echo both unit
systems; `CPCVResult.candle_duration_h` lets the adapters derive the
frequency exactly from the data's own index.

> **ALL `dsr_validation` and `mintrl` values persisted in
> `backtest/trials.log` BEFORE commit `2af7cd8` (2026-06-11) are
> units-invalid. They must not be compared against post-fix values,
> used as thresholds, or cited as probabilities/sample sizes. The
> rows themselves remain valid trial records (sharpe, n_trades,
> distribution stats are unaffected).**

**2. Cross-trial variance scaling (BLP eq. 7).** v1 computed
`sr_zero_expected` as the bare Gumbel term (implicit V[{SR_n}] = 1.0
in annualised units, and per-strategy N that equalled 1 for most
Phase-4 first variations — a double no-op). v2:

    sr_zero_expected = sqrt(V[{SR_n}]) × [(1−γ)Φ⁻¹(1−1/N) + γΦ⁻¹(1−1/(N·e))]

with V[{SR_n}] = realized population variance of observed annualised
Sharpes across trials in the SAME strategy family, and N = the
per-family trial count (+1 for the trial being deflated). The family
taxonomy lives in `backtest/strategy_families.json` (non-sacred;
ex-ante assignments by anomaly mechanism, never by observed Sharpe;
8 families per the audit's Appendix A). Families with < 2 finite
trials fall back to V = 1.0 (conservative) with a warning
(`backtest/families.py`).

**3. Verdict gates (composition layer, `backtest/verdict.py`).**
Preconditions unchanged in shape (trade/signal-event floor 30 +
MinTRL, now units-correct). Quality gates:

- *mt gate:* corrected DSR ≥ 0.95 (subsumes the v1 raw
  `SR > sr_zero_expected`; margins still recorded as forensics).
- *baseline gate, directional/long-only:* BOTH required —
  (a) OLS alpha of per-bar strategy returns on same-instrument
  buy-and-hold returns > 0 at 95 % one-sided with Newey-West (HAC)
  standard errors, and (b) annualised information ratio vs B&H ≥ 0.5.
- *baseline gate, market/delta-neutral* (taxonomy flag
  `"neutral": true`): PSR(SR > 0) ≥ 0.95 against benchmark 0 — a
  B&H comparison is meaningless for a delta-neutral book.

The v1 raw-Sharpe-beats-B&H comparison retired strategies for failing
to beat a 1.94-Sharpe bull-window B&H with no significance test
(audit §6 found three retirements that pass the corrected family-layer
gates). It survives only as the forensic fields
`baseline_sharpe_at_eval` / `sr_margin_vs_baseline`.

**4. Per-bar return persistence.** Every trial run persists its
per-bar strategy return series (and aligned benchmark series) to
`backtest/reports/per_bar_returns/<trial_id>.parquet` via
`backtest.trials.record_trial`'s optional per-bar arguments (wired in
`runner.py` and the trial-script template pattern). The audit's S1
and holdout-bootstrap analyses were impossible because these series
were never saved; rows recorded before 2026-06-11 remain
unrecoverable. The trials.log SCHEMA is unchanged — the parquet is a
sidecar keyed by `trial_id`.

**5. Event-based block sizing.** `CPCVConfig.block_mode = "event"`
(default `"calendar"`, unchanged behaviour) sizes blocks by equal
signal-event count via `locate_signal_events`, with a hard floor of
5 blocks × 5 events = 25 total events. Built for sparse event-driven
designs that died as CPCVError under calendar deciles (audit §3).
Single-symbol only this iteration.

**Expected consequence of the units fix — read before interpreting
new verdicts.** Units-correct MinTRL at annualised |SR| ≈ 1.0 is
≈ 2.71 YEARS of history regardless of bar frequency. The ~29-month
dev windows therefore render `under_tested` for most moderate-Sharpe
strategies (audit §4: minimum detectable annualised SR on the current
windows is ≈ 1.03–1.07). That is the honest reading of the data, not
a harness bug: claims the window cannot support are neither kept nor
retired.

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
determines the verdict (keep / retire / under_tested). Any earlier peek invalidates the split —
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

- Default (`block_mode="calendar"`): equal-row split into N = 10
  blocks (configurable; calibration pending in Phase 3b step 6).
- Gate spec v2 option (`block_mode="event"`): contiguous blocks sized
  by EQUAL SIGNAL-EVENT COUNT via `CPCVConfig.locate_signal_events`,
  minimum 5 blocks of 5 events (25 total). For sparse event-driven
  designs whose events concentrate into too few calendar blocks.
  Single-symbol only this iteration.
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
gate: under gate spec v2 the mt gate is **corrected DSR ≥ 0.95**, with
`bars_per_year` conversion and family-scaled `sr_zero_expected` per
§ Gate spec v2.

> SUPERSEDED (v1 historical note): the v1 framework observed that DSR
> "saturates to a step function at production T" and therefore gated
> on the raw binary `SR > sr_zero_expected(N)`. The 2026-06 audit
> showed that saturation was an artifact of the units bug (annualised
> SR in the per-bar eq.9 inflated z by ~√bars-per-year), not a
> property of DSR. Units-correct DSR discriminates properly — the
> zero-edge calibration test in `backtest/tests/test_dsr.py`
> (`test_zero_edge_synthetic_does_not_clear_095`) pins this. The v1
> `backtest/calibration.py` thresholds derived under the old units
> are likewise invalid for comparison against post-fix DSR values.

## Verdict tree

At production T (~20k bars), DSR collapses to a step function at
sr_zero_expected(N): strategies are either confidently above the
multiple-testing null (DSR ≈ 1) or confidently below (DSR ≈ 0), with a
transition band narrower than typical strategy run-to-run noise. The
probability is no longer a tunable gate. The verdict logic reflects this:
binary keep/retire on the quality side, with an under-tested precondition
for strategies that don't have enough data to render a verdict at all.

### Three signals (gate spec v2)

- Corrected DSR ≥ 0.95 — multiple-testing null with family-scaled
  eq.7 haircut and per-bar units. PASS = the observed Sharpe is
  confidently above what N family trials of trial-fishing would
  produce. FAIL = indistinguishable from MT noise.
- Baseline gate — directional: NW-alpha > 0 at 95 % AND annualised
  IR ≥ 0.5 vs same-instrument B&H; delta-neutral: PSR(SR>0) ≥ 0.95.
  PASS = the strategy adds statistically significant value over the
  passive alternative (or over zero, for a neutral book).
- MinTRL preconditions — bar-count via BLP eq. 13 (units-correct)
  plus a heuristic trade-count floor (total_trades >= 30, or
  signal_event_count >= 30 when supplied). PASS = enough data for
  the SR estimate to be statistically meaningful. FAIL = under-tested.

### Tree

    Precondition (compute first):
      if event_floor_fail OR T < min_trl:
          verdict = "under_tested"
          # quality bools not computed; recorded as None

    Quality (only if precondition passes):
      if DSR >= 0.95 AND baseline_gate_pass:
          verdict = "keep"
      else:
          verdict = "retire"

### State definitions

- keep — cleared for production sizing (Kelly active, full allocation
  within caps). Goes to Phase 4 paper deploy.
- retire — does not deploy. Either fails the multiple-testing null or
  fails the baseline floor. Archive per `CLAUDE.md` ("archive by default,
  delete only with approval").
- under_tested — neither pass nor fail. Insufficient data to render a
  verdict. Keep on paper for further data accumulation; does not clear
  the deploy gate on its own.

No "monitor" state. Phase 4 paper monitoring is the real monitor state —
strategies that pass keep go there and get killed if they underperform
backtest expectations.

### Forensic recording

Every final_gate row in `trials.log` records the full DSR float, the three
at-eval thresholds (`sr_zero_expected_at_eval`, `mintrl_required_at_eval`,
`baseline_sharpe_at_eval`), and the four component booleans
(`trade_count_pass`, `mintrl_pass`, `mt_mean_pass`, `baseline_pass`). The
verdict itself is binary; the floats are forensic context for the rare
borderline cases that warrant human review per `CLAUDE.md` ("DSR within
±0.05 of threshold on holdout").

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

Gate spec v2 adds a SIDECAR (not a schema change): each trial's
per-bar strategy/benchmark return series persists to
`backtest/reports/per_bar_returns/<trial_id>.parquet`, joined on
`trial_id`. Rows recorded before 2026-06-11 have no sidecar and their
per-bar series are unrecoverable (audit §2).

## Minimum Track Record Length (MinTRL)

Secondary sanity check, also from Bailey & López de Prado. MinTRL estimates
how many observations are required for an observed Sharpe to be statistically
distinguishable from zero at a given confidence level. If a strategy has
fewer observations than MinTRL, it is flagged as *under-tested* rather than
passed or failed. An under-tested strategy can be kept on paper for further
data collection, but it does not clear the deploy gate on its own.

Gate spec v2 units note: eq.13 runs on the PER-BAR Sharpe
(`sr_ann / √bars_per_year`). At annualised |SR| ≈ 1.0 the requirement
is ≈ 2.71 years of history, frequency-independent to first order —
higher-frequency bars do not shorten the calendar requirement. The
`mintrl` values persisted before 2026-06-11 were computed with the
annualised SR plugged in directly and are units-invalid (see § Gate
spec v2).

The implementation is bar-level: T = len(returns), where the returns series
is the per-bar return vector that the CPCV adapter produces by concatenating
per-block returns from each `engine.run()` call. Bar-level MinTRL works
well for high-trade strategies, but for low-trade strategies (DCA,
MeanReversion) the bar-level Sharpe is dominated by zero returns between
trades and MinTRL will not catch the under-testing. A heuristic trade-count
floor of 30 trades acts as a second precondition for those cases, paired
with bar-level MinTRL inside the verdict tree's under-tested branch.

## Baseline comparison

Gate spec v2: the baseline floor is statistical, not a raw-Sharpe
race. Directional/long-only strategies must show (a) positive OLS
alpha of per-bar strategy returns on same-instrument buy-and-hold
returns, significant at 95 % one-sided under Newey-West (HAC) errors,
AND (b) an annualised information ratio vs that benchmark of ≥ 0.5.
Market/delta-neutral strategies (taxonomy `"neutral": true`) are
benchmarked against zero: PSR(SR > 0) ≥ 0.95. The v1 raw comparison
("strategy Sharpe > B&H Sharpe, strict") is recorded as forensics
only — the audit showed it retired strategies for failing to beat a
1.94-Sharpe bull-window B&H with no significance test at all.

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
