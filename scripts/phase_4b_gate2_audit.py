"""scripts/phase_4b_gate2_audit.py — Gate 2 (combined-position-sanity) audit.

Replays the Phase 4.B Variation #1 dev window through engine_perp.run_perp
with the Path-(a) post-fix `exit_mr_ratio_threshold=0.01`, then reads
PerpSimulator.exit_forensics to attribute funding_flip-exit closes to
their basis, per-leg PnL, and funding-cash components.

Audit, NOT a trial.  Does NOT call trials.record_trial.  Does NOT modify
trials.log, holdout_manifest.json, or any sacred-harness file.

Decision rule (defaults; thresholds noted in report so chat can override):
  max_basis    = max(basis_at_exit_abs_pct) over funding_flip exits
  fcs          = funding_cash_share
  if   fcs >= 0.80 and max_basis <= 0.03 → "ii: tolerance miscalibrated"
  elif fcs >= 0.80 and max_basis  > 0.03 → "i_clean: real dislocations,
                                              funding-dominated attribution"
  elif fcs <  0.50                       → "i_dirty: silent directional"
  else                                   → "ambiguous"

Run from repo root: ``python scripts/phase_4b_gate2_audit.py``
"""

from __future__ import annotations

import math
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from loguru import logger

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import backtest.holdout as holdout
from backtest.engine_perp import run_perp
from data import okx_funding
from strategies.funding_rate_harvest import FundingRateHarvestStrategy


STRATEGY_ID = "FundingRateHarvest_BTC"

# Decision-rule thresholds (defaults; surfaced in report so chat can override).
FUNDING_DOMINATED_FCS_FLOOR = 0.80
DISLOCATION_BASIS_FLOOR = 0.03
DIRTY_FCS_CEILING = 0.50


# ── 1. Manifest entry ────────────────────────────────────────────────────────

manifest = holdout.load_manifest()
entry = manifest[STRATEGY_ID]
funding_cadence_hours = int(entry.get("funding_cadence_hours", 8))
legs = entry["legs"]
holdout_start = pd.Timestamp(entry["holdout_start"])
data_start = pd.Timestamp(entry["data_start"])

logger.info(
    f"[gate2-audit] dev window {data_start.isoformat()} → "
    f"{holdout_start.isoformat()}; cadence={funding_cadence_hours}h"
)


# ── 2. Dev legs + funding ────────────────────────────────────────────────────

dev = holdout.load_dev(STRATEGY_ID)
df_spot = dev["spot"]
df_perp = dev["perp"]

dev_span_days = (holdout_start - data_start).days
months = int(math.ceil(dev_span_days / 30.44)) + 1
funding_full = okx_funding.load_or_fetch_funding_history(
    legs["perp"], months=months,
)
funding_dev = funding_full[funding_full.index < holdout_start]

logger.info(
    f"[gate2-audit] dev legs: spot={len(df_spot)} perp={len(df_perp)}; "
    f"funding_dev={len(funding_dev)} settlements"
)


# ── 3. Run engine_perp with Path-(a) post-fix params ─────────────────────────

strategy = FundingRateHarvestStrategy(
    symbol=legs["spot"], timeframe=entry["timeframe"],
)
result = run_perp(
    df_spot=df_spot,
    df_perp=df_perp,
    funding_history=funding_dev,
    strategy=strategy,
    period_label="phase4b-gate2-audit",
    initial_balance=10_000.0,
    leverage=5.0,
    margin_mode="cross",
    spot_symbol=legs["spot"],
    perp_symbol=legs["perp"],
    flip_exit_n=4,
    flip_exit_threshold=0.0,
    exit_mr_ratio_threshold=0.01,  # Path (a) post-fix
)


# ── 4. Pull exit_forensics off the simulator ─────────────────────────────────

# The strategy doesn't hold a reference to the simulator; engine_perp
# constructs and discards it.  But the close events were already
# logged into PerpSimulator.exit_forensics during the run, and that
# state lives on the simulator instance the engine_perp loop used.
# Since engine_perp returns BacktestResult only, we replicate the
# audit by re-instantiating and running again to keep this script
# self-contained — slightly redundant but matches the scoping
# constraint of not touching engine_perp's return shape.
#
# Cleaner alternative: build a thin replay that exposes the
# simulator.  Implemented inline here to keep the patch surface
# minimal.

from paper_trading.perp_simulator import PerpSimulator  # noqa: E402
from strategies.base import Signal  # noqa: E402

