"""scripts/phase_4b_full_cpcv_v1.py — Phase 4.B Variation #1 first full_cpcv.

Replays the FundingRateHarvest_BTC dev window through
`backtest.cpcv_perp.run_cpcv_perp` with the post-gate-1+2 parameter
set (Path-(a) `exit_mr_ratio_threshold=0.01`, §5 tolerance widened
±5% chat 2026-05-02), computes DSR validation, and appends one
`trial_type='full_cpcv'` row to trials.log via the schema-validating
writer.

Pre-flight enforces "exactly one prior smoke row, zero prior
full_cpcv rows" so the script is single-shot — re-running is a
drift signal worth surfacing immediately.

Read-only with respect to manifest / sacred-harness files; the only
write is the trials.log append.
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


VARIATION_ID = "phase4b-delta-neutral-singlepair-btc-v1"
STRATEGY_ID = "FundingRateHarvest_BTC"
# Gate spec v2 (2026-06-11): explicit bar frequency for the
# units-correct DSR / MinTRL / verdict (manifest timeframe).
from backtest.dsr import bars_per_year_for_timeframe
BARS_PER_YEAR = bars_per_year_for_timeframe("1h")
LITERATURE_PATH = ROOT / "research" / "funding-rate-literature.md"


# ── 1. Manifest entry ────────────────────────────────────────────────────────

logger.info("[full_cpcv-v1] loading manifest")
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
    f"[full_cpcv-v1] manifest: timeframe={entry['timeframe']!r} "
    f"cadence={funding_cadence_hours}h legs={legs} "
    f"dev=[{data_start.isoformat()} → {holdout_start.isoformat()})"
)


# ── 2. Pre-flight ────────────────────────────────────────────────────────────
# Permitted prior states:
#   (a) [smoke]                              — clean first-run.
#   (b) [smoke, full_cpcv × N (all superseded)]
#                                            — re-run after one or
#                                              more prior degenerate
#                                              rows were tagged
#                                              superseded per
#                                              trials.log Policy (c).
# Anything else aborts.  The N-superseded generalisation
# (Mandate F, chat 2026-05-02) lets the script re-run after both
# the original degenerate row (stale-cache bug) and the partial-
# coverage row (months-back math bug) have been superseded —
# without further pre-flight edits per fix iteration.

prior_rows = list(_trials.read_trials(strategy_id=STRATEGY_ID))
smoke_rows = [r for r in prior_rows if r.get("trial_type") == "smoke"]
fcpcv_rows = [r for r in prior_rows if r.get("trial_type") == "full_cpcv"]
clean_fcpcv = [r for r in fcpcv_rows if not r.get("superseded_by")]

if len(smoke_rows) == 1 and len(clean_fcpcv) == 0:
    if len(fcpcv_rows) == 0:
        logger.info(
            "[full_cpcv-v1] pre-flight clean: prior rows = [smoke]"
        )
    else:
        logger.info(
            f"[full_cpcv-v1] pre-flight: {len(fcpcv_rows)} prior "
            f"full_cpcv rows all superseded; proceeding with re-run.  "
            f"Superseded SHAs: "
            + ", ".join(r["superseded_by"][:7] for r in fcpcv_rows)
        )
else:
    summary = [
        (
            r.get("trial_type"),
            (r.get("superseded_by") or "None")[:7],
        )
        for r in prior_rows
    ]
    logger.error(
        f"[full_cpcv-v1] pre-flight: unexpected prior-row state "
        f"{summary}.  Aborting."
    )
    sys.exit(1)


# ── 3. Headline run on full dev window ───────────────────────────────────────

# Used for two things downstream:
#   (a) the trial row's `sharpe` field carries the headline (full-
#       window) Sharpe, NOT the per-block CV mean — DSR deflates
#       the headline.
#   (b) per-block exit-reason histogram is aggregated across the
#       cpcv runs separately; the headline run gives the full-
#       window count for cross-checking.

logger.info("[full_cpcv-v1] running headline engine_perp on full dev window")
dev = holdout.load_dev(STRATEGY_ID)
df_spot = dev["spot"]
df_perp = dev["perp"]

# Funding cache must cover from data_start back to NOW (the
# archive's youngest boundary), not just the dev_span.  The dev
# window ends at holdout_start (Sep 2025), but the archive fetches
# back from "now" — so months-back is computed against
# now − data_start, not holdout_start − data_start.  (Bug
# diagnosed in chat 2026-05-02 after trial_id 2b9bd83b…'s blocks
# 0–1 saw funding_settlements=0; the dev_span math missed the
# now-vs-holdout asymmetry.)
now_utc = pd.Timestamp.now(tz="UTC")
months_back_days = (now_utc - data_start).days
months = int(math.ceil(months_back_days / 30.44)) + 1
logger.info(
    f"[full_cpcv-v1] requesting funding months={months} "
    f"(now − data_start {months_back_days}d)"
)
funding_full = okx_funding.load_or_fetch_funding_history(
    legs["perp"], months=months,
)
funding_dev = funding_full[funding_full.index < holdout_start]

# Substrate-coverage assertion (chat 2026-05-02, post-trial_id
# 2b9bd83b... supersession diagnosis): funding_dev must start at or
# before data_start, otherwise early CPCV blocks land with zero
# funding settlements and don't actually exercise the strategy.
# ANOMALY A's aggregate-ratio floor missed this for 2 of 10 blocks;
# the structural check is per-block coverage, expressed at the
# funding-history level here (assert before run_cpcv_perp invokes
# its per-block split).
if funding_dev.index.min() > pd.Timestamp(data_start):
    logger.error(
        f"[full_cpcv-v1] SUBSTRATE COVERAGE FAILURE: "
        f"funding_dev earliest {funding_dev.index.min()} "
        f"is AFTER data_start {data_start}. Funding cache "
        f"does not cover the dev window's earliest blocks. "
        f"Check months-back math (months={months}) or extend "
        f"the Path-5 archive. Aborting before run_cpcv_perp."
    )
    sys.exit(1)
logger.info(
    f"[full_cpcv-v1] substrate coverage OK: funding earliest "
    f"{funding_dev.index.min()} <= data_start {data_start}"
)

headline_strategy = FundingRateHarvestStrategy(
    symbol=legs["spot"], timeframe=entry["timeframe"],
)
headline_result = run_perp(
    df_spot=df_spot,
    df_perp=df_perp,
    funding_history=funding_dev,
    strategy=headline_strategy,
    period_label="phase4b-full-cpcv-v1-headline",
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
    f"[full_cpcv-v1] headline: sharpe={sr_observed:.4f} "
    f"n_trades={n_trades_headline} "
    f"return_pct={headline_result.metrics.total_return_pct:+.2f}% "
    f"exit_reasons={dict(headline_exit_reasons)}"
)


# ── 4. CPCV block-Sharpe distribution via run_cpcv_perp ──────────────────────

# Loguru sink that captures per-block engine_perp summary lines and
# every CLOSE event so we can attribute exits across all blocks
# without extending CPCVResult.  Sink stays installed only for the
# duration of the cpcv run, then is removed.
_cpcv_log_capture: list[str] = []

def _capture_sink(message) -> None:
    record = message.record
    name = record["name"]
    if name in ("backtest.engine_perp", "paper_trading.perp_simulator"):
        _cpcv_log_capture.append(record["message"])

_capture_handler_id = logger.add(_capture_sink, level="INFO")

logger.info("[full_cpcv-v1] running run_cpcv_perp (n_blocks=10)")
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
    "exit_mr_ratio_threshold": 0.01,  # Path (a) cushion-threshold fix
    "flip_exit_n": 4,
    "flip_exit_threshold": 0.0,
    "initial_balance": 10000.0,
}
cpcv_result = run_cpcv_perp(
    strategy_id=STRATEGY_ID,
    params=PARAMS,
    config=cpcv_config,
    strategy_factory=lambda: FundingRateHarvestStrategy(
        symbol=legs["spot"], timeframe=entry["timeframe"],
    ),
)
logger.remove(_capture_handler_id)


# ── 4b. Pre-record anomaly checks (A/B/C) ────────────────────────────────────
# These ABORT before record_trial fires so a stale-cache or
# silent-degenerate run cannot pollute trials.log.  The post-record
# anomaly block (later in the script) handles non-blocking flags
# (extreme block Sharpes, zero-trade blocks under different
# definitions, etc.).

# Aggregate per-block engine_perp summary lines.
# Pattern: "cpcv-perp-block-N complete | return=X% | sharpe=Y |
#           trades=Z | funding_violations=W"
# We can't read funding_settlements directly from the summary line
# (engine_perp logs it at "Starting" not "complete").  Instead we
# parse "Starting cpcv-perp-block-N run | ... | funding_settlements=K"
# and extract K per block.
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

# Aggregate exit-reason histogram across all blocks via the
# `[PERP] CLOSE delta-neutral | reason=<R>` log lines.
_REASON_RE = re.compile(
    r"\[PERP\] CLOSE delta-neutral \| reason=([\w_]+)"
)
_block_exit_reasons: Counter = Counter()
for line in _cpcv_log_capture:
    m = _REASON_RE.search(line)
    if m:
        _block_exit_reasons[m.group(1)] += 1

# Callback-derived signal_event_count total (the "expected" funding
# count if every block's full hours-window had been processed).
callback_signal_events_total = (
    int(sum(cpcv_result.signal_events_per_block))
    if cpcv_result.signal_events_per_block is not None
    else 0
)

logger.info(
    f"[full_cpcv-v1] anomaly inputs: "
    f"funding_settlements_processed_total="
    f"{funding_settlements_processed_total}  "
    f"callback_signal_events_total={callback_signal_events_total}  "
    f"block_exit_reasons={dict(_block_exit_reasons)}"
)

# ANOMALY A — funding events not actually processed.
# Tightened to 0.85 (chat 2026-05-02): the original 0.5 floor was
# permissive enough that the partial-coverage harness bug
# (trial_id e7eba18a..., 1981/2620 = 0.756 ratio) squeaked
# through.  After the harness-layer fix in this commit the ratio
# should land near ~1.0; 0.85 leaves headroom for legitimate
# small gaps (occasional missed settlements during OKX outages)
# without permitting harness-layer math errors.
if (
    callback_signal_events_total > 0
    and funding_settlements_processed_total
    < 0.85 * callback_signal_events_total
):
    logger.error(
        f"[full_cpcv-v1] ANOMALY A: funding cache likely stale — "
        f"callback expected {callback_signal_events_total} "
        f"settlements, simulator processed "
        f"{funding_settlements_processed_total}.  Aborting before "
        "record_trial.  Investigate: "
        "  (1) `ls -la backtest/cache/perp_funding/` for thin "
        "      pre-Path-5 caches.  "
        "  (2) `python -c \"from data.okx_funding import "
        "load_or_fetch_funding_history; "
        "print(len(load_or_fetch_funding_history('BTC/USDT', "
        "months=29)))\"` to verify a clean fetch."
    )
    sys.exit(1)

# ANOMALY B — exit-reason histogram dominated by backtest_end
# A clean funding-harvest run produces many funding_flip exits.
# When > 80% of closes are backtest_end, the strategy did not
# actually trade its edge — likely funding-cache or signal-
# boundary issue.
total_block_exits = sum(_block_exit_reasons.values())
backtest_end_share = (
    _block_exit_reasons.get("backtest_end", 0) / total_block_exits
    if total_block_exits > 0 else 0.0
)
if total_block_exits > 0 and backtest_end_share > 0.8:
    logger.error(
        f"[full_cpcv-v1] ANOMALY B: exit-reason histogram dominated "
        f"by backtest_end ({backtest_end_share*100:.1f}% of "
        f"{total_block_exits} closes).  Strategy didn't trade its "
        f"actual edge.  Likely funding-cache or signal-boundary "
        f"issue.  Block exit reasons: {dict(_block_exit_reasons)}.  "
        "Aborting before record_trial."
    )
    sys.exit(1)

# ANOMALY C — flat sharpe distribution (flag, do not abort)
sd_dict = cpcv_result.sharpe_distribution
_flat_distribution = (
    sd_dict["std"] < 0.05
    and -0.5 < sd_dict["mean"] < 0.5
)
if _flat_distribution:
    logger.warning(
        f"[full_cpcv-v1] ANOMALY C (flag, non-blocking): sharpe "
        f"distribution is flat — std={sd_dict['std']:.4f} "
        f"mean={sd_dict['mean']:.4f}.  All blocks did roughly the "
        "same near-zero thing; check whether the strategy actually "
        "trades or whether signals are silently identical across "
        "blocks."
    )

# ANOMALY D — per-block funding coverage (chat 2026-05-02).
# ANOMALY A's aggregate floor catches large absolute coverage
# gaps; ANOMALY D catches the structural failure where some
# blocks have ZERO funding events while others have full
# coverage.  A single zero-coverage block means that block didn't
# exercise the strategy's edge, regardless of what the aggregate
# ratio says.  This is the structural check the 2b9bd83b... and
# e7eba18a... regressions slipped past.
zero_coverage_blocks = [
    bid for bid, n in _funding_settlements_processed.items()
    if n == 0
]
if zero_coverage_blocks:
    logger.error(
        f"[full_cpcv-v1] ANOMALY D: {len(zero_coverage_blocks)} "
        f"of {len(_funding_settlements_processed)} blocks had "
        f"funding_settlements=0 — block IDs "
        f"{sorted(zero_coverage_blocks)}.  Strategy cannot have "
        f"exercised its edge in those blocks.  Aborting before "
        f"record_trial.  Likely cause: harness-layer months-back "
        f"math undersized the funding fetch; verify "
        f"backtest/cpcv_perp.py:_funding_months_window is fixed."
    )
    sys.exit(1)


# ── 5. DSR on validation (dev) ───────────────────────────────────────────────

# count_trials_for_dsr reflects the count BEFORE this row is appended;
# the BLP convention is to compute DSR with the pre-append N (this
# trial counts as the trial-being-deflated, not as part of the
# inflation budget).
n_trials_pre = _trials.count_trials_for_dsr(STRATEGY_ID)
logger.info(
    f"[full_cpcv-v1] n_trials_for_dsr({STRATEGY_ID}) "
    f"pre-append = {n_trials_pre}"
)
# DSR helper requires n_trials >= 1.  If trials.log has no full_cpcv
# rows for this strategy (smoke rows are excluded from the DSR count
# per validation_framework.md), pass n_trials=1 explicitly: this
# trial is its own deflation budget.
n_trials_for_dsr = max(n_trials_pre + 1, 1)

# dsr_from_cpcv_result reads count_trials_for_dsr internally.  It
# returns 0 here (smoke excluded), so we patch it with our explicit
# n_trials via a thin re-call into deflated_sharpe — but the
# higher-level helper handles strategy_id lookup itself.  Use the
# helper directly; it will read n_trials_pre = 0 and raise if we
# don't pre-seed.  Simplest: monkeypatch the lookup for this script.
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
    f"[full_cpcv-v1] DSR validation: dsr={dsr_result.dsr:.4f} "
    f"sr_zero_expected={dsr_result.sr_zero_expected:.4f} "
    f"sr_std={dsr_result.sr_std:.4f} T={dsr_result.t} "
    f"n_trials={dsr_result.n_trials}"
)


# ── 6. Verdict-tree preview ──────────────────────────────────────────────────

valid_returns = [r for r in cpcv_result.per_block_returns if r.size > 0]
concat_returns = (
    np.concatenate(valid_returns) if valid_returns else np.array([])
)
# Baseline for verdict tree: spot leg over dev window (matches the
# perp leg's time index by construction since legs share the same
# manifest entry).
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
    f"[full_cpcv-v1] verdict={verdict.verdict} "
    f"trade_count_pass={verdict.trade_count_pass} "
    f"signal_event_count_pass={verdict.signal_event_count_pass} "
    f"mintrl_pass={verdict.mintrl_pass} "
    f"mt_mean_pass={verdict.mt_mean_pass} "
    f"baseline_pass={verdict.baseline_pass}"
)


# ── 7. Build full_cpcv event payload + record ────────────────────────────────

# Hypothesis text verbatim from literature.md (mirrors smoke writer).
lit_text = LITERATURE_PATH.read_text(encoding="utf-8")
m = re.search(
    r"\*\*Hypothesis\.\*\*\s*(.*?)\n\n\*\*Substrate\.\*\*",
    lit_text, flags=re.DOTALL,
)
if m is None:
    logger.error(
        f"[full_cpcv-v1] could not extract Hypothesis from "
        f"{LITERATURE_PATH}; aborting"
    )
    sys.exit(1)
hypothesis_text = m.group(1).strip()

notes = (
    "Variation #1 first full_cpcv.  Gate-2 audit (chat 2026-05-02) "
    "decision 'ii: tolerance miscalibrated': max basis-at-exit "
    "2.67%, p95 1.32%, funding_cash_share 93.22%, 16 funding_flip "
    "exits in smoke.  §5 tolerance widened from ±1% to ±5% (commit "
    "pending).  Path-(a) cushion-threshold fix in effect "
    "(exit_mr_ratio_threshold=0.01 = literature account margin "
    "ratio).  Headline exit reasons: "
    f"{dict(headline_exit_reasons)}; sum across cpcv blocks of "
    f"trade counts: {sum(cpcv_result.trades_per_path)}; total "
    f"signal events (callback hours/cadence): "
    f"{total_signal_events}; settlements actually processed by "
    f"simulator across blocks: {funding_settlements_processed_total}; "
    f"block exit reasons: {dict(_block_exit_reasons)}."
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

logger.info("[full_cpcv-v1] recording trial row via trials.record_trial")
_trials.record_trial(
    event,
    # Gate spec v2 (2026-06-11): persist the per-bar series the
    # verdict ran on (audit: never saved -> S1/bootstrap blocked).
    per_bar_returns=concat_returns,
    per_bar_benchmark=(
        baseline_df["close"].pct_change().dropna().values.astype(float)
    ),
)


# ── 8. Verify row + per-block summary ────────────────────────────────────────

post_rows = list(
    _trials.read_trials(
        strategy_id=STRATEGY_ID, trial_type="full_cpcv",
    )
)
# Filter out rows tagged superseded_by (Policy-(c) invalidation
# pattern); we expect exactly one clean (unsuperseded) row after
# this append.  Mirrors the pre-flight's supersession awareness.
clean_post = [r for r in post_rows if not r.get("superseded_by")]
assert len(clean_post) == 1, (
    f"expected exactly 1 clean full_cpcv row after append, got "
    f"{len(clean_post)} (total full_cpcv rows = {len(post_rows)}, "
    f"of which {len(post_rows) - len(clean_post)} are superseded)"
)
new_row = clean_post[-1]
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

# Approximate per-block exit reasons by re-walking the headline result's
# trade history binned by entry_time into n_blocks chunks of dev window.
# (Per-block sims don't return their exit_reason histograms via
# CPCVResult; the headline run is a sufficient check on aggregate
# behaviour for the row's notes field, which we already populated.
# Per-block exit-reason resolution would require extending CPCVResult
# — out of scope per autonomy table.)
print()
print("=" * 76)
print("Phase 4.B Variation #1 — full_cpcv summary")
print("=" * 76)
print(f"Strategy        : {STRATEGY_ID}")
print(f"Variation       : {VARIATION_ID}")
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

# Anomaly flags.
anomalies: list[str] = []
for i, s in enumerate(per_block_sharpes):
    if not math.isnan(s) and (s <= -2.0 or s >= 6.0):
        anomalies.append(f"block {i}: extreme Sharpe {s:.4f} (≤-2 or ≥+6)")
for i, t in enumerate(trades_per_path):
    if t == 0:
        anomalies.append(f"block {i}: zero trades (degenerate path)")
if events_per_block is not None:
    # Floor: 8h cadence × block_hours, where block_hours ≈ dev_hours / n_blocks
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
