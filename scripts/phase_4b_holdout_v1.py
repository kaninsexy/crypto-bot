"""scripts/phase_4b_holdout_v1.py — Phase 4.B Variation #1 holdout / final_gate.

Single-pass holdout-window run for FundingRateHarvest_BTC, mirroring
the dev-window full_cpcv runner but adapted for the holdout boundary:

  * Reads holdout via `holdout.load_holdout` with caller grammar
    `phase4.FundingRateHarvest_BTC.final_dsr` (single-access invariant
    enforced by the module).
  * Runs one `engine_perp.run_perp` pass over the holdout window with
    the locked Variation #1 parameter set (verbatim from the prior
    full_cpcv row trial_id `f2c343c3...`).
  * Computes `dsr_holdout` directly from the holdout returns
    (`equity_curve.pct_change().dropna()`) via `deflated_sharpe`.
  * Inherits `cpcv` block + `dsr_validation` from the prior full_cpcv
    row (the holdout pass does NOT re-run CPCV — final_gate carries
    forward dev-window block-Sharpe distribution as substrate truth).
  * Appends one `trial_type='final_gate'` row through the schema-
    validating writer.

Pre-flight enforces:
  * exactly one clean (non-superseded) prior full_cpcv row,
  * zero prior final_gate rows,
  * zero uncleared holdout-access events for the strategy.

Anything else aborts.  Re-running after an append is a drift signal.

Read-only with respect to manifest / sacred-harness files; the only
writes are the trials.log append and the holdout_access.log append
(both via their authoritative schema-validating writers).
"""

from __future__ import annotations

import json
import math
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from loguru import logger

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import backtest.holdout as holdout
from backtest import trials as _trials
from backtest.dsr import deflated_sharpe
from backtest.engine_perp import run_perp
from backtest.verdict import compute_verdict
from data import okx_funding
from strategies.archive.funding_rate_harvest.funding_rate_harvest import FundingRateHarvestStrategy


VARIATION_ID = "phase4b-delta-neutral-singlepair-btc-v1"
STRATEGY_ID = "FundingRateHarvest_BTC"
# Gate spec v2 (2026-06-11): explicit bar frequency for the
# units-correct DSR / MinTRL / verdict (manifest timeframe).
from backtest.dsr import bars_per_year_for_timeframe
BARS_PER_YEAR = bars_per_year_for_timeframe("1h")
LITERATURE_PATH = ROOT / "research" / "funding-rate-literature.md"

# Locked-param set for Variation #1.  Must match the prior full_cpcv
# row verbatim — the holdout claim is "the dev-window Sharpe replays
# on unseen data", and any param drift here invalidates that claim.
PARAMS: dict = {
    "signal_cadence": "8h",
    "timeframe": "1h",
    "target_vol_annual": 0.05,
    "notional_capital_per_leg": 10000,
    "exit_funding_flip_n_settlements": 4,
    "exit_margin_breach_threshold": 0.01,
    "leverage": 5.0,
    "margin_mode": "cross",
    "exit_mr_ratio_threshold": 0.01,
    "flip_exit_n": 4,
    "flip_exit_threshold": 0.0,
    "initial_balance": 10000.0,
}


# ── 1. Manifest entry ────────────────────────────────────────────────────────

logger.info("[holdout-v1] loading manifest")
manifest = holdout.load_manifest()
if STRATEGY_ID not in manifest:
    logger.error(f"manifest missing entry for {STRATEGY_ID}; aborting")
    sys.exit(1)
entry = manifest[STRATEGY_ID]
funding_cadence_hours = int(entry.get("funding_cadence_hours", 8))
legs = entry["legs"]
holdout_start = pd.Timestamp(entry["holdout_start"])
data_start = pd.Timestamp(entry["data_start"])

logger.info(
    f"[holdout-v1] manifest: timeframe={entry['timeframe']!r} "
    f"cadence={funding_cadence_hours}h legs={legs} "
    f"holdout_start={holdout_start.isoformat()}"
)


# ── 2. Pre-flight ────────────────────────────────────────────────────────────
# Required prior state:
#   * exactly one clean (non-superseded) full_cpcv row for this
#     strategy (we inherit cpcv block + dsr_validation from it).
#   * zero final_gate rows for this strategy (single-shot rule —
#     final_gate is the audit boundary; supersession is forbidden by
#     trials.py schema validation, and re-appending would silently
#     duplicate the deploy-decision artifact).
#   * zero uncleared holdout_access events for this strategy
#     (single-access invariant; load_holdout will also enforce, but
#     we surface the failure mode cleanly here before any data load).

