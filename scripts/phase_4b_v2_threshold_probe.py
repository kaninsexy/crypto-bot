"""scripts/phase_4b_v2_threshold_probe.py — V2 threshold calibration probe.

Reads the dev-window funding history for FundingRateHarvest_BTC,
computes the V2 entry / exit thresholds per the pre-specified rule
documented in research/funding-rate-literature.md § "Variation #2 --
phase4b-threshold-entry-singlepair-btc-v2", surfaces them for human
review, and writes them to scripts/phase_4b_v2_probe_output.json.

Calibration rule (from the V2 hypothesis-of-record):
  entry_threshold = 33rd percentile of all positive-funding sessions
                    in the dev window (annualised = rate_per_8h × 1095)
  exit_threshold  = 50% of entry_threshold

The probe NEVER touches holdout data: the funding history is sliced
to `funding.index < holdout_start` before any percentile is taken.
The output JSON is a runtime artefact (gitignored); the V2 full_cpcv
script reads it to set strategy parameters.
"""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from loguru import logger

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import backtest.holdout as holdout
from data import okx_funding


STRATEGY_ID = "FundingRateHarvest_BTC"
OUTPUT_PATH = ROOT / "scripts" / "phase_4b_v2_probe_output.json"
ANNUALISATION = 1095  # 8h settlements per year
ENTRY_PERCENTILE = 33
EXIT_FRACTION = 0.5
MIN_POSITIVE_SESSIONS = 30


def main() -> None:
    logger.info("[v2-probe] loading manifest entry for {}", STRATEGY_ID)
    manifest = holdout.load_manifest()
    if STRATEGY_ID not in manifest:
        logger.error("[v2-probe] manifest missing entry for {}; aborting",
                     STRATEGY_ID)
        sys.exit(1)
    entry = manifest[STRATEGY_ID]
    holdout_start = pd.Timestamp(entry["holdout_start"])
    data_start = pd.Timestamp(entry["data_start"])
    legs = entry["legs"]
    funding_cadence_hours = int(entry.get("funding_cadence_hours", 8))
    logger.info(
        "[v2-probe] manifest: cadence={}h legs={} dev=[{} → {})",
        funding_cadence_hours, legs,
        data_start.isoformat(), holdout_start.isoformat(),
    )

    # Months-back math mirrored from phase_4b_full_cpcv_v1.py: the
    # archive fetches back from "now", so the months count is computed
    # against now − data_start, not holdout_start − data_start.
    now_utc = pd.Timestamp.now(tz="UTC")
    months_back_days = (now_utc - data_start).days
    months = int(math.ceil(months_back_days / 30.44)) + 1
    logger.info(
        "[v2-probe] requesting funding months={} (now − data_start {}d)",
        months, months_back_days,
    )
    funding_full = okx_funding.load_or_fetch_funding_history(
        legs["perp"], months=months,
    )

    # Substrate-coverage assertion: funding cache must reach back at
    # least to data_start so dev-window percentiles are not biased by
    # missing early data.
    if funding_full.empty:
        logger.error("[v2-probe] funding history empty; aborting")
        sys.exit(1)
    if funding_full.index.min() > pd.Timestamp(data_start):
        logger.error(
            "[v2-probe] SUBSTRATE COVERAGE FAILURE: funding earliest {} "
            "is AFTER data_start {}. Funding cache does not cover the "
            "dev window's earliest sessions. Check months-back math "
            "(months={}) or extend the Path-5 archive. Aborting.",
            funding_full.index.min(), data_start, months,
        )
        sys.exit(1)
    logger.info(
        "[v2-probe] substrate coverage OK: funding earliest {} ≤ "
        "data_start {}", funding_full.index.min(), data_start,
    )

    # Slice to dev window only — holdout never touched here.
    funding_dev = funding_full[funding_full.index < holdout_start]
    n_total = int(len(funding_dev))
    if n_total == 0:
        logger.error("[v2-probe] no dev-window sessions; aborting")
        sys.exit(1)

    # Annualise each rate.
    rates_per_8h = funding_dev["funding_rate"].astype(float).to_numpy()
    rates_annual = rates_per_8h * ANNUALISATION

    # Filter to positive-funding sessions only (annualised > 0).
    positive_mask = rates_annual > 0.0
    positive_sessions = rates_annual[positive_mask]
    n_positive = int(positive_sessions.size)
    pct_positive = (n_positive / n_total) if n_total > 0 else 0.0

    if n_positive < MIN_POSITIVE_SESSIONS:
        logger.error(
            "[v2-probe] only {} positive-funding sessions in dev window "
            "(< {} required); insufficient data for a robust "
            "percentile. Aborting.",
            n_positive, MIN_POSITIVE_SESSIONS,
        )
        sys.exit(1)

    entry_threshold = float(np.percentile(positive_sessions, ENTRY_PERCENTILE))
    exit_threshold = float(entry_threshold * EXIT_FRACTION)

    print()
    print("=" * 72)
    print("Phase 4.B V2 threshold calibration probe")
    print("=" * 72)
    print(f"  Strategy           : {STRATEGY_ID}")
    print(f"  Dev window         : {data_start.isoformat()} → "
          f"{holdout_start.isoformat()}")
    print(f"  Total sessions     : {n_total}")
    print(f"  Positive sessions  : {n_positive} ({pct_positive*100:.2f}%)")
    print(f"  Entry threshold    : {entry_threshold:.6f}  "
          f"({ENTRY_PERCENTILE}rd pct of positive, annualised)")
    print(f"  Exit threshold     : {exit_threshold:.6f}  "
          f"({int(EXIT_FRACTION*100)}% of entry)")
    print(f"  Annualisation      : rate_per_8h × {ANNUALISATION}")
    print("=" * 72)
    print()

    # Persist to JSON.  Gitignored — runtime artefact, not committed.
    payload = {
        "entry_threshold": entry_threshold,
        "exit_threshold": exit_threshold,
        "n_total_sessions": n_total,
        "n_positive_sessions": n_positive,
        "pct_positive": pct_positive,
        "calibration_rule": (
            "33rd percentile of positive dev-window sessions "
            "(annualised = rate_per_8h x 1095); exit at 50% of entry"
        ),
        "probe_ts": datetime.now(timezone.utc).isoformat(),
        "holdout_start": holdout_start.isoformat(),
        "data_start": data_start.isoformat(),
    }
    OUTPUT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    logger.info("[v2-probe] wrote {}", OUTPUT_PATH)


if __name__ == "__main__":
    main()
