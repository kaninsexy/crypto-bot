"""scripts/fetch_lunarcrush_history.py

Fetches LunarCrush historical sentiment data (Galaxy Score + close
price) for BTC and ETH and writes one parquet file per symbol to
data/lunarcrush/<sym>_galaxy.parquet -- the format the trial scripts
(scripts/run_social_sentiment_momentum_trial.py) expect.

Exit codes:
  0  success
  2  LUNARCRUSH_API_KEY missing OR API error -- structured
     TRIAL_ERROR_TYPE block emitted to stdout for orchestrator parsing.

Per-symbol parquet schema (one row per UTC day):
    timestamp     datetime64[ns, UTC]
    close         float64
    galaxy_score  float64
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
OUT_DIR = ROOT / "data" / "lunarcrush"
COINS = ["BTC", "ETH"]
HISTORY_DAYS = 365
# v4 is the current LunarCrush public API; v3 was deprecated end-2023.
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

        df = pd.DataFrame([
            {
                "timestamp": pd.to_datetime(r.get("time"), unit="s",
                                            utc=True),
                "close": float(r.get("close") or r.get("price") or 0.0),
                "galaxy_score": float(r.get("galaxy_score") or 0.0),
            }
            for r in rows
            if r.get("time") is not None
        ])
        if df.empty:
            _emit_error("no parsable rows for " + coin)
            return 2
        df = df.sort_values("timestamp").reset_index(drop=True)

        out_path = OUT_DIR / (coin + "_galaxy.parquet")
        tmp = out_path.with_suffix(".parquet.tmp")
        df.to_parquet(tmp, index=False)
        os.replace(tmp, out_path)
        print(
            "Saved " + str(len(df)) + " rows to data/lunarcrush/"
            + coin + "_galaxy.parquet"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