# Mirror engine_perp.run_perp's bar loop, capturing the simulator.
common_idx = df_spot.index.intersection(df_perp.index).sort_values()
df_spot_aligned = df_spot.loc[common_idx]
df_perp_aligned = df_perp.loc[common_idx]
funding_window = funding_dev[
    (funding_dev.index >= common_idx[0])
    & (funding_dev.index <= common_idx[-1])
]
sim = PerpSimulator(
    initial_balance=10_000.0,
    spot_symbol=legs["spot"],
    perp_symbol=legs["perp"],
    leverage=5.0,
    flip_exit_n=4,
    flip_exit_threshold=0.0,
    exit_mr_ratio_threshold=0.01,
    margin_mode="cross",
)
strategy_audit = FundingRateHarvestStrategy(
    symbol=legs["spot"], timeframe=entry["timeframe"],
)
WARM = 50
for i in range(WARM, len(common_idx)):
    ts = common_idx[i]
    spot_close = float(df_spot_aligned.iloc[i]["close"])
    perp_high = float(df_perp_aligned.iloc[i]["high"])
    perp_low = float(df_perp_aligned.iloc[i]["low"])
    perp_close = float(df_perp_aligned.iloc[i]["close"])
    sim.update_spot_close(spot_close)
    if len(funding_window) > 0:
        prev_ts = common_idx[i - 1]
        settlements = funding_window[
            (funding_window.index > prev_ts) & (funding_window.index <= ts)
        ]
        for _, srow in settlements.iterrows():
            sim.apply_funding_settlement(
                float(srow["funding_rate"]), float(srow["mark_price"]),
            )
            if sim.position is None:
                break
    df_slice = df_perp_aligned.iloc[: i + 1]
    try:
        signal = strategy_audit.generate_signal(df_slice)
    except ValueError:
        continue
    if signal.action != "HOLD":
        sim.execute_signal(signal, perp_close)
    sim.tick_ohlcv_candle(high=perp_high, low=perp_low, close=perp_close)
# Force-close at end so backtest_end exits show up in the ledger.
if sim.position is not None:
    sim.update_spot_close(float(df_spot_aligned.iloc[-1]["close"]))
    perp_last = float(df_perp_aligned.iloc[-1]["close"])
    sim.execute_signal(
        Signal(
            action="SELL", strategy=strategy_audit.name,
            price=perp_last, reason="backtest_end",
            order_type="market",
        ),
        perp_last,
    )

forensics = sim.exit_forensics
logger.info(
    f"[gate2-audit] exit_forensics rows={len(forensics)} "
    f"(reasons={Counter(r['exit_reason'] for r in forensics)})"
)


# ── 5. Filter to funding_flip exits and compute distributions ────────────────

ff = [r for r in forensics if r["exit_reason"] == "funding_flip"]
n_ff = len(ff)


def _quantiles(vals: list[float]) -> dict:
    if not vals:
        return {k: float("nan") for k in ("p25", "p50", "p75", "p95", "max")}
    import numpy as np
    arr = np.asarray(vals, dtype=float)
    qs = np.percentile(arr, [25, 50, 75, 95])
    return {
        "p25": float(qs[0]),
        "p50": float(qs[1]),
        "p75": float(qs[2]),
        "p95": float(qs[3]),
        "max": float(arr.max()),
    }


basis_pcts = [r["basis_at_exit_abs_pct"] for r in ff]
basis_dist = _quantiles(basis_pcts)


# ── 6. Per-trade attribution (sorted by basis_pct desc) ──────────────────────

ff_sorted = sorted(
    ff, key=lambda r: r["basis_at_exit_abs_pct"], reverse=True,
)


# ── 7. Aggregate shares ──────────────────────────────────────────────────────

if ff:
    basis_pnl_combined = sum(r["spot_pnl"] + r["perp_pnl"] for r in ff)
    funding_cash_total = sum(r["funding_cash"] for r in ff)
    gross_components = abs(basis_pnl_combined) + abs(funding_cash_total)
    basis_pnl_share = (
        abs(basis_pnl_combined) / gross_components
        if gross_components > 0 else 0.0
    )
    funding_cash_share = (
        abs(funding_cash_total) / gross_components
        if gross_components > 0 else 0.0
    )
    max_basis = max(basis_pcts)
else:
    basis_pnl_combined = 0.0
    funding_cash_total = 0.0
    gross_components = 0.0
    basis_pnl_share = float("nan")
    funding_cash_share = float("nan")
    max_basis = float("nan")


# ── 8. Decision rule ─────────────────────────────────────────────────────────

if n_ff == 0:
    decision = "no funding_flip exits in window — N/A"
elif (
    funding_cash_share >= FUNDING_DOMINATED_FCS_FLOOR
    and max_basis <= DISLOCATION_BASIS_FLOOR
):
    decision = "ii: tolerance miscalibrated"
elif (
    funding_cash_share >= FUNDING_DOMINATED_FCS_FLOOR
    and max_basis > DISLOCATION_BASIS_FLOOR
):
    decision = "i_clean: real dislocations, funding-dominated attribution"
elif funding_cash_share < DIRTY_FCS_CEILING:
    decision = "i_dirty: silent directional"