prior_rows = list(_trials.read_trials(strategy_id=STRATEGY_ID))
fcpcv_rows = [r for r in prior_rows if r.get("trial_type") == "full_cpcv"]
clean_fcpcv = [r for r in fcpcv_rows if not r.get("superseded_by")]
final_gate_rows = [r for r in prior_rows if r.get("trial_type") == "final_gate"]

if len(clean_fcpcv) != 1:
    logger.error(
        f"[holdout-v1] pre-flight: expected exactly 1 clean full_cpcv "
        f"row, found {len(clean_fcpcv)} (total full_cpcv rows: "
        f"{len(fcpcv_rows)}, of which "
        f"{len(fcpcv_rows) - len(clean_fcpcv)} are superseded). "
        f"Aborting."
    )
    sys.exit(1)
if len(final_gate_rows) != 0:
    logger.error(
        f"[holdout-v1] pre-flight: found {len(final_gate_rows)} prior "
        f"final_gate rows; final_gate is single-shot and supersession "
        f"is forbidden by schema. Aborting."
    )
    sys.exit(1)

prior_row = clean_fcpcv[0]
prior_trial_id = prior_row["trial_id"]
logger.info(
    f"[holdout-v1] pre-flight clean: inheriting cpcv + dsr_validation "
    f"from prior full_cpcv trial_id={prior_trial_id}"
)

# Sanity-check inherited fields exist on the prior row.
for required_key in ("cpcv", "dsr_validation", "split_holdout_start",
                     "symbols", "hypothesis", "params"):
    if required_key not in prior_row:
        logger.error(
            f"[holdout-v1] prior full_cpcv row missing {required_key!r}; "
            f"aborting"
        )
        sys.exit(1)

# Param-drift guard: every PARAMS key must match the prior row's
# value exactly.  The prior row may carry additional keys (e.g. the
# 12-key superset vs the 8-key user-listed locked set); we only
# require subset agreement on the keys we send to run_perp.
for k, v in PARAMS.items():
    if k not in prior_row["params"]:
        logger.error(
            f"[holdout-v1] PARAMS key {k!r} absent from prior row; "
            f"aborting"
        )
        sys.exit(1)
    if prior_row["params"][k] != v:
        logger.error(
            f"[holdout-v1] PARAMS drift on {k!r}: prior={prior_row['params'][k]!r} "
            f"new={v!r}; aborting"
        )
        sys.exit(1)


# ── 3. Funding cache + substrate coverage (pre-load_holdout) ─────────────────
# Do this BEFORE load_holdout: the single-access invariant on the
# holdout audit log is consumed at load_holdout call time, so any
# pre-flight gate that can be checked from non-holdout sources
# (funding cache, manifest) MUST be checked first.  An access event
# is appended even if downstream code aborts, and the access cannot
# be reversed without sacred-harness intervention.

now_utc = pd.Timestamp.now(tz="UTC")
months_back_days = (now_utc - data_start).days
months = int(math.ceil(months_back_days / 30.44)) + 1
logger.info(
    f"[holdout-v1] requesting funding months={months} "
    f"(now − data_start {months_back_days}d)"
)
funding_full = okx_funding.load_or_fetch_funding_history(
    legs["perp"], months=months,
)

# Check: at least one funding event lands within one cadence period
# of holdout_start.  Mid-cadence holdout boundaries (e.g. 22:36 vs
# the 8h grid 00:00/08:00/16:00) legitimately leave a sub-cadence
# gap; anything beyond one cadence period is a real coverage gap
# (cache too thin / months-back undersized).  The dev-side check
# uses strict `<= data_start` because data_start is grid-aligned by
# manifest; that invariant doesn't hold at holdout_start.
cadence_slack = pd.Timedelta(hours=funding_cadence_hours)
funding_after_holdout = funding_full[funding_full.index >= holdout_start]
if funding_after_holdout.empty:
    logger.error(
        f"[holdout-v1] SUBSTRATE COVERAGE FAILURE: no funding rows at "
        f"or after holdout_start {holdout_start}. Aborting before "
        f"load_holdout (single-access budget preserved)."
    )
    sys.exit(1)
earliest_funding = funding_after_holdout.index.min()
if earliest_funding > holdout_start + cadence_slack:
    logger.error(
        f"[holdout-v1] SUBSTRATE COVERAGE FAILURE: earliest funding "
        f"after holdout_start is {earliest_funding}, more than one "
        f"cadence period ({funding_cadence_hours}h) after "
        f"holdout_start {holdout_start}. Funding cache does not cover "
        f"the holdout boundary. Aborting before load_holdout (single-"
        f"access budget preserved)."
    )
    sys.exit(1)
