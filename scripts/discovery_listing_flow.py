#!/usr/bin/env python3
"""
scripts/discovery_listing_flow.py — DISCOVERY SCREEN, NOT A TRIAL.

Family 3 of the §C.4 first batch in `docs/research_revival_2026-09.md`:
**listing / delisting flow**.  Mechanism: price-insensitive flows around
Binance perp listing (attention, index/market-maker inventory) and
delisting (forced closure).

This is a **discovery screen** under the discovery / confirmation split
of `docs/research_revival_2026-09.md` §C.2, NOT a counted trial:

  * It writes **no** `backtest/trials.log` row.  It does not import
    `backtest.trials` and never calls `record_trial`.  The ledger row in
    `research/discovery/listing_flow.md` IS its record.
  * It reads **only** the sealed discovery window
    2020-01-01 → 2022-12-31.  Every timestamp it touches is
    hard-asserted `< 2023-01-01T00:00:00Z`; the run aborts otherwise.
  * Its ledger-row count `N_disc` is carried into the confirmation
    trial's pre-registration as an additional Bonferroni-style haircut
    on the confirmation DSR.

Pre-registered kill test (verbatim, §C.4 row 3)
───────────────────────────────────────────────
  ~900 listings + all delistings 2020–22: abnormal return −5…+20 days
  around the event vs matched names.  Threshold: |CAR| ≥ 3 % with t > 3
  in a pre-specified window

Statistic
─────────
Abnormal return is market-adjusted against the **matched names**
benchmark: the equal-weight cross-sectional mean return of every symbol
with a bar that day (the listed cohort), so `ar[s,t] = r[s,t] − r̄[t]`.
CAR is the cumulative sum of `ar` over an event-relative window.

The pre-specified windows are fixed BEFORE any statistic is read and are
not re-selected from the −5…+20 curve:

  * **listings**  → `[0, +20]` (the post-listing inventory/attention
    window; day −5…−1 does not exist for a name that has not listed yet)
  * **delistings** → `[−5, 0]` (the forced-closure window ending at the
    last traded bar)

The headline value is `mean(CAR)` in % over the pre-specified window
with its cross-sectional t-statistic and N = event count.  The full
−5…+20 profile is printed for context but is NOT the statistic — reading
the best window off that curve is exactly the p-hacking the split
forbids.

Usage
─────
    python3 scripts/discovery_listing_flow.py --selftest
    python3 scripts/discovery_listing_flow.py                  # real data
    python3 scripts/discovery_listing_flow.py --event-kind delisting
    python3 scripts/discovery_listing_flow.py --append-ledger

**Status 2026-09-02:** the discovery / confirmation split is PROPOSED,
not approved (`docs/proposed_backtest_rule_discovery_2026-09.md`).
Until the human pre-authorizes the `.claude/rules/backtest.md` edit,
run `--selftest` only.
"""

from __future__ import annotations

import argparse
import math
import subprocess
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

FAMILY = "listing_flow"
LEDGER = _REPO_ROOT / "research" / "discovery" / f"{FAMILY}.md"

DISCOVERY_END = pd.Timestamp("2023-01-01T00:00:00Z")
DISCOVERY_START = pd.Timestamp("2020-01-01T00:00:00Z")

WINDOW_START = -5
WINDOW_END = 20
#: Pre-specified (frozen before any statistic is read) test windows.
PRESPECIFIED_WINDOW = {
    "listing": (0, 20),
    "delisting": (-5, 0),
}
THRESHOLD_CAR_PCT = 3.0
THRESHOLD_T = 3.0


# ── Discovery-window guard ───────────────────────────────────────────────────

def assert_discovery_window(label: str, index) -> None:
    """Abort unless every timestamp in `index` is < 2023-01-01 UTC."""
    idx = pd.DatetimeIndex(index)
    if len(idx) == 0:
        return
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    worst = idx.max()
    if worst >= DISCOVERY_END:
        raise SystemExit(
            f"DISCOVERY WINDOW VIOLATION in {label}: max timestamp "
            f"{worst.isoformat()} >= {DISCOVERY_END.isoformat()}. "
            "Discovery screens must never read 2023+ data "
            "(docs/research_revival_2026-09.md §C.2 item 1c). Aborting."
        )


# ── Statistic ────────────────────────────────────────────────────────────────

def abnormal_returns(close: pd.DataFrame) -> pd.DataFrame:
    """`ar[s,t] = r[s,t] − r̄[t]`, the matched-names (equal-weight
    listed-cohort) benchmark adjustment."""
    rets = close.sort_index().pct_change()
    bench = rets.mean(axis=1, skipna=True)
    return rets.sub(bench, axis=0)