else:
    decision = "ambiguous"


# ── 9. Print report ──────────────────────────────────────────────────────────

print()
print("=" * 76)
print("Phase 4.B Gate 2 audit — combined-position-sanity attribution")
print("=" * 76)
print(f"Strategy             : {STRATEGY_ID}")
print(f"Dev window           : {data_start.isoformat()} → "
      f"{holdout_start.isoformat()}")
print(
    f"Total exits          : {len(forensics)} "
    f"(reasons {dict(Counter(r['exit_reason'] for r in forensics))})"
)
print(f"funding_flip exits   : {n_ff}")
print()
print("--- A. |basis_at_exit_abs_pct| over funding_flip exits ---")
if n_ff > 0:
    print(
        f"  count={n_ff}  p25={basis_dist['p25']*100:.3f}%  "
        f"p50={basis_dist['p50']*100:.3f}%  p75={basis_dist['p75']*100:.3f}%  "
        f"p95={basis_dist['p95']*100:.3f}%  max={basis_dist['max']*100:.3f}%"
    )
else:
    print("  (no funding_flip exits)")
print()
print("--- B. Per-trade attribution (top 10 by basis_pct) ---")
print(
    f"  {'exit_time':<25} "
    f"{'basis%':>8} {'spot_pnl':>10} {'perp_pnl':>10} "
    f"{'sp+pp':>10} {'funding':>10} {'total':>10}"
)
for r in ff_sorted[:10]:
    print(
        f"  {r['exit_time'].isoformat()[:19]:<25} "
        f"{r['basis_at_exit_abs_pct']*100:>7.3f}% "
        f"{r['spot_pnl']:>+10.2f} {r['perp_pnl']:>+10.2f} "
        f"{r['spot_pnl']+r['perp_pnl']:>+10.2f} "
        f"{r['funding_cash']:>+10.2f} {r['total_pnl']:>+10.2f}"
    )
if n_ff > 10:
    print(f"  ... ({n_ff - 10} more)")
print()
print("--- C. Aggregate shares across funding_flip exits ---")
print(f"  basis_pnl_combined  = ${basis_pnl_combined:+,.2f}")
print(f"  funding_cash_total  = ${funding_cash_total:+,.2f}")
print(f"  gross_components    = ${gross_components:,.2f}")
print(
    f"  basis_pnl_share     = "
    f"{basis_pnl_share*100:.2f}%" if not math.isnan(basis_pnl_share)
    else "  basis_pnl_share     = NaN"
)
print(
    f"  funding_cash_share  = "
    f"{funding_cash_share*100:.2f}%" if not math.isnan(funding_cash_share)
    else "  funding_cash_share  = NaN"
)
print()
print("--- D. Decision rule ---")
print(
    f"  thresholds: fcs_floor={FUNDING_DOMINATED_FCS_FLOOR:.2f}  "
    f"basis_dislocation_floor={DISLOCATION_BASIS_FLOOR:.2f}  "
    f"dirty_fcs_ceiling={DIRTY_FCS_CEILING:.2f}"
)
print(
    f"  inputs:     max_basis={max_basis*100:.3f}%  "
    f"funding_cash_share={funding_cash_share*100:.2f}%"
    if not math.isnan(max_basis)
    else f"  inputs:     n_ff={n_ff} (no funding_flip exits to evaluate)"
)
print(f"  decision:   {decision}")
print("=" * 76)


# ── 10. Re-export for downstream consumers ───────────────────────────────────

# Persist a JSON summary so the full_cpcv runner / chat agent can
# read the audit result without re-running.
import json
out_path = Path("/tmp/phase_4b_gate2_audit.json")
out_path.write_text(json.dumps({
    "ts": datetime.now(timezone.utc).isoformat(),
    "strategy_id": STRATEGY_ID,
    "n_funding_flip_exits": n_ff,
    "max_basis_at_exit_abs_pct": (
        None if math.isnan(max_basis) else max_basis
    ),
    "funding_cash_share": (
        None if math.isnan(funding_cash_share) else funding_cash_share
    ),
    "basis_pnl_share": (
        None if math.isnan(basis_pnl_share) else basis_pnl_share
    ),
    "basis_pnl_combined": basis_pnl_combined,
    "funding_cash_total": funding_cash_total,
    "decision": decision,
    "thresholds": {
        "funding_dominated_fcs_floor": FUNDING_DOMINATED_FCS_FLOOR,
        "dislocation_basis_floor": DISLOCATION_BASIS_FLOOR,
        "dirty_fcs_ceiling": DIRTY_FCS_CEILING,
    },
    "basis_distribution_pct": {
        k: (v * 100.0 if not math.isnan(v) else None)
        for k, v in basis_dist.items()
    },
}, indent=2, default=str))
print(f"\nAudit summary written to {out_path}")
