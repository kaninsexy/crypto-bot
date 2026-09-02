#!/usr/bin/env python3
"""
scripts/discovery_funding_dispersion.py — DISCOVERY SCREEN, NOT A TRIAL.

Family 1 of the §C.4 first batch in `docs/research_revival_2026-09.md`:
**funding-dispersion carry**.  Mechanism: leveraged longs in small/mid-cap
perps pay funding, and desks can't scale into them.

This is a **discovery screen** under the discovery / confirmation split
of `docs/research_revival_2026-09.md` §C.2, NOT a counted trial:

  * It writes **no** `backtest/trials.log` row.  It does not import
    `backtest.trials` and never calls `record_trial`.  The ledger row in
    `research/discovery/funding_dispersion_carry.md` IS its record.
  * It reads **only** the sealed discovery window
    2020-01-01 → 2022-12-31.  Every timestamp it touches is
    hard-asserted `< 2023-01-01T00:00:00Z`; the run aborts otherwise.
  * Its ledger-row count `N_disc` is carried into the confirmation
    trial's pre-registration as an additional Bonferroni-style haircut
    on the confirmation DSR.

Pre-registered kill test (verbatim, §C.4 row 1)
───────────────────────────────────────────────
  Daily decile sort on trailing 3×8h funding, top-150 by dollar volume:
  is (funding accrued − next-day price spread) > 0 net of 0.05 %×2 per
  rebalance, 2020–22?  Threshold: net ≥ 0.15 %/day on the 10-1 spread

Statistic
─────────
Book convention is the unit-long / unit-short 10-1 spread: SHORT the top
funding decile (it receives the funding its longs pay) and LONG the
bottom decile (it pays that decile's funding).  Per rebalance day t, on
the next day's realisations:

    net_t = (f_top − f_bot) − (r_top − r_bot) − 2 × 0.05 %

`f` is the day's summed 8h funding, `r` the close-to-close return, both
equal-weighted within the decile.  The reported statistic is
`mean(net_t)` in %/day with its t-statistic and N = number of rebalance
days.  Threshold: ≥ 0.15 %/day.

Usage
─────
    python3 scripts/discovery_funding_dispersion.py --selftest
    python3 scripts/discovery_funding_dispersion.py            # real data
    python3 scripts/discovery_funding_dispersion.py --append-ledger

**Status 2026-09-02 (updated later the same day):** the discovery /
confirmation split is APPROVED and IN FORCE. The rule text landed in
`.claude/rules/backtest.md` § "Discovery / confirmation split" at commit
`6a564ec`, under the human pre-authorization in that day's megaloop
prompt. Real-data screens and ledger rows are authorised; `--selftest`
is no longer the only permitted mode. The paragraph this replaces said
the opposite and was written hours before the rule landed.
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

FAMILY = "funding_dispersion_carry"
LEDGER = _REPO_ROOT / "research" / "discovery" / f"{FAMILY}.md"

#: Discovery never reads 2023+ data (§C.2 item 1c).  Strict upper bound.
DISCOVERY_END = pd.Timestamp("2023-01-01T00:00:00Z")
DISCOVERY_START = pd.Timestamp("2020-01-01T00:00:00Z")

TOP_N_BY_DOLLAR_VOLUME = 150
N_DECILES = 10
DOLLAR_VOLUME_LOOKBACK_DAYS = 30
COST_PER_REBALANCE = 2 * 0.0005      # 0.05 % × 2, per §C.4
THRESHOLD_PCT_PER_DAY = 0.15
# Harvey-Liu: t > 3 for a multiply-tested claim. Cited as the basis of the
# discovery/confirmation split in `.claude/rules/backtest.md`, asserted by this
# script's own --selftest, and enforced by the sibling listing-flow screen.
# It reaches `verdict` as of 2026-09-02 (see the note there).
T_THRESHOLD = 3.0


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

def _decile_labels(values: pd.Series, n_deciles: int) -> pd.Series:
    """Tie-safe decile index 0..n_deciles-1 (0 = lowest)."""
    k = len(values)
    ranks = values.rank(method="first").to_numpy() - 1.0
    return pd.Series(
        np.floor(ranks * n_deciles / k).astype(int).clip(0, n_deciles - 1),
        index=values.index,
    )


def screen_funding_dispersion(
    close: pd.DataFrame,
    funding_daily: pd.DataFrame,
    dollar_volume: pd.DataFrame,
    *,
    top_n: int = TOP_N_BY_DOLLAR_VOLUME,
    n_deciles: int = N_DECILES,
    cost_per_rebalance: float = COST_PER_REBALANCE,
    dv_lookback: int = DOLLAR_VOLUME_LOOKBACK_DAYS,
) -> dict:
    """Compute the §C.4 row-1 kill-test statistic.

    Args:
      close:         Wide daily close panel `[date × symbol]`.
      funding_daily: Wide daily SUMMED funding rate `[date × symbol]`
                     (the trailing 3×8h settlements of that UTC day).
      dollar_volume: Wide daily quote volume `[date × symbol]`; the
                     universe rule uses its trailing `dv_lookback` mean.
      top_n:         Universe size by trailing dollar volume.
      n_deciles:     Sort granularity (10 → deciles).
      cost_per_rebalance: Round-trip cost charged to the spread daily.
      dv_lookback:   Trailing window for the dollar-volume screen.

    Returns:
      dict with `net_series` (daily net spread, decimal), `mean_pct`,
      `t_stat`, `n_days`, `mean_gross_pct` (before costs),
      `mean_funding_pct`, `mean_price_pct`, `start`, `end`.
    """
    close = close.sort_index()
    funding_daily = funding_daily.reindex(
        index=close.index, columns=close.columns).fillna(0.0)
    dollar_volume = dollar_volume.reindex(
        index=close.index, columns=close.columns)

    rets = close.pct_change().shift(-1)          # next-day return
    fwd_funding = funding_daily.shift(-1)        # next-day funding
    dv_trail = dollar_volume.rolling(
        dv_lookback, min_periods=max(2, dv_lookback // 3)).mean()

    rows = []
    for t in close.index:
        sig = funding_daily.loc[t]
        r_next = rets.loc[t]
        f_next = fwd_funding.loc[t]
        dv = dv_trail.loc[t]
        ok = (
            sig.notna() & r_next.notna() & f_next.notna()
            & dv.notna() & (dv > 0) & close.loc[t].notna()
        )
        elig = sig.index[ok.to_numpy()]
        if len(elig) < n_deciles * 2:
            continue
        universe = dv.loc[elig].nlargest(min(top_n, len(elig))).index
        if len(universe) < n_deciles * 2:
            continue
        dec = _decile_labels(sig.loc[universe], n_deciles)
        top = dec.index[dec == n_deciles - 1]
        bot = dec.index[dec == 0]
        if len(top) == 0 or len(bot) == 0:
            continue
        f_spread = float(f_next[top].mean() - f_next[bot].mean())
        r_spread = float(r_next[top].mean() - r_next[bot].mean())
        rows.append((t, f_spread, r_spread,
                     f_spread - r_spread - cost_per_rebalance))

    if len(rows) == 0:
        return {
            "net_series": pd.Series(dtype=float), "mean_pct": float("nan"),
            "t_stat": float("nan"), "n_days": 0, "mean_gross_pct": float("nan"),
            "mean_funding_pct": float("nan"), "mean_price_pct": float("nan"),
            "start": None, "end": None,
        }

    idx = pd.DatetimeIndex([r[0] for r in rows])
    f_ser = pd.Series([r[1] for r in rows], index=idx)
    r_ser = pd.Series([r[2] for r in rows], index=idx)
    net = pd.Series([r[3] for r in rows], index=idx)
    n = len(net)
    sd = float(net.std(ddof=1)) if n > 1 else float("nan")
    t_stat = (
        float(net.mean()) / (sd / math.sqrt(n))
        if n > 1 and sd > 0 else float("nan")
    )
    return {
        "net_series": net,
        "mean_pct": float(net.mean()) * 100.0,
        "t_stat": t_stat,
        "n_days": n,
        "mean_gross_pct": float((f_ser - r_ser).mean()) * 100.0,
        "mean_funding_pct": float(f_ser.mean()) * 100.0,
        "mean_price_pct": float(r_ser.mean()) * 100.0,
        "start": idx.min(),
        "end": idx.max(),
    }


# ── Real-data loading (Binance UM archive; discovery window only) ────────────

def load_discovery_panels(cache_dir=None, max_symbols=None) -> dict:
    """Build the daily close / funding / dollar-volume panels from the
    `backtest/cache/binance_um/` parquet cache.

    Everything is capped at `until=DISCOVERY_END`, which
    `data.binance_vision_um` applies as a STRICT `<` filter.
    """
    from data import binance_vision_um as um

    kwargs = {} if cache_dir is None else {"cache_dir": cache_dir}
    universe = um.universe_table(**kwargs)
    if len(universe) == 0:
        raise SystemExit(
            "universe.parquet is empty — run scripts/prefetch_binance_um.py "
            "first (this screen does no network fetching of its own)."
        )
    listed = universe[
        universe["first_month"] < "2023-01"
    ]["symbol"].tolist()
    if max_symbols is not None:
        listed = listed[:max_symbols]

    closes, fundings, volumes = {}, {}, {}
    for sym in listed:
        try:
            k = um.fetch_klines(
                sym, "1d", "2020-01", "2022-12", until=DISCOVERY_END, **kwargs)
            f = um.fetch_funding(
                sym, "2020-01", "2022-12", until=DISCOVERY_END, **kwargs)
        except Exception as exc:                      # noqa: BLE001
            print(f"  skip {sym}: {exc.__class__.__name__}: {exc}")
            continue
        if len(k) == 0:
            continue
        assert_discovery_window(f"klines[{sym}]", k.index)
        closes[sym] = k["close"]
        volumes[sym] = k["quote_volume"]
        if len(f) > 0:
            assert_discovery_window(f"funding[{sym}]", f.index)
            # Millisecond jitter on settlement stamps → floor to the hour
            # before bucketing into UTC days.
            fr = f["last_funding_rate"].copy()
            fr.index = pd.DatetimeIndex(fr.index).floor("h")
            fundings[sym] = fr.groupby(fr.index.floor("D")).sum()

    if len(closes) == 0:
        raise SystemExit("no cached symbols inside the discovery window")

    close = pd.DataFrame(closes).sort_index()
    volume = pd.DataFrame(volumes).reindex(
        index=close.index, columns=close.columns)
    funding = (
        pd.DataFrame(fundings).reindex(
            index=close.index, columns=close.columns).fillna(0.0)
        if fundings else
        pd.DataFrame(0.0, index=close.index, columns=close.columns)
    )
    for name, frame in (("close", close), ("funding", funding),
                        ("volume", volume)):
        assert_discovery_window(f"panel[{name}]", frame.index)
    return {"close": close, "funding_daily": funding, "dollar_volume": volume}


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
    """Append one row to `research/discovery/funding_dispersion_carry.md`.

    Append-only: never edits or deletes an existing row (README rule 4).
    """
    if not LEDGER.exists():
        raise SystemExit(f"ledger missing: {LEDGER}")
    start = result["start"]
    end = result["end"]
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
        f"trailing 3x8h funding sum, decile {N_DECILES} minus decile 1 "
        "(short top / long bottom)",
        f"top-{TOP_N_BY_DOLLAR_VOLUME} by trailing "
        f"{DOLLAR_VOLUME_LOOKBACK_DAYS}d quote volume",
        "next 1 day",
        "mean daily net 10-1 spread, %/day (net of 0.05%x2)",
        f"{result['mean_pct']:.4f} %/day",
        f"{result['t_stat']:.2f}",
        str(result["n_days"]),
        rng,
        f"scripts/discovery_funding_dispersion.py @ {_git_commit()}",
        conclusion,
        "",
    ]).strip()
    with LEDGER.open("a", encoding="utf-8") as fh:
        fh.write(row + "\n")
    print(f"[ledger] appended → {LEDGER}")


def verdict(result: dict) -> str:
    """Both pre-registered conditions, or it does not survive.

    BUG FIX 2026-09-02, disclosed because the timing matters: this function
    previously checked ONLY the %/day threshold and ignored the t-stat. The
    first real run returned 0.2748 %/day (clears 0.15) at t = 2.939 (misses
    t > 3) and was reported as "survives". It is not.

    Both conditions were pre-registered, not invented after the fact:
      * `.claude/rules/backtest.md` § "Discovery / confirmation split" cites
        Harvey-Liu, "t > 3 for multiply-tested claims", as the basis of the
        whole split;
      * this script's own `--selftest` already asserts `t_stat > 3.0` on its
        synthetic positive case, so the author knew the bar — it just never
        reached `verdict`;
      * the sibling screen `scripts/discovery_listing_flow.py` enforces
        `|t| > T_THRESHOLD` alongside its effect-size bar, so the two family
        screens were applying different rules to the same batch.

    The fix is made AFTER seeing 2.939, which is exactly the situation the
    no-p-hacking rule polices — so note the direction: it makes the test
    STRICTER, and it makes a bar that was already written down actually bind.
    Moving a threshold to admit a result is p-hacking; repairing a check that
    failed to apply a pre-registered threshold is the opposite. The superseded
    ledger row is retained per README rule 4, with a correction row appended.
    """
    if result["n_days"] == 0 or not math.isfinite(result["mean_pct"]):
        return "inconclusive — no usable rebalance days"
    clears_effect = result["mean_pct"] >= THRESHOLD_PCT_PER_DAY
    t_stat = result.get("t_stat", float("nan"))
    clears_t = math.isfinite(t_stat) and abs(t_stat) > T_THRESHOLD
    if clears_effect and clears_t:
        return (
            f"survives — net {result['mean_pct']:.4f} %/day >= "
            f"{THRESHOLD_PCT_PER_DAY} %/day at |t|={abs(t_stat):.2f} > "
            f"{T_THRESHOLD}"
        )
    misses = []
    if not clears_effect:
        misses.append(
            f"net {result['mean_pct']:.4f} %/day < {THRESHOLD_PCT_PER_DAY} %/day")
    if not clears_t:
        misses.append(f"|t|={abs(t_stat):.2f} <= {T_THRESHOLD}")
    return "killed — " + "; ".join(misses)


def report(result: dict) -> None:
    print("── funding-dispersion carry: §C.4 row-1 kill test ──")
    print(f"  statistic     : mean daily net 10-1 spread (%/day)")
    print(f"  value         : {result['mean_pct']:.4f} %/day")
    print(f"  t-stat        : {result['t_stat']:.3f}")
    print(f"  N (days)      : {result['n_days']}")
    print(f"  gross (pre-cost): {result['mean_gross_pct']:.4f} %/day")
    print(f"    funding leg : {result['mean_funding_pct']:.4f} %/day")
    print(f"    price leg   : {result['mean_price_pct']:.4f} %/day")
    if result["start"] is not None:
        print(
            f"  data range    : {pd.Timestamp(result['start']).date()} → "
            f"{pd.Timestamp(result['end']).date()}"
        )
    print(f"  threshold     : >= {THRESHOLD_PCT_PER_DAY} %/day")
    print(f"  conclusion    : {verdict(result)}")


# ── Selftest (synthetic; no cache, no network) ───────────────────────────────

def selftest() -> int:
    """Exercise the statistic machinery on a planted synthetic panel."""
    n_days, n_syms = 200, 20
    idx = pd.date_range("2020-01-01", periods=n_days, freq="1D", tz="UTC")
    syms = [f"S{i:02d}USDT" for i in range(n_syms)]
    rng = np.random.default_rng(20260902)

    # Funding is a constant per-symbol ladder: 0.0002 × i per day.
    funding = pd.DataFrame(
        np.tile(np.arange(n_syms) * 0.0002, (n_days, 1)),
        index=idx, columns=syms,
    )
    # Prices are pure noise → the price leg contributes ~0 in expectation.
    rets = rng.normal(0.0, 0.001, size=(n_days, n_syms))
    close = pd.DataFrame(
        100.0 * np.cumprod(1.0 + rets, axis=0), index=idx, columns=syms)
    volume = pd.DataFrame(1e9, index=idx, columns=syms)

    res = screen_funding_dispersion(close, funding, volume, dv_lookback=5)

    # 20 symbols / 10 deciles → 2 per decile.
    # top decile funding mean = (0.0002*18 + 0.0002*19)/2 = 0.0037
    # bottom decile           = (0.0002*0  + 0.0002*1 )/2 = 0.0001
    # gross spread 0.0036, minus 0.001 cost → 0.0026 = 0.26 %/day
    expected = (0.0037 - 0.0001 - COST_PER_REBALANCE) * 100.0
    assert res["n_days"] > 150, res["n_days"]
    assert abs(res["mean_pct"] - expected) < 0.05, (
        f"mean {res['mean_pct']} vs expected {expected}")
    assert res["t_stat"] > 3.0, res["t_stat"]
    assert res["mean_gross_pct"] > res["mean_pct"], "cost must reduce net"

    # Decile labelling is tie-safe and covers every bucket.
    lab = _decile_labels(pd.Series([1.0] * 20, index=syms), 10)
    assert sorted(lab.unique().tolist()) == list(range(10))

    # Guard fires on a 2023 timestamp.
    try:
        assert_discovery_window(
            "selftest", pd.DatetimeIndex(["2023-01-01T00:00:00Z"]))
    except SystemExit:
        pass
    else:                                              # pragma: no cover
        raise AssertionError("discovery-window guard failed to fire")

    # A zero-dispersion panel must not manufacture a surviving spread.
    flat = pd.DataFrame(0.0, index=idx, columns=syms)
    res_flat = screen_funding_dispersion(close, flat, volume, dv_lookback=5)
    assert res_flat["mean_pct"] < 0, res_flat["mean_pct"]
    assert "killed" in verdict(res_flat)

    print("selftest OK — funding-dispersion statistic machinery works")
    print(
        f"  planted spread recovered: {res['mean_pct']:.4f} %/day "
        f"(expected {expected:.4f}), t={res['t_stat']:.2f}, "
        f"N={res['n_days']}"
    )
    return 0


# ── CLI ──────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--selftest", action="store_true",
                    help="run on a synthetic panel; no cache needed")
    ap.add_argument("--append-ledger", action="store_true",
                    help="append the result row to the discovery ledger")
    ap.add_argument("--max-symbols", type=int, default=None,
                    help="cap the universe (debugging)")
    ap.add_argument("--cache-dir", default=None,
                    help="override backtest/cache/binance_um")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    print(
        "NOTE: discovery screen, NOT a trial — no trials.log row is "
        "written (docs/research_revival_2026-09.md §C.2)."
    )
    panels = load_discovery_panels(
        cache_dir=args.cache_dir, max_symbols=args.max_symbols)
    result = screen_funding_dispersion(**panels)
    report(result)
    if args.append_ledger:
        append_ledger_row(result, verdict(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