logger.info(
    f"[holdout-v1] substrate coverage OK: earliest funding after "
    f"holdout_start = {earliest_funding} "
    f"(gap {(earliest_funding - holdout_start).total_seconds()/3600:.2f}h "
    f"<= cadence {funding_cadence_hours}h)"
)


# ── 4. Load holdout (single-access invariant) ────────────────────────────────

logger.info("[holdout-v1] calling holdout.load_holdout")
hold = holdout.load_holdout(
    STRATEGY_ID,
    caller="phase4.FundingRateHarvest_BTC.final_dsr",
    reason="phase4.B Variation #1 final_gate",
)
df_spot = hold["spot"]
df_perp = hold["perp"]
data_end = max(df_spot.index.max(), df_perp.index.max())
logger.info(
    f"[holdout-v1] holdout loaded: spot rows={len(df_spot)} "
    f"perp rows={len(df_perp)} "
    f"window=[{holdout_start.isoformat()} → {data_end.isoformat()}]"
)

# Final filter: funding rows actually in [holdout_start, data_end).
funding_holdout = funding_full[
    (funding_full.index >= holdout_start)
    & (funding_full.index < data_end)
]
logger.info(
    f"[holdout-v1] funding rows in holdout window = "
    f"{len(funding_holdout)}"
)


# ── 5. Single run_perp pass over holdout window ──────────────────────────────

logger.info("[holdout-v1] running engine_perp on holdout window")
strategy = FundingRateHarvestStrategy(
    symbol=legs["spot"], timeframe=entry["timeframe"],
)
result = run_perp(
    df_spot=df_spot,
    df_perp=df_perp,
    funding_history=funding_holdout,
    strategy=strategy,
    period_label="phase4b-holdout-v1",
    initial_balance=PARAMS["initial_balance"],
    leverage=PARAMS["leverage"],
    margin_mode=PARAMS["margin_mode"],
    spot_symbol=legs["spot"],
    perp_symbol=legs["perp"],
    flip_exit_n=PARAMS["flip_exit_n"],
    flip_exit_threshold=PARAMS["flip_exit_threshold"],
    exit_mr_ratio_threshold=PARAMS["exit_mr_ratio_threshold"],
)
sr_observed = float(result.metrics.sharpe_ratio)
n_trades_holdout = int(result.metrics.total_trades)
holdout_exit_reasons = Counter(
    t.exit_reason for t in result.trade_history
)
logger.info(
    f"[holdout-v1] holdout: sharpe={sr_observed:.4f} "
    f"n_trades={n_trades_holdout} "
    f"return_pct={result.metrics.total_return_pct:+.2f}% "
    f"exit_reasons={dict(holdout_exit_reasons)}"
)


# ── 6. Returns + anomaly checks ──────────────────────────────────────────────

returns = result.equity_curve.pct_change().dropna().values.astype(float)
logger.info(f"[holdout-v1] returns series T={len(returns)}")

# ANOMALY B — exit-reason histogram dominated by backtest_end.
# A clean funding-harvest holdout should produce funding_flip /
# margin_breach exits; > 80% backtest_end means the strategy never
# actually rotated through its edge, just sat in one position until
# the window closed.
total_holdout_exits = sum(holdout_exit_reasons.values())
backtest_end_share = (
    holdout_exit_reasons.get("backtest_end", 0) / total_holdout_exits
    if total_holdout_exits > 0 else 0.0
)
if total_holdout_exits > 0 and backtest_end_share > 0.8:
    logger.error(
        f"[holdout-v1] ANOMALY B: exit-reason histogram dominated by "
        f"backtest_end ({backtest_end_share*100:.1f}% of "
        f"{total_holdout_exits} closes). Strategy didn't trade its "
        f"actual edge over the holdout. Holdout exit reasons: "
        f"{dict(holdout_exit_reasons)}. Aborting before record_trial."
    )
    sys.exit(1)

# ANOMALY A — funding-cache-stale check is structurally satisfied
# above by the substrate-coverage assertion (earliest_funding <=
# holdout_start ensures the cache covers the holdout boundary).
# ANOMALY D (per-block zero-coverage) does not apply to single-window
# holdout runs.  ANOMALY C (flat distribution) is a CPCV-only
# diagnostic.


# ── 7. Compute structural signal_event_count ─────────────────────────────────

# For final_gate the "trade count" precondition uses the structural
# funding cadence (Track 2 / 2026-05-02): how many funding settlements
# the holdout window spans.  Closed-trade count remains forensics.
total_signal_events = int(len(funding_holdout))
logger.info(
    f"[holdout-v1] signal_event_count={total_signal_events} "
    f"(funding-cadence count over holdout window)"
)


