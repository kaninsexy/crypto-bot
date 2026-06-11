"""scripts/phase_4b_v2b_full_cpcv.py -- Phase 4.B Variation #2b first full_cpcv.

V2b hypothesis (`phase4b-volregime-conditional-singlepair-btc-v2b`):
same delta-neutral BTC single-pair construction as V1, plus a
volatility-regime gate that holds the position only when realized
30-day BTC vol is below the dev-window median (LV regime); flat in
HV regime. Hypothesis-of-record lives in
`research/funding-rate-literature.md` § "Variation #2 candidate (b) --
phase4b-volregime-conditional-singlepair-btc-v2b".

This script reads `scripts/phase_4b_v2b_probe_output.json` (produced
by `phase_4b_v2b_volregime_probe.py`) for the calibrated
`vol_regime_threshold`, computes the 30d-trailing realized-vol time
series on dev-window 1h spot returns, then mirrors the structure of
`phase_4b_v2_full_cpcv.py`: headline run on the dev window
-> CPCV via run_cpcv_perp -> ANOMALY A/B/C/D pre-record checks ->
DSR validation -> verdict tree -> schema-validated record_trial
append.

Pre-flight: aborts if any prior row already has variation_id ==
VARIATION_ID (single-shot guard). Re-running after the row is tagged
superseded follows the same Policy-(c) supersession pattern as the
V1/V2 scripts.

Read-only with respect to manifest / sacred-harness files; the only
write is the trials.log append.

Authored by the autonomous Phase-4.B-V2b implementation pass
2026-05-08; not run automatically. The probe output values must
be reviewed before this script runs, and the perp OHLCV cache
must be seeded (data/okx_perp.load_or_fetch_perp_ohlcv) before
holdout.load_dev() will succeed for FundingRateHarvest_BTC.
"""

from __future__ import annotations

import json
import math
import os
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
from backtest.cpcv_common import CPCVConfig
from backtest.cpcv_perp import run_cpcv_perp
from backtest.dsr import dsr_from_cpcv_result
from backtest.engine_perp import run_perp
from backtest.verdict import compute_verdict
from data import okx_funding
from strategies.archive.funding_rate_harvest.funding_rate_harvest import (
    FundingRateHarvestStrategy,
    make_funding_settlement_counter,
)


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
        "[full_cpcv-v2b] probe output {} not found. Run "
        "phase_4b_v2b_volregime_probe.py first and review output "
        "before running this script.",
        PROBE_OUTPUT_PATH,
    )
    sys.exit(1)
probe = json.loads(PROBE_OUTPUT_PATH.read_text(encoding="utf-8"))
vol_regime_threshold = float(probe["vol_regime_threshold"])
vol_window_hours = int(probe["vol_window_hours"])
annualisation_hours = int(probe["annualisation_hours"])
logger.info(
    "[full_cpcv-v2b] probe loaded: vol_regime_threshold={:.6f} "
    "vol_window_hours={} annualisation_hours={} (rule={!r})",
    vol_regime_threshold, vol_window_hours, annualisation_hours,
    probe.get("calibration_rule"),
)


# -- 1. Manifest entry --------------------------------------------------------

logger.info("[full_cpcv-v2b] loading manifest")
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
    f"[full_cpcv-v2b] manifest: timeframe={entry['timeframe']!r} "
    f"cadence={funding_cadence_hours}h legs={legs} "
    f"dev=[{data_start.isoformat()} -> {holdout_start.isoformat()})"
)


# -- 2. Pre-flight ------------------------------------------------------------
# Single-shot guard: abort if any prior row already has variation_id
# == VARIATION_ID. V2b stands on its own structural-redesign citation
# chain (Almeida et al. 2024 + Schmeling et al.) and does NOT require
# a prior V1 row to be present in this machine's trials.log -- V1's
# canonical outcome lives in research/funding-rate-literature.md
# (dsr_holdout 0.0054, retired post-holdout 2026-05-02), which may
# have been recorded on a different harness path or machine. Removing
# the V1-precedence check unblocks operators whose trials.log doesn't
# carry the V1 row but who do have the V1 outcome via the literature.

