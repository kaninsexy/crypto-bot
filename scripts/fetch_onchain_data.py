"""scripts/fetch_onchain_data.py

Fetches Glassnode on-chain metrics (MVRV Z-score, NVT, Puell
Multiple) for BTC and writes one parquet file per metric-bundle to
data/onchain/<sym>_mvrv.parquet -- the format the trial scripts
(scripts/run_on_chain_metric_models_trial.py) expect.

Exit codes:
  0  success
  2  GLASSNODE_API_KEY missing OR API error -- structured
     TRIAL_ERROR_TYPE block emitted to stdout for orchestrator parsing.

Per-symbol parquet schema (one row per UTC day):
    timestamp        datetime64[ns, UTC]
    mvrv_z_score     float64
    nvt              float64
    puell_multiple   float64
"""

from __future__ import annotations

import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8',
                              errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8',
                              errors='replace')

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "onchain"
SYMBOL = "BTC"
HISTORY_DAYS = 365
API_BASE = "https://api.glassnode.com/v1/metrics"

# (column_label, glassnode_path)
METRICS = [
    ("mvrv_z_score", "/market/mvrv_z_score"),
    ("nvt", "/indicators/nvt"),
    ("puell_multiple", "/indicators/puell_multiple"),
]


def _emit_error(msg: str) -> None:
    print("TRIAL_ERROR_TYPE: deferred_no_data")
    print("TRIAL_ERROR_FETCH: scripts/fetch_onchain_data.py")
    print("TRIAL_ERROR_MSG: " + msg)


def main() -> int:
    api_key = os.environ.get("GLASSNODE_API_KEY")
    if not api_key:
        _emit_error(
            "GLASSNODE_API_KEY not set -- on-chain data requires a "
            "paid Glassnode subscription"
        )
        return 2

    try:
        import pandas as pd
        import requests
    except ImportError as exc:
        _emit_error("required library not installed: " + str(exc))
        return 2

    end_ts = int(datetime.now(timezone.utc).timestamp())
    start_ts = int(
        (datetime.now(timezone.utc) - timedelta(days=HISTORY_DAYS)).timestamp()
    )

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    series_by_label: dict = {}
    for label, path in METRICS:
        url = API_BASE + path
        params = {
            "a": SYMBOL,
            "s": start_ts,
            "u": end_ts,
            "i": "24h",
            "api_key": api_key,
        }
        try:
            resp = requests.get(url, params=params, timeout=30)
        except Exception as exc:  # noqa: BLE001
            _emit_error("network error fetching " + label + ": " + str(exc))
            return 2
        if resp.status_code != 200:
            _emit_error(
                "Glassnode API returned " + str(resp.status_code)
                + " for " + label + ": " + resp.text[:200]
            )
            return 2
        try:
            rows = resp.json()
        except Exception as exc:  # noqa: BLE001
            _emit_error("failed to parse JSON for " + label + ": " + str(exc))
            return 2
        if not rows:
            _emit_error("Glassnode returned empty data for " + label)
            return 2
        series_by_label[label] = rows

    # Join all metrics on timestamp, ffill gaps inside each series.
    frames = []
    for label, rows in series_by_label.items():
        df = pd.DataFrame([
            {
                "timestamp": pd.to_datetime(r.get("t"), unit="s", utc=True),
                label: float(r.get("v") or 0.0),
            }
            for r in rows if r.get("t") is not None
        ])
        if df.empty:
            _emit_error("no parsable rows for " + label)
            return 2
        frames.append(df.set_index("timestamp").sort_index())

    merged = frames[0]
    for f in frames[1:]:
        merged = merged.join(f, how="outer")
    merged = merged.sort_index().ffill().dropna(how="all").reset_index()

    if merged.empty:
        _emit_error("merged dataframe is empty after join")
        return 2

    out_path = OUT_DIR / (SYMBOL + "_mvrv.parquet")
    tmp = out_path.with_suffix(".parquet.tmp")
    merged.to_parquet(tmp, index=False)
    os.replace(tmp, out_path)
    print(
        "Saved " + str(len(merged)) + " rows to data/onchain/"
        + SYMBOL + "_mvrv.parquet"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
