"""
scripts/setup_new_machine.py — one-command new-machine setup.

Cross-platform (Windows / macOS / Linux) bootstrap for a fresh clone:

  1. pip install -r requirements.txt
  2. Prefetch spot OHLCV for every (symbol, timeframe) combination
     referenced by holdout_manifest.json, using the existing cache
     loader. Idempotent — re-running only refetches stale cache files.
  3. Print a summary of what was fetched and what failed.
  4. Exit 0 if every fetch succeeded, 1 if any failed.

Usage:
  python scripts/setup_new_machine.py

Manifest entries with a `legs` (Phase 4.B perp + spot) key get only
the spot leg fetched — perp data goes through a different download
path and is out of scope for this generalised setup script.

No commits, no deploys, no holdout reads. Cache writes are clipped
to the per-symbol dev cutoff so the HoldoutBypass enforcement in
backtest.cache catches any accidental leak.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest.cache import get_symbol_dev_cutoff, load_or_download_ohlcv
from backtest.holdout import load_manifest
from backtest.runner import download_history


REQUIREMENTS_PATH = ROOT / "requirements.txt"
CACHE_DIR = ROOT / "backtest" / "cache" / "ohlcv"


def _resolve_entry_symbols(entry: dict) -> list[str]:
    """Extract the spot symbols from a manifest entry.

    Handles all three substrate shapes:
      * `symbol`  → single-element list
      * `symbols` → list as-is
      * `legs`    → spot leg only; perp out of scope
    """
    if "symbols" in entry:
        return list(entry["symbols"])
    if "symbol" in entry:
        return [entry["symbol"]]
    if "legs" in entry and isinstance(entry["legs"], dict):
        spot = entry["legs"].get("spot")
        return [spot] if spot else []
    return []


def _months_needed(data_start: pd.Timestamp) -> int:
    """Months-back-to-now math, same as scripts/prefetch_daily_ohlcv.py."""
    import math
    now_utc = pd.Timestamp.now(tz="UTC")
    months_back_days = (now_utc - data_start).days
    return int(math.ceil(months_back_days / 30.44)) + 1


def _step_pip_install() -> bool:
    """Run pip install -r requirements.txt. Returns True on success."""
    if not REQUIREMENTS_PATH.exists():
        print(
            f"[setup] WARNING: {REQUIREMENTS_PATH} not found — skipping "
            f"pip install step."
        )
        return True
    print(f"[setup] pip install -r {REQUIREMENTS_PATH}")
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", str(REQUIREMENTS_PATH)],
            cwd=str(ROOT),
            text=True,
            capture_output=False,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[setup] pip install raised: {exc}", file=sys.stderr)
        return False
    if proc.returncode != 0:
        print(
            f"[setup] pip install exited with code {proc.returncode}",
            file=sys.stderr,
        )
        return False
    print("[setup] pip install OK")
    return True


def _step_prefetch_all() -> tuple[list[str], list[str]]:
    """Iterate every manifest entry and fetch each (symbol, timeframe)
    combination. Returns (successes, failures) lists of human-readable
    "<sym> @ <tf>" strings.
    """
    manifest = load_manifest()
    if not manifest:
        print(
            "[setup] WARNING: holdout_manifest.json is empty — nothing "
            "to prefetch."
        )
        return [], []

    # Deduplicate (symbol, timeframe) across strategies — many entries
    # share BTC/USDT or ETH/USDT; the cache loader is idempotent but
    # the dedupe avoids redundant log noise.
    plan: dict[tuple[str, str], pd.Timestamp] = {}
    skipped_legs: list[str] = []
    for sid, entry in manifest.items():
        symbols = _resolve_entry_symbols(entry)
        if not symbols:
            print(
                f"[setup] WARNING: '{sid}' has no resolvable spot "
                f"symbols (probably perp-only); skipping."
            )
            skipped_legs.append(sid)
            continue
        timeframe = entry.get("timeframe", "1d")
        try:
            data_start = pd.Timestamp(entry["data_start"])
        except (KeyError, ValueError) as exc:
            print(
                f"[setup] WARNING: '{sid}' missing/invalid data_start "
                f"({exc!r}); skipping."
            )
            continue
        for sym in symbols:
            key = (sym, timeframe)
            # Take the EARLIEST data_start across strategies sharing
            # this (sym, tf) so the months-needed computation covers
            # the broadest history requested by any consumer.
            if key not in plan or data_start < plan[key]:
                plan[key] = data_start

    print(
        f"[setup] prefetch plan: {len(plan)} unique (symbol, timeframe) "
        f"pair(s) across {len(manifest)} manifest entries."
    )
    if skipped_legs:
        print(f"[setup] skipped (no spot symbols): {skipped_legs}")
    print(f"[setup] cache_dir={CACHE_DIR}")
    print()

    successes: list[str] = []
    failures: list[str] = []
    for (sym, tf), data_start in sorted(plan.items()):
        label = f"{sym} @ {tf}"
        months = _months_needed(data_start)
        until_ts = get_symbol_dev_cutoff(sym)
        print(f"[setup] {label} (months={months}) …")
        try:
            df = load_or_download_ohlcv(
                symbol=sym,
                timeframe=tf,
                months=months,
                download_fn=download_history,
                cache_dir=CACHE_DIR,
                until_ts=until_ts,
            )
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{label}: {exc.__class__.__name__}: {exc}")
            print(f"  FAIL: {exc.__class__.__name__}: {exc}")
            continue
        if df.empty:
            failures.append(f"{label}: empty DataFrame")
            print(f"  FAIL: empty DataFrame")
            continue
        first_ts = df.index.min()
        last_ts = df.index.max()
        successes.append(label)
        print(
            f"  OK rows={len(df):,} range={first_ts.date()} → "
            f"{last_ts.date()}"
        )

    return successes, failures


def main() -> int:
    print("=" * 72)
    print("setup_new_machine — one-command bootstrap for a fresh clone")
    print(f"  python: {sys.version.split()[0]} ({sys.platform})")
    print(f"  repo:   {ROOT}")
    print("=" * 72)
    print()

    pip_ok = _step_pip_install()
    print()
    successes, failures = _step_prefetch_all()
    print()

    print("=" * 72)
    print("Summary")
    print("=" * 72)
    print(f"  pip install:     {'OK' if pip_ok else 'FAILED'}")
    print(f"  prefetch OK:     {len(successes)}")
    print(f"  prefetch FAILED: {len(failures)}")
    if failures:
        print()
        print("Failures:")
        for f in failures:
            print(f"  - {f}")

    if not pip_ok or failures:
        print()
        print("setup_new_machine: DONE WITH ERRORS")
        return 1

    print()
    print("setup_new_machine: DONE — environment ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