prior_rows = list(_trials.read_trials(strategy_id=STRATEGY_ID))
v2b_rows = [r for r in prior_rows if r.get("variation_id") == VARIATION_ID]
# 2026-06-11 extended-window re-test: the human work order explicitly
# re-runs the SAME variation_id on the regenerated substrate window
# under gate spec v2 (no new variation slot; prior rows remain valid
# draws, NOT superseded).  The env flag is the deliberate per-run
# opt-in; without it the single-shot guard behaves exactly as before.
_RERUN_OK = os.environ.get("GATE_V2_RERUN_2026_06_11") == "1"
if v2b_rows and _RERUN_OK:
    logger.warning(
        "[full_cpcv-v2b] pre-flight: {} prior V2b row(s) present; "
        "proceeding anyway under GATE_V2_RERUN_2026_06_11=1 "
        "(extended-window re-test under gate spec v2).",
        len(v2b_rows),
    )
elif v2b_rows:
    logger.error(
        "[full_cpcv-v2b] pre-flight: {} prior row(s) already exist with "
        "variation_id={!r}. Single-shot guard tripped -- re-running "
        "requires that the prior row be tagged superseded_by per "
        "trials.log Policy (c). Aborting.",
        len(v2b_rows), VARIATION_ID,
    )
    sys.exit(1)

prior_non_v2b = [
    r for r in prior_rows
    if r.get("variation_id") != VARIATION_ID
]
logger.info(
    "[full_cpcv-v2b] pre-flight clean: {} prior non-V2b row(s) for "
    "{} in this trials.log; zero V2b rows. (V1 outcome in literature "
    "is the canonical record; V2b proceeds independently.)",
    len(prior_non_v2b), STRATEGY_ID,
)


# -- 3. Headline run on full dev window ---------------------------------------

logger.info("[full_cpcv-v2b] running headline engine_perp on full dev window")
dev = holdout.load_dev(STRATEGY_ID)
df_spot = dev["spot"]
df_perp = dev["perp"]

# Months-back math identical to V1/V2: archive fetches back from "now",
# so months is computed against now - data_start.
now_utc = pd.Timestamp.now(tz="UTC")
months_back_days = (now_utc - data_start).days
months = int(math.ceil(months_back_days / 30.44)) + 1
logger.info(
    f"[full_cpcv-v2b] requesting funding months={months} "
    f"(now - data_start {months_back_days}d)"
)
funding_full = okx_funding.load_or_fetch_funding_history(
    legs["perp"], months=months,
)
funding_dev = funding_full[funding_full.index < holdout_start]

# Substrate-coverage assertion (mirrored from V1/V2).
if funding_dev.index.min() > pd.Timestamp(data_start):
    logger.error(
        f"[full_cpcv-v2b] SUBSTRATE COVERAGE FAILURE: "
        f"funding_dev earliest {funding_dev.index.min()} "
        f"is AFTER data_start {data_start}. Funding cache "
        f"does not cover the dev window's earliest blocks. "
        f"Check months-back math (months={months}) or extend "
        f"the Path-5 archive. Aborting before run_cpcv_perp."
    )
    sys.exit(1)
logger.info(
    f"[full_cpcv-v2b] substrate coverage OK: funding earliest "
    f"{funding_dev.index.min()} <= data_start {data_start}"
)