# ── 8. DSR on holdout ────────────────────────────────────────────────────────

# count_trials_for_dsr reflects N before this row appends.  Per BLP
# convention the trial-being-deflated is itself part of the budget,
# so we pass max(N + 1, 1) — same convention as the full_cpcv runner.
n_trials_pre = _trials.count_trials_for_dsr(STRATEGY_ID)
n_trials_for_dsr = max(n_trials_pre + 1, 1)
logger.info(
    f"[holdout-v1] n_trials_for_dsr({STRATEGY_ID}) "
    f"pre-append = {n_trials_pre}, using N={n_trials_for_dsr} "
    f"(self-deflation convention)"
)
dsr_holdout_result = deflated_sharpe(
    sr_candidate=sr_observed,
    returns=returns,
    n_trials=n_trials_for_dsr,
    bars_per_year=BARS_PER_YEAR,
)
logger.info(
    f"[holdout-v1] DSR holdout: dsr={dsr_holdout_result.dsr:.4f} "
    f"sr_zero_expected={dsr_holdout_result.sr_zero_expected:.4f} "
    f"sr_std={dsr_holdout_result.sr_std:.4f} "
    f"T={dsr_holdout_result.t} n_trials={dsr_holdout_result.n_trials}"
)


# ── 9. Verdict-tree on holdout ───────────────────────────────────────────────

# Baseline for verdict tree: spot leg over holdout window (matches
# perp leg's index by manifest construction).
baseline_df = df_spot.copy()

# Patch count_trials_for_dsr lookup the same way the full_cpcv runner
# does, so compute_verdict's internal DSR call sees N >= 1 even when
# trials.log has only smoke-excluded rows for this strategy.
import backtest.trials as _t_mod
_orig_count = _t_mod.count_trials_for_dsr
_t_mod.count_trials_for_dsr = lambda sid: max(_orig_count(sid), 1)
try:
    verdict = compute_verdict(
        strategy_id=STRATEGY_ID,
        sr_candidate=sr_observed,
        returns=returns,
        total_trades=n_trades_holdout,
        baseline_df=baseline_df,
        n_trials=n_trials_for_dsr,
        min_trade_count=30,
        confidence=0.95,
        signal_event_count=total_signal_events,
        min_signal_event_count=30,
        bars_per_year=BARS_PER_YEAR,
    )
finally:
    _t_mod.count_trials_for_dsr = _orig_count

logger.info(
    f"[holdout-v1] verdict={verdict.verdict} "
    f"trade_count_pass={verdict.trade_count_pass} "
    f"signal_event_count_pass={verdict.signal_event_count_pass} "
    f"mintrl_pass={verdict.mintrl_pass} "
    f"mt_mean_pass={verdict.mt_mean_pass} "
    f"baseline_pass={verdict.baseline_pass}"
)


# ── 10. Build final_gate event payload + record ──────────────────────────────

# Hypothesis text verbatim from literature.md (mirrors smoke + full_cpcv).
lit_text = LITERATURE_PATH.read_text(encoding="utf-8")
m = re.search(
    r"\*\*Hypothesis\.\*\*\s*(.*?)\n\n\*\*Substrate\.\*\*",
    lit_text, flags=re.DOTALL,
)
if m is None:
    logger.error(
        f"[holdout-v1] could not extract Hypothesis from "
        f"{LITERATURE_PATH}; aborting"
    )
    sys.exit(1)
hypothesis_text = m.group(1).strip()

notes = (
    f"Variation #1 final_gate (holdout). Single run_perp pass over "
    f"[{holdout_start.isoformat()}, {data_end.isoformat()}); cpcv + "
    f"dsr_validation inherited verbatim from prior full_cpcv "
    f"trial_id={prior_trial_id}. Holdout exit reasons: "
    f"{dict(holdout_exit_reasons)}. Funding rows in holdout window: "
    f"{len(funding_holdout)}. signal_event_count uses structural "
    f"funding cadence (Track 2 / 2026-05-02), not closed-trade count."
)

