"""scripts/phase_4b_v2b_volregime_probe.py -- V2b vol-regime calibration probe.

Reads the dev-window BTC/USDT spot price history for
FundingRateHarvest_BTC, computes a rolling 30-day annualized realized
volatility series at 1h cadence, and writes the dev-window MEDIAN of
that series as the LV/HV partition threshold for the V2b regime gate.
The threshold is consumed by `scripts/phase_4b_v2b_full_cpcv.py` to
parameterise `FundingRateHarvestStrategy(vol_regime_threshold=...)`.

Source paper: Almeida, Grith, Miftachov, Wang (2024). "Risk Premia in
the Bitcoin Market." arXiv 2410.15195v2. Documents two distinct
option-implied BTC volatility regimes (LV BVRP=0.17, HV BVRP=0.12)
with materially different risk-premium decompositions. The
realized-vol proxy implemented here approximates that partition without
requiring Deribit option data; a sanity check comparing realized-vol
regime membership against an option-implied baseline is left for a
later validation pass per the open-gap note in
research/funding-rate-variation-2-candidates.md.

Calibration rule (pre-specified, dev-only):
  vol_window_hours    = 720  # 30 days at 1h cadence
  realized_vol(t)     = std(close_returns_per_1h, last 720 bars) * sqrt(8760)
                        # 8760 hourly bars per year = annualisation
  vol_regime_threshold = median(realized_vol) over dev-window samples
                         where realized_vol is finite (i.e., post-warmup)

The probe NEVER touches holdout data: the 1h close series is sliced to
`close.index < holdout_start` before the rolling-window std is taken.
The output JSON is a runtime artefact (gitignored); the V2b full_cpcv
script reads it to set the strategy parameter.

Output schema (scripts/phase_4b_v2b_probe_output.json):
  vol_regime_threshold: float       (annualized vol)
  n_dev_samples: int                (count of finite vol observations)
  n_below_threshold: int            (LV regime sample count -- expected ~50%)
  n_above_threshold: int            (HV regime sample count -- expected ~50%)
  vol_window_hours: int             (720)
  annualisation_hours: int          (8760)
  calibration_rule: str
  probe_ts: str (ISO 8601 UTC)
  holdout_start: str
  data_start: str
"""

from __future__ import annotations

import json
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
from backtest.holdout import _load_symbol_df  # spot-only loader, bypasses perp cache


STRATEGY_ID = "FundingRateHarvest_BTC"
OUTPUT_PATH = ROOT / "scripts" / "phase_4b_v2b_probe_output.json"
VOL_WINDOW_HOURS = 720          # 30 days at 1h cadence
ANNUALISATION_HOURS = 8760      # 24 * 365
MIN_DEV_SAMPLES = 1000          # robust median requires plenty of obs