# -- 3b. Compute vol_history for the dev window. The strategy looks
# this up at each bar; the threshold splits LV/HV per the probe. The
# window matches the probe's window so the partition produced here
# matches the partition the probe calibrated the threshold against.
logger.info(
    f"[full_cpcv-v2b] computing realized-vol time series "
    f"(window={vol_window_hours}h, annualisation=sqrt({annualisation_hours}))"
)
spot_closes = df_spot["close"].astype(float).sort_index()
returns_1h = spot_closes.pct_change()
vol_per_1h = returns_1h.rolling(vol_window_hours).std()
realized_vol_annualized = (
    vol_per_1h * np.sqrt(annualisation_hours)
).dropna()
vol_history_dev = pd.DataFrame(
    {"realized_vol_annualized": realized_vol_annualized}
)
vol_history_dev = vol_history_dev[vol_history_dev.index < holdout_start]
n_lv = int((vol_history_dev["realized_vol_annualized"] < vol_regime_threshold).sum())
n_hv = int((vol_history_dev["realized_vol_annualized"] >= vol_regime_threshold).sum())
n_total = int(len(vol_history_dev))
logger.info(
    f"[full_cpcv-v2b] vol_history: rows={n_total} "
    f"LV={n_lv} ({n_lv/n_total*100:.1f}%) "
    f"HV={n_hv} ({n_hv/n_total*100:.1f}%) "
    f"threshold={vol_regime_threshold:.4f}"
)


def _make_v2b_strategy() -> FundingRateHarvestStrategy:
    """Construct a V2b-configured strategy and wire in vol history.

    The factory closes over `vol_history_dev` and the calibrated
    threshold. `set_vol_history` is the strategy's seam for regime
    lookup without modifying engine_perp.
    """
    s = FundingRateHarvestStrategy(
        symbol=legs["spot"],
        timeframe=entry["timeframe"],
        vol_regime_threshold=vol_regime_threshold,
    )
    s.set_vol_history(vol_history_dev)
    return s


headline_strategy = _make_v2b_strategy()
headline_result = run_perp(
    df_spot=df_spot,
    df_perp=df_perp,
    funding_history=funding_dev,
    strategy=headline_strategy,
    period_label="phase4b-full-cpcv-v2b-headline",
    initial_balance=10_000.0,
    leverage=5.0,
    margin_mode="cross",
    spot_symbol=legs["spot"],
    perp_symbol=legs["perp"],
    flip_exit_n=4,
    flip_exit_threshold=0.0,
    exit_mr_ratio_threshold=0.01,
)
sr_observed = float(headline_result.metrics.sharpe_ratio)
n_trades_headline = int(headline_result.metrics.total_trades)
headline_exit_reasons = Counter(
    t.exit_reason for t in headline_result.trade_history
)
logger.info(
    f"[full_cpcv-v2b] headline: sharpe={sr_observed:.4f} "
    f"n_trades={n_trades_headline} "
    f"return_pct={headline_result.metrics.total_return_pct:+.2f}% "
    f"exit_reasons={dict(headline_exit_reasons)}"
)


# -- 4. CPCV block-Sharpe distribution via run_cpcv_perp ----------------------

_cpcv_log_capture: list[str] = []

def _capture_sink(message) -> None:
    record = message.record
    name = record["name"]
    if name in ("backtest.engine_perp", "paper_trading.perp_simulator"):
        _cpcv_log_capture.append(record["message"])

_capture_handler_id = logger.add(_capture_sink, level="INFO")

logger.info("[full_cpcv-v2b] running run_cpcv_perp (n_blocks=10)")
cpcv_config = CPCVConfig(
    n_blocks=10, k_held_out=2, purge_periods=0, embargo_periods=0,
    count_signal_events_per_block=make_funding_settlement_counter(
        funding_cadence_hours,
    ),
)
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
    # V2b additions:
    "vol_regime_threshold": vol_regime_threshold,
    "vol_window_hours": vol_window_hours,
    "annualisation_hours": annualisation_hours,
}
cpcv_result = run_cpcv_perp(
    strategy_id=STRATEGY_ID,
    params=PARAMS,
    config=cpcv_config,
    strategy_factory=_make_v2b_strategy,
)
logger.remove(_capture_handler_id)


# -- 4b. Pre-record anomaly checks (A/B/C/D) ----------------------------------
# Identical to V1/V2 -- same regex patterns, same thresholds.

