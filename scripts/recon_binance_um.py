#!/usr/bin/env python3
"""Descriptive data-recon report for the Binance USDT-M perp archive cache.

STRICTLY DESCRIPTIVE. This script must never compute:
  - forward return conditional on any signal
  - any long/short spread
  - any Sharpe ratio or other strategy-performance statistic

It only characterises the *shape* of the data available in the
pre-2023 discovery window: coverage, gaps, funding cross-section,
open-interest event density, listing/delisting event density. All
computations are hard-capped at ts < 2023-01-01 (asserted at every
load site).

Usage (stage-by-stage, so each fits inside a short shell timeout;
each stage caches its intermediate result under /tmp/recon/ so a
rerun of a later stage does not require recomputing earlier ones):

    python scripts/recon_binance_um.py --stage integrity
    python scripts/recon_binance_um.py --stage universe
    python scripts/recon_binance_um.py --stage funding
    python scripts/recon_binance_um.py --stage oi
    python scripts/recon_binance_um.py --stage delist
    python scripts/recon_binance_um.py --stage report --out docs/recon_binance_um_2026-09.md

    python scripts/recon_binance_um.py --stage all --out docs/recon_binance_um_2026-09.md
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parent.parent
CACHE_DIR = REPO / "backtest" / "cache" / "binance_um"
TMP_DIR = Path("/tmp/recon")
TMP_DIR.mkdir(parents=True, exist_ok=True)

CUTOFF = pd.Timestamp("2023-01-01", tz="UTC")

METRICS_SYMS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT",
    "DOGEUSDT", "SOLUSDT", "DOTUSDT", "LINKUSDT", "LTCUSDT",
]


# ---------------------------------------------------------------- loaders --

def _assert_cut(df: pd.DataFrame, name: str) -> pd.DataFrame:
    if len(df) and df.index.max() >= CUTOFF:
        raise AssertionError(f"{name}: data >= 2023-01-01 present (max={df.index.max()})")
    return df


def load_universe() -> pd.DataFrame:
    u = pd.read_parquet(CACHE_DIR / "universe.parquet")
    return u


def pre2023_symbols(u: pd.DataFrame) -> list:
    return sorted(u.loc[u["first_month"] < "2023-01", "symbol"].tolist())


def load_klines(sym: str):
    p = CACHE_DIR / "klines" / f"{sym}_1d.parquet"
    if not p.exists():
        return None
    df = pd.read_parquet(p)
    df = df.loc[df.index < CUTOFF]
    if len(df) == 0:
        return None
    return _assert_cut(df, f"klines/{sym}")


def load_funding(sym: str):
    p = CACHE_DIR / "funding" / f"{sym}.parquet"
    if not p.exists():
        return None
    df = pd.read_parquet(p)
    df = df.loc[df.index < CUTOFF]
    if len(df) == 0:
        return None
    return _assert_cut(df, f"funding/{sym}")


def load_metrics(sym: str):
    p = CACHE_DIR / "metrics_5m" / f"{sym}.parquet"
    if not p.exists():
        return None
    df = pd.read_parquet(p)
    df = df.loc[df.index < CUTOFF]
    if len(df) == 0:
        return None
    return _assert_cut(df, f"metrics_5m/{sym}")


def save_json(name: str, obj) -> None:
    (TMP_DIR / name).write_text(json.dumps(obj, indent=2, default=str))


def load_json(name: str):
    p = TMP_DIR / name
    if not p.exists():
        return None
    return json.loads(p.read_text())


def md_table(df: pd.DataFrame, index_name: str = None, floatfmt: str = "{:.4f}") -> str:
    d = df.copy()
    for c in d.columns:
        if pd.api.types.is_float_dtype(d[c]):
            d[c] = d[c].map(lambda x: floatfmt.format(x) if pd.notna(x) else "")
    d = d.reset_index(drop=True)
    return d.to_markdown(index=False)


# ------------------------------------------------ stage: klines panel (shared) --

def build_klines_panel(symbols: list) -> pd.DataFrame:
    """Long panel [date, symbol, open/high/low/close/volume/quote_volume,
    daily_return, trailing30_qv_median, trailing30_ret_std]. Cached."""
    cache = TMP_DIR / "klines_panel.parquet"
    if cache.exists():
        return pd.read_parquet(cache)

    frames = []
    for i, sym in enumerate(symbols):
        k = load_klines(sym)
        if k is None:
            continue
        d = k[["open", "high", "low", "close", "volume", "quote_volume"]].copy()
        d["symbol"] = sym
        d["date"] = d.index.normalize()
        d = d.reset_index(drop=True)
        d["daily_return"] = d["close"].pct_change()
        d["trailing30_qv_median"] = d["quote_volume"].rolling(30, min_periods=30).median()
        d["trailing30_ret_std"] = d["daily_return"].rolling(30, min_periods=30).std()
        frames.append(d)
        if (i + 1) % 50 == 0:
            print(f"  klines panel: {i + 1}/{len(symbols)} symbols", file=sys.stderr)
    panel = pd.concat(frames, ignore_index=True)
    panel.to_parquet(cache)
    print(f"klines panel: {len(panel)} rows, {panel['symbol'].nunique()} symbols -> {cache}", file=sys.stderr)
    return panel


# ------------------------------------------------------------------ stage 1 --

def stage_integrity(symbols: list, funding_symbols: list) -> None:
    print("=== stage: integrity ===", file=sys.stderr)
    kline_syms, funding_syms = [], []
    zero_vol_total = 0
    zero_vol_by_symbol = {}
    gap_records = []
    interval_hours_values = {}
    settlement_dev_rows = []
    jitter_max_sec = 0.0
    jitter_examples = []
    settlement_count_hist = {}

    for i, sym in enumerate(symbols):
        k = load_klines(sym)
        if k is not None:
            kline_syms.append(sym)
            zc = int((k["volume"] == 0).sum())
            zero_vol_total += zc
            if zc > 0:
                zero_vol_by_symbol[sym] = zc
            full_idx = pd.date_range(k.index.min(), k.index.max(), freq="D", tz="UTC")
            missing = full_idx.difference(k.index)
            gap_records.append({
                "symbol": sym, "missing_days": int(len(missing)),
                "span_days": int(len(full_idx)),
                "first": str(k.index.min().date()), "last": str(k.index.max().date()),
            })

        f = load_funding(sym)
        if f is not None:
            funding_syms.append(sym)
            for v in f["funding_interval_hours"].dropna().unique().tolist():
                interval_hours_values[str(v)] = interval_hours_values.get(str(v), 0) + 1

            dates = f.index.floor("D")
            per_day = f.groupby(dates).size()
            for c, n in per_day.value_counts().items():
                settlement_count_hist[str(c)] = settlement_count_hist.get(str(c), 0) + int(n)
            dev = per_day[per_day != 3]
            if len(dev) > 0:
                by_month = dev.groupby(dev.index.to_period("M")).size()
                for month, cnt in by_month.items():
                    settlement_dev_rows.append({
                        "symbol": sym, "month": str(month), "deviating_days": int(cnt),
                    })

            secs = (f.index.hour * 3600 + f.index.minute * 60 + f.index.second
                    + f.index.microsecond / 1e6)
            grid = 8 * 3600
            mod = secs % grid
            dist = np.minimum(mod, grid - mod)
            mx = float(np.max(dist)) if len(dist) else 0.0
            if mx > jitter_max_sec:
                jitter_max_sec = mx
                idx_max = int(np.argmax(dist))
                jitter_examples = [{"symbol": sym, "ts": str(f.index[idx_max]), "offset_sec": mx}]

        if (i + 1) % 50 == 0:
            print(f"  integrity: {i + 1}/{len(symbols)} symbols", file=sys.stderr)

    gap_df = pd.DataFrame(gap_records).sort_values("missing_days", ascending=False)
    top10_gaps = gap_df.head(10).to_dict(orient="records")

    dev_df = pd.DataFrame(settlement_dev_rows)
    dev_summary = (dev_df.groupby("symbol")["deviating_days"].sum().sort_values(ascending=False).head(15).to_dict()
                   if len(dev_df) else {})
    dev_month_top = (dev_df.sort_values("deviating_days", ascending=False).head(15).to_dict(orient="records")
                      if len(dev_df) else [])

    out = {
        "n_kline_symbols": len(kline_syms),
        "n_funding_symbols": len(funding_syms),
        "kline_only_symbols": sorted(set(kline_syms) - set(funding_syms)),
        "funding_interval_hours_value_counts": interval_hours_values,
        "settlement_count_histogram": settlement_count_hist,
        "n_symbol_months_deviating_from_3": len(dev_df),
        "deviating_symbols_top15_by_total_days": dev_summary,
        "deviating_symbol_months_top15": dev_month_top,
        "top10_gap_symbols": top10_gaps,
        "zero_volume_days_total": zero_vol_total,
        "zero_volume_days_top10_symbols": dict(
            sorted(zero_vol_by_symbol.items(), key=lambda kv: -kv[1])[:10]),
        "funding_jitter_max_offset_seconds": jitter_max_sec,
        "funding_jitter_example": jitter_examples,
    }
    save_json("stage1_integrity.json", out)
    print(f"stage 1 done: {len(kline_syms)} kline syms, {len(funding_syms)} funding syms, "
          f"jitter max {jitter_max_sec:.1f}s", file=sys.stderr)


# ------------------------------------------------------------------ stage 2 --

def stage_universe(symbols: list) -> None:
    print("=== stage: universe ===", file=sys.stderr)
    u = load_universe()
    pre = u[u["symbol"].isin(symbols)].copy()

    months = [str(p) for p in pd.period_range("2020-01", "2022-12", freq="M")]

    panel = build_klines_panel(symbols)
    panel["month"] = pd.PeriodIndex(panel["date"], freq="M").astype(str)

    active_by_month = panel.groupby("month")["symbol"].nunique().reindex(months).fillna(0).astype(int)
    listings_by_month = pre.groupby("first_month").size().reindex(months).fillna(0).astype(int)
    delistings_by_month = pre.groupby("last_month").size().reindex(months).fillna(0).astype(int)

    first_actual = panel.groupby("symbol")["date"].min()
    month_end_ts = {m: pd.Period(m, freq="M").end_time.normalize().tz_localize("UTC") for m in months}

    active_sets = panel.groupby("month")["symbol"].apply(set)
    ge90_counts = {}
    for m in months:
        me = month_end_ts[m]
        active = active_sets.get(m, set())
        cnt = sum(1 for s in active if (me - first_actual.get(s, me)).days >= 90)
        ge90_counts[m] = cnt

    monthend_rows = panel.sort_values("date").groupby(["symbol", "month"]).last().reset_index()
    qv_stats = monthend_rows.groupby("month")["trailing30_qv_median"].agg(
        median="median", p20=lambda s: s.quantile(0.20), p80=lambda s: s.quantile(0.80),
        n_nonnull="count").reindex(months)

    thresholds = {"ge_10m": 10e6, "ge_50m": 50e6, "ge_200m": 200e6}
    invest_counts = {}
    for m in months:
        sub = monthend_rows.loc[monthend_rows["month"] == m, "trailing30_qv_median"].dropna()
        invest_counts[m] = {k: int((sub >= v).sum()) for k, v in thresholds.items()}

    timeline_df = pd.DataFrame({
        "n_symbols_with_klines": active_by_month,
        "listings": listings_by_month,
        "delistings": delistings_by_month,
        "n_ge90d_history": pd.Series(ge90_counts),
        "median_trailing30d_qv": qv_stats["median"],
        "p20_trailing30d_qv": qv_stats["p20"],
        "p80_trailing30d_qv": qv_stats["p80"],
        "n_ge_10m": pd.Series({m: invest_counts[m]["ge_10m"] for m in months}),
        "n_ge_50m": pd.Series({m: invest_counts[m]["ge_50m"] for m in months}),
        "n_ge_200m": pd.Series({m: invest_counts[m]["ge_200m"] for m in months}),
    })
    timeline_df.index.name = "month"
    timeline_df.to_parquet(TMP_DIR / "stage2_timeline.parquet")

    out = {
        "n_pre2023_symbols": len(pre),
        "peak_active_symbols": int(active_by_month.max()),
        "peak_active_month": str(active_by_month.idxmax()),
        "final_month_2022_12": {
            "n_symbols_with_klines": int(active_by_month.loc["2022-12"]),
            "n_ge90d_history": int(ge90_counts["2022-12"]),
            "n_ge_10m": invest_counts["2022-12"]["ge_10m"],
            "n_ge_50m": invest_counts["2022-12"]["ge_50m"],
            "n_ge_200m": invest_counts["2022-12"]["ge_200m"],
        },
    }
    save_json("stage2_summary.json", out)
    print(f"stage 2 done: peak {out['peak_active_symbols']} active symbols "
          f"({out['peak_active_month']}); 2022-12 snapshot={out['final_month_2022_12']}",
          file=sys.stderr)


# ------------------------------------------------------------------ stage 3 --

def _spearman(a: pd.Series, b: pd.Series) -> float:
    df = pd.concat([a, b], axis=1).dropna()
    if len(df) < 5:
        return np.nan
    return float(df.iloc[:, 0].corr(df.iloc[:, 1], method="spearman"))


def stage_funding(funding_symbols: list, all_symbols: list) -> None:
    print("=== stage: funding ===", file=sys.stderr)
    frames = []
    for i, sym in enumerate(funding_symbols):
        f = load_funding(sym)
        if f is None:
            continue
        daily = f.groupby(f.index.floor("D"))["last_funding_rate"].sum()
        d = daily.rename("daily_funding").reset_index()
        d.columns = ["date", "daily_funding"]
        d["symbol"] = sym
        frames.append(d)
        if (i + 1) % 50 == 0:
            print(f"  funding: {i + 1}/{len(funding_symbols)} symbols", file=sys.stderr)
    fund = pd.concat(frames, ignore_index=True)
    fund["date"] = pd.to_datetime(fund["date"])
    fund.to_parquet(TMP_DIR / "stage3_daily_funding.parquet")

    pivot = fund.pivot(index="date", columns="symbol", values="daily_funding").sort_index()

    n_per_day = pivot.notna().sum(axis=1)
    pct = pivot.stack()

    def cs_quantiles(row):
        r = row.dropna()
        if len(r) == 0:
            return pd.Series({q: np.nan for q in [10, 25, 50, 75, 90]})
        return pd.Series({q: r.quantile(q / 100) for q in [10, 25, 50, 75, 90]})

    q_daily = pivot.apply(cs_quantiles, axis=1)
    q_daily.columns = [f"p{q}_daily_pct" for q in [10, 25, 50, 75, 90]]
    for c in q_daily.columns:
        q_daily[c] = q_daily[c] * 100.0
    q_annual = q_daily.copy()
    q_annual.columns = [c.replace("daily_pct", "annualised_pct") for c in q_annual.columns]
    for c in q_annual.columns:
        q_annual[c] = q_annual[c] * 365.0

    frac_pos = float((pct > 0.0009).sum() / pct.notna().sum())
    frac_neg = float((pct < -0.0009).sum() / pct.notna().sum())

    disp_daily = (q_daily["p90_daily_pct"] - q_daily["p10_daily_pct"])
    disp_by_month = disp_daily.groupby(pd.PeriodIndex(disp_daily.index, freq="M")).mean()

    lags = [1, 3, 7]
    persistence = {}
    for lag in lags:
        corrs = []
        for i in range(len(pivot) - lag):
            a = pivot.iloc[i]
            b = pivot.iloc[i + lag]
            c = _spearman(a, b)
            if not np.isnan(c):
                corrs.append(c)
        persistence[f"lag_{lag}d_median_spearman"] = float(np.median(corrs)) if corrs else np.nan
        persistence[f"lag_{lag}d_n_days"] = len(corrs)

    rank_pct = pivot.rank(axis=1, pct=True)
    top_decile = rank_pct >= 0.9
    run_lengths = []
    for sym in top_decile.columns:
        col = top_decile[sym].dropna()
        if len(col) == 0:
            continue
        vals = col.values
        run = 0
        for v in vals:
            if v:
                run += 1
            else:
                if run > 0:
                    run_lengths.append(run)
                run = 0
        if run > 0:
            run_lengths.append(run)
    avg_run_len = float(np.mean(run_lengths)) if run_lengths else np.nan

    panel = build_klines_panel(all_symbols)
    vol = panel[["date", "symbol", "trailing30_qv_median"]].copy()
    merged = fund.merge(vol, on=["date", "symbol"], how="inner")
    merged = merged.dropna(subset=["trailing30_qv_median"])
    daily_corrs = []
    for date, grp in merged.groupby("date"):
        if len(grp) < 5:
            continue
        c = grp["daily_funding"].corr(grp["trailing30_qv_median"], method="spearman")
        if pd.notna(c):
            daily_corrs.append(c)
    funding_vol_corr = float(np.median(daily_corrs)) if daily_corrs else np.nan

    out = {
        "n_days": int(len(pivot)),
        "n_symbols_avg_per_day": float(n_per_day.mean()),
        "cs_quantiles_overall": {
            "p10_daily_pct": float(q_daily["p10_daily_pct"].mean()),
            "p25_daily_pct": float(q_daily["p25_daily_pct"].mean()),
            "p50_daily_pct": float(q_daily["p50_daily_pct"].mean()),
            "p75_daily_pct": float(q_daily["p75_daily_pct"].mean()),
            "p90_daily_pct": float(q_daily["p90_daily_pct"].mean()),
            "p10_annualised_pct": float(q_annual["p10_annualised_pct"].mean()),
            "p25_annualised_pct": float(q_annual["p25_annualised_pct"].mean()),
            "p50_annualised_pct": float(q_annual["p50_annualised_pct"].mean()),
            "p75_annualised_pct": float(q_annual["p75_annualised_pct"].mean()),
            "p90_annualised_pct": float(q_annual["p90_annualised_pct"].mean()),
        },
        "frac_daily_funding_gt_0.09pct": frac_pos,
        "frac_daily_funding_lt_neg0.09pct": frac_neg,
        "dispersion_p90_p10_by_month_pct": {str(k): float(v) for k, v in disp_by_month.items()},
        "persistence": persistence,
        "avg_consecutive_days_in_top_decile": avg_run_len,
        "n_top_decile_runs": len(run_lengths),
        "funding_vs_trailing30d_volume_rank_spearman_median": funding_vol_corr,
        "n_days_used_for_funding_vol_corr": len(daily_corrs),
    }
    save_json("stage3_funding.json", out)
    q_daily.to_parquet(TMP_DIR / "stage3_qdaily.parquet")
    disp_by_month.to_frame("dispersion_pct").to_parquet(TMP_DIR / "stage3_dispersion_by_month.parquet")
    print(f"stage 3 done: p50 daily funding = {out['cs_quantiles_overall']['p50_daily_pct']:.4f}%/day, "
          f"persistence lag1={persistence['lag_1d_median_spearman']:.3f}", file=sys.stderr)


# ------------------------------------------------------------------ stage 4 --

def _cluster_events(mask: pd.Series, series: pd.Series):
    """Cluster consecutive True hours into events; return list of dicts
    with start/end/extreme value."""
    events = []
    in_event = False
    start = None
    extreme = None
    last_true_idx = None
    idx = mask.index
    for ts, v in zip(idx, mask.values):
        if v:
            if not in_event:
                in_event = True
                start = ts
                extreme = series.loc[ts]
            else:
                extreme = min(extreme, series.loc[ts])
            last_true_idx = ts
        else:
            if in_event:
                events.append({"start": start, "end": last_true_idx, "extreme": extreme})
                in_event = False
    if in_event:
        events.append({"start": start, "end": last_true_idx, "extreme": extreme})
    return events


def stage_oi(all_symbols: list) -> None:
    print("=== stage: oi ===", file=sys.stderr)
    panel = build_klines_panel(all_symbols)

    all_events = {}
    all_events_clean = {}
    per_symbol_quarter_counts = {"0.15": {}, "0.20": {}, "0.30": {}}
    btc_calendar = []
    coincidence = {"n_events_20pct": 0, "n_coincident_with_2std_move": 0}
    zero_oi_hours_by_symbol = {}

    for sym in METRICS_SYMS:
        m = load_metrics(sym)
        if m is None:
            print(f"  no metrics for {sym}, skipping", file=sys.stderr)
            continue
        oi = m["sum_open_interest"].resample("1h").last().dropna()
        zero_oi_hours_by_symbol[sym] = int((oi == 0).sum())
        pct24 = oi.pct_change(24)

        klines_sym = panel[panel["symbol"] == sym].set_index("date")

        for thr_name, thr in (("0.15", -0.15), ("0.20", -0.20), ("0.30", -0.30)):
            mask = pct24 <= thr
            events = _cluster_events(mask, pct24)
            for e in events:
                # flag events whose extreme reading coincides with a hard
                # zero in the underlying OI feed -- these are metrics-feed
                # data-gap artifacts (OI does not genuinely go to zero),
                # not real deleveraging events.
                e["is_artifact"] = bool(oi.loc[e["start"]:e["end"]].eq(0).any())
                q = pd.Period(e["start"], freq="Q")
                per_symbol_quarter_counts[thr_name].setdefault(sym, {})
                per_symbol_quarter_counts[thr_name][sym][str(q)] = (
                    per_symbol_quarter_counts[thr_name][sym].get(str(q), 0) + 1)
            if thr_name == "0.20":
                for e in events:
                    day = pd.Timestamp(e["start"]).normalize()
                    ret = np.nan
                    std30 = np.nan
                    if day in klines_sym.index:
                        ret = klines_sym.loc[day, "daily_return"]
                        std30 = klines_sym.loc[day, "trailing30_ret_std"]
                    coincident = bool(pd.notna(ret) and pd.notna(std30) and std30 > 0
                                       and abs(ret) >= 2 * std30)
                    coincidence["n_events_20pct"] += 1
                    if coincident:
                        coincidence["n_coincident_with_2std_move"] += 1
                    if sym == "BTCUSDT":
                        btc_calendar.append({
                            "date": str(day.date()),
                            "oi_drop_pct": round(float(e["extreme"]) * 100, 2),
                            "return_pct": round(float(ret) * 100, 2) if pd.notna(ret) else None,
                            "trailing30d_std_pct": round(float(std30) * 100, 2) if pd.notna(std30) else None,
                            "data_artifact": e["is_artifact"],
                        })
                all_events.setdefault(sym, 0)
                all_events[sym] += len(events)
                all_events_clean.setdefault(sym, 0)
                all_events_clean[sym] += sum(1 for e in events if not e["is_artifact"])
        print(f"  oi: {sym} done ({all_events.get(sym, 0)} 20%-events, "
              f"{zero_oi_hours_by_symbol[sym]} zero-OI feed-glitch hours)", file=sys.stderr)

    out = {
        "n_events_by_symbol_20pct": all_events,
        "n_events_by_symbol_20pct_excl_data_artifacts": all_events_clean,
        "zero_oi_hours_by_symbol": zero_oi_hours_by_symbol,
        "events_per_symbol_quarter": per_symbol_quarter_counts,
        "coincidence_with_2x_trailing30d_std_move": coincidence,
        "btcusdt_calendar_20pct": sorted(btc_calendar, key=lambda r: r["date"]),
    }
    save_json("stage4_oi.json", out)
    print(f"stage 4 done: {sum(all_events.values())} total 20%-events across {len(all_events)} symbols "
          f"({sum(all_events_clean.values())} excluding zero-OI feed-glitch artifacts)",
          file=sys.stderr)


# ------------------------------------------------------------------ stage 5 --

def stage_delist(all_symbols: list) -> None:
    print("=== stage: delist ===", file=sys.stderr)
    u = load_universe()
    pre = u[u["symbol"].isin(all_symbols)].copy()
    panel = build_klines_panel(all_symbols)
    first_actual = panel.groupby("symbol")["date"].min()
    last_actual = panel.groupby("symbol")["date"].max()

    listings = {}
    for year in (2020, 2021, 2022):
        yr_syms = pre.loc[pre["first_month"].str.startswith(str(year)), "symbol"].tolist()
        n = 0
        for s in yr_syms:
            fa = first_actual.get(s)
            if fa is None:
                continue
            subsequent_days = (pd.Timestamp("2022-12-31", tz="UTC") - fa).days
            if subsequent_days >= 60:
                n += 1
        listings[year] = {"n_listed": len(yr_syms), "n_with_ge60d_subsequent": n}

    delisted_pre2023 = pre.loc[pre["last_month"] <= "2022-11"].copy()
    n_delist_ge60 = 0
    delist_rows = []
    for _, row in delisted_pre2023.iterrows():
        s = row["symbol"]
        fa = first_actual.get(s)
        la = last_actual.get(s)
        if fa is None or la is None:
            continue
        lifespan = (la - fa).days
        ok = lifespan >= 60
        if ok:
            n_delist_ge60 += 1
        delist_rows.append({"symbol": s, "first": str(fa.date()), "last": str(la.date()),
                             "lifespan_days": int(lifespan), "ge60d": ok})

    out = {
        "listings_by_year": listings,
        "n_delisted_before_2023": len(delisted_pre2023),
        "n_delisted_before_2023_with_ge60d_prior_data": n_delist_ge60,
        "delisting_detail": sorted(delist_rows, key=lambda r: r["first"]),
    }
    save_json("stage5_delist.json", out)
    print(f"stage 5 done: listings={listings}, delistings_ge60d={n_delist_ge60}/{len(delisted_pre2023)}",
          file=sys.stderr)


# ---------------------------------------------------------------------- report --

def stage_report(out_path: str) -> None:
    print("=== stage: report ===", file=sys.stderr)
    s1 = load_json("stage1_integrity.json")
    s2 = load_json("stage2_summary.json")
    s3 = load_json("stage3_funding.json")
    s4 = load_json("stage4_oi.json")
    s5 = load_json("stage5_delist.json")
    missing = [n for n, v in [("integrity", s1), ("universe", s2), ("funding", s3),
                               ("oi", s4), ("delist", s5)] if v is None]
    if missing:
        raise RuntimeError(f"missing stage cache(s), run them first: {missing}")

    timeline = pd.read_parquet(TMP_DIR / "stage2_timeline.parquet")
    disp = pd.read_parquet(TMP_DIR / "stage3_dispersion_by_month.parquet")

    lines = []
    lines.append("# Binance USDT-M perp archive - descriptive data recon (discovery window < 2023-01-01)")
    lines.append("")
    lines.append(
        "Generated by `scripts/recon_binance_um.py`. Descriptive recon only - no forward returns, "
        "no long/short spreads, no Sharpe computed anywhere in this report. All data is asserted "
        "to be strictly before 2023-01-01 at every load site."
    )
    lines.append("")

    # Section 1
    lines.append("## 1. Data integrity")
    lines.append("")
    lines.append(f"- Symbols with 1d klines (pre-2023 listing cohort): **{s1['n_kline_symbols']}**")
    lines.append(f"- Symbols with funding data: **{s1['n_funding_symbols']}**")
    lines.append(f"- Symbols with klines but no funding file: **{len(s1['kline_only_symbols'])}** "
                 f"({', '.join(s1['kline_only_symbols'][:15])}{' ...' if len(s1['kline_only_symbols']) > 15 else ''})")
    lines.append("")
    lines.append(f"- `funding_interval_hours` values observed (value -> count of symbols exhibiting it at least once): "
                 f"{s1['funding_interval_hours_value_counts']}")
    lines.append(f"- Settlements-per-day histogram across all symbol-days (expect all mass at `3`): "
                 f"{s1['settlement_count_histogram']}")
    lines.append(f"- Symbol-months with a day count deviating from 3 settlements: "
                 f"**{s1['n_symbol_months_deviating_from_3']}**")
    if s1["deviating_symbol_months_top15"]:
        dev_df = pd.DataFrame(s1["deviating_symbol_months_top15"])
        lines.append("")
        lines.append("Top symbol-months by deviating-day count:")
        lines.append("")
        lines.append(md_table(dev_df))
    else:
        lines.append("  (no symbol-months deviate from the 3-settlement/day 8h grid - no 4h-interval "
                      "regime found in this cohort)")
    lines.append("")
    lines.append(f"- Funding timestamp jitter: max absolute offset from the 8h grid = "
                 f"**{s1['funding_jitter_max_offset_seconds']:.1f} seconds** "
                 f"(example: {s1['funding_jitter_example']})")
    lines.append("")
    lines.append("Top-10 symbols by missing kline days (within their own first-to-last span):")
    lines.append("")
    lines.append(md_table(pd.DataFrame(s1["top10_gap_symbols"])))
    lines.append("")
    lines.append(f"- Zero-volume kline days, total across all symbols: **{s1['zero_volume_days_total']}**")
    if s1["zero_volume_days_top10_symbols"]:
        lines.append(f"  top symbols: {s1['zero_volume_days_top10_symbols']}")
    lines.append("")

    # Section 2
    lines.append("## 2. Universe timeline")
    lines.append("")
    lines.append(f"- Pre-2023 listing cohort size: **{s2['n_pre2023_symbols']}**")
    lines.append(f"- Peak simultaneously-active symbols: **{s2['peak_active_symbols']}** in {s2['peak_active_month']}")
    lines.append(f"- 2022-12 snapshot: {s2['final_month_2022_12']}")
    lines.append("")
    lines.append("Monthly timeline (symbols with klines, listings, delistings, symbols with >=90d history, "
                 "cross-sectional trailing-30d quote-volume median/p20/p80, and investable-universe counts "
                 "at three liquidity thresholds):")
    lines.append("")
    show = timeline.reset_index()
    show["median_trailing30d_qv"] = show["median_trailing30d_qv"].map(lambda x: f"{x:,.0f}" if pd.notna(x) else "")
    show["p20_trailing30d_qv"] = show["p20_trailing30d_qv"].map(lambda x: f"{x:,.0f}" if pd.notna(x) else "")
    show["p80_trailing30d_qv"] = show["p80_trailing30d_qv"].map(lambda x: f"{x:,.0f}" if pd.notna(x) else "")
    lines.append(show.to_markdown(index=False))
    lines.append("")

    # Section 3
    lines.append("## 3. Funding-rate cross-section")
    lines.append("")
    lines.append(f"- Days covered: **{s3['n_days']}**; average symbols/day: **{s3['n_symbols_avg_per_day']:.1f}**")
    cs = s3["cs_quantiles_overall"]
    lines.append("")
    lines.append("Cross-sectional funding quantiles, averaged over all days (daily %/day and annualised %):")
    lines.append("")
    qtab = pd.DataFrame({
        "quantile": ["p10", "p25", "p50", "p75", "p90"],
        "daily_pct": [cs["p10_daily_pct"], cs["p25_daily_pct"], cs["p50_daily_pct"],
                      cs["p75_daily_pct"], cs["p90_daily_pct"]],
        "annualised_pct": [cs["p10_annualised_pct"], cs["p25_annualised_pct"], cs["p50_annualised_pct"],
                           cs["p75_annualised_pct"], cs["p90_annualised_pct"]],
    })
    lines.append(md_table(qtab))
    lines.append("")
    lines.append(f"- Fraction of symbol-days with daily funding > 0.09%: **{s3['frac_daily_funding_gt_0.09pct']:.4f}**")
    lines.append(f"- Fraction of symbol-days with daily funding < -0.09%: **{s3['frac_daily_funding_lt_neg0.09pct']:.4f}**")
    lines.append("")
    disp_vals_ = pd.read_parquet(TMP_DIR / "stage3_dispersion_by_month.parquet")["dispersion_pct"]
    lines.append(f"Cross-sectional dispersion (p90 minus p10, daily %) by month "
                 f"(range across the 36 months: {disp_vals_.min():.3f}% to {disp_vals_.max():.3f}%, i.e. "
                 f"roughly {disp_vals_.min()*100:.0f}-{disp_vals_.max()*100:.0f} bp/day; trend is an elevated "
                 f"2020-2021 regime cooling toward single-digit-bp dispersion by late 2021 through 2022):")
    lines.append("")
    disp_show = disp.reset_index()
    disp_show.columns = ["month", "dispersion_pct"]
    disp_show["dispersion_pct"] = disp_show["dispersion_pct"].map(lambda x: f"{x:.4f}")
    lines.append(disp_show.to_markdown(index=False))
    lines.append("")
    p = s3["persistence"]
    lines.append("Rank persistence of daily funding (median cross-sectional Spearman correlation, day t vs t+lag; "
                 "no returns involved):")
    lines.append("")
    lines.append(f"- t vs t+1: **{p['lag_1d_median_spearman']:.3f}** ({p['lag_1d_n_days']} day-pairs)")
    lines.append(f"- t vs t+3: **{p['lag_3d_median_spearman']:.3f}** ({p['lag_3d_n_days']} day-pairs)")
    lines.append(f"- t vs t+7: **{p['lag_7d_median_spearman']:.3f}** ({p['lag_7d_n_days']} day-pairs)")
    lines.append(f"- Average consecutive days a symbol stays in the cross-sectional top decile once it enters: "
                 f"**{s3['avg_consecutive_days_in_top_decile']:.2f}** (over {s3['n_top_decile_runs']} runs)")
    lines.append(f"- Median daily Spearman correlation between a symbol's funding and its trailing-30d quote-volume "
                 f"rank: **{s3['funding_vs_trailing30d_volume_rank_spearman_median']:.3f}** "
                 f"({s3['n_days_used_for_funding_vol_corr']} days)")
    lines.append("")

    # Section 4
    lines.append("## 4. Open-interest / deleveraging event density (10 metrics symbols)")
    lines.append("")
    lines.append(f"- 24h OI-decline events >=20%, by symbol (raw): {s4['n_events_by_symbol_20pct']}")
    lines.append("")
    total_zero_oi_hours = sum(s4.get("zero_oi_hours_by_symbol", {}).values())
    lines.append(
        f"- **Data-integrity caveat:** the `sum_open_interest` feed drops to a hard **0** for a handful of "
        f"hours per symbol ({s4.get('zero_oi_hours_by_symbol', {})}, {total_zero_oi_hours} hours total across "
        f"all 10 symbols) -- this is a metrics-feed data gap, not a genuine OI wipeout (BTC open interest does "
        f"not actually go to zero and recover within the hour). Any 24h window touching one of these hours "
        f"produces a spurious ~-100% reading. Events overlapping a zero-OI hour are flagged `data_artifact` "
        f"below and excluded from the artifact-adjusted counts."
    )
    lines.append(f"- 24h OI-decline events >=20%, by symbol (**excluding** zero-OI feed-glitch artifacts): "
                 f"{s4.get('n_events_by_symbol_20pct_excl_data_artifacts', {})}")
    lines.append("")
    lines.append("Events per symbol per quarter, by threshold (15% / 20% / 30%; raw, includes the small "
                 "number of data-artifact events noted above):")
    for thr in ("0.15", "0.20", "0.30"):
        lines.append("")
        lines.append(f"threshold {thr}:")
        lines.append(f"```\n{json.dumps(s4['events_per_symbol_quarter'][thr], indent=2)}\n```")
    lines.append("")
    coin = s4["coincidence_with_2x_trailing30d_std_move"]
    frac = (coin["n_coincident_with_2std_move"] / coin["n_events_20pct"]) if coin["n_events_20pct"] else float("nan")
    lines.append(f"- Of the {coin['n_events_20pct']} 20%-OI-collapse events (all 10 symbols, raw), "
                 f"**{coin['n_coincident_with_2std_move']}** ({frac:.1%}) coincided with a same-day |close-close "
                 f"return| >= 2x trailing-30d daily std.")
    lines.append("")
    lines.append("BTCUSDT 20%-OI-collapse event calendar (merged to one row per calendar date -- "
                 "worst hourly cluster of the day; a date can host >1 independent hourly cluster, "
                 "noted in `n_clusters`; `data_artifact=True` marks a zero-OI feed-glitch hour, see caveat above):")
    lines.append("")
    if s4["btcusdt_calendar_20pct"]:
        btc_df = pd.DataFrame(s4["btcusdt_calendar_20pct"])
        agg = btc_df.groupby("date").agg(
            oi_drop_pct=("oi_drop_pct", "min"),
            return_pct=("return_pct", "first"),
            trailing30d_std_pct=("trailing30d_std_pct", "first"),
            n_clusters=("oi_drop_pct", "size"),
            data_artifact=("data_artifact", "any"),
        ).reset_index().sort_values("date")
        lines.append(md_table(agg))
    else:
        lines.append("(none)")
    lines.append("")

    # Section 5
    lines.append("## 5. Listing/delisting event density")
    lines.append("")
    lines.append("Listings per year with >=60 days of subsequent data before 2023-01-01:")
    lines.append("")
    lyears = pd.DataFrame(s5["listings_by_year"]).T
    lyears.index.name = "year"
    lines.append(lyears.reset_index().to_markdown(index=False))
    lines.append("")
    lines.append(f"- Delistings before 2023 (last_month <= 2022-11): **{s5['n_delisted_before_2023']}**")
    lines.append(f"- ...of which with >=60 days of prior data: **{s5['n_delisted_before_2023_with_ge60d_prior_data']}**")
    lines.append("")

    # Section 6
    lines.append("## 6. Implications for the section-C.4 kill tests")
    lines.append("")
    total_listing_events = sum(v["n_with_ge60d_subsequent"] for v in s5["listings_by_year"].values())
    total_delist_events = s5["n_delisted_before_2023_with_ge60d_prior_data"]
    n_oi_events = sum(s4["n_events_by_symbol_20pct"].values())
    disp_vals_2 = pd.read_parquet(TMP_DIR / "stage3_dispersion_by_month.parquet")["dispersion_pct"]
    lines.append(
        f"**Funding-rate cross-section / carry:** {s3['n_days']} days with an average of "
        f"{s3['n_symbols_avg_per_day']:.0f} symbols/day; cross-sectional dispersion (p90-p10) ranges "
        f"~{disp_vals_2.min()*100:.0f}-{disp_vals_2.max()*100:.0f} bp/day across the window (highest in "
        f"2020-2021, cooling to single-digit bp by late 2021/2022) and rank persistence is "
        f"{p['lag_1d_median_spearman']:.2f}/{p['lag_3d_median_spearman']:.2f}/{p['lag_7d_median_spearman']:.2f} "
        f"at lag 1/3/7 days - enough breadth and enough day-count for a cross-sectional funding-carry kill test, "
        f"but funding is missing for {s2['n_pre2023_symbols'] - s1['n_funding_symbols']} of the "
        f"{s2['n_pre2023_symbols']} pre-2023 symbols, and high funding shows a "
        f"{s3['funding_vs_trailing30d_volume_rank_spearman_median']:.2f} rank correlation with trailing volume, "
        f"i.e. any carry construction should stratify or control for cap/liquidity rather than treat the "
        f"cross-section as homogeneous."
    )
    lines.append("")
    n_oi_events_clean = sum(s4.get("n_events_by_symbol_20pct_excl_data_artifacts", {}).values())
    lines.append(
        f"**Open-interest deleveraging:** {n_oi_events} events >=20% across the 10 metrics symbols in this "
        f"window ({n_oi_events_clean} after excluding zero-OI feed-glitch artifacts -- see the data-integrity "
        f"caveat in section 4), but metrics for the 9 alts only start 2021-12 (BTCUSDT alone covers 2020-09 "
        f"to 2022-12) - event density and BTC-specific counts are usable now, a cross-sectional alt study "
        f"effectively only has ~13 months (2021-12 to 2022-12) rather than the full window. Any kill-test "
        f"implementation must exclude or repair the zero-OI hours before computing 24h OI-change features, "
        f"or it will manufacture spurious collapse events."
    )
    lines.append("")
    lines.append(
        f"**Listing/delisting effects:** {total_listing_events} qualifying listing events (>=60d subsequent "
        f"data) and {total_delist_events} qualifying delisting events (>=60d prior data) before 2023-01-01 - "
        f"this is the N available for a listing/delisting event study in the discovery window; delisting N in "
        f"particular is thin ({total_delist_events}) and any kill test built on it should treat results as "
        f"indicative rather than well-powered."
    )
    lines.append("")
    lines.append(
        "**General caveat:** all of the above is descriptive - no forward returns, spreads, or Sharpe were "
        "computed in producing this report; a kill test still needs its own return-based design and its own "
        "holdout-respecting trial entry."
    )
    lines.append("")

    report = "\n".join(lines)
    out_p = REPO / out_path
    out_p.parent.mkdir(parents=True, exist_ok=True)
    out_p.write_text(report)
    print(f"report written to {out_p} ({len(report)} chars)", file=sys.stderr)


# ------------------------------------------------------------------------ main --

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True,
                     choices=["integrity", "universe", "funding", "oi", "delist", "report", "all"])
    ap.add_argument("--out", default="docs/recon_binance_um_2026-09.md")
    args = ap.parse_args()

    u = load_universe()
    symbols = pre2023_symbols(u)
    funding_symbols = [s for s in symbols if (CACHE_DIR / "funding" / f"{s}.parquet").exists()]
    print(f"pre-2023 cohort: {len(symbols)} symbols, {len(funding_symbols)} with funding", file=sys.stderr)

    stages = {
        "integrity": lambda: stage_integrity(symbols, funding_symbols),
        "universe": lambda: stage_universe(symbols),
        "funding": lambda: stage_funding(funding_symbols, symbols),
        "oi": lambda: stage_oi(symbols),
        "delist": lambda: stage_delist(symbols),
        "report": lambda: stage_report(args.out),
    }

    if args.stage == "all":
        for name in ["integrity", "universe", "funding", "oi", "delist", "report"]:
            stages[name]()
    else:
        stages[args.stage]()


if __name__ == "__main__":
    main()