def screen_listing_flow(
    close: pd.DataFrame,
    events: pd.DataFrame,
    *,
    event_kind: str = "listing",
    window_start: int = WINDOW_START,
    window_end: int = WINDOW_END,
) -> dict:
    """Compute the §C.4 row-3 kill-test statistic.

    Args:
      close:       Wide daily close panel `[date × symbol]`.
      events:      DataFrame with columns `symbol`, `event_date`, `kind`.
      event_kind:  `"listing"` or `"delisting"` — selects both the event
                   subset and the pre-specified test window.
      window_start / window_end: Event-relative profile bounds (−5…+20).

    Returns:
      dict with `profile` (mean AR and CAR per event-day offset),
      `car_series` (per-event CAR over the pre-specified window),
      `mean_car_pct`, `t_stat`, `n_events`, `window`, `start`, `end`.
    """
    if event_kind not in PRESPECIFIED_WINDOW:
        raise ValueError(
            f"event_kind must be one of {sorted(PRESPECIFIED_WINDOW)}; "
            f"got {event_kind!r}"
        )
    ar = abnormal_returns(close)
    idx = ar.index
    pos = {ts: i for i, ts in enumerate(idx)}
    offsets = list(range(window_start, window_end + 1))
    w_lo, w_hi = PRESPECIFIED_WINDOW[event_kind]

    sel = events[events["kind"] == event_kind]
    per_offset = {o: [] for o in offsets}
    car_rows = []
    used_dates = []

    ar_v = ar.to_numpy()
    col = {s: j for j, s in enumerate(ar.columns)}

    for _, ev in sel.iterrows():
        sym = ev["symbol"]
        if sym not in col:
            continue
        ts = pd.Timestamp(ev["event_date"])
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        if ts not in pos:
            continue
        i0 = pos[ts]
        j = col[sym]
        window_vals = []
        for o in offsets:
            i = i0 + o
            if 0 <= i < len(idx):
                v = ar_v[i, j]
                if np.isfinite(v):
                    per_offset[o].append(v)
                    if w_lo <= o <= w_hi:
                        window_vals.append(v)
        if len(window_vals) == 0:
            continue
        car_rows.append(float(np.sum(window_vals)))
        used_dates.append(ts)

    car = np.asarray(car_rows, dtype=float)
    n = car.size
    sd = float(car.std(ddof=1)) if n > 1 else float("nan")
    t_stat = (
        float(car.mean()) / (sd / math.sqrt(n))
        if n > 1 and sd > 0 else float("nan")
    )

    profile = []
    running = 0.0
    for o in offsets:
        vals = np.asarray(per_offset[o], dtype=float)
        m = float(vals.mean()) if vals.size else float("nan")
        if math.isfinite(m):
            running += m
        profile.append({
            "offset": o,
            "mean_ar_pct": m * 100.0,
            "cum_car_pct": running * 100.0,
            "n": int(vals.size),
        })

    return {
        "profile": pd.DataFrame(profile),
        "car_series": car,
        "mean_car_pct": float(car.mean()) * 100.0 if n else float("nan"),
        "t_stat": t_stat,
        "n_events": n,
        "window": (w_lo, w_hi),
        "event_kind": event_kind,
        "start": min(used_dates) if used_dates else None,
        "end": max(used_dates) if used_dates else None,
    }


# ── Real-data loading (Binance UM archive; discovery window only) ────────────