_FS_RE = re.compile(
    r"Starting cpcv-perp-block-(\d+) run \| .*? funding_settlements=(\d+)"
)
_funding_settlements_processed: dict[int, int] = {}
for line in _cpcv_log_capture:
    m = _FS_RE.search(line)
    if m:
        block_id = int(m.group(1))
        _funding_settlements_processed[block_id] = int(m.group(2))
funding_settlements_processed_total = sum(
    _funding_settlements_processed.values()
)

_REASON_RE = re.compile(
    r"\[PERP\] CLOSE delta-neutral \| reason=([\w_]+)"
)
_block_exit_reasons: Counter = Counter()
for line in _cpcv_log_capture:
    m = _REASON_RE.search(line)
    if m:
        _block_exit_reasons[m.group(1)] += 1

callback_signal_events_total = (
    int(sum(cpcv_result.signal_events_per_block))
    if cpcv_result.signal_events_per_block is not None
    else 0
)

logger.info(
    f"[full_cpcv-v2b] anomaly inputs: "
    f"funding_settlements_processed_total="
    f"{funding_settlements_processed_total}  "
    f"callback_signal_events_total={callback_signal_events_total}  "
    f"block_exit_reasons={dict(_block_exit_reasons)}"
)

# ANOMALY A -- funding events not actually processed.
if (
    callback_signal_events_total > 0
    and funding_settlements_processed_total
    < 0.85 * callback_signal_events_total
):
    logger.error(
        f"[full_cpcv-v2b] ANOMALY A: funding cache likely stale -- "
        f"callback expected {callback_signal_events_total} "
        f"settlements, simulator processed "
        f"{funding_settlements_processed_total}. Aborting before "
        "record_trial."
    )
    sys.exit(1)

# ANOMALY B -- exit-reason histogram dominated by backtest_end.
total_block_exits = sum(_block_exit_reasons.values())
backtest_end_share = (
    _block_exit_reasons.get("backtest_end", 0) / total_block_exits
    if total_block_exits > 0 else 0.0
)
if total_block_exits > 0 and backtest_end_share > 0.8:
    logger.error(
        f"[full_cpcv-v2b] ANOMALY B: exit-reason histogram dominated "
        f"by backtest_end ({backtest_end_share*100:.1f}% of "
        f"{total_block_exits} closes). Strategy didn't trade its "
        f"actual edge. Block exit reasons: {dict(_block_exit_reasons)}. "
        "Aborting before record_trial."
    )
    sys.exit(1)

# ANOMALY C -- flat sharpe distribution (flag, do not abort).
sd_dict = cpcv_result.sharpe_distribution
_flat_distribution = (
    sd_dict["std"] < 0.05
    and -0.5 < sd_dict["mean"] < 0.5
)
if _flat_distribution:
    logger.warning(
        f"[full_cpcv-v2b] ANOMALY C (flag, non-blocking): sharpe "
        f"distribution is flat -- std={sd_dict['std']:.4f} "
        f"mean={sd_dict['mean']:.4f}."
    )

# ANOMALY D -- per-block funding coverage.
zero_coverage_blocks = [
    bid for bid, n in _funding_settlements_processed.items()
    if n == 0
]
if zero_coverage_blocks:
    logger.error(
        f"[full_cpcv-v2b] ANOMALY D: {len(zero_coverage_blocks)} "
        f"of {len(_funding_settlements_processed)} blocks had "
        f"funding_settlements=0 -- block IDs "
        f"{sorted(zero_coverage_blocks)}. Aborting before "
        f"record_trial."
    )
    sys.exit(1)


# -- 5. DSR on validation (dev) -----------------------------------------------

n_trials_pre = _trials.count_trials_for_dsr(STRATEGY_ID)
logger.info(
    f"[full_cpcv-v2b] n_trials_for_dsr({STRATEGY_ID}) "
    f"pre-append = {n_trials_pre}"
)
n_trials_for_dsr = max(n_trials_pre + 1, 1)

