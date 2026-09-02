"""
scratch/mintrl_precheck_2026_06_11.py — MinTRL pre-check (work-order
step 3, gate spec v2) for the 5 re-run candidates.

Per strategy: units-correct BLP eq.13 at target TRUE annualised Sharpe
1.0, with skew/kurt taken from the dev-window primary-symbol B&H
per-bar returns (actual data moments; at per-bar SR ~0.01-0.05 the
moment corrections are small but real).  available_bars = dev-window
bar count of the (intersection) substrate.  available < min_trl =>
"insufficient data" — record and skip the trial.

Reads the REGENERATED manifest; run after the regen step.
"""
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.dsr import bars_per_year_for_timeframe, min_track_record_length  # noqa: E402
from backtest.holdout import load_dev, load_manifest                            # noqa: E402

BATCH = [
    ("CrossSectionalMomentum", "BTC/USDT"),
    ("AltcoinSeasonRotation", "BTC/USDT"),
    ("NewsSentimentMomentum", "BTC/USDT"),
    ("AttentionMomentum", "BTC/USDT"),
    ("FundingRateHarvest_BTC", None),  # legs entry; spot leg used
]


def main() -> int:
    manifest = load_manifest()
    print(f"{'strategy':28s} {'tf':3s} {'avail_bars':>10s} {'min_trl':>9s} "
          f"{'avail_yrs':>9s} {'need_yrs':>8s} verdict")
    results = {}
    for sid, primary in BATCH:
        entry = manifest[sid]
        tf = entry["timeframe"]
        bpy = bars_per_year_for_timeframe(tf)
        dev = load_dev(sid)
        if isinstance(dev, dict):           # legs
            frame = dev["spot"]
        elif "symbol" in dev.columns:
            frame = dev[dev["symbol"] == primary].sort_index()
            # available bars for a basket = intersection timeline.
            counts = dev.groupby("symbol").size()
            n_avail = int(counts.min())
        else:
            frame = dev
        if not isinstance(dev, pd.DataFrame) or "symbol" not in getattr(dev, "columns", []):
            n_avail = len(frame)
        rets = frame["close"].astype(float).pct_change().dropna().values
        out = min_track_record_length(
            sr_candidate=1.0, returns=rets, bars_per_year=bpy,
        )
        ok = n_avail >= out.min_trl
        verdict = "TESTABLE" if ok else "INSUFFICIENT DATA"
        results[sid] = dict(
            available_bars=n_avail, min_trl=out.min_trl,
            avail_years=n_avail / bpy, need_years=out.min_trl_years,
            testable=ok, skew=out.skew, kurt=out.kurt,
        )
        print(f"{sid:28s} {tf:3s} {n_avail:>10,} {out.min_trl:>9.0f} "
              f"{n_avail/bpy:>9.2f} {out.min_trl_years:>8.2f} {verdict}")
    import json
    (ROOT / "scratch" / "mintrl_precheck_results.json").write_text(
        json.dumps(results, indent=2), encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
