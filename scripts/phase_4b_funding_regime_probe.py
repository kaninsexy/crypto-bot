"""Phase 4.B Variation #1 calibration probe — negative-funding-regime
duration distribution for BTC-USDT-SWAP across the manifest dev window.

ONE-TIME CALIBRATION PROBE.  DO NOT execute as part of CI — the
output is committed into research/funding-rate-literature.md
§ Variation #1 § Parameters / `exit_funding_flip_n_settlements` (=4),
sourced from the p95 statistic of the run-length distribution per
chat-side adjudication 2026-05-02 (the probe's `recommended_N` field
= round(p50) = 1 is superseded; median measures noise, not regime).

Probe is read-only with respect to project state: it writes a single
JSON output to /tmp/, does NOT touch data/, research/, docs/,
backtest/, or any sacred-harness file.  Re-running the probe produces
the same output up to live-OKX latest-funding-row drift; the
committed parameter value (=4) is fixed by the dev-window slice and
is invariant under that drift.

Run from repo root: ``python scripts/phase_4b_funding_regime_probe.py``
"""
from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest.holdout import load_manifest
from data import okx_funding


# ── 1. Manifest dev window ───────────────────────────────────────────────────
manifest = load_manifest()
entry = manifest["FundingRateHarvest_BTC"]
data_start_ts = pd.Timestamp(entry["data_start"])
dev_end_ts = pd.Timestamp(entry["dev_end"])
print(f"manifest dev window: {data_start_ts.isoformat()} → {dev_end_ts.isoformat()}")

# ── 2. Fetch funding history covering at least the dev window ────────────────
now_dt = datetime.now(timezone.utc)
span_days = (now_dt - data_start_ts.to_pydatetime()).days
months_needed = int(math.ceil(span_days / 30.44)) + 1  # +1 month cushion
print(f"requesting months={months_needed} (span_days={span_days})")

# Use the no-cache fetcher to ensure we get the full Path 5 assembly.
# Per-month archive + deep-mark caches inside the data layer persist
# the heavy work, so subsequent invocations are fast.
df = okx_funding.fetch_funding_history(
    "BTC/USDT", months=months_needed,
    batch_size=okx_funding.DEFAULT_BATCH_SIZE,
    request_delay_s=okx_funding.DEFAULT_REQUEST_DELAY_S,
)
print(f"fetched rows={len(df)} earliest={df.index.min()} latest={df.index.max()}")

# ── 3. Restrict to dev window ────────────────────────────────────────────────
mask = (df.index >= data_start_ts) & (df.index <= dev_end_ts)
dev_df = df.loc[mask].sort_index()
print(f"dev-window rows={len(dev_df)}")

# ── 4. Identify negative-funding runs ────────────────────────────────────────
# Definition: maximal consecutive sequence of settlements where
# funding_rate < 0.0.  A single negative settlement followed by a
# positive one is a run of length 1.
fr = dev_df["funding_rate"].astype(float).values
is_neg = fr < 0.0

run_lengths: list[int] = []
run_starts_idx: list[int] = []
i = 0
n = len(is_neg)
while i < n:
    if is_neg[i]:
        j = i
        while j < n and is_neg[j]:
            j += 1
        run_lengths.append(j - i)
        run_starts_idx.append(i)
        i = j
    else:
        i += 1

n_runs = len(run_lengths)
print(f"n_negative_runs={n_runs}")

if n_runs == 0:
    duration_dist = {"p05": 0, "p25": 0, "p50": 0, "p75": 0, "p95": 0,
                     "max": 0, "mean": 0.0}
    longest_run_start_iso = None
    longest_run_end_iso = None
    recommended_N = 0
else:
    arr = np.asarray(run_lengths, dtype=float)
    quantiles = np.percentile(arr, [5, 25, 50, 75, 95], method="linear")
    duration_dist = {
        "p05": int(round(float(quantiles[0]))),
        "p25": int(round(float(quantiles[1]))),
        "p50": int(round(float(quantiles[2]))),
        "p75": int(round(float(quantiles[3]))),
        "p95": int(round(float(quantiles[4]))),
        "max": int(arr.max()),
        "mean": float(arr.mean()),
    }
    longest_idx = int(np.argmax(arr))
    longest_start_i = run_starts_idx[longest_idx]
    longest_len = run_lengths[longest_idx]
    longest_run_start_iso = dev_df.index[longest_start_i].isoformat()
    longest_run_end_iso = dev_df.index[longest_start_i + longest_len - 1].isoformat()
    recommended_N = duration_dist["p50"]

# ── 5. Emit JSON ─────────────────────────────────────────────────────────────
result = {
    "symbol": "BTC-USDT-SWAP",
    "dev_window": {
        "start": data_start_ts.isoformat(),
        "end": dev_end_ts.isoformat(),
    },
    "n_settlements_total": int(len(dev_df)),
    "n_settlements_negative": int(is_neg.sum()),
    "negative_runs": {
        "n_runs": n_runs,
        "duration_distribution": duration_dist,
        "longest_run_start": longest_run_start_iso,
        "longest_run_end": longest_run_end_iso,
    },
    "recommended_N": recommended_N,
    "convention_note": (
        "A negative run is a maximal consecutive sequence of "
        "settlements where funding_rate < 0.0.  Duration is the "
        "count of settlements in the run.  A single negative "
        "settlement followed by a positive one is a run of "
        "length 1 (not zero).  Settlement cadence on OKX BTC-USDT-"
        "SWAP is 8h, so a run of length N covers N * 8 hours of "
        "sustained negative funding."
    ),
    "fetch_metadata": {
        "fetcher": "data.okx_funding.fetch_funding_history (Path 5 hybrid)",
        "rows_total_after_dev_filter": int(len(dev_df)),
        "rows_full_fetch": int(len(df)),
        "fetch_earliest": df.index.min().isoformat() if len(df) else None,
        "fetch_latest": df.index.max().isoformat() if len(df) else None,
        "probe_run_at_utc": datetime.now(timezone.utc).isoformat(),
    },
}

out_path = Path("/tmp/funding_negative_regime_distribution.json")
out_path.write_text(json.dumps(result, indent=2))
print(f"\nwrote {out_path}")
print(f"recommended_N={recommended_N}")
