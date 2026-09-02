#!/usr/bin/env python
"""Apply the dependence correction to the three 2026-09-02 discovery screens.

Decision rule pre-committed at `a410b68` (`research/discovery/README.md`
§ "Dependence-corrected significance") BEFORE any number here was computed.

NOT A NEW SCREEN. This re-derives the SAME statistic each screen already
recorded — same signal, same universe, same horizon, same window — and
recomputes only its standard error under dependence. `N_disc` is unchanged for
every family and no `trials.log` row is written.

Runtime is dominated by rebuilding the deleveraging panels from the metrics
cache (~4 min for 182 symbols). Everything is cache-only; nothing downloads.

Usage:
    python scripts/cluster_robust_screens.py --json docs/cluster_robust_2026-09.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.cluster_robust_check import (  # noqa: E402
    cluster_robust_mean,
    merge_dates_into_episodes,
    newey_west_mean,
)


def _pack(r, extra=None) -> dict:
    d = {
        "mean": r.mean, "n": r.n, "n_groups": r.n_groups, "dof": r.dof,
        "se_ordinary": r.se_ordinary, "se_robust": r.se_robust,
        "t_ordinary": r.t_ordinary, "t_robust": r.t_robust,
        "design_effect": r.design_effect, "mde_robust": r.mde_robust,
        "effect_exceeds_mde": bool(abs(r.mean) > r.mde_robust),
        "t_robust_clears_3": bool(abs(r.t_robust) > 3.0),
        "small_g": r.small_g, "method": r.method,
    }
    if extra:
        d.update(extra)
    return d


# ── family 2: deleveraging reversal ──────────────────────────────────────────

def run_deleveraging() -> dict:
    import scripts.discovery_deleveraging_reversal as d

    base = Path(d.__file__).resolve().parent.parent / "backtest" / "cache" / "binance_um"
    symbols = sorted(p.name[:-len(".parquet")]
                     for p in (base / "metrics_5m").glob("*.parquet"))
    panels = d.load_discovery_panels(symbols=symbols)
    res = d.screen_deleveraging_reversal(**panels)
    ev = res["events"]
    print(f"  deleveraging: {len(ev)} events rebuilt")

    out = {}
    dates = pd.to_datetime(ev["timestamp"]).dt.normalize()
    episodes = merge_dates_into_episodes(dates, window_days=5)

    for h, key in ((3, "h3_headline"), (1, "h1_lead")):
        col = ev[f"rev_{h}"].to_numpy(dtype=float) * 100.0   # percent
        ok = np.isfinite(col)
        out[key] = {
            "by_date": _pack(cluster_robust_mean(col[ok], dates[ok].to_numpy()),
                             {"horizon_days": h, "cluster": "event UTC date"}),
            "by_episode": _pack(
                cluster_robust_mean(col[ok], np.asarray(episodes)[ok]),
                {"horizon_days": h, "cluster": "episode (dates within 5 days merged)"}),
        }
    return out


# ── family 3: listing flow ───────────────────────────────────────────────────

def run_listing_flow() -> dict:
    import scripts.discovery_listing_flow as l

    panels = l.load_discovery_panels()
    res = l.screen_listing_flow(**panels)
    car = np.asarray(res["car_series"], dtype=float) * 100.0   # percent
    dates = pd.to_datetime(pd.Series(res["event_dates"])).dt.normalize()
    if len(car) != len(dates):
        return {"error": f"car_series {len(car)} != event_dates {len(dates)}"}
    ok = np.isfinite(car)
    print(f"  listing_flow: {int(ok.sum())} events rebuilt")

    months = dates.dt.to_period("M").astype(str)
    # Non-overlapping variant: one CAR per calendar month (the mean of that
    # month's listings), so no two observations share a [+0,+20] window.
    md = pd.DataFrame({"m": months[ok].to_numpy(), "car": car[ok]})
    monthly = md.groupby("m")["car"].mean()

    return {
        "by_date": _pack(cluster_robust_mean(car[ok], dates[ok].to_numpy()),
                         {"cluster": "listing UTC date"}),
        "by_month": _pack(cluster_robust_mean(car[ok], months[ok].to_numpy()),
                          {"cluster": "listing calendar month"}),
        "non_overlapping_monthly": _pack(
            cluster_robust_mean(monthly.to_numpy(), np.arange(len(monthly))),
            {"cluster": "one CAR per calendar month (no window overlap)"}),
    }


# ── family 1: funding dispersion ─────────────────────────────────────────────

def run_funding() -> dict:
    import scripts.discovery_funding_dispersion as f

    panels = f.load_discovery_panels()
    res = f.screen_funding_dispersion(**panels)
    net = res["net_series"].dropna().to_numpy(dtype=float) * 100.0
    print(f"  funding_dispersion: {len(net)} rebalance days rebuilt")

    lag1 = float(pd.Series(net).autocorr(lag=1))
    out = {"autocorr_lag1": lag1}
    for lag in (5, 10, 20):
        out[f"nw_lag{lag}"] = _pack(newey_west_mean(net, lag=lag),
                                    {"lag": lag})
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--json", default=None)
    ap.add_argument("--only", default=None,
                    help="deleveraging | listing | funding")
    args = ap.parse_args(argv[1:])

    print("Dependence correction — NOT a new screen. N_disc unchanged; "
          "no trials.log row.\n")
    out: dict = {}
    jobs = {"funding": run_funding, "listing": run_listing_flow,
            "deleveraging": run_deleveraging}
    for name, fn in jobs.items():
        if args.only and args.only != name:
            continue
        print(f"[{name}]")
        try:
            out[name] = fn()
        except Exception as exc:                       # noqa: BLE001
            print(f"  ERROR: {exc.__class__.__name__}: {exc}")
            out[name] = {"error": f"{exc.__class__.__name__}: {exc}"}
        print()

    print(json.dumps(out, indent=2, default=float)[:200] + " ...")
    if args.json:
        Path(args.json).write_text(json.dumps(out, indent=2, default=float),
                                   encoding="utf-8")
        print(f"\n[json] {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