import backtest.trials as _t_mod
_orig_count = _t_mod.count_trials_for_dsr
_t_mod.count_trials_for_dsr = lambda sid: max(_orig_count(sid), 1)
try:
    dsr_result = dsr_from_cpcv_result(
        result=cpcv_result,
        strategy_id=STRATEGY_ID,
        sr_candidate=sr_observed,
        bars_per_year=BARS_PER_YEAR,
    )
finally:
    _t_mod.count_trials_for_dsr = _orig_count
logger.info(
    f"[full_cpcv-v2b] DSR validation: dsr={dsr_result.dsr:.4f} "
    f"sr_zero_expected={dsr_result.sr_zero_expected:.4f} "
    f"sr_std={dsr_result.sr_std:.4f} T={dsr_result.t} "
    f"n_trials={dsr_result.n_trials}"
)


# -- 6. Verdict-tree preview --------------------------------------------------

valid_returns = [r for r in cpcv_result.per_block_returns if r.size > 0]
concat_returns = (
    np.concatenate(valid_returns) if valid_returns else np.array([])
)
baseline_df = df_spot.copy()
total_signal_events = (
    int(sum(cpcv_result.signal_events_per_block))
    if cpcv_result.signal_events_per_block is not None
    else 0
)
verdict = compute_verdict(
    strategy_id=STRATEGY_ID,
    sr_candidate=sr_observed,
    returns=concat_returns,
    total_trades=int(sum(cpcv_result.trades_per_path)),
    baseline_df=baseline_df,
    n_trials=n_trials_for_dsr,
    min_trade_count=30,
    confidence=0.95,
    signal_event_count=total_signal_events,
    min_signal_event_count=30,
    bars_per_year=BARS_PER_YEAR,
)

logger.info(
    f"[full_cpcv-v2b] verdict={verdict.verdict} "
    f"trade_count_pass={verdict.trade_count_pass} "
    f"signal_event_count_pass={verdict.signal_event_count_pass} "
    f"mintrl_pass={verdict.mintrl_pass} "
    f"mt_mean_pass={verdict.mt_mean_pass} "
    f"baseline_pass={verdict.baseline_pass}"
)


# -- 7. Build full_cpcv event payload + record --------------------------------

# Hypothesis text verbatim from V2b section in literature.md. The
# regex anchors on the V2b candidate header, then captures the
# **Hypothesis.** paragraph up to the next paragraph break.
lit_text = LITERATURE_PATH.read_text(encoding="utf-8")
m = re.search(
    r"## Variation #2 candidate \(b\) -- "
    r"`phase4b-volregime-conditional-singlepair-btc-v2b`"
    r".*?\*\*Hypothesis\.\*\*\s*(.*?)\n\n\*\*",
    lit_text, flags=re.DOTALL,
)
if m is None:
    logger.error(
        f"[full_cpcv-v2b] could not extract V2b Hypothesis from "
        f"{LITERATURE_PATH}; aborting"
    )
    sys.exit(1)
hypothesis_text = m.group(1).strip()

