"""scripts/warm_google_trends_cache.py -- pre-warm Google Trends cache.

Fetches ~38 months of daily Google Trends search-volume data for the
five AttentionMomentum keywords and writes them to the parquet cache
under backtest/cache/google_trends/. Run before an orchestrator pass
so trial subprocesses hit cache on first call instead of pulling
from Trends live and tripping the 429 rate limit.

Usage:
    python scripts/warm_google_trends_cache.py

Output is ASCII-only (Windows cp1252 compatible). Continues past
per-keyword failures so a single rate-limited keyword does not block
the rest of the warm-up.

Cache files land at:
    backtest/cache/google_trends/<keyword>_38mo.parquet

The cache TTL is 24h by default (see data/google_trends.DEFAULT_TTL_HOURS),
so re-running this script within a day is a no-op cache-hit pass.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Project root on sys.path so `from data.google_trends import ...` works
# regardless of the operator's working directory.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data.google_trends import (
    GoogleTrendsError,
    load_or_fetch_trends,
)


# AttentionMomentum (sq-018) trades a 5-asset basket; pre-warming all
# five keywords avoids a live fetch in the trial subprocess.
KEYWORDS: list[str] = ["bitcoin", "ethereum", "solana", "bnb", "ripple"]

# 38 months mirrors the OHLCV cache convention (e.g.
# BTC-USDT_1d_38mo.parquet) so the search-volume window covers the
# full historical run.
MONTHS: int = 38


def main() -> int:
    print(
        f"warming Google Trends cache: {len(KEYWORDS)} keyword(s), "
        f"{MONTHS} months each"
    )
    failures = 0
    for kw in KEYWORDS:
        try:
            df = load_or_fetch_trends(kw, MONTHS)
        except GoogleTrendsError as exc:
            print(f"{kw}: FAIL {exc}")
            failures += 1
            continue
        except Exception as exc:  # noqa: BLE001 -- script-level catch-all
            print(f"{kw}: FAIL {exc.__class__.__name__}: {exc}")
            failures += 1
            continue
        rows = len(df) if df is not None else 0
        print(f"{kw}: ok rows={rows}")
    print(
        f"done: {len(KEYWORDS) - failures}/{len(KEYWORDS)} keyword(s) cached"
    )
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
