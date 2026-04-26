"""scripts/regime_distribution_audit.py — One-off diagnostic.

Counts regime hours across the full BTC/USDT 1h cache window, broken down by
calendar quarter. Output supports the regime-conditional holdout sacred-harness
design discussion — answering the question "is there enough bear-regime
substrate for any holdout design?".

Not part of the production pipeline; not imported by anything.
Read-only against the parquet cache: does NOT route through
backtest.cache.load_or_download_ohlcv, so there is no holdout-bypass-context
interaction and no chance of writing to backtest/holdout_access.log.

Usage:
    python scripts/regime_distribution_audit.py        # run from project root

Outputs:
    1. stdout: human-readable per-quarter regime-hour table + totals
       + BEAR+CRASH summary line.
    2. logs/regime_distribution_audit.csv: same data in CSV form
       (one row per quarter, columns = the 6 regimes from ALL_REGIMES).
       Overwritten if it exists. Creates logs/ if absent.
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

import pandas as pd

# Make the project root importable regardless of cwd.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

# Silence loguru BEFORE importing the detector — its __init__ and every
# detected regime change emit INFO logs that would otherwise pollute stdout.
from loguru import logger  # noqa: E402
logger.remove()

from portfolio.regime_detector import RegimeDetector, ALL_REGIMES  # noqa: E402

# ── Constants ────────────────────────────────────────────────────────────────
SYMBOL          = "BTC/USDT"
TIMEFRAME       = "1h"
WARMUP          = 1000  # bars; > EMA200 + RSI(14) + ATR(14) + ADX(14) + 100-bar high
CACHE_DIR       = PROJECT_ROOT / "backtest" / "cache" / "ohlcv"
LOG_DIR         = PROJECT_ROOT / "logs"
CSV_OUT_PATH    = LOG_DIR / "regime_distribution_audit.csv"
PROGRESS_EVERY  = 2000  # bars between stderr heartbeat lines


# ── Cache loader ─────────────────────────────────────────────────────────────
# Mirrors backtest/holdout.py:_load_symbol_df. Duplicated rather than imported
# so this script never reaches into the sacred harness module surface.

_MONTHS_RE = re.compile(r"_(\d+)mo\.parquet$")


def _parse_months(path: Path) -> int:
    m = _MONTHS_RE.search(path.name)
    if m is None:
        raise ValueError(
            f"Cache file '{path.name}' does not match the expected "
            "naming convention '{symbol}_{timeframe}_{N}mo.parquet'."
        )
    return int(m.group(1))


def load_btc_cache() -> pd.DataFrame:
    """Load the broadest BTC/USDT 1h cache file from disk.

    When multiple files match (e.g. 12mo and 36mo), picks the highest
    {N}mo to use the broadest date range available.
    """
    prefix = SYMBOL.replace("/", "-")
    candidates = list(CACHE_DIR.glob(f"{prefix}_{TIMEFRAME}_*.parquet"))
    if not candidates:
        raise FileNotFoundError(
            f"No cache file for {SYMBOL} {TIMEFRAME} in {CACHE_DIR}"
        )
    best = max(candidates, key=_parse_months)
    df = pd.read_parquet(best)

    # The cache uses UTC by convention. If it was written tz-naive, localize
    # so pd.Grouper(freq='QE') has a consistent comparable index dtype.
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    return df


# ── Main ─────────────────────────────────────────────────────────────────────
def main() -> int:
    t0 = time.perf_counter()

    df = load_btc_cache()
    n_total = len(df)
    if n_total <= WARMUP:
        print(
            f"ERROR: only {n_total} bars in cache; need > WARMUP={WARMUP}.",
            file=sys.stderr,
        )
        return 1

    print(
        f"Loaded {SYMBOL} {TIMEFRAME} cache: {n_total} bars from "
        f"{df.index[0].strftime('%Y-%m-%d %H:%M')} to "
        f"{df.index[-1].strftime('%Y-%m-%d %H:%M')}"
    )
    print(f"Skipped first {WARMUP} bars (warmup), labeled {n_total - WARMUP}")
    print()

    detector = RegimeDetector()

    timestamps: list[pd.Timestamp] = []
    regimes:    list[str]          = []

    # Walk forward. Slice is df.iloc[i-W:i+1] (length W+1 rows) so total cost
    # is O(n*W) rather than O(n^2) from cumulative .iloc[:i+1] slicing.
    # The detector instance is reused across calls so its 3-candle hysteresis
    # state evolves bar-by-bar exactly as the live bot would experience it.
    for i in range(WARMUP, n_total):
        window  = df.iloc[i - WARMUP : i + 1]
        reading = detector.detect(window)
        timestamps.append(df.index[i])
        regimes.append(reading.regime)

        labeled_so_far = i - WARMUP
        if labeled_so_far > 0 and labeled_so_far % PROGRESS_EVERY == 0:
            elapsed = time.perf_counter() - t0
            total   = n_total - WARMUP
            print(
                f"  ...labeled {labeled_so_far}/{total} bars "
                f"({labeled_so_far / total:.0%}) in {elapsed:.1f}s",
                file=sys.stderr,
            )

    # Build labeled DataFrame, bucket by calendar quarter.
    labels = pd.DataFrame(
        {"regime": regimes},
        index=pd.DatetimeIndex(timestamps, name="ts"),
    )

    # pd.Grouper(freq='QE') — quarter-end. ('Q' was deprecated in Pandas 2.2+.)
    # reindex(columns=ALL_REGIMES, fill_value=0) keeps every regime column
    # present even if it had zero hours in some quarter.
    counts = (
        labels.groupby([pd.Grouper(freq="QE"), "regime"])
              .size()
              .unstack(fill_value=0)
              .reindex(columns=ALL_REGIMES, fill_value=0)
              .astype(int)
    )

    # ── Stdout table ─────────────────────────────────────────────────────────
    print("Quarterly regime hours:")
    quarter_w = 10
    col_w     = {r: max(len(r), 6) + 3 for r in ALL_REGIMES}
    header    = " " * quarter_w + "".join(r.rjust(col_w[r]) for r in ALL_REGIMES)
    print(header)
    for q_end, row in counts.iterrows():
        label = f"{q_end.year}-Q{q_end.quarter}"
        line  = label.ljust(quarter_w) + "".join(
            str(int(row[r])).rjust(col_w[r]) for r in ALL_REGIMES
        )
        print(line)

    # ── Totals + BEAR+CRASH summary ──────────────────────────────────────────
    totals        = counts.sum(axis=0)
    total_labeled = int(totals.sum())
    print()
    print("Totals:")
    for r in ALL_REGIMES:
        print(f"  {r}: {int(totals[r])}")
    bear_crash = int(totals["BEAR"]) + int(totals["CRASH"])
    pct = (bear_crash / total_labeled * 100) if total_labeled > 0 else 0.0
    print(f"  BEAR + CRASH: {bear_crash} hours ({pct:.1f}% of labeled bars)")

    # ── CSV output ───────────────────────────────────────────────────────────
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    csv_out = counts.copy()
    # Convert quarter-end Timestamps → quarterly Periods so the CSV index
    # reads as e.g. "2023Q2" rather than "2023-06-30 00:00:00+00:00".
    csv_out.index      = csv_out.index.to_period("Q")
    csv_out.index.name = "quarter"
    csv_out.to_csv(CSV_OUT_PATH)
    print()
    print(f"Wrote {CSV_OUT_PATH.relative_to(PROJECT_ROOT)}")

    elapsed = time.perf_counter() - t0
    print(f"Runtime: {elapsed:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
