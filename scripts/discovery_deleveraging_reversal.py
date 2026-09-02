#!/usr/bin/env python3
"""
scripts/discovery_deleveraging_reversal.py — DISCOVERY SCREEN, NOT A TRIAL.

Family 2 of the §C.4 first batch in `docs/research_revival_2026-09.md`:
**deleveraging reversal**.  Mechanism: forced liquidations sell at any
price; the counterparty is the liquidated long (or short).

This is a **discovery screen** under the discovery / confirmation split
of `docs/research_revival_2026-09.md` §C.2, NOT a counted trial:

  * It writes **no** `backtest/trials.log` row.  It does not import
    `backtest.trials` and never calls `record_trial`.  The ledger row in
    `research/discovery/deleveraging_reversal.md` IS its record.
  * It reads **only** the sealed discovery window
    2020-01-01 → 2022-12-31.  Every timestamp it touches is
    hard-asserted `< 2023-01-01T00:00:00Z`; the run aborts otherwise.
  * Its ledger-row count `N_disc` is carried into the confirmation
    trial's pre-registration as an additional Bonferroni-style haircut
    on the confirmation DSR.

Pre-registered kill test (verbatim, §C.4 row 2)
───────────────────────────────────────────────
  Event = 24h OI drop ≥ 20 % with price move ≥ 2σ; measure 1–5-day
  forward return vs unconditional, 2020–22, ≥ 100 events across the
  universe.  Threshold: mean 3-day reversal ≥ 1.5 % with t > 3

Statistic
─────────
For an event on symbol `s` at day `t`, the h-day **reversal** is

    rev_h = −sign(r_t) × (close[t+h] / close[t] − 1)

i.e. positive when price moves back against the event-day move.  The
pre-registered headline is `mean(rev_3)` in %, with its t-statistic
against zero, and N = event count (≥ 100 required).  The unconditional
mean `rev_3` over all non-event symbol-days is reported alongside, with
a Welch t for the difference, per "vs unconditional" in the kill test.

Open interest comes from the daily `metrics` archive (5-minute rows,
resampled to daily last).  σ is the trailing 30-day standard deviation
of daily returns ending the day BEFORE the event, so the screen is
computable ex ante.

Usage
─────
    python3 scripts/discovery_deleveraging_reversal.py --selftest
    python3 scripts/discovery_deleveraging_reversal.py            # real data
    python3 scripts/discovery_deleveraging_reversal.py --append-ledger

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

FAMILY = "deleveraging_reversal"
LEDGER = _REPO_ROOT / "research" / "discovery" / f"{FAMILY}.md"

DISCOVERY_END = pd.Timestamp("2023-01-01T00:00:00Z")
DISCOVERY_START = pd.Timestamp("2020-01-01T00:00:00Z")

OI_DROP_THRESHOLD = -0.20        # 24h OI drop ≥ 20 %
SIGMA_MULTIPLE = 2.0             # price move ≥ 2σ
SIGMA_LOOKBACK_DAYS = 30
HORIZONS = (1, 2, 3, 4, 5)
HEADLINE_HORIZON = 3
THRESHOLD_REVERSAL_PCT = 1.5
THRESHOLD_T = 3.0
MIN_EVENTS = 100


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

def _t_stat(sample: np.ndarray) -> float:
    arr = np.asarray(sample, dtype=float)
    arr = arr[np.isfinite(arr)]
    n = arr.size
    if n < 2:
        return float("nan")
    sd = float(arr.std(ddof=1))
    if sd <= 0:
        return float("nan")
    return float(arr.mean()) / (sd / math.sqrt(n))


def _welch_t(a: np.ndarray, b: np.ndarray) -> float:
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    a = a[np.isfinite(a)]
    b = b[np.isfinite(b)]
    if a.size < 2 or b.size < 2:
        return float("nan")
    va, vb = a.var(ddof=1) / a.size, b.var(ddof=1) / b.size
    denom = math.sqrt(va + vb)
    if denom <= 0:
        return float("nan")
    return float(a.mean() - b.mean()) / denom


def screen_deleveraging_reversal(
    close: pd.DataFrame,
    open_interest: pd.DataFrame,
    *,
    oi_drop_threshold: float = OI_DROP_THRESHOLD,
    sigma_multiple: float = SIGMA_MULTIPLE,
    sigma_lookback: int = SIGMA_LOOKBACK_DAYS,
    horizons=HORIZONS,
    headline_horizon: int = HEADLINE_HORIZON,
) -> dict:
    """Compute the §C.4 row-2 kill-test statistic.

    Args:
      close:          Wide daily close panel `[date × symbol]`.
      open_interest:  Wide daily open-interest panel, same shape.
      oi_drop_threshold: 24h OI change at or below this is an event leg
                      (−0.20 = a 20 % drop).
      sigma_multiple: |r_t| ≥ this × trailing σ is the other event leg.
      sigma_lookback: Trailing window for σ, ending at t−1 (ex ante).
      horizons:       Forward horizons measured, in days.
      headline_horizon: The pre-registered horizon (3 days).

    Returns:
      dict with `events` (DataFrame), per-horizon means/t-stats,
      `mean_pct`, `t_stat`, `n_events`, `uncond_mean_pct`,
      `diff_t`, `start`, `end`.
    """
    close = close.sort_index()
    open_interest = open_interest.reindex(
        index=close.index, columns=close.columns)

    rets = close.pct_change()
    sigma = rets.rolling(
        sigma_lookback, min_periods=max(5, sigma_lookback // 3)
    ).std().shift(1)
    oi_chg = open_interest.pct_change()

    is_event = (
        (oi_chg <= oi_drop_threshold)
        & (rets.abs() >= sigma_multiple * sigma)
        & rets.notna() & sigma.notna() & oi_chg.notna()
    )

    fwd = {
        h: (close.shift(-h) / close - 1.0) for h in horizons
    }
    sign = np.sign(rets)

    ev_rows = []
    uncond = {h: [] for h in horizons}
    ev_idx = np.flatnonzero(is_event.to_numpy().ravel())
    cols = list(close.columns)
    n_cols = len(cols)
    ev_pairs = {(i // n_cols, i % n_cols) for i in ev_idx}

    ret_v = rets.to_numpy()
    sign_v = sign.to_numpy()
    fwd_v = {h: fwd[h].to_numpy() for h in horizons}
    n_rows = close.shape[0]

    for i in range(n_rows):
        for j in range(n_cols):
            s = sign_v[i, j]
            if not np.isfinite(s) or s == 0:
                continue
            revs = {}
            for h in horizons:
                f = fwd_v[h][i, j]
                revs[h] = -s * f if np.isfinite(f) else np.nan
            if (i, j) in ev_pairs:
                if not np.isfinite(revs[headline_horizon]):
                    continue
                ev_rows.append({
                    "timestamp": close.index[i],
                    "symbol": cols[j],
                    "event_return": float(ret_v[i, j]),
                    **{f"rev_{h}": revs[h] for h in horizons},
                })
            else:
                for h in horizons:
                    if np.isfinite(revs[h]):
                        uncond[h].append(revs[h])

    events = pd.DataFrame(ev_rows)
    per_horizon = {}
    for h in horizons:
        vals = (
            events[f"rev_{h}"].to_numpy(dtype=float)
            if len(events) else np.array([], dtype=float)
        )
        vals = vals[np.isfinite(vals)]
        per_horizon[h] = {
            "mean_pct": float(vals.mean()) * 100.0 if vals.size else float("nan"),
            "t_stat": _t_stat(vals),
            "n": int(vals.size),
            "uncond_mean_pct": (
                float(np.mean(uncond[h])) * 100.0 if uncond[h] else float("nan")
            ),
            "diff_t": _welch_t(vals, np.asarray(uncond[h], dtype=float)),
        }

    head = per_horizon[headline_horizon]
    return {
        "events": events,
        "per_horizon": per_horizon,
        "mean_pct": head["mean_pct"],
        "t_stat": head["t_stat"],
        "n_events": head["n"],
        "uncond_mean_pct": head["uncond_mean_pct"],
        "diff_t": head["diff_t"],
        "start": close.index.min() if len(close) else None,
        "end": close.index.max() if len(close) else None,
    }


# ── Real-data loading (Binance UM archive; discovery window only) ────────────

def load_discovery_panels(cache_dir=None, max_symbols=None) -> dict:
    """Build daily close + open-interest panels from
    `backtest/cache/binance_um/`, capped strictly before 2023-01-01."""
    from data import binance_vision_um as um

    kwargs = {} if cache_dir is None else {"cache_dir": cache_dir}
    universe = um.universe_table(**kwargs)
    if len(universe) == 0:
        raise SystemExit(
            "universe.parquet is empty — run scripts/prefetch_binance_um.py "
            "first (this screen does no network fetching of its own)."
        )
    listed = universe[universe["first_month"] < "2023-01"]["symbol"].tolist()
    if max_symbols is not None:
        listed = listed[:max_symbols]

    closes, ois = {}, {}
    for sym in listed:
        try:
            k = um.fetch_klines(
                sym, "1d", "2020-01", "2022-12", until=DISCOVERY_END, **kwargs)
            m = um.fetch_metrics(
                sym, "2020-01-01", "2022-12-31",
                until=DISCOVERY_END, **kwargs)
        except Exception as exc:                       # noqa: BLE001
            print(f"  skip {sym}: {exc.__class__.__name__}: {exc}")
            continue
        if len(k) == 0 or len(m) == 0:
            continue
        assert_discovery_window(f"klines[{sym}]", k.index)
        assert_discovery_window(f"metrics[{sym}]", m.index)
        closes[sym] = k["close"]
        daily = um.resample_metrics(m, "1D")
        if "sum_open_interest" in daily.columns and len(daily) > 0:
            ois[sym] = daily["sum_open_interest"]

    if len(closes) == 0 or len(ois) == 0:
        raise SystemExit(
            "no cached symbols with BOTH klines and metrics inside the "
            "discovery window"
        )
    close = pd.DataFrame(closes).sort_index()
    oi = pd.DataFrame(ois).reindex(index=close.index, columns=close.columns)
    assert_discovery_window("panel[close]", close.index)
    assert_discovery_window("panel[open_interest]", oi.index)
    return {"close": close, "open_interest": oi}


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
    """Append one row to `research/discovery/deleveraging_reversal.md`."""
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
    row = " | ".join([
        "",
        date.today().isoformat(),
        FAMILY,
        f"24h OI drop <= {OI_DROP_THRESHOLD:.0%} AND |r| >= "
        f"{SIGMA_MULTIPLE:g}x trailing {SIGMA_LOOKBACK_DAYS}d sigma",
        "all UM symbols with klines AND metrics in window",
        f"+{HEADLINE_HORIZON} days",
        "mean 3-day reversal, % (sign-adjusted against the event move)",
        f"{result['mean_pct']:.4f} %",
        f"{result['t_stat']:.2f}",
        str(result["n_events"]),
        rng,
        f"scripts/discovery_deleveraging_reversal.py @ {_git_commit()}",
        conclusion,
        "",
    ]).strip()
    with LEDGER.open("a", encoding="utf-8") as fh:
        fh.write(row + "\n")
    print(f"[ledger] appended → {LEDGER}")


def verdict(result: dict) -> str:
    if result["n_events"] < MIN_EVENTS:
        return (
            f"inconclusive — {result['n_events']} events < {MIN_EVENTS} "
            "required by the kill test"
        )
    ok = (
        math.isfinite(result["mean_pct"])
        and result["mean_pct"] >= THRESHOLD_REVERSAL_PCT
        and math.isfinite(result["t_stat"])
        and result["t_stat"] > THRESHOLD_T
    )
    if ok:
        return (
            f"survives — mean 3-day reversal {result['mean_pct']:.3f} % "
            f">= {THRESHOLD_REVERSAL_PCT} % with t={result['t_stat']:.2f} > "
            f"{THRESHOLD_T}"
        )
    return (
        f"killed — mean 3-day reversal {result['mean_pct']:.3f} % / "
        f"t={result['t_stat']:.2f} misses the "
        f"{THRESHOLD_REVERSAL_PCT} % & t>{THRESHOLD_T} bar"
    )


def report(result: dict) -> None:
    print("── deleveraging reversal: §C.4 row-2 kill test ──")
    print("  statistic     : mean 3-day reversal, % (t vs 0)")
    print(f"  value         : {result['mean_pct']:.4f} %")
    print(f"  t-stat        : {result['t_stat']:.3f}")
    print(f"  N (events)    : {result['n_events']}")
    print(f"  unconditional : {result['uncond_mean_pct']:.4f} % "
          f"(Welch t of the difference: {result['diff_t']:.3f})")
    for h, s in result["per_horizon"].items():
        print(
            f"    h={h}d  mean={s['mean_pct']:+.4f} %  t={s['t_stat']:.2f}  "
            f"N={s['n']}  uncond={s['uncond_mean_pct']:+.4f} %"
        )
    if result["start"] is not None:
        print(
            f"  data range    : {pd.Timestamp(result['start']).date()} → "
            f"{pd.Timestamp(result['end']).date()}"
        )
    print(
        f"  threshold     : mean >= {THRESHOLD_REVERSAL_PCT} % with "
        f"t > {THRESHOLD_T}, N >= {MIN_EVENTS}"
    )
    print(f"  conclusion    : {verdict(result)}")


# ── Selftest (synthetic; no cache, no network) ───────────────────────────────

def selftest() -> int:
    """Plant known deleveraging events and check the machinery finds them."""
    n_days, n_syms = 400, 6
    idx = pd.date_range("2020-01-01", periods=n_days, freq="1D", tz="UTC")
    syms = [f"S{i}USDT" for i in range(n_syms)]
    rng = np.random.default_rng(4242)

    rets = rng.normal(0.0, 0.01, size=(n_days, n_syms))
    oi = np.full((n_days, n_syms), 1.0e6)

    # Plant events: a −6 % day (≫ 2σ ≈ 2 %) with a −30 % OI drop,
    # followed by +1 % per day (plus noise) for 3 days — a 3.03 %
    # three-day reversal in expectation, with finite dispersion so the
    # t-statistic is a real number rather than a divide-by-zero.
    planted = []
    for j in range(n_syms):
        for t in range(60, 360, 12):
            rets[t, j] = -0.06
            oi[t, j] = oi[t - 1, j] * 0.70
            for k in (1, 2, 3):
                rets[t + k, j] = 0.01 + rng.normal(0.0, 0.01)
            planted.append((t, j))

    close = pd.DataFrame(
        100.0 * np.cumprod(1.0 + rets, axis=0), index=idx, columns=syms)
    oi_df = pd.DataFrame(oi, index=idx, columns=syms)

    res = screen_deleveraging_reversal(close, oi_df)

    assert res["n_events"] >= MIN_EVENTS, res["n_events"]
    assert res["n_events"] == len(planted), (res["n_events"], len(planted))
    # (1.01)^3 − 1 = 3.0301 % in expectation
    assert abs(res["mean_pct"] - 3.0301) < 0.6, res["mean_pct"]
    assert res["t_stat"] < 1.0e3, "t-statistic must be finite and sane"
    assert res["t_stat"] > THRESHOLD_T, res["t_stat"]
    assert "survives" in verdict(res)
    # 1-day and 5-day horizons are populated too.
    assert res["per_horizon"][1]["n"] == res["n_events"]
    assert res["per_horizon"][5]["n"] == res["n_events"]
    assert math.isfinite(res["uncond_mean_pct"])

    # No OI drop → no events at all, and the kill test says inconclusive.
    flat_oi = pd.DataFrame(1.0e6, index=idx, columns=syms)
    res_none = screen_deleveraging_reversal(close, flat_oi)
    assert res_none["n_events"] == 0, res_none["n_events"]
    assert "inconclusive" in verdict(res_none)

    try:
        assert_discovery_window(
            "selftest", pd.DatetimeIndex(["2023-06-01T00:00:00Z"]))
    except SystemExit:
        pass
    else:                                              # pragma: no cover
        raise AssertionError("discovery-window guard failed to fire")

    print("selftest OK — deleveraging-reversal statistic machinery works")
    print(
        f"  planted reversal recovered: {res['mean_pct']:.4f} % "
        f"(expected 3.0301), t={res['t_stat']:.2f}, N={res['n_events']}"
    )
    return 0


# ── CLI ──────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--selftest", action="store_true",
                    help="run on a synthetic panel; no cache needed")
    ap.add_argument("--append-ledger", action="store_true",
                    help="append the result row to the discovery ledger")
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
    result = screen_deleveraging_reversal(**panels)
    report(result)
    if args.append_ledger:
        append_ledger_row(result, verdict(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