notes = (
    f"V2b vol-regime-conditional structural redesign. "
    f"vol_regime_threshold={vol_regime_threshold:.6f} "
    f"(annualised) calibrated as the dev-window median of rolling "
    f"{vol_window_hours}h std of 1h returns x sqrt({annualisation_hours}). "
    f"Probe output: scripts/phase_4b_v2b_probe_output.json. "
    f"vol_history dev rows: {n_total} (LV {n_lv}, HV {n_hv}). "
    f"Headline exit reasons: {dict(headline_exit_reasons)}; "
    f"sum across cpcv blocks of trade counts: "
    f"{sum(cpcv_result.trades_per_path)}; total signal events "
    f"(callback hours/cadence): {total_signal_events}; settlements "
    f"actually processed by simulator across blocks: "
    f"{funding_settlements_processed_total}; block exit reasons: "
    f"{dict(_block_exit_reasons)}. Source: Almeida, Grith, "
    f"Miftachov, Wang (2024) arXiv 2410.15195v2; Schmeling, "
    f"Schrimpf, Todorov (BIS WP 1087); Ruan & Streltsov (SSRN "
    f"4218907)."
)
if os.environ.get("GATE_V2_RERUN_2026_06_11") == "1":
    notes = (
        "Extended-window re-test under gate spec v2 (2026-06-11 work "
        "order): same hypothesis + params as the 2026-05-08 V2b trial, "
        "regenerated substrate window (data_start from funding/perp "
        "availability, dev_end 2025-05-01), units-correct DSR/MinTRL, "
        "family-scaled eq.7, neutral PSR baseline gate. Consumes no "
        "new variation slot. " + notes
    )

event = {
    "strategy_id": STRATEGY_ID,
    "variation_id": VARIATION_ID,
    "trial_type": "full_cpcv",
    "params": PARAMS,
    "hypothesis": hypothesis_text,
    "split_holdout_start": entry["holdout_start"],
    "symbols": [legs["spot"]],
    "n_trades": int(sum(cpcv_result.trades_per_path)),
    "sharpe": sr_observed,
    "cpcv": {
        "n_paths": int(cpcv_config.n_blocks),
        "n_blocks": int(cpcv_config.n_blocks),
        "k_held_out": int(cpcv_config.k_held_out),
        "purge_periods": int(cpcv_config.purge_periods),
        "embargo_periods": int(cpcv_config.embargo_periods),
        "sharpe_distribution": cpcv_result.sharpe_distribution,
    },
    "dsr_validation": float(dsr_result.dsr),
    "signal_event_count": total_signal_events,
    "mintrl": (
        float(verdict.mintrl_required_at_eval)
        if np.isfinite(verdict.mintrl_required_at_eval) else None
    ),
    "buy_and_hold_sharpe": float(verdict.baseline_sharpe_at_eval),
    "notes": notes,
}

logger.info("[full_cpcv-v2b] recording trial row via trials.record_trial")
_trials.record_trial(
    event,
    # Gate spec v2 (2026-06-11): persist the per-bar series the
    # verdict ran on (audit: never saved -> S1/bootstrap blocked).
    per_bar_returns=concat_returns,
    per_bar_benchmark=(
        baseline_df["close"].pct_change().dropna().values.astype(float)
    ),
)


# -- 8. Verify row + per-block summary ----------------------------------------

post_rows = list(
    _trials.read_trials(
        strategy_id=STRATEGY_ID, trial_type="full_cpcv",
    )
)
clean_post_v2b = [
    r for r in post_rows
    if r.get("variation_id") == VARIATION_ID
    and not r.get("superseded_by")
]
_prior_clean_fc = [
    r for r in v2b_rows
    if r.get("trial_type") == "full_cpcv" and not r.get("superseded_by")
]
_expected_clean = (len(_prior_clean_fc) if _RERUN_OK else 0) + 1
assert len(clean_post_v2b) == _expected_clean, (
    f"expected {_expected_clean} clean V2b full_cpcv row(s) after "
    f"append, got {len(clean_post_v2b)}"
)
new_row = clean_post_v2b[-1]
assert "cpcv" in new_row and "sharpe_distribution" in new_row["cpcv"], (
    "cpcv block missing sharpe_distribution"
)
assert "dsr_validation" in new_row, "dsr_validation missing from row"
assert (
    new_row.get("signal_event_count") is not None
    and int(new_row["signal_event_count"]) > 0
), "signal_event_count missing or zero"


# -- 9. Per-block table + flagged anomalies -----------------------------------

per_block_sharpes = cpcv_result.per_block_sharpes
trades_per_path = cpcv_result.trades_per_path
events_per_block = cpcv_result.signal_events_per_block