def main() -> int:
    logger.info("[v2b-probe] loading manifest entry for {}", STRATEGY_ID)
    manifest = holdout.load_manifest()
    if STRATEGY_ID not in manifest:
        logger.error(
            "[v2b-probe] manifest missing entry for {}; aborting",
            STRATEGY_ID,
        )
        return 1
    entry = manifest[STRATEGY_ID]
    holdout_start = pd.Timestamp(entry["holdout_start"])
    data_start = pd.Timestamp(entry["data_start"])
    legs = entry["legs"]
    logger.info(
        "[v2b-probe] manifest: timeframe={} legs={} dev=[{} -> {})",
        entry["timeframe"], legs,
        data_start.isoformat(), holdout_start.isoformat(),
    )

    # 1. Load dev OHLCV (spot leg only).  load_dev() requires both
    #    legs of a `legs`-typed entry; the V2b probe only needs the
    #    SPOT close-price series (vol is computed on spot returns,
    #    not on the perp leg).  Bypassing load_dev sidesteps an
    #    irrelevant perp-cache dependency for this probe.  The spot
    #    cache is still gated by the standard L1 parquet contract.
    df_spot = _load_symbol_df(legs["spot"], entry["timeframe"])
    if df_spot.empty:
        logger.error("[v2b-probe] spot frame empty; aborting")
        return 1
    # Slice to dev only (data_start <= ts < holdout_start) so no
    # holdout sample reaches the threshold computation.
    df_spot = df_spot[
        (df_spot.index >= data_start) & (df_spot.index < holdout_start)
    ].sort_index()
    if df_spot.empty:
        logger.error(
            "[v2b-probe] dev-window slice of spot frame is empty; "
            "aborting (data_start={} holdout_start={})",
            data_start, holdout_start,
        )
        return 1
    if df_spot.index.max() >= holdout_start:
        logger.error(
            "[v2b-probe] dev spot frame leaks holdout: max {} >= "
            "holdout_start {}; aborting",
            df_spot.index.max(), holdout_start,
        )
        return 1
    logger.info(
        "[v2b-probe] dev spot frame: rows={} from {} to {}",
        len(df_spot), df_spot.index.min(), df_spot.index.max(),
    )

    # 2. Compute rolling realized vol on 1h close-to-close returns.
    closes = df_spot["close"].astype(float)
    returns_1h = closes.pct_change()
    vol_per_1h = returns_1h.rolling(VOL_WINDOW_HOURS).std()
    realized_vol_annualized = vol_per_1h * np.sqrt(ANNUALISATION_HOURS)
    realized_vol_annualized = realized_vol_annualized.dropna()
    n_dev_samples = int(len(realized_vol_annualized))
    if n_dev_samples < MIN_DEV_SAMPLES:
        logger.error(
            "[v2b-probe] only {} finite vol observations in dev "
            "(< {} required for a robust median); aborting.",
            n_dev_samples, MIN_DEV_SAMPLES,
        )
        return 1

    threshold = float(np.median(realized_vol_annualized.values))
    n_below = int((realized_vol_annualized.values < threshold).sum())
    n_above = int((realized_vol_annualized.values >= threshold).sum())

    print()
    print("=" * 76)
    print("Phase 4.B V2b vol-regime calibration probe")
    print("=" * 76)
    print(f"  Strategy             : {STRATEGY_ID}")
    print(
        f"  Dev window           : {data_start.isoformat()} -> "
        f"{holdout_start.isoformat()}"
    )
    print(f"  Vol window           : {VOL_WINDOW_HOURS}h ({VOL_WINDOW_HOURS//24}d)")
    print(f"  Annualisation        : sqrt({ANNUALISATION_HOURS}) (1h cadence)")
    print(f"  Finite vol samples   : {n_dev_samples}")
    print(
        f"  Vol percentiles      : p05={np.percentile(realized_vol_annualized.values, 5):.4f}  "
        f"p25={np.percentile(realized_vol_annualized.values, 25):.4f}  "
        f"median={threshold:.4f}  "
        f"p75={np.percentile(realized_vol_annualized.values, 75):.4f}  "
        f"p95={np.percentile(realized_vol_annualized.values, 95):.4f}"
    )
    print(
        f"  LV regime samples    : {n_below} ({n_below/n_dev_samples*100:.1f}%) "
        f"-- vol < threshold (harvest)"
    )
    print(
        f"  HV regime samples    : {n_above} ({n_above/n_dev_samples*100:.1f}%) "
        f"-- vol >= threshold (flat)"
    )
    print("=" * 76)
    print()

    # Persist to JSON.  Runtime artefact (gitignored).
    payload = {
        "vol_regime_threshold": threshold,
        "n_dev_samples": n_dev_samples,
        "n_below_threshold": n_below,
        "n_above_threshold": n_above,
        "vol_window_hours": VOL_WINDOW_HOURS,
        "annualisation_hours": ANNUALISATION_HOURS,
        "calibration_rule": (
            f"median of rolling {VOL_WINDOW_HOURS}h std of 1h returns x "
            f"sqrt({ANNUALISATION_HOURS}) over the dev window; LV regime "
            "= vol < threshold (harvest), HV regime = vol >= threshold (flat)"
        ),
        "probe_ts": datetime.now(timezone.utc).isoformat(),
        "holdout_start": holdout_start.isoformat(),
        "data_start": data_start.isoformat(),
    }
    OUTPUT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    logger.info("[v2b-probe] wrote {}", OUTPUT_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
