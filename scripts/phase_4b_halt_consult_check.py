"""scripts/phase_4b_halt_consult_check.py — Phase 4.B Track A/B HALT-AND-CONSULT.

Live OKX queries against `data/okx_perp.py` and `data/okx_funding.py`
to verify two prompt-mandated halt triggers:

  Track A: BTC-USDT-SWAP and ETH-USDT-SWAP 1h OHLCV history depth
           covers the dev_end boundary 2025-09-12 UTC.
  Track B: funding cadence on both pairs is 8h.

Either trigger firing means we stop and surface to chat per the
prompt — no doc edits, no harness changes, no manifest mods.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data.okx_perp import fetch_perp_ohlcv
from data.okx_funding import detect_funding_cadence, fetch_funding_history


DEV_END = pd.Timestamp("2025-09-12T00:00:00Z")
PAIRS = ["BTC/USDT", "ETH/USDT"]


def check_track_a():
    print("=" * 76)
    print("Track A — OHLCV history depth verification")
    print("=" * 76)
    print(f"dev_end boundary: {DEV_END.isoformat()}\n")
    halt_a = []
    for sym in PAIRS:
        print(f"-- {sym} 1h × 36mo --")
        df = fetch_perp_ohlcv(sym, "1h", months=36)
        earliest = df.index.min() if len(df) else None
        latest = df.index.max() if len(df) else None
        depth_days = (latest - earliest).days if earliest is not None else 0
        covers = earliest is not None and earliest <= DEV_END
        print(
            f"   rows={len(df)}  earliest={earliest}  latest={latest}  "
            f"depth={depth_days}d"
        )
        print(f"   covers dev_end? {covers}")
        if not covers:
            halt_a.append((sym, earliest))
        print()
    if halt_a:
        print(f"HALT-AND-CONSULT (Track A): pairs short of dev_end: {halt_a}")
    else:
        print("Track A: no HALT trigger.")
    return halt_a


def check_track_b():
    print()
    print("=" * 76)
    print("Track B — funding cadence verification (8h expected)")
    print("=" * 76)
    halt_b = []
    for sym in PAIRS:
        print(f"-- {sym} funding × 1mo --")
        df = fetch_funding_history(sym, months=1)
        if len(df) < 2:
            print(f"   WARN: {len(df)} rows — too few to assess cadence")
            halt_b.append((sym, "insufficient rows"))
            continue
        cadence = detect_funding_cadence(df)
        print(
            f"   rows={len(df)}  earliest={df.index.min()}  "
            f"latest={df.index.max()}"
        )
        print(
            f"   cadence: median={cadence['median_seconds']/3600:.2f}h  "
            f"min={cadence['min_seconds']/3600:.2f}h  "
            f"max={cadence['max_seconds']/3600:.2f}h  "
            f"is_8h={cadence['is_8h']}"
        )
        if not cadence["is_8h"]:
            halt_b.append((sym, cadence))
        print()
    if halt_b:
        print(f"HALT-AND-CONSULT (Track B): non-8h cadence pairs: {halt_b}")
    else:
        print("Track B: no HALT trigger.")
    return halt_b


def main() -> int:
    halt_a = check_track_a()
    halt_b = check_track_b()
    print()
    print("=" * 76)
    print("HALT-AND-CONSULT summary")
    print("=" * 76)
    print(f"  Track A halt: {bool(halt_a)}  details={halt_a}")
    print(f"  Track B halt: {bool(halt_b)}  details={halt_b}")
    return 1 if (halt_a or halt_b) else 0


if __name__ == "__main__":
    raise SystemExit(main())
