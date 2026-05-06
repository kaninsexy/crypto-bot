"""scripts/fetch_lunarcrush_history.py

Fetches LunarCrush historical sentiment data (Galaxy Score + price)
for BTC and ETH and writes to data/lunarcrush_history.json.

Exit codes:
  0  success
  2  LUNARCRUSH_API_KEY missing OR API error -- structured
     TRIAL_ERROR_TYPE block emitted to stdout for orchestrator parsing.

Output JSON shape:
  {
    "fetched_at": "<utc-iso>",
    "api_version": "v4",
    "coins": {
      "BTC": [{"time": <unix>, "galaxy_score": <float>,
               "price": <float>, "sentiment": <float>}, ...],
      "ETH": [...]
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
OUT_PATH = ROOT / "data" / "lunarcrush_history.json"
COINS = ["BTC", "ETH"]
HISTORY_DAYS = 365
# v4 is the current LunarCrush public API; v3 was deprecated end-2023.
# v4 still exposes Galaxy Score + price + sentiment on the time-series
# endpoint, which is what the spec requires.
API_BASE = "https://lunarcrush.com/api4"


def _emit_error(msg: str) -> None:
    print("TRIAL_ERROR_TYPE: deferred_no_data")
    print("TRIAL_ERROR_FETCH: scripts/fetch_lunarcrush_history.py")
    print("TRIAL_ERROR_MSG: " + msg)


def main() -> int:
    api_key = os.environ.get("LUNARCRUSH_API_KEY")
    if not api_key:
        _emit_error("LUNARCRUSH_API_KEY not set")
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
        "api_version": "v4",
        "coins": {},
    }

    headers = {"Authorization": "Bearer " + api_key}
    for coin in COINS:
        url = API_BASE + "/public/coins/" + coin + "/time-series/v2"
        params = {"bucket": "day", "start": start_ts, "end": end_ts}
        try:
            resp = requests.get(url, params=params, headers=headers,
                                timeout=30)
        except Exception as exc:  # noqa: BLE001
            _emit_error("network error fetching " + coin + ": " + str(exc))
            return 2
        if resp.status_code != 200:
            _emit_error(
                "LunarCrush API returned " + str(resp.status_code)
                + " for " + coin + ": " + resp.text[:200]
            )
            return 2
        try:
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            _emit_error("failed to parse JSON for " + coin + ": " + str(exc))
            return 2
        rows = payload.get("data") or []
        if not rows:
            _emit_error(
                "LunarCrush returned empty data for " + coin
                + ": " + str(payload)[:200]
            )
            return 2
        out["coins"][coin] = [
            {
                "time": r.get("time"),
                "galaxy_score": r.get("galaxy_score"),
                "price": r.get("close") or r.get("price"),
                "sentiment": r.get("sentiment"),
            }
            for r in rows
        ]

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, indent=2), encoding="utf-8")
    os.replace(tmp, OUT_PATH)

    n_total = sum(len(v) for v in out["coins"].values())
    print("Saved " + str(n_total) + " rows to data/lunarcrush_history.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
