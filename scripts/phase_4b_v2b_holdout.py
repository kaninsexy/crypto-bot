"""scripts/phase_4b_v2b_holdout.py -- Phase 4.B Variation #2b holdout / final_gate.

Single-pass holdout-window run for FundingRateHarvest_BTC under the
V2b vol-regime-conditional construction, mirroring
`phase_4b_holdout_v1.py` shape but with the V2b gate wired through:

  * Reads holdout via `holdout.load_holdout` with caller grammar
    `phase4.FundingRateHarvest_BTC.final_dsr` (single-access invariant
    enforced by the module).
  * Reads the V2b probe output for `vol_regime_threshold` (held fixed
    from dev; NO in-holdout recalibration).
  * Computes a vol_history series on the FULL spot cache (dev +
    holdout) BEFORE the holdout load, so the rolling 720h window is
    already warmed up at holdout_start. The strategy's lookup at
    each holdout bar finds a finite vol value.
  * Runs one `engine_perp.run_perp` pass over the holdout window with
    the locked V2b parameter set (verbatim from the prior V2b
    full_cpcv row trial_id `a6bc5ab5...`).
  * Computes `dsr_holdout` directly from the holdout returns
    (`equity_curve.pct_change().dropna()`) via `deflated_sharpe`.
  * Inherits `cpcv` block + `dsr_validation` from the prior V2b
    full_cpcv row (the holdout pass does NOT re-run CPCV --
    final_gate carries forward dev-window block-Sharpe distribution
    as substrate truth).
  * Appends one `trial_type='final_gate'` row through the schema-
    validating writer.

Pre-flight enforces:
  * exactly one clean (non-superseded) prior V2b full_cpcv row,
  * zero prior final_gate rows for FundingRateHarvest_BTC,
  * funding-cache substrate coverage at holdout_start (BEFORE
    load_holdout, so single-access budget is preserved if abort).

Anything else aborts. Re-running after an append is a drift signal.

Read-only with respect to manifest / sacred-harness files; the only
writes are the trials.log append and the holdout_access.log append
(both via their authoritative schema-validating writers).

Authored 2026-05-08 by the autonomous Phase-4.B-V2b implementation
pass after V2b dev-CPCV cleared the verdict tree (KEEP, sharpe
2.8992, dsr 1.0). The holdout result is the load-bearing test:
V1 cleared dev but failed holdout; V2b's structural-redesign claim
is that the vol-regime gate addresses the dev-vs-holdout gap.
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
from backtest.holdout import _load_symbol_df  # for pre-load vol history
from backtest.verdict import compute_verdict
from data import okx_funding
from strategies.funding_rate_harvest import FundingRateHarvestStrategy


VARIATION_ID = "phase4b-volregime-conditional-singlepair-btc-v2b"
STRATEGY_ID = "FundingRateHarvest_BTC"
# Gate spec v2 (2026-06-11): explicit bar frequency for the
# units-correct DSR / MinTRL / verdict (manifest timeframe).
from backtest.dsr import bars_per_year_for_timeframe
BARS_PER_YEAR = bars_per_year_for_timeframe("1h")
LITERATURE_PATH = ROOT / "research" / "funding-rate-literature.md"
PROBE_OUTPUT_PATH = ROOT / "scripts" / "phase_4b_v2b_probe_output.json"


# -- 0. Probe output ----------------------------------------------------------

if not PROBE_OUTPUT_PATH.exists():
    logger.error(
        "[holdout-v2b] probe output {} not found. The holdout pass "
        "uses the dev-calibrated threshold; running probe in-holdout "
        "would violate the single-access invariant. Aborting before "
        "any data load.",
        PROBE_OUTPUT_PATH,
    )
    sys.exit(1)
probe = json.loads(PROBE_OUTPUT_PATH.read_text(encoding="utf-8"))
vol_regime_threshold = float(probe["vol_regime_threshold"])
vol_window_hours = int(probe["vol_window_hours"])
annualisation_hours = int(probe["annualisation_hours"])
logger.info(
    "[holdout-v2b] probe loaded (dev-calibrated): "
    "vol_regime_threshold={:.6f} vol_window_hours={} "
    "annualisation_hours={} (rule={!r})",
    vol_regime_threshold, vol_window_hours, annualisation_hours,
    probe.get("calibration_rule"),
)


# Locked-param set for V2b. Must match the prior V2b full_cpcv row
# verbatim -- the holdout claim is "the dev-window Sharpe replays on
# unseen data under the same construction", and any param drift here
# invalidates that claim.
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
    # V2b additions (must match prior V2b full_cpcv row exactly):
    "vol_regime_threshold": vol_regime_threshold,
    "vol_window_hours": vol_window_hours,
    "annualisation_hours": annualisation_hours,
}


# -- 1. Manifest entry --------------------------------------------------------

logger.info("[holdout-v2b] loading manifest")
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
    f"[holdout-v2b] manifest: timeframe={entry['timeframe']!r} "
    f"cadence={funding_cadence_hours}h legs={legs} "
    f"holdout_start={holdout_start.isoformat()}"
)


# -- 2. Pre-flight ------------------------------------------------------------
# Required prior state:
#   * exactly one clean (non-superseded) V2b full_cpcv row for this
#     strategy (we inherit cpcv block + dsr_validation from it).
#   * zero final_gate rows for this strategy (single-shot rule).
#   * funding-cache substrate coverage at holdout_start (checked
#     below before load_holdout).

prior_rows = list(_trials.read_trials(strategy_id=STRATEGY_ID))
v2b_fcpcv_rows = [
    r for r in prior_rows
    if r.get("trial_type") == "full_cpcv"
    and r.get("variation_id") == VARIATION_ID
]
clean_v2b_fcpcv = [
    r for r in v2b_fcpcv_rows if not r.get("superseded_by")
]
final_gate_rows = [
    r for r in prior_rows if r.get("trial_type") == "final_gate"
]

if len(clean_v2b_fcpcv) != 1:
    logger.error(
        f"[holdout-v2b] pre-flight: expected exactly 1 clean V2b "
        f"full_cpcv row, found {len(clean_v2b_fcpcv)} (total V2b "
        f"full_cpcv: {len(v2b_fcpcv_rows)}, of which "
        f"{len(v2b_fcpcv_rows) - len(clean_v2b_fcpcv)} are "
        f"superseded). Aborting."
    )
    sys.exit(1)
if len(final_gate_rows) != 0:
    logger.error(
        f"[holdout-v2b] pre-flight: found {len(final_gate_rows)} prior "
        f"final_gate rows for {STRATEGY_ID}; final_gate is single-shot "
        f"and supersession is forbidden by schema. Aborting."
    )
    sys.exit(1)

prior_row = clean_v2b_fcpcv[0]
prior_trial_id = prior_row["trial_id"]
logger.info(
    f"[holdout-v2b] pre-flight clean: inheriting cpcv + dsr_validation "
    f"from prior V2b full_cpcv trial_id={prior_trial_id}"
)

# Sanity-check inherited fields exist on the prior row.
for required_key in ("cpcv", "dsr_validation", "split_holdout_start",
                     "symbols", "hypothesis", "params"):
    if required_key not in prior_row:
        logger.error(
            f"[holdout-v2b] prior V2b full_cpcv row missing "
            f"{required_key!r}; aborting"
        )
        sys.exit(1)

# Param-drift guard: every PARAMS key must match the prior row's
# value exactly.
for k, v in PARAMS.items():
    if k not in prior_row["params"]:
        logger.error(
            f"[holdout-v2b] PARAMS key {k!r} absent from prior V2b "
            f"row; aborting"
        )
        sys.exit(1)
    if prior_row["params"][k] != v:
        logger.error(
            f"[holdout-v2b] PARAMS drift on {k!r}: "
            f"prior={prior_row['params'][k]!r} new={v!r}; aborting"
        )
        sys.exit(1)


# -- 3. Funding cache + substrate coverage (pre-load_holdout) -----------------
# Do this BEFORE load_holdout so the single-access budget is preserved
# on any pre-flight failure.

now_utc = pd.Timestamp.now(tz="UTC")
months_back_days = (now_utc - data_start).days
months = int(math.ceil(months_back_days / 30.44)) + 1
logger.info(
    f"[holdout-v2b] requesting funding months={months} "
    f"(now - data_start {months_back_days}d)"
)
funding_full = okx_funding.load_or_fetch_funding_history(
    legs["perp"], months=months,
)

cadence_slack = pd.Timedelta(hours=funding_cadence_hours)
funding_after_holdout = funding_full[funding_full.index >= holdout_start]
if funding_after_holdout.empty:
    logger.error(
        f"[holdout-v2b] SUBSTRATE COVERAGE FAILURE: no funding rows at "
        f"or after holdout_start {holdout_start}. Aborting before "
        f"load_holdout (single-access budget preserved)."
    )
    sys.exit(1)
earliest_funding = funding_after_holdout.index.min()
if earliest_funding > holdout_start + cadence_slack:
    logger.error(
        f"[holdout-v2b] SUBSTRATE COVERAGE FAILURE: earliest funding "
        f"after holdout_start is {earliest_funding}, more than one "
        f"cadence period ({funding_cadence_hours}h) after "
        f"holdout_start {holdout_start}. Aborting before load_holdout "
        f"(single-access budget preserved)."
    )
    sys.exit(1)
logger.info(
    f"[holdout-v2b] funding substrate coverage OK: earliest funding "
    f"after holdout_start = {earliest_funding} "
    f"(gap "
    f"{(earliest_funding - holdout_start).total_seconds()/3600:.2f}h "
    f"<= cadence {funding_cadence_hours}h)"
)


# -- 3b. Pre-load vol_history on FULL spot cache (no holdout access) ----------
# The V2b regime gate needs a finite vol value at every holdout bar.
# Computing vol_history on the holdout-only window would leave the
# first 720h NaN; we instead compute on the full cached spot series
# (dev + holdout) and pass the full series to the strategy. The
# threshold itself is dev-calibrated and held fixed -- this load
# does NOT consume the holdout single-access budget because we go
# through the L1 parquet cache directly, not through load_holdout.

logger.info(
    f"[holdout-v2b] computing vol_history on FULL spot cache "
    f"(window={vol_window_hours}h, annualisation=sqrt({annualisation_hours}))"
)
df_spot_full = _load_symbol_df(legs["spot"], entry["timeframe"]).sort_index()
spot_closes_full = df_spot_full["close"].astype(float)
returns_full = spot_closes_full.pct_change()
vol_per_1h_full = returns_full.rolling(vol_window_hours).std()
realized_vol_full = (
    vol_per_1h_full * np.sqrt(annualisation_hours)
).dropna()
vol_history_full = pd.DataFrame(
    {"realized_vol_annualized": realized_vol_full}
)
vol_after_holdout = vol_history_full[
    vol_history_full.index >= holdout_start
]
n_total_vol = int(len(vol_history_full))
n_post_holdout_vol = int(len(vol_after_holdout))
if n_post_holdout_vol == 0:
    logger.error(
        "[holdout-v2b] vol_history has zero post-holdout-start rows; "
        "the spot cache may not cover the holdout window. Aborting "
        "before load_holdout (single-access budget preserved)."
    )
    sys.exit(1)
n_lv_post = int(
    (vol_after_holdout["realized_vol_annualized"]
     < vol_regime_threshold).sum()
)
n_hv_post = int(
    (vol_after_holdout["realized_vol_annualized"]
     >= vol_regime_threshold).sum()
)
logger.info(
    f"[holdout-v2b] vol_history: full rows={n_total_vol} "
    f"(post-holdout {n_post_holdout_vol}: "
    f"LV={n_lv_post} ({n_lv_post/n_post_holdout_vol*100:.1f}%) "
    f"HV={n_hv_post} ({n_hv_post/n_post_holdout_vol*100:.1f}%)) "
    f"threshold={vol_regime_threshold:.4f}"
)


# -- 4. Load holdout (single-access invariant) --------------------------------

logger.info("[holdout-v2b] calling holdout.load_holdout")
hold = holdout.load_holdout(
    STRATEGY_ID,
    caller="phase4.FundingRateHarvest_BTC.final_dsr",
    reason="phase4.B Variation #2b final_gate (vol-regime-conditional)",
)
df_spot = hold["spot"]
df_perp = hold["perp"]
data_end = max(df_spot.index.max(), df_perp.index.max())
logger.info(
    f"[holdout-v2b] holdout loaded: spot rows={len(df_spot)} "
    f"perp rows={len(df_perp)} "
    f"window=[{holdout_start.isoformat()} -> {data_end.isoformat()}]"
)

# Final filter: funding rows actually in [holdout_start, data_end).
funding_holdout = funding_full[
    (funding_full.index >= holdout_start)
    & (funding_full.index < data_end)
]
logger.info(
    f"[holdout-v2b] funding rows in holdout window = "
    f"{len(funding_holdout)}"
)


# -- 5. Single run_perp pass over holdout window ------------------------------

logger.info("[holdout-v2b] running engine_perp on holdout window")
strategy = FundingRateHarvestStrategy(
    symbol=legs["spot"],
    timeframe=entry["timeframe"],
    vol_regime_threshold=vol_regime_threshold,
)
strategy.set_vol_history(vol_history_full)

result = run_perp(
    df_spot=df_spot,
    df_perp=df_perp,
    funding_history=funding_holdout,
    strategy=strategy,
    period_label="phase4b-holdout-v2b",
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
    f"[holdout-v2b] holdout: sharpe={sr_observed:.4f} "
    f"n_trades={n_trades_holdout} "
    f"return_pct={result.metrics.total_return_pct:+.2f}% "
    f"exit_reasons={dict(holdout_exit_reasons)}"
)


# -- 6. Returns + anomaly checks ----------------------------------------------

returns = result.equity_curve.pct_change().dropna().values.astype(float)
logger.info(f"[holdout-v2b] returns series T={len(returns)}")

# ANOMALY B -- exit-reason histogram dominated by backtest_end.
total_holdout_exits = sum(holdout_exit_reasons.values())
backtest_end_share = (
    holdout_exit_reasons.get("backtest_end", 0) / total_holdout_exits
    if total_holdout_exits > 0 else 0.0
)
if total_holdout_exits > 0 and backtest_end_share > 0.8:
    logger.error(
        f"[holdout-v2b] ANOMALY B: exit-reason histogram dominated by "
        f"backtest_end ({backtest_end_share*100:.1f}% of "
        f"{total_holdout_exits} closes). Strategy didn't trade its "
        f"actual edge over the holdout. Holdout exit reasons: "
        f"{dict(holdout_exit_reasons)}. Aborting before record_trial."
    )
    sys.exit(1)


# -- 7. Compute structural signal_event_count ---------------------------------

# For final_gate, the precondition uses the structural funding cadence
# (Track 2 / 2026-05-02): how many funding settlements the holdout
# window spans. Closed-trade count remains forensics.
total_signal_events = int(len(funding_holdout))
logger.info(
    f"[holdout-v2b] signal_event_count={total_signal_events} "
    f"(funding-cadence count over holdout window)"
)


# -- 8. DSR on holdout --------------------------------------------------------

n_trials_pre = _trials.count_trials_for_dsr(STRATEGY_ID)
n_trials_for_dsr = max(n_trials_pre + 1, 1)
logger.info(
    f"[holdout-v2b] n_trials_for_dsr({STRATEGY_ID}) "
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
    f"[holdout-v2b] DSR holdout: dsr={dsr_holdout_result.dsr:.4f} "
    f"sr_zero_expected={dsr_holdout_result.sr_zero_expected:.4f} "
    f"sr_std={dsr_holdout_result.sr_std:.4f} "
    f"T={dsr_holdout_result.t} n_trials={dsr_holdout_result.n_trials}"
)


# -- 9. Verdict-tree on holdout -----------------------------------------------

baseline_df = df_spot.copy()

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
    f"[holdout-v2b] verdict={verdict.verdict} "
    f"trade_count_pass={verdict.trade_count_pass} "
    f"signal_event_count_pass={verdict.signal_event_count_pass} "
    f"mintrl_pass={verdict.mintrl_pass} "
    f"mt_mean_pass={verdict.mt_mean_pass} "
    f"baseline_pass={verdict.baseline_pass}"
)


# -- 10. Build final_gate event payload + record -----------------------------

# Hypothesis text verbatim from the V2b section in literature.md.
lit_text = LITERATURE_PATH.read_text(encoding="utf-8")
m = re.search(
    r"## Variation #2 candidate \(b\) -- "
    r"`phase4b-volregime-conditional-singlepair-btc-v2b`"
    r".*?\*\*Hypothesis\.\*\*\s*(.*?)\n\n\*\*",
    lit_text, flags=re.DOTALL,
)
if m is None:
    logger.error(
        f"[holdout-v2b] could not extract V2b Hypothesis from "
        f"{LITERATURE_PATH}; aborting"
    )
    sys.exit(1)
hypothesis_text = m.group(1).strip()

notes = (
    f"V2b vol-regime-conditional final_gate (holdout). Single "
    f"run_perp pass over [{holdout_start.isoformat()}, "
    f"{data_end.isoformat()}); cpcv + dsr_validation inherited verbatim "
    f"from prior V2b full_cpcv trial_id={prior_trial_id}. "
    f"vol_regime_threshold={vol_regime_threshold:.6f} (dev-calibrated, "
    f"held fixed). Post-holdout vol distribution: "
    f"LV={n_lv_post} ({n_lv_post/n_post_holdout_vol*100:.1f}%) / "
    f"HV={n_hv_post} ({n_hv_post/n_post_holdout_vol*100:.1f}%). "
    f"Holdout exit reasons: {dict(holdout_exit_reasons)}. "
    f"Funding rows in holdout window: {len(funding_holdout)}. "
    f"signal_event_count uses structural funding cadence "
    f"(Track 2 / 2026-05-02), not closed-trade count. Source: "
    f"Almeida, Grith, Miftachov, Wang (2024) arXiv 2410.15195v2; "
    f"Schmeling, Schrimpf, Todorov (BIS WP 1087); Ruan & Streltsov "
    f"(SSRN 4218907)."
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
    # Inherited from prior V2b full_cpcv row.
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
    # v2 final_gate fields.
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

logger.info("[holdout-v2b] recording final_gate row via trials.record_trial")
_trials.record_trial(
    event,
    # Gate spec v2 (2026-06-11): persist the per-bar series the
    # verdict ran on (audit: never saved -> S1/bootstrap blocked).
    per_bar_returns=returns,
    per_bar_benchmark=(
        baseline_df["close"].pct_change().dropna().values.astype(float)
    ),
)


# -- 11. Verify row + summary print -------------------------------------------

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
print("Phase 4.B Variation #2b -- final_gate (holdout) summary")
print("=" * 76)
print(f"Strategy        : {STRATEGY_ID}")
print(f"Variation       : {VARIATION_ID}")
print(
    f"Holdout window  : {holdout_start.isoformat()} -> "
    f"{data_end.isoformat()}"
)
print(f"Inherited from  : V2b full_cpcv trial_id={prior_trial_id}")
print(f"Vol threshold   : {vol_regime_threshold:.6f} (dev-calibrated)")
print(
    f"Post-holdout vol: LV={n_lv_post} "
    f"({n_lv_post/n_post_holdout_vol*100:.1f}%) "
    f"HV={n_hv_post} "
    f"({n_hv_post/n_post_holdout_vol*100:.1f}%)"
)
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
    "is now exhausted -- re-running this script will fail at "
    "load_holdout."
)
