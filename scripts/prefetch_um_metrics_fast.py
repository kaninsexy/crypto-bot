#!/usr/bin/env python
"""Parallel prefetch of the Binance UM 5-minute metrics archive.

WHY THIS EXISTS. `scripts/prefetch_binance_um.py --metrics` fetches one zip
per (symbol, day) SERIALLY. Measured 2026-09-02: ~1.5 requests/second, so the
100 symbols x 396 days this screen needs would take ~11 hours. The bottleneck
is round-trip latency, not the archive or any rate limit — the requests are
independent GETs of static S3 objects. A thread pool collapses it to ~40
minutes.

WHAT IT DOES NOT CHANGE. Cache format, parse path, and 404 bookkeeping are the
existing ones, reused by import from `data.binance_vision_um`:

    metrics_5m/{SYM}.parquet        <- _merge_cache, same columns, same index
    metrics_5m/{SYM}.missing.json   <- _save_missing, same 404 ledger

so `um.fetch_metrics(...)` reads back everything this writes and will not
re-download it. This script is a faster WRITER for the same cache, not a second
substrate. It is a prefetch utility only: no screen and no trial calls it, and
nothing in the validation harness imports it.

Politeness: `--workers` defaults to 24. The archive is static S3 content and
these are plain GETs, but the pool is bounded and each worker reuses one
`requests.Session` (connection reuse, and `requests.Session` is not documented
as thread-safe, so one per worker rather than one shared).

Usage:
    python scripts/prefetch_um_metrics_fast.py \\
        --symbols-from-cache --limit 110 \\
        --start 2021-12-01 --end 2022-12-31 --workers 24
"""

from __future__ import annotations

import argparse
import glob
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data.binance_vision_um import (  # noqa: E402
    DEFAULT_CACHE_DIR,
    _download_zip,
    _load_missing,
    _merge_cache,
    _read_parquet_if_exists,
    _save_missing,
    day_range,
    normalise_symbol,
    parse_metrics_csv,
)

BASE = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
METRICS = BASE + "/data/futures/um/daily/metrics"

_local = threading.local()


def _session() -> requests.Session:
    """One Session per worker thread (connection reuse; not shared)."""
    s = getattr(_local, "session", None)
    if s is None:
        s = requests.Session()
        _local.session = s
    return s


def _fetch_one(sym: str, day: str):
    """Return (day, DataFrame|None). None means 404 — a real absence."""
    url = f"{METRICS}/{sym}/{sym}-metrics-{day}.zip"
    raw = _download_zip(url, session=_session())
    if raw is None:
        return day, None
    return day, parse_metrics_csv(raw)


def symbols_from_cache(limit: int, start: str, end: str) -> list[str]:
    """Rank cached symbols by mean daily quote volume over the window.

    Ranking uses only ALREADY-CACHED daily klines, so symbol selection needs no
    network and is reproducible from the repo's own cache.
    """
    rows = []
    for p in glob.glob(str(Path(DEFAULT_CACHE_DIR) / "klines" / "*_1d.parquet")):
        sym = os.path.basename(p).replace("_1d.parquet", "")
        try:
            df = pd.read_parquet(p, columns=["quote_volume"])
        except Exception:  # noqa: BLE001 — a corrupt cache file is just skipped
            continue
        w = df.loc[start:end, "quote_volume"]
        if len(w) >= 60:
            rows.append((sym, float(w.mean())))
    rows.sort(key=lambda r: -r[1])
    return [s for s, _ in rows[:limit]]


def prefetch_symbol(sym: str, days: list[str], cache_dir: Path, workers: int,
                    force: bool) -> dict:
    sym = normalise_symbol(sym)
    cache_path = Path(cache_dir) / "metrics_5m" / f"{sym}.parquet"
    miss_path = cache_path.with_suffix(".missing.json")
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    cached = None if force else _read_parquet_if_exists(cache_path)
    missing = set() if force else _load_missing(miss_path)
    have = set()
    if cached is not None and len(cached) > 0:
        have = set(cached.index.strftime("%Y-%m-%d"))

    todo = [d for d in days if d not in have and d not in missing]
    if not todo:
        return {"symbol": sym, "downloaded": 0, "missing": 0, "skipped": len(days),
                "rows": 0 if cached is None else len(cached)}

    frames, n_404 = [], 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_fetch_one, sym, d): d for d in todo}
        for fut in as_completed(futs):
            day = futs[fut]
            try:
                _, df = fut.result()
            except Exception as exc:  # noqa: BLE001 — one bad day must not kill the symbol
                print(f"    ! {sym} {day}: {exc.__class__.__name__}: {exc}")
                continue
            if df is None:
                missing.add(day)
                n_404 += 1
            elif len(df) > 0:
                frames.append(df)

    if frames:
        merged = _merge_cache(cached, pd.concat(frames).sort_index())
        merged.to_parquet(cache_path)
        cached = merged
    _save_missing(miss_path, missing)
    return {
        "symbol": sym,
        "downloaded": len(frames),
        "missing": n_404,
        "skipped": len(days) - len(todo),
        "rows": 0 if cached is None else len(cached),
    }


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sel = ap.add_mutually_exclusive_group(required=True)
    sel.add_argument("--symbols", help="comma-separated symbol list")
    sel.add_argument("--symbols-from-cache", action="store_true",
                     help="rank already-cached symbols by mean quote volume in the window")
    ap.add_argument("--limit", type=int, default=110,
                    help="how many symbols when using --symbols-from-cache")
    ap.add_argument("--start", required=True, help="first day YYYY-MM-DD (inclusive)")
    ap.add_argument("--end", required=True, help="last day YYYY-MM-DD (INCLUSIVE)")
    ap.add_argument("--workers", type=int, default=24)
    ap.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR))
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv[1:])

    days = day_range(args.start, args.end)
    syms = (symbols_from_cache(args.limit, args.start, args.end)
            if args.symbols_from_cache
            else [s.strip() for s in args.symbols.split(",") if s.strip()])

    print(f"metrics prefetch: {len(syms)} symbols x {len(days)} days "
          f"({args.start} -> {args.end} inclusive), {args.workers} workers")
    t0 = time.time()
    done = 0
    for i, sym in enumerate(syms, 1):
        st = time.time()
        r = prefetch_symbol(sym, days, Path(args.cache_dir), args.workers, args.force)
        done += 1
        el = time.time() - t0
        eta = (el / done) * (len(syms) - done)
        print(f"  [{i:3d}/{len(syms)}] {r['symbol']:14s} "
              f"new={r['downloaded']:4d} 404={r['missing']:4d} "
              f"cached={r['skipped']:4d} rows={r['rows']:6d} "
              f"({time.time()-st:5.1f}s, eta {eta/60:5.1f}m)", flush=True)
    print(f"done in {(time.time()-t0)/60:.1f} min")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