def load_discovery_panels(cache_dir=None, max_symbols=None) -> dict:
    """Build the daily close panel and the listing/delisting event table
    from `backtest/cache/binance_um/`, capped strictly before 2023-01-01.

    Listing date = the symbol's FIRST 1d bar inside the window;
    delisting date = its LAST 1d bar, and only when `universe.parquet`
    marks the symbol delisted and that last bar is inside the window
    (a symbol still trading in 2023 has no delisting event here).
    """
    from data import binance_vision_um as um

    kwargs = {} if cache_dir is None else {"cache_dir": cache_dir}
    universe = um.universe_table(**kwargs)
    if len(universe) == 0:
        raise SystemExit(
            "universe.parquet is empty — run scripts/prefetch_binance_um.py "
            "first (this screen does no network fetching of its own)."
        )
    listed = universe[universe["first_month"] < "2023-01"]
    if max_symbols is not None:
        listed = listed.head(max_symbols)

    closes = {}
    rows = []
    for _, u in listed.iterrows():
        sym = u["symbol"]
        try:
            k = um.fetch_klines(
                sym, "1d", "2020-01", "2022-12", until=DISCOVERY_END, **kwargs)
        except Exception as exc:                       # noqa: BLE001
            print(f"  skip {sym}: {exc.__class__.__name__}: {exc}")
            continue
        if len(k) == 0:
            continue
        assert_discovery_window(f"klines[{sym}]", k.index)
        closes[sym] = k["close"]
        # Listing: first bar in-window, and only if the archive itself
        # starts here (not a symbol that pre-dates 2020-01).
        if str(u["first_month"]) >= "2020-01":
            rows.append({
                "symbol": sym, "event_date": k.index[0], "kind": "listing"})
        if bool(u.get("delisted", False)) and str(u["last_month"]) < "2023-01":
            rows.append({
                "symbol": sym, "event_date": k.index[-1], "kind": "delisting"})

    if len(closes) == 0:
        raise SystemExit("no cached symbols inside the discovery window")
    close = pd.DataFrame(closes).sort_index()
    assert_discovery_window("panel[close]", close.index)
    events = pd.DataFrame(rows, columns=["symbol", "event_date", "kind"])
    if len(events) > 0:
        assert_discovery_window("events", pd.DatetimeIndex(
            events["event_date"]))
    return {"close": close, "events": events}


# ── Ledger ───────────────────────────────────────────────────────────────────

def _git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(_REPO_ROOT), capture_output=True, text=True, check=True,
        ).stdout.strip()
    except Exception:                                  # noqa: BLE001
        return "unknown"


def append_ledger_row(result: dict, conclusion: str) -> None:
    """Append one row to `research/discovery/listing_flow.md`."""
    if not LEDGER.exists():
        raise SystemExit(f"ledger missing: {LEDGER}")
    start, end = result["start"], result["end"]
    rng = (
        f"{pd.Timestamp(start).date()} → {pd.Timestamp(end).date()}"
        if start is not None else "n/a"
    )
    if end is not None and pd.Timestamp(end) >= DISCOVERY_END:
        raise SystemExit(
            "refusing to append a ledger row whose data range reaches "
            f"{end} (>= {DISCOVERY_END.date()})"
        )
    lo, hi = result["window"]
    row = " | ".join([
        "",
        date.today().isoformat(),
        FAMILY,
        f"{result['event_kind']} event, abnormal return vs equal-weight "
        "listed-cohort benchmark",
        "all UM symbols with 1d klines in window",
        f"pre-specified [{lo:+d}, {hi:+d}] days",
        f"mean CAR over the pre-specified window, %",
        f"{result['mean_car_pct']:.4f} %",
        f"{result['t_stat']:.2f}",
        str(result["n_events"]),
        rng,
        f"scripts/discovery_listing_flow.py @ {_git_commit()}",
        conclusion,
        "",
    ]).strip()
    with LEDGER.open("a", encoding="utf-8") as fh:
        fh.write(row + "\n")
    print(f"[ledger] appended → {LEDGER}")


def verdict(result: dict) -> str:
    if result["n_events"] == 0 or not math.isfinite(result["mean_car_pct"]):
        return "inconclusive — no usable events"
    ok = (
        abs(result["mean_car_pct"]) >= THRESHOLD_CAR_PCT
        and math.isfinite(result["t_stat"])
        and abs(result["t_stat"]) > THRESHOLD_T
    )
    if ok:
        return (
            f"survives — |CAR| {abs(result['mean_car_pct']):.3f} % >= "
            f"{THRESHOLD_CAR_PCT} % with |t|={abs(result['t_stat']):.2f} > "
            f"{THRESHOLD_T}"
        )
    return (
        f"killed — |CAR| {abs(result['mean_car_pct']):.3f} % / "
        f"t={result['t_stat']:.2f} misses the {THRESHOLD_CAR_PCT} % & "
        f"|t|>{THRESHOLD_T} bar"
    )