event = {
    "strategy_id": STRATEGY_ID,
    "variation_id": VARIATION_ID,
    "trial_type": "final_gate",
    "params": PARAMS,
    "hypothesis": hypothesis_text,
    "split_holdout_start": prior_row["split_holdout_start"],
    "symbols": list(prior_row["symbols"]),
    "n_trades": n_trades_holdout,
    "sharpe": sr_observed,
    # Inherited from prior full_cpcv row.
    "cpcv": prior_row["cpcv"],
    "dsr_validation": float(prior_row["dsr_validation"]),
    # New for final_gate.
    "dsr_holdout": float(dsr_holdout_result.dsr),
    "signal_event_count": total_signal_events,
    "mintrl": (
        float(verdict.mintrl_required_at_eval)
        if np.isfinite(verdict.mintrl_required_at_eval) else None
    ),
    "buy_and_hold_sharpe": float(verdict.baseline_sharpe_at_eval),
    # v2 final_gate fields (verdict + 4 component bools + 3 at-eval
    # floats + total_trades).
    "verdict": verdict.verdict,
    "trade_count_pass": bool(verdict.trade_count_pass),
    "mintrl_pass": bool(verdict.mintrl_pass),
    "mt_mean_pass": (
        bool(verdict.mt_mean_pass)
        if verdict.mt_mean_pass is not None else None
    ),
    "baseline_pass": (
        bool(verdict.baseline_pass)
        if verdict.baseline_pass is not None else None
    ),
    "sr_zero_expected_at_eval": float(verdict.sr_zero_expected_at_eval),
    "mintrl_required_at_eval": float(verdict.mintrl_required_at_eval),
    "baseline_sharpe_at_eval": float(verdict.baseline_sharpe_at_eval),
    "total_trades": int(n_trades_holdout),
    "notes": notes,
}

logger.info("[holdout-v1] recording final_gate row via trials.record_trial")
_trials.record_trial(
    event,
    # Gate spec v2 (2026-06-11): persist the per-bar series the
    # verdict ran on (audit: never saved -> S1/bootstrap blocked).
    per_bar_returns=returns,
    per_bar_benchmark=(
        baseline_df["close"].pct_change().dropna().values.astype(float)
    ),
)


# ── 11. Verify row + summary print ───────────────────────────────────────────

post_rows = list(
    _trials.read_trials(
        strategy_id=STRATEGY_ID, trial_type="final_gate",
    )
)
assert len(post_rows) == 1, (
    f"expected exactly 1 final_gate row after append, got {len(post_rows)}"
)
new_row = post_rows[-1]
assert new_row["verdict"] == verdict.verdict
assert "dsr_holdout" in new_row, "dsr_holdout missing from row"
assert "cpcv" in new_row and "sharpe_distribution" in new_row["cpcv"], (
    "inherited cpcv block missing sharpe_distribution"
)
assert (
    new_row.get("signal_event_count") is not None
    and int(new_row["signal_event_count"]) > 0
), "signal_event_count missing or zero"

print()
print("=" * 76)
print("Phase 4.B Variation #1 — final_gate (holdout) summary")
print("=" * 76)
print(f"Strategy        : {STRATEGY_ID}")
print(f"Variation       : {VARIATION_ID}")
print(
    f"Holdout window  : {holdout_start.isoformat()} → "
    f"{data_end.isoformat()}"
)
print(f"Inherited from  : full_cpcv trial_id={prior_trial_id}")
print(
    f"Sharpe (holdout): {sr_observed:.4f}   "
    f"return_pct={result.metrics.total_return_pct:+.2f}%   "
    f"n_trades={n_trades_holdout}"
)
print(
    f"DSR validation  : {prior_row['dsr_validation']:.6f}  (inherited)"
)
print(
    f"DSR holdout     : {dsr_holdout_result.dsr:.6f}  "
    f"(sr_zero_expected={dsr_holdout_result.sr_zero_expected:.4f}, "
    f"sr_std={dsr_holdout_result.sr_std:.4f}, "
    f"T={dsr_holdout_result.t}, "
    f"n_trials={dsr_holdout_result.n_trials})"
)
print(
    f"Signal events   : {total_signal_events}  "
    f"(funding cadence over holdout window)"
)
print(
    f"Verdict-tree    : {verdict.verdict}  "
    f"(trade_count_pass={verdict.trade_count_pass}, "
    f"signal_event_count_pass={verdict.signal_event_count_pass}, "
    f"mintrl_pass={verdict.mintrl_pass}, "
    f"mt_mean_pass={verdict.mt_mean_pass}, "
    f"baseline_pass={verdict.baseline_pass}, "
    f"baseline_sr={verdict.baseline_sharpe_at_eval:.4f})"
)
print(f"Holdout exit reasons: {dict(holdout_exit_reasons)}")
print("=" * 76)
print()
print(
    "Final_gate row appended via record_trial; check backtest/trials.log "
    "for the schema-validated record. Holdout single-access invariant "
    "is now exhausted — re-running this script will fail at "
    "load_holdout."
)
