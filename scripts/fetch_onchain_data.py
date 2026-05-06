"""scripts/fetch_onchain_data.py

Fetches Glassnode on-chain metrics (MVRV Z-score, NVT, Puell
Multiple) for BTC and writes to data/onchain_history.json.

Exit codes:
  0  success
  2  GLASSNODE_API_KEY missing OR API error -- structured
     TRIAL_ERROR_TYPE block emitted to stdout for orchestrator parsing.

Output JSON shape:
  {
    "fetched_at": "<utc-iso>",
    "asset": "BTC",
    "metrics": {
      "mvrv_z_score": [{"time": <unix>, "value": <float>}, ...],
      "nvt": [...],
      "puell_multiple": [...]
    }
  }
"""

from __future__ import annotations

import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8',
                              errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8',
                              errors='replace')

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_PATH = ROOT / "data" / "onchain_history.json"
HISTORY_DAYS = 365
API_BASE = "https://api.glassnode.com/v1/metrics"

# (label, glassnode_path)
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
        import requests
    except ImportError as exc:
        _emit_error("requests library not installed: " + str(exc))
        return 2

    end_ts = int(datetime.now(timezone.utc).timestamp())
    start_ts = int(
        (datetime.now(timezone.utc) - timedelta(days=HISTORY_DAYS)).timestamp()
    )

    out = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "asset": "BTC",
        "metrics": {},
    }

    for label, path in METRICS:
        url = API_BASE + path
        params = {
            "a": "BTC",
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
        out["metrics"][label] = [
            {"time": r.get("t"), "value": r.get("v")} for r in rows
        ]

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, indent=2), encoding="utf-8")
    os.replace(tmp, OUT_PATH)

    n_total = sum(len(v) for v in out["metrics"].values())
    print("Saved " + str(n_total) + " rows to data/onchain_history.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
