"""scripts/phase_4b_v2_full_cpcv.py — Phase 4.B Variation #2 first full_cpcv.

V2 hypothesis (`phase4b-threshold-entry-singlepair-btc-v2`): same
delta-neutral BTC single-pair construction as V1, but with a
minimum funding-rate entry gate calibrated from the dev-window 33rd
percentile of positive-funding sessions.  Hypothesis-of-record lives
in research/funding-rate-literature.md § "Variation #2 --
phase4b-threshold-entry-singlepair-btc-v2".

This script reads scripts/phase_4b_v2_probe_output.json (produced by
phase_4b_v2_threshold_probe.py) for the calibrated thresholds, then
mirrors the structure of phase_4b_full_cpcv_v1.py: headline run on
the dev window → CPCV via run_cpcv_perp → ANOMALY A/B/C/D pre-record
checks → DSR validation → verdict tree → schema-validated record_trial
append.

Pre-flight: aborts if any prior row already has variation_id ==
VARIATION_ID (single-shot guard).  Re-running after the row is
tagged superseded follows the same Policy-(c) supersession pattern as
the V1 script.

Read-only with respect to manifest / sacred-harness files; the only
write is the trials.log append.

This script is authored and committed but NOT run automatically.
The probe output values must be reviewed by the human before this
script runs.
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
from backtest.cpcv_common import CPCVConfig
from backtest.cpcv_perp import run_cpcv_perp
from backtest.dsr import dsr_from_cpcv_result
from backtest.engine_perp import run_perp
from backtest.verdict import compute_verdict
from data import okx_funding
from strategies.funding_rate_harvest import (
    FundingRateHarvestStrategy,
    make_funding_settlement_counter,
)


VARIATION_ID = "phase4b-threshold-entry-singlepair-btc-v2"
STRATEGY_ID = "FundingRateHarvest_BTC"
LITERATURE_PATH = ROOT / "research" / "funding-rate-literature.md"
PROBE_OUTPUT_PATH = ROOT / "scripts" / "phase_4b_v2_probe_output.json"


# ── 0. Probe output ──────────────────────────────────────────────────────────

if not PROBE_OUTPUT_PATH.exists():
    logger.error(
        "[full_cpcv-v2] probe output {} not found.  Run "
        "phase_4b_v2_threshold_probe.py first and review output "
        "before running this script.",
        PROBE_OUTPUT_PATH,
    )
    sys.exit(1)
probe = json.loads(PROBE_OUTPUT_PATH.read_text(encoding="utf-8"))
entry_threshold = float(probe["entry_threshold"])
exit_threshold = float(probe["exit_threshold"])
logger.info(
    "[full_cpcv-v2] probe thresholds loaded: entry={:.6f} "
    "exit={:.6f} (calibration_rule={!r})",
    entry_threshold, exit_threshold, probe.get("calibration_rule"),
)


# ── 1. Manifest entry ────────────────────────────────────────────────────────

logger.info("[full_cpcv-v2] loading manifest")
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
    f"[full_cpcv-v2] manifest: timeframe={entry['timeframe']!r} "
    f"cadence={funding_cadence_hours}h legs={legs} "
    f"dev=[{data_start.isoformat()} → {holdout_start.isoformat()})"
)


# ── 2. Pre-flight ────────────────────────────────────────────────────────────
# Single-shot guard: abort if any prior row already has variation_id
# == VARIATION_ID.  Otherwise accept any pre-state in which V1 has
# produced at least one trial row (smoke or full_cpcv) — this is a
# V2-specific prior-state check that the V1 script's stricter
# row-count gate doesn't apply to.

prior_rows = list(_trials.read_trials(strategy_id=STRATEGY_ID))
v2_rows = [r for r in prior_rows if r.get("variation_id") == VARIATION_ID]
if v2_rows:
    logger.error(
        "[full_cpcv-v2] pre-flight: {} prior row(s) already exist with "
        "variation_id={!r}.  Single-shot guard tripped — re-running "
        "requires that the prior row be tagged superseded_by per "
        "trials.log Policy (c).  Aborting.",
        len(v2_rows), VARIATION_ID,
    )
    sys.exit(1)

v1_smoke_rows = [
    r for r in prior_rows
    if r.get("trial_type") == "smoke"
    and r.get("variation_id") != VARIATION_ID
]
v1_fcpcv_rows = [
    r for r in prior_rows
    if r.get("trial_type") == "full_cpcv"
    and r.get("variation_id") != VARIATION_ID
]
if not v1_smoke_rows and not v1_fcpcv_rows:
    logger.error(
        "[full_cpcv-v2] pre-flight: no prior V1 smoke or full_cpcv "
        "rows for {}; expected V1 work to predate V2.  Aborting.",
        STRATEGY_ID,
    )
    sys.exit(1)

logger.info(
    "[full_cpcv-v2] pre-flight clean: prior V1 rows = "
    "{} smoke + {} full_cpcv; zero V2 rows.",
    len(v1_smoke_rows), len(v1_fcpcv_rows),
)


# ── 3. Headline run on full dev window ───────────────────────────────────────

logger.info("[full_cpcv-v2] running headline engine_perp on full dev window")
dev = holdout.load_dev(STRATEGY_ID)
df_spot = dev["spot"]
df_perp = dev["perp"]

# Months-back math identical to V1: archive fetches back from "now",
# so months is computed against now − data_start.
now_utc = pd.Timestamp.now(tz="UTC")
months_back_days = (now_utc - data_start).days
months = int(math.ceil(months_back_days / 30.44)) + 1
logger.info(
    f"[full_cpcv-v2] requesting funding months={months} "
    f"(now − data_start {months_back_days}d)"
)
funding_full = okx_funding.load_or_fetch_funding_history(
    legs["perp"], months=months,
)
funding_dev = funding_full[funding_full.index < holdout_start]

# Substrate-coverage assertion (mirrored from V1).
if funding_dev.index.min() > pd.Timestamp(data_start):
    logger.error(
        f"[full_cpcv-v2] SUBSTRATE COVERAGE FAILURE: "
        f"funding_dev earliest {funding_dev.index.min()} "
        f"is AFTER data_start {data_start}. Funding cache "
        f"does not cover the dev window's earliest blocks. "
        f"Check months-back math (months={months}) or extend "
        f"the Path-5 archive. Aborting before run_cpcv_perp."
    )
    sys.exit(1)
logger.info(
    f"[full_cpcv-v2] substrate coverage OK: funding earliest "
    f"{funding_dev.index.min()} <= data_start {data_start}"
)


def _make_v2_strategy() -> FundingRateHarvestStrategy:
    """Construct a V2-configured strategy and wire in funding history.

    The factory closes over `funding_dev` and the calibrated
    thresholds.  `set_funding_history` is the strategy's seam for
    threshold lookup without modifying engine_perp.
    """
    s = FundingRateHarvestStrategy(
        symbol=legs["spot"],
        timeframe=entry["timeframe"],
        min_funding_rate_entry=entry_threshold,
        exit_funding_rate_threshold=exit_threshold,
    )
    s.set_funding_history(funding_dev)
    return s


headline_strategy = _make_v2_strategy()
headline_result = run_perp(
    df_spot=df_spot,
    df_perp=df_perp,
    funding_history=funding_dev,
    strategy=headline_strategy,
    period_label="phase4b-full-cpcv-v2-headline",
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
    f"[full_cpcv-v2] headline: sharpe={sr_observed:.4f} "
    f"n_trades={n_trades_headline} "
    f"return_pct={headline_result.metrics.total_return_pct:+.2f}% "
    f"exit_reasons={dict(headline_exit_reasons)}"
)


# ── 4. CPCV block-Sharpe distribution via run_cpcv_perp ──────────────────────

_cpcv_log_capture: list[str] = []

def _capture_sink(message) -> None:
    record = message.record
    name = record["name"]
    if name in ("backtest.engine_perp", "paper_trading.perp_simulator"):
        _cpcv_log_capture.append(record["message"])

_capture_handler_id = logger.add(_capture_sink, level="INFO")

logger.info("[full_cpcv-v2] running run_cpcv_perp (n_blocks=10)")
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
    # V2 additions:
    "min_funding_rate_entry": entry_threshold,
    "exit_funding_rate_threshold": exit_threshold,
}
cpcv_result = run_cpcv_perp(
    strategy_id=STRATEGY_ID,
    params=PARAMS,
    config=cpcv_config,
    strategy_factory=_make_v2_strategy,
)
logger.remove(_capture_handler_id)


# ── 4b. Pre-record anomaly checks (A/B/C/D) ──────────────────────────────────
# Identical to V1 — same regex patterns, same thresholds.

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
    f"[full_cpcv-v2] anomaly inputs: "
    f"funding_settlements_processed_total="
    f"{funding_settlements_processed_total}  "
    f"callback_signal_events_total={callback_signal_events_total}  "
    f"block_exit_reasons={dict(_block_exit_reasons)}"
)

# ANOMALY A — funding events not actually processed.
if (
    callback_signal_events_total > 0
    and funding_settlements_processed_total
    < 0.85 * callback_signal_events_total
):
    logger.error(
        f"[full_cpcv-v2] ANOMALY A: funding cache likely stale — "
        f"callback expected {callback_signal_events_total} "
        f"settlements, simulator processed "
        f"{funding_settlements_processed_total}.  Aborting before "
        "record_trial."
    )
    sys.exit(1)

# ANOMALY B — exit-reason histogram dominated by backtest_end.
total_block_exits = sum(_block_exit_reasons.values())
backtest_end_share = (
    _block_exit_reasons.get("backtest_end", 0) / total_block_exits
    if total_block_exits > 0 else 0.0
)
if total_block_exits > 0 and backtest_end_share > 0.8:
    logger.error(
        f"[full_cpcv-v2] ANOMALY B: exit-reason histogram dominated "
        f"by backtest_end ({backtest_end_share*100:.1f}% of "
        f"{total_block_exits} closes).  Strategy didn't trade its "
        f"actual edge.  Block exit reasons: {dict(_block_exit_reasons)}.  "
        "Aborting before record_trial."
    )
    sys.exit(1)

# ANOMALY C — flat sharpe distribution (flag, do not abort).
sd_dict = cpcv_result.sharpe_distribution
_flat_distribution = (
    sd_dict["std"] < 0.05
    and -0.5 < sd_dict["mean"] < 0.5
)
if _flat_distribution:
    logger.warning(
        f"[full_cpcv-v2] ANOMALY C (flag, non-blocking): sharpe "
        f"distribution is flat — std={sd_dict['std']:.4f} "
        f"mean={sd_dict['mean']:.4f}."
    )

# ANOMALY D — per-block funding coverage.
zero_coverage_blocks = [
    bid for bid, n in _funding_settlements_processed.items()
    if n == 0
]
if zero_coverage_blocks:
    logger.error(
        f"[full_cpcv-v2] ANOMALY D: {len(zero_coverage_blocks)} "
        f"of {len(_funding_settlements_processed)} blocks had "
        f"funding_settlements=0 — block IDs "
        f"{sorted(zero_coverage_blocks)}.  Aborting before "
        f"record_trial."
    )
    sys.exit(1)


# ── 5. DSR on validation (dev) ───────────────────────────────────────────────

n_trials_pre = _trials.count_trials_for_dsr(STRATEGY_ID)
logger.info(
    f"[full_cpcv-v2] n_trials_for_dsr({STRATEGY_ID}) "
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
    )
finally:
    _t_mod.count_trials_for_dsr = _orig_count
logger.info(
    f"[full_cpcv-v2] DSR validation: dsr={dsr_result.dsr:.4f} "
    f"sr_zero_expected={dsr_result.sr_zero_expected:.4f} "
    f"sr_std={dsr_result.sr_std:.4f} T={dsr_result.t} "
    f"n_trials={dsr_result.n_trials}"
)


# ── 6. Verdict-tree preview ──────────────────────────────────────────────────

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
)

logger.info(
    f"[full_cpcv-v2] verdict={verdict.verdict} "
    f"trade_count_pass={verdict.trade_count_pass} "
    f"signal_event_count_pass={verdict.signal_event_count_pass} "
    f"mintrl_pass={verdict.mintrl_pass} "
    f"mt_mean_pass={verdict.mt_mean_pass} "
    f"baseline_pass={verdict.baseline_pass}"
)


# ── 7. Build full_cpcv event payload + record ────────────────────────────────

# Hypothesis text verbatim from V2 section in literature.md.  The
# regex anchors on the V2 section header, then captures the
# **Hypothesis.** paragraph up to the next paragraph break.
lit_text = LITERATURE_PATH.read_text(encoding="utf-8")
m = re.search(
    r"## Variation #2 -- `phase4b-threshold-entry-singlepair-btc-v2`"
    r".*?\*\*Hypothesis\.\*\*\s*(.*?)\n\n\*\*",
    lit_text, flags=re.DOTALL,
)
if m is None:
    logger.error(
        f"[full_cpcv-v2] could not extract V2 Hypothesis from "
        f"{LITERATURE_PATH}; aborting"
    )
    sys.exit(1)
hypothesis_text = m.group(1).strip()

notes = (
    f"V2 threshold-entry redesign. entry_threshold={entry_threshold:.6f} "
    f"exit_threshold={exit_threshold:.6f} calibrated from dev-window "
    f"33rd percentile of positive-funding sessions per "
    f"funding-rate-literature.md V2 hypothesis. Probe output: "
    f"scripts/phase_4b_v2_probe_output.json. Headline exit reasons: "
    f"{dict(headline_exit_reasons)}; sum across cpcv blocks of trade "
    f"counts: {sum(cpcv_result.trades_per_path)}; total signal events "
    f"(callback hours/cadence): {total_signal_events}; settlements "
    f"actually processed by simulator across blocks: "
    f"{funding_settlements_processed_total}; block exit reasons: "
    f"{dict(_block_exit_reasons)}."
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

logger.info("[full_cpcv-v2] recording trial row via trials.record_trial")
_trials.record_trial(event)


# ── 8. Verify row + per-block summary ────────────────────────────────────────

post_rows = list(
    _trials.read_trials(
        strategy_id=STRATEGY_ID, trial_type="full_cpcv",
    )
)
clean_post_v2 = [
    r for r in post_rows
    if r.get("variation_id") == VARIATION_ID
    and not r.get("superseded_by")
]
assert len(clean_post_v2) == 1, (
    f"expected exactly 1 clean V2 full_cpcv row after append, got "
    f"{len(clean_post_v2)}"
)
new_row = clean_post_v2[-1]
assert "cpcv" in new_row and "sharpe_distribution" in new_row["cpcv"], (
    "cpcv block missing sharpe_distribution"
)
assert "dsr_validation" in new_row, "dsr_validation missing from row"
assert (
    new_row.get("signal_event_count") is not None
    and int(new_row["signal_event_count"]) > 0
), "signal_event_count missing or zero"


# ── 9. Per-block table + flagged anomalies ───────────────────────────────────

per_block_sharpes = cpcv_result.per_block_sharpes
trades_per_path = cpcv_result.trades_per_path
events_per_block = cpcv_result.signal_events_per_block

print()
print("=" * 76)
print("Phase 4.B Variation #2 — full_cpcv summary")
print("=" * 76)
print(f"Strategy        : {STRATEGY_ID}")
print(f"Variation       : {VARIATION_ID}")
print(f"Entry threshold : {entry_threshold:.6f}")
print(f"Exit threshold  : {exit_threshold:.6f}")
print(
    f"Dev window      : {data_start.isoformat()} → "
    f"{holdout_start.isoformat()}"
)
print(f"n_blocks        : {cpcv_config.n_blocks}")
print(f"Sharpe (head)   : {sr_observed:.4f}")
sd = cpcv_result.sharpe_distribution
print(
    f"CPCV dist       : mean={sd['mean']:.4f}  std={sd['std']:.4f}  "
    f"p05={sd['quantiles']['p05']:.4f}  p25={sd['quantiles']['p25']:.4f}  "
    f"p50={sd['quantiles']['p50']:.4f}  p75={sd['quantiles']['p75']:.4f}  "
    f"p95={sd['quantiles']['p95']:.4f}  "
    f"min={min(s for s in per_block_sharpes if not math.isnan(s)):.4f}  "
    f"max={max(s for s in per_block_sharpes if not math.isnan(s)):.4f}"
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
    per_block_sharpes, trades_per_path, events_per_block or [None] * len(per_block_sharpes),
)):
    s_str = f"{s:.4f}" if not math.isnan(s) else "NaN"
    e_str = str(e) if e is not None else "—"
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
        anomalies.append(f"block {i}: extreme Sharpe {s:.4f} (≤-2 or ≥+6)")
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