def report(result: dict) -> None:
    lo, hi = result["window"]
    print(f"── listing/delisting flow: §C.4 row-3 kill test "
          f"({result['event_kind']}) ──")
    print(f"  statistic     : mean CAR over pre-specified "
          f"[{lo:+d}, {hi:+d}] days, %")
    print(f"  value         : {result['mean_car_pct']:.4f} %")
    print(f"  t-stat        : {result['t_stat']:.3f}")
    print(f"  N (events)    : {result['n_events']}")
    if result["start"] is not None:
        print(
            f"  data range    : {pd.Timestamp(result['start']).date()} → "
            f"{pd.Timestamp(result['end']).date()}"
        )
    print(f"  threshold     : |CAR| >= {THRESHOLD_CAR_PCT} % with "
          f"|t| > {THRESHOLD_T}")
    print("  profile (context only — NOT the statistic):")
    prof = result["profile"]
    for _, r in prof.iterrows():
        print(
            f"    d{int(r['offset']):+03d}  AR={r['mean_ar_pct']:+.4f} %  "
            f"CAR={r['cum_car_pct']:+.4f} %  n={int(r['n'])}"
        )
    print(f"  conclusion    : {verdict(result)}")


# ── Selftest (synthetic; no cache, no network) ───────────────────────────────

def selftest() -> int:
    """Plant a known post-listing abnormal drift and recover it."""
    n_days, n_syms, n_events = 440, 24, 20
    idx = pd.date_range("2020-01-01", periods=n_days, freq="1D", tz="UTC")
    syms = [f"S{i:02d}USDT" for i in range(n_syms)]
    rng = np.random.default_rng(31337)

    rets = rng.normal(0.0, 0.01, size=(n_days, n_syms))
    # Plant +0.40 % abnormal per day on days 0..+20 after each event,
    # one event per symbol so no two windows overlap.
    events = []
    for k in range(n_events):
        j = k
        t0 = 40 + k * 18
        for o in range(0, 21):
            rets[t0 + o, j] += 0.004
        events.append(
            {"symbol": syms[j], "event_date": idx[t0], "kind": "listing"})
    events_df = pd.DataFrame(events)

    close = pd.DataFrame(
        100.0 * np.cumprod(1.0 + rets, axis=0), index=idx, columns=syms)
    res = screen_listing_flow(close, events_df, event_kind="listing")

    # 21 days × 0.40 % ≈ 8.4 %, minus the benchmark's own share of the
    # planted drift (1/24 of it) → ≈ 8.05 %.
    assert res["n_events"] == n_events, res["n_events"]
    assert res["window"] == (0, 20), res["window"]
    assert abs(res["mean_car_pct"] - 8.05) < 1.0, res["mean_car_pct"]
    assert res["t_stat"] > THRESHOLD_T, res["t_stat"]
    assert "survives" in verdict(res)
    assert len(res["profile"]) == (WINDOW_END - WINDOW_START + 1)

    # No planted effect → the screen must not manufacture one.
    clean = pd.DataFrame(
        100.0 * np.cumprod(
            1.0 + rng.normal(0.0, 0.01, size=(n_days, n_syms)), axis=0),
        index=idx, columns=syms,
    )
    res_null = screen_listing_flow(clean, events_df, event_kind="listing")
    assert abs(res_null["t_stat"]) < THRESHOLD_T, res_null["t_stat"]
    assert "killed" in verdict(res_null) or "inconclusive" in verdict(res_null)

    # Delisting selects the OTHER pre-specified window.
    del_events = events_df.assign(kind="delisting")
    res_del = screen_listing_flow(close, del_events, event_kind="delisting")
    assert res_del["window"] == (-5, 0), res_del["window"]

    try:
        assert_discovery_window(
            "selftest", pd.DatetimeIndex(["2023-02-01T00:00:00Z"]))
    except SystemExit:
        pass
    else:                                              # pragma: no cover
        raise AssertionError("discovery-window guard failed to fire")

    print("selftest OK — listing-flow CAR machinery works")
    print(
        f"  planted CAR recovered: {res['mean_car_pct']:.4f} % "
        f"(expected ~8.05), t={res['t_stat']:.2f}, N={res['n_events']}"
    )
    return 0


# ── CLI ──────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--selftest", action="store_true",
                    help="run on a synthetic panel; no cache needed")
    ap.add_argument("--append-ledger", action="store_true",
                    help="append the result row to the discovery ledger")
    ap.add_argument("--event-kind", choices=sorted(PRESPECIFIED_WINDOW),
                    default="listing")
    ap.add_argument("--max-symbols", type=int, default=None)
    ap.add_argument("--cache-dir", default=None)
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    print(
        "NOTE: discovery screen, NOT a trial — no trials.log row is "
        "written (docs/research_revival_2026-09.md §C.2)."
    )
    panels = load_discovery_panels(
        cache_dir=args.cache_dir, max_symbols=args.max_symbols)
    result = screen_listing_flow(
        panels["close"], panels["events"], event_kind=args.event_kind)
    report(result)
    if args.append_ledger:
        append_ledger_row(result, verdict(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
