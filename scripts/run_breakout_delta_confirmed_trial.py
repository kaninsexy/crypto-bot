"""scripts/run_breakout_delta_confirmed_trial.py -- Phase 4.E BreakoutDeltaConfirmed full_cpcv trial.

Thin wrapper; all logic in scripts/phase4e_trial_common.py: dual-fee gate
(Gate 1), CPCVError-safe retire row, per-bar store. Single-pair BTCUSDT 1h,
long-only. NO holdout access. NOT executed this chunk -- running any trial
is the human-only step (Gate 5); trials.log stays untouched until then.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.phase4e_trial_common import run

if __name__ == "__main__":
    raise SystemExit(run("BreakoutDeltaConfirmed"))
