#!/usr/bin/env python
"""§A.6 fallback: passive BTC/ETH + rules-based trend/vol overlay.

PRE-REGISTRATION: `research/passive-overlay-literature.md`, committed at
cca9b24 BEFORE this script existed. Rule, parameters, window, metrics and
failure conditions are frozen there. This file implements them and must not
introduce a parameter the pre-registration does not name.

THIS IS NOT AN ALPHA CLAIM AND WRITES NO TRIALS.LOG ROW.

    A `trials.log` row is a claim submitted to the multiple-testing
    correction. This design makes no claim of edge -- its counterparty is
    nobody, because it extracts nothing. Counting it would inflate N for
    every strategy that DOES make a claim, making the DSR haircut harsher for
    real candidates on the strength of something that never competed.

    This module therefore does not import `backtest.trials` and never calls
    `record_trial`. That is enforced by a test, not left to discipline
    (`backtest/tests/test_passive_overlay.py`).

It is scored OUT of the verdict tree, on drawdown and Calmar. It would FAIL
gate v2's IR-vs-buy-and-hold test by construction, because a rule that sits
flat through part of a bull market must underperform buy-and-hold over a
mostly-bull window. That is the wrong instrument, not a verdict.

Usage:
    python scripts/run_passive_overlay.py
    python scripts/run_passive_overlay.py --json out.json --chart out.svg
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

# ── Frozen parameters (research/passive-overlay-literature.md) ───────────────
ASSETS = ("BTC-USDT", "ETH-USDT")
MA_WINDOW = 200               # Faber (2007)
VOL_LOOKBACK = 30             # Barroso & Santa-Clara (2015)
VOL_TARGET_ANNUAL = 0.20
MAX_LEVERAGE = 1.0            # never above 1.0
COST_PER_SIDE = 0.0010        # OKX spot taker, 0.10 %
TRADING_DAYS = 365            # crypto trades every day
WINDOW_START = "2021-05-30"   # first date with a valid 200d signal
WINDOW_END = "2025-05-01"     # dev/holdout boundary, respected

CACHE = _REPO / "backtest" / "cache" / "ohlcv"

# I3 robustness substrate. Binance SPOT daily klines reach back to 2017, so the
# March 2020 crash -- the V-shaped case a 200-day-MA overlay handles WORST, and
# the case the OKX window excludes by accident -- becomes reachable. Same
# instrument class as the pre-registration (spot), different venue; the
# cross-venue provenance precedent is the 2026-06-11 BNB backfill.
#
# NOTHING about the RULE changes across substrates: same 200-day MA, same 20 %
# vol target, same 1.0x cap, same monthly rebalance, same cost. Only the price
# series and therefore the available window differ.
BINANCE_SYMBOLS = {"BTC-USDT": "BTCUSDT", "ETH-USDT": "ETHUSDT"}


def load_prices_binance(start_month: str = "2019-06") -> pd.DataFrame:
    """Wide daily close panel from the Binance spot archive (cache-first)."""
    from data.binance_vision import load_klines

    cols = {}
    for okx_sym, bn_sym in BINANCE_SYMBOLS.items():
        df = load_klines(bn_sym, start_month, "2025-05", interval="1d")
        cols[okx_sym] = df["close"].astype(float)
    px = pd.DataFrame(cols).sort_index()
    px = px[px.index <= pd.Timestamp(WINDOW_END, tz="UTC")]
    return px.dropna(how="any")


def load_prices() -> pd.DataFrame:
    """Wide daily close panel for the overlay assets, from the OKX cache.

    Reads the parquet directly rather than through `backtest.cache`: this is
    a report, not a trial, and it must not touch the holdout-enforcement path
    or look like a strategy requesting data. The window is capped at
    WINDOW_END regardless, which is the dev/holdout boundary.
    """
    cols = {}
    for sym in ASSETS:
        cands = sorted(CACHE.glob(f"{sym}_1d_*.parquet"))
        if not cands:
            raise SystemExit(f"no daily cache for {sym} in {CACHE}")
        best = max(cands, key=lambda p: int(p.stem.split("_")[-1].rstrip("mo")))
        cols[sym] = pd.read_parquet(best)["close"]
    px = pd.DataFrame(cols).sort_index()
    px = px[px.index <= pd.Timestamp(WINDOW_END, tz="UTC")]
    return px.dropna(how="any")


def build_weights(px: pd.DataFrame) -> pd.DataFrame:
    """Target weight per asset per day, per the frozen rule.

    Signals use only information available at the prior close (`.shift(1)`),
    so there is no lookahead: the weight applied to day t's return is decided
    from data up to t-1.
    """
    rets = px.pct_change()

    ma = px.rolling(MA_WINDOW, min_periods=MA_WINDOW).mean()
    above = (px > ma)

    realised = rets.rolling(VOL_LOOKBACK, min_periods=VOL_LOOKBACK).std() \
        * np.sqrt(TRADING_DAYS)
    scale = (VOL_TARGET_ANNUAL / realised).clip(upper=MAX_LEVERAGE)

    raw = above.astype(float) * scale * (1.0 / len(ASSETS))   # equal weight
    raw = raw.shift(1)                                        # decided at t-1

    # Monthly rebalance: adopt the weight on the first UTC day of each month
    # and hold it until the next one. Compare adjacent month periods directly
    # rather than differencing them -- Period arithmetic drops the timezone
    # and overflows on a tz-aware index.
    per = raw.index.tz_localize(None).to_period("M")
    is_start = np.concatenate([[True], per[1:] != per[:-1]])
    held = raw.where(pd.Series(is_start, index=raw.index), axis=0).ffill()
    return held.fillna(0.0)


def equity_curve(px: pd.DataFrame, weights: pd.DataFrame) -> tuple:
    """Return (equity Series, daily net return Series, total cost paid)."""
    rets = px.pct_change().fillna(0.0)
    gross = (weights * rets).sum(axis=1)
    turnover = weights.diff().abs().sum(axis=1).fillna(weights.iloc[0].abs().sum())
    cost = turnover * COST_PER_SIDE
    net = gross - cost
    return (1.0 + net).cumprod(), net, float(cost.sum())


def buy_and_hold(px: pd.DataFrame) -> tuple:
    w = pd.DataFrame(1.0 / len(ASSETS), index=px.index, columns=px.columns)
    rets = px.pct_change().fillna(0.0)
    gross = (w * rets).sum(axis=1)
    net = gross.copy()
    net.iloc[0] -= COST_PER_SIDE            # one entry cost, then hold
    return (1.0 + net).cumprod(), net, COST_PER_SIDE


def metrics(equity: pd.Series, net: pd.Series, weights: pd.DataFrame | None,
            cost: float) -> dict:
    n_years = (equity.index[-1] - equity.index[0]).days / 365.25
    total = float(equity.iloc[-1])
    ann_ret = total ** (1.0 / n_years) - 1.0 if n_years > 0 else float("nan")
    ann_vol = float(net.std() * np.sqrt(TRADING_DAYS))
    dd = equity / equity.cummax() - 1.0
    max_dd = float(dd.min())
    calmar = ann_ret / abs(max_dd) if max_dd < 0 else float("nan")
    roll12 = equity.pct_change(365).dropna()
    worst12 = float(roll12.min()) if len(roll12) else float("nan")
    tim = (float((weights.sum(axis=1) > 1e-9).mean())
           if weights is not None else 1.0)
    return {
        "total_return": total - 1.0,
        "ann_return": ann_ret,
        "ann_vol": ann_vol,
        "max_drawdown": max_dd,
        "calmar": calmar,
        "worst_rolling_12m": worst12,
        "time_in_market": tim,
        "total_cost_paid": cost,
        "years": n_years,
    }


def diagnostics(equity: pd.Series, weights: pd.DataFrame) -> dict:
    """Robustness diagnostics (I3): whipsaws, round trips, drawdown episodes.

    `max_dd_date` matters more than it looks: if the deepest drawdown is not in
    the crash the window was extended to include, then that crash is not what
    the headline rests on -- and the reader should be told which episode is.
    """
    dd = equity / equity.cummax() - 1.0
    in_mkt = (weights.sum(axis=1) > 1e-9).astype(int)
    chg = in_mkt.diff().fillna(0)
    entries = list(in_mkt.index[chg == 1])
    exits = list(in_mkt.index[chg == -1])

    whipsaw = {}
    for e in entries:
        nxt = [x for x in exits if x > e]
        if nxt and (nxt[0] - e).days <= 30:      # in and back out inside a month
            whipsaw[e.year] = whipsaw.get(e.year, 0) + 1

    episodes, inside, trough = [], False, 0.0
    for v in dd.to_numpy():
        if v < -0.05:
            inside, trough = True, min(trough, v)
        elif inside and v > -0.005:              # recovered to within 0.5 %
            episodes.append(trough)
            inside, trough = False, 0.0
    if inside:
        episodes.append(trough)

    return {
        "max_dd_date": str(dd.idxmin().date()),
        "round_trips": len(entries),
        "whipsaws_by_year": {str(k): v for k, v in whipsaw.items()},
        "whipsaws_total": sum(whipsaw.values()),
        "drawdown_episodes_over_5pct": len(episodes),
        "episode_depths": [round(float(e) * 100, 1) for e in episodes],
    }


def _pct(x: float) -> str:
    return "n/a" if not np.isfinite(x) else f"{x * 100:+.2f} %"


def render(ov: dict, bh: dict) -> str:
    rows = [
        ("Annualised return", _pct(ov["ann_return"]), _pct(bh["ann_return"])),
        ("Annualised volatility", _pct(ov["ann_vol"]), _pct(bh["ann_vol"])),
        ("Max drawdown", _pct(ov["max_drawdown"]), _pct(bh["max_drawdown"])),
        ("Calmar", f"{ov['calmar']:.2f}", f"{bh['calmar']:.2f}"),
        ("Worst rolling 12m", _pct(ov["worst_rolling_12m"]),
         _pct(bh["worst_rolling_12m"])),
        ("Time in market", f"{ov['time_in_market'] * 100:.1f} %", "100.0 %"),
        ("Total cost paid", _pct(ov["total_cost_paid"]),
         _pct(bh["total_cost_paid"])),
    ]
    w = max(len(r[0]) for r in rows)
    out = [f"{'metric'.ljust(w)} | {'overlay':>12} | {'buy & hold':>12}",
           f"{'-' * w}-+-{'-' * 12}-+-{'-' * 12}"]
    for name, a, b in rows:
        out.append(f"{name.ljust(w)} | {a:>12} | {b:>12}")
    return "\n".join(out)


def assess(ov: dict, bh: dict) -> dict:
    """Apply the PRE-COMMITTED failure conditions. No post-hoc narration."""
    dd_red = 1.0 - abs(ov["max_drawdown"]) / abs(bh["max_drawdown"])
    checks = {
        "drawdown_reduced_20pct": (dd_red >= 0.20, f"{dd_red * 100:.1f} % relative reduction (need >= 20 %)"),
        "calmar_improved": (ov["calmar"] > bh["calmar"], f"{ov['calmar']:.2f} vs {bh['calmar']:.2f}"),
        "time_in_market_50pct": (ov["time_in_market"] >= 0.50, f"{ov['time_in_market'] * 100:.1f} % (need >= 50 %)"),
    }
    return {"drawdown_reduction": dd_red, "checks": checks,
            "passes": all(v[0] for v in checks.values())}


def svg_chart(eq_ov: pd.Series, eq_bh: pd.Series, path: Path) -> None:
    """Minimal dependency-free SVG of the two equity curves (log scale)."""
    W, H, PAD = 900, 420, 55
    idx = eq_ov.index
    xs = np.linspace(PAD, W - PAD, len(idx))
    both = np.concatenate([eq_ov.to_numpy(), eq_bh.to_numpy()])
    lo, hi = float(np.nanmin(both)), float(np.nanmax(both))
    lo = max(lo, 1e-6)

    def ypix(v):
        t = (np.log(np.maximum(v, 1e-6)) - np.log(lo)) / (np.log(hi) - np.log(lo))
        return H - PAD - t * (H - 2 * PAD)

    def poly(s, colour, label):
        pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ypix(s.to_numpy())))
        return (f'<polyline fill="none" stroke="{colour}" stroke-width="2" '
                f'points="{pts}"><title>{label}</title></polyline>')

    ticks = []
    for frac in (0, 0.25, 0.5, 0.75, 1.0):
        i = int(frac * (len(idx) - 1))
        ticks.append(f'<text x="{xs[i]:.0f}" y="{H - PAD + 18}" font-size="11" '
                     f'text-anchor="middle" fill="#555">{idx[i].date()}</text>')
    gl = []
    for v in (0.5, 1, 2, 4, 8):
        if lo <= v <= hi:
            y = ypix(v)
            gl.append(f'<line x1="{PAD}" y1="{y:.1f}" x2="{W - PAD}" y2="{y:.1f}" '
                      f'stroke="#eee"/><text x="{PAD - 8}" y="{y + 4:.1f}" '
                      f'font-size="11" text-anchor="end" fill="#555">{v}x</text>')
    path.write_text(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
        f'viewBox="0 0 {W} {H}"><rect width="{W}" height="{H}" fill="white"/>'
        + "".join(gl)
        + poly(eq_bh, "#b0b0b0", "50/50 BTC/ETH buy & hold")
        + poly(eq_ov, "#1f6feb", "passive + 200d/vol overlay")
        + "".join(ticks)
        + f'<text x="{PAD}" y="26" font-size="14" fill="#111">Passive + overlay vs buy &amp; hold (log scale, growth of 1)</text>'
        + f'<rect x="{W - 250}" y="40" width="12" height="3" fill="#1f6feb"/>'
          f'<text x="{W - 232}" y="46" font-size="11" fill="#111">overlay</text>'
        + f'<rect x="{W - 250}" y="60" width="12" height="3" fill="#b0b0b0"/>'
          f'<text x="{W - 232}" y="66" font-size="11" fill="#111">buy &amp; hold</text>'
        + "</svg>", encoding="utf-8")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--json", default=None)
    ap.add_argument("--chart", default=None)
    ap.add_argument("--substrate", choices=("okx", "binance"), default="okx",
                    help="okx = the pre-registered OKX spot cache (window from "
                         "2021-05-30); binance = the I3 robustness substrate, "
                         "Binance spot, which reaches March 2020. The RULE is "
                         "identical either way.")
    ap.add_argument("--window-start", default=None,
                    help="override the first scored date (default: the "
                         "substrate's first date with a valid 200d signal)")
    args = ap.parse_args(argv[1:])

    print("NOTE: A.6 fallback deliverable. NOT an alpha claim; writes NO "
          "trials.log row; scored outside the verdict tree.")
    if args.substrate == "binance":
        px = load_prices_binance()
        default_start = str((px.index[0] + pd.Timedelta(days=MA_WINDOW)).date())
    else:
        px = load_prices()
        default_start = WINDOW_START
    start = args.window_start or default_start
    print(f"substrate: {args.substrate}; scoring from {start}")
    weights = build_weights(px)

    live = px.index >= pd.Timestamp(start, tz="UTC")
    px_w, w_w = px[live], weights[live]
    if len(px_w) < 400:
        raise SystemExit(f"only {len(px_w)} bars in the scoring window")

    eq_ov, net_ov, cost_ov = equity_curve(px_w, w_w)
    eq_bh, net_bh, cost_bh = buy_and_hold(px_w)
    ov = metrics(eq_ov, net_ov, w_w, cost_ov)
    bh = metrics(eq_bh, net_bh, None, cost_bh)
    verdict = assess(ov, bh)

    print(f"\nwindow: {px_w.index[0].date()} → {px_w.index[-1].date()}  "
          f"({ov['years']:.2f} years, {len(px_w)} bars)")
    print(f"assets: {', '.join(ASSETS)} equal weight\n")
    print(render(ov, bh))
    print(f"\ndrawdown reduction vs buy & hold: "
          f"{verdict['drawdown_reduction'] * 100:.1f} % (relative)")

    diag = diagnostics(eq_ov, w_w)
    print("\nrobustness diagnostics:")
    print(f"  deepest drawdown occurred    : {diag['max_dd_date']}")
    print(f"  round trips (flat<->long)    : {diag['round_trips']}")
    print(f"  whipsaws (<=30d round trip)  : {diag['whipsaws_total']}")
    print(f"  drawdown episodes over 5 %   : "
          f"{diag['drawdown_episodes_over_5pct']}  {diag['episode_depths']}")
    print("\npre-committed failure checks:")
    for name, (ok, detail) in verdict["checks"].items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    print(f"\noverall: {'PASSES' if verdict['passes'] else 'FAILS'} "
          "its own pre-committed conditions")

    if args.json:
        Path(args.json).write_text(json.dumps(
            {"window": [str(px_w.index[0].date()), str(px_w.index[-1].date())],
             "overlay": ov, "buy_and_hold": bh,
             "drawdown_reduction": verdict["drawdown_reduction"],
             "diagnostics": diag,
             "checks": {k: v[0] for k, v in verdict["checks"].items()},
             "passes": verdict["passes"]}, indent=2), encoding="utf-8")
        print(f"\n[json] {args.json}")
    if args.chart:
        svg_chart(eq_ov, eq_bh, Path(args.chart))
        print(f"[chart] {args.chart}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
