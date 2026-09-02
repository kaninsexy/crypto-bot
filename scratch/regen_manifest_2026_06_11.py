"""
scratch/regen_manifest_2026_06_11.py — extended-window manifest regen
(2026-06-11 work order, step 2; human pre-authorized).

Boundary spec:
  data_start    = max(2021-01-01, basket-intersection first available
                  candle from the best cache file per symbol;
                  AttentionMomentum additionally floored at the Google
                  Trends series start; FundingRateHarvest_BTC at the
                  funding-history start).
  dev_end = holdout_start = 2025-05-01T00:00:00Z (single global boundary)
  data_end      = min(2026-06-11T00:00:00Z [latest complete day],
                  basket-intersection last available candle).

Contamination disclosure is written into entry-level `notes`:
  - AttentionMomentum + FundingRateHarvest_BTC accessed the OLD
    holdout window (2025-09-22 -> 2026-04/05) on 2026-05-08; verdicts
    observed, params NOT retuned since.
  - GLOBAL: the new holdout's first segment (2025-05-01 -> old
    holdout_start 2025-09-12/22) was inside the OLD dev window, which
    every pre-2026-06-11 trial read freely.  No strategy's new
    holdout is fully unseen; the two named above additionally saw the
    rest of it.

Audit path: backtest.generate_holdout_manifest.regenerate_manifest_
explicit (one regenerated=true event per changed strategy, caller +
reason + git_commit attribution).

--dry-run prints the computed entries without writing.
"""
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest import generate_holdout_manifest as gen          # noqa: E402
from backtest.holdout import (                                  # noqa: E402
    _load_perp_df,
    _load_symbol_df,
    load_manifest,
)

FLOOR = pd.Timestamp("2021-01-01T00:00:00+00:00")
HOLDOUT_START = pd.Timestamp("2025-05-01T00:00:00+00:00")
DATA_END_CAP = pd.Timestamp("2026-06-11T00:00:00+00:00")  # latest complete day boundary

GLOBAL_NOTE = (
    "2026-06-11 extended-window regen: holdout_start moved EARLIER "
    "(2025-05-01 vs old 2025-09-12/22), so the new holdout's first "
    "~4.5 months were inside the OLD dev window that every "
    "pre-2026-06-11 trial read freely. No new-holdout verdict is on "
    "fully unseen data. See docs/bot_status.md 'Holdout regeneration "
    "2026-06-11' for the full disclosure."
)
CONTAMINATED_NOTE = (
    "CONTAMINATION ASTERISK: this strategy ran a final_gate/holdout "
    "evaluation on the OLD holdout window (2025-09-22 -> 2026-04/05) "
    "on 2026-05-08; the verdict was observed (params NOT retuned "
    "since). Any new-holdout verdict is doubly non-virgin: old "
    "holdout seen entirely; new-holdout overlap segment "
    "2025-05-01 -> 2025-09-22 seen as dev. " + GLOBAL_NOTE
)
CONTAMINATED = {"AttentionMomentum", "FundingRateHarvest_BTC"}


def _spot_bounds(symbol: str, timeframe: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    df = _load_symbol_df(symbol, timeframe)
    return df.index.min(), df.index.max()


def main() -> int:
    dry = "--dry-run" in sys.argv
    old = dict(load_manifest())
    new_entries: dict = {}
    rows = []

    # Google Trends availability (AttentionMomentum substrate floor).
    import glob as _glob
    tr_files = _glob.glob(str(ROOT / "backtest/cache/google_trends/*.parquet"))
    tr_starts = []
    for f in tr_files:
        tdf = pd.read_parquet(f)
        tr_starts.append(pd.Timestamp(tdf.index.min()))
    trends_start = max(tr_starts) if tr_starts else None

    # Funding availability (FundingRateHarvest_BTC substrate floor).
    import glob as _glob2
    f_candidates = sorted(
        _glob2.glob(str(ROOT / "backtest/cache/perp_funding/BTC-USDT-SWAP_funding_*mo.parquet")),
        key=lambda p: int(p.rsplit("_", 1)[-1].replace("mo.parquet", "")),
    )
    fdf = pd.read_parquet(f_candidates[-1])
    funding_start, funding_end = fdf.index.min(), fdf.index.max()

    for sid, entry in old.items():
        e = dict(entry)
        tf = e["timeframe"]
        if "legs" in e:
            s0, s1 = _spot_bounds(e["legs"]["spot"], tf)
            p = _load_perp_df(e["legs"]["perp"], tf)
            p0, p1 = p.index.min(), p.index.max()
            first = max(s0, p0, funding_start)
            last = min(s1, p1, funding_end)
            detail = (
                f"spot {s0.date()}..{s1.date()}, perp {p0.date()}.."
                f"{p1.date()}, funding {funding_start.date()}.."
                f"{funding_end.date()}"
            )
        else:
            syms = e.get("symbols", [e.get("symbol")])
            bounds = [_spot_bounds(s, tf) for s in syms]
            first = max(b[0] for b in bounds)
            last = min(b[1] for b in bounds)
            detail = f"{len(syms)} symbol(s), intersection {first.date()}..{last.date()}"
        if sid == "AttentionMomentum" and trends_start is not None:
            first = max(first, trends_start)
            trends_end = min(
                pd.Timestamp(pd.read_parquet(f).index.max()) for f in tr_files
            )
            last = min(last, trends_end)
            detail += f"; trends {trends_start.date()}..{trends_end.date()}"

        data_start = max(FLOOR, first)
        data_end = min(DATA_END_CAP, last)
        e["data_start"] = data_start.isoformat()
        e["data_end"] = data_end.isoformat()
        e["dev_end"] = HOLDOUT_START.isoformat()
        e["holdout_start"] = HOLDOUT_START.isoformat()
        e["notes"] = CONTAMINATED_NOTE if sid in CONTAMINATED else GLOBAL_NOTE
        new_entries[sid] = e
        dev_months = (HOLDOUT_START - data_start).days / 30.44
        rows.append((sid, tf, data_start.date(), data_end.date(),
                     f"{dev_months:.1f}", detail))

    print(f"{'strategy':32s} {'tf':3s} {'data_start':11s} {'data_end':11s} {'devM':>5s}")
    for r in sorted(rows):
        print(f"{r[0]:32s} {r[1]:3s} {str(r[2]):11s} {str(r[3]):11s} {r[4]:>5s}  {r[5]}")

    if dry:
        print("\n--dry-run: manifest NOT written.")
        return 0

    stale = gen.regenerate_manifest_explicit(
        new_entries,
        caller="phase4.gate_v2_rerun_batch.manifest_regen",
        reason=(
            "2026-06-11 extended-window regeneration (human "
            "pre-authorized in the gate-spec-v2 re-run work order): "
            "data_start back to max(2021-01-01, first available "
            "candle); single global dev_end/holdout_start "
            "2025-05-01T00:00Z; data_end latest complete day. "
            "Supersedes the 2025-09 80/20 split."
        ),
    )
    print(f"\nchanged holdout_start: {len(stale)} strategies")
    return 0


if __name__ == "__main__":
    sys.exit(main())