print()
print("=" * 76)
print("Phase 4.B Variation #2b -- full_cpcv summary")
print("=" * 76)
print(f"Strategy        : {STRATEGY_ID}")
print(f"Variation       : {VARIATION_ID}")
print(f"Vol threshold   : {vol_regime_threshold:.6f} (annualised)")
print(f"Vol window      : {vol_window_hours}h")
print(
    f"Dev window      : {data_start.isoformat()} -> "
    f"{holdout_start.isoformat()}"
)
print(f"n_blocks        : {cpcv_config.n_blocks}")
print(f"Sharpe (head)   : {sr_observed:.4f}")
sd = cpcv_result.sharpe_distribution
finite_sharpes = [s for s in per_block_sharpes if not math.isnan(s)]
print(
    f"CPCV dist       : mean={sd['mean']:.4f}  std={sd['std']:.4f}  "
    f"p05={sd['quantiles']['p05']:.4f}  p25={sd['quantiles']['p25']:.4f}  "
    f"p50={sd['quantiles']['p50']:.4f}  p75={sd['quantiles']['p75']:.4f}  "
    f"p95={sd['quantiles']['p95']:.4f}  "
    f"min={min(finite_sharpes):.4f}  max={max(finite_sharpes):.4f}"
    if finite_sharpes else "no finite block sharpes"
)
print(
    f"DSR validation  : dsr={dsr_result.dsr:.4f}  "
    f"sr_zero_expected={dsr_result.sr_zero_expected:.4f}  "
    f"margin={sr_observed - dsr_result.sr_zero_expected:+.4f}"
)
print(
    f"Verdict-tree    : {verdict.verdict}  "
    f"(mt_mean_pass={verdict.mt_mean_pass}, "
    f"baseline_pass={verdict.baseline_pass}, "
    f"baseline_sr={verdict.baseline_sharpe_at_eval:.4f})"
)
print()
print(f"{'block':>5} {'sharpe':>10} {'n_trades':>10} {'n_funding_evts':>15}")
for i, (s, t, e) in enumerate(zip(
    per_block_sharpes, trades_per_path,
    events_per_block or [None] * len(per_block_sharpes),
)):
    s_str = f"{s:.4f}" if not math.isnan(s) else "NaN"
    e_str = str(e) if e is not None else "-"
    print(f"{i:>5} {s_str:>10} {t:>10} {e_str:>15}")
print()
print(f"Headline exit reasons (full-window run): {dict(headline_exit_reasons)}")
print(
    f"Block exit reasons (across cpcv blocks):  "
    f"{dict(_block_exit_reasons)}"
)
print(
    f"Funding settlements processed by simulator across blocks: "
    f"{funding_settlements_processed_total}  "
    f"(callback hours/cadence: {callback_signal_events_total})"
)
print()

anomalies: list[str] = []
for i, s in enumerate(per_block_sharpes):
    if not math.isnan(s) and (s <= -2.0 or s >= 6.0):
        anomalies.append(f"block {i}: extreme Sharpe {s:.4f} (<=-2 or >=+6)")
for i, t in enumerate(trades_per_path):
    if t == 0:
        anomalies.append(f"block {i}: zero trades (degenerate path)")
if events_per_block is not None:
    dev_hours = (holdout_start - data_start).total_seconds() / 3600.0
    expected_per_block = (dev_hours / cpcv_config.n_blocks) // funding_cadence_hours
    floor_threshold = max(int(expected_per_block * 0.7), 5)
    for i, e in enumerate(events_per_block):
        if e < floor_threshold:
            anomalies.append(
                f"block {i}: signal_event_count={e} below "
                f"floor {floor_threshold} (data gap or block-boundary "
                "alignment issue)"
            )

if anomalies:
    print("Anomalies:")
    for a in anomalies:
        print(f"  - {a}")
else:
    print("Anomalies: none")
print("=" * 76)
print()
print(
    "Trial row appended via record_trial; check backtest/trials.log "
    "for the schema-validated record."
)
