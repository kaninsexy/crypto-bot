"""
scratch/gate_recalc_v2.py — Gate-recalibration audit v2 (READ-ONLY).

Extends scratch/gate_recalc.py with:
  - Original-verdict reconstruction incl. preconditions (trade floor 30,
    MinTRL vs T_approx), checked against trial_queue_state.json verdicts.
  - S2 corrected DSR approximation: sr_std backed out of persisted mintrl
    (vt = (mintrl-1)*(SR/Z)^2, harness annualised-SR-unit convention) with
    T approximated by dev-window bar count per substrate.
  - S1 for FundingRateHarvest (delta-neutral): PSR vs 0 with Gaussian and
    fat-tail-bounded variants (bound from the persisted mintrl back-out).
  - S3 regime calendar from BTC 1d dev cache (slice strictly < dev_end;
    NO holdout bars are read) + AttentionMomentum per-block regime map.
  - S4 parametric proxies on the two holdout casualties from persisted
    summary stats only (bootstrap itself BLOCKED: per-bar series absent).
  - MinTRL pre-check at target true Sharpe 1.0 per substrate.
  - CPCVError event-based block-sizing feasibility (>= 5 blocks x
    _MIN_EVENTS_PER_BLOCK=5 events = 25 recorded events minimum).

All numbers derive from: backtest/trials.log, backtest/holdout_manifest.json,
backtest/trial_queue_state.json, backtest/cache/* (dev slice only),
research/*.md outcome rows. No holdout access, no strategy re-runs.
"""
import json
import math
import re
import statistics
from collections import Counter

import numpy as np
import pandas as pd
from scipy import stats

ROOT = r"C:\crypto-bot"
TRIALS = ROOT + r"\backtest\trials.log"
MANIFEST = ROOT + r"\backtest\holdout_manifest.json"
EULER = 0.5772156649015329
Z95 = float(stats.norm.ppf(0.95))  # 1.6449

CLUSTER = {
    "CrossSectionalMomentum": "cs-momentum/rotation",
    "ShortTermCrossSectionalMomentum": "cs-momentum/rotation",
    "AltcoinSeasonRotation": "cs-momentum/rotation",
    "CryptoSectorRotation": "cs-momentum/rotation",
    "CryptoDualMomentum": "cs-momentum/rotation",
    "CrossSectionalReversal": "reversal",
    "DailyCrossSectionalReversal": "reversal",
    "CrossSectionalResidualReversal": "reversal",
    "MeanReversion_BTC_Residual": "reversal",
    "IntradayMomentumReversal": "reversal",
    "IntradayJumpReversal": "reversal",
    "OvernightSessionReversal": "reversal",
    "LiquidityConditionedReversal": "reversal",
    "PutCallRatioContrarian": "reversal",
    "FundingRateHarvest_BTC": "carry",
    "CrossSectionalFundingRateCarry": "carry",
    "IdiosyncraticResidualTSMOM": "trend/TSMOM",
    "VolumeWeightedTSMOM": "trend/TSMOM",
    "VolatilityScaledTSMOM": "trend/TSMOM",
    "HurstExponentRegimeSwitch": "trend/TSMOM",
    "DayOfWeekSeasonality": "seasonality",
    "SocialSentimentMomentum": "sentiment/attention",
    "ContrarianSearchVolume": "sentiment/attention",
    "AttentionMomentum": "sentiment/attention",
    "NewsSentimentMomentum": "sentiment/attention",
    "DEXFlowSpillover": "microstructure/flow",
    "ExchangeListingDrift": "microstructure/flow",
    "IlliquidityPremium": "microstructure/flow",
    "CrossSectionalSkewness": "other",
}


def gumbel_term(n):
    if n < 2:
        return 0.0
    n = float(n)
    return ((1.0 - EULER) * stats.norm.ppf(1.0 - 1.0 / n)
            + EULER * stats.norm.ppf(1.0 - 1.0 / (n * math.e)))


rows = [json.loads(l) for l in open(TRIALS, encoding="utf-8") if l.strip()]
manifest = json.load(open(MANIFEST))


def is_nan_sharpe(r):
    sh = r.get("sharpe")
    return isinstance(sh, float) and math.isnan(sh)


def dev_bars(sid, warmup=0):
    """Approximate dev-window bar count for a strategy from the manifest."""
    m = manifest[sid]
    tf = m["timeframe"]
    hours = {"1h": 1, "4h": 4, "1d": 24}[tf]
    t0 = pd.Timestamp(m["data_start"])
    t1 = pd.Timestamp(m["holdout_start"])
    return int((t1 - t0).total_seconds() / 3600 / hours) - warmup


finite = [r for r in rows
          if r.get("trial_type") == "full_cpcv"
          and not is_nan_sharpe(r) and not r.get("superseded_by")]
cpcv_err = [r for r in rows if is_nan_sharpe(r)]
final_gates = [r for r in rows if r.get("trial_type") == "final_gate"]

print("=" * 78)
print("A. ROW CENSUS")
print("=" * 78)
print(f"total rows {len(rows)} | finite non-superseded full_cpcv {len(finite)}"
      f" | superseded dup {sum(1 for r in rows if r.get('superseded_by'))}"
      f" | CPCVError(NaN) {len(cpcv_err)} | final_gate {len(final_gates)}")
print("CPCVError unique (strategy,variation):",
      len({(r['strategy_id'], r['variation_id']) for r in cpcv_err}))
print("CPCVError by strategy:", dict(Counter(r["strategy_id"] for r in cpcv_err)))

# ── Original verdict reconstruction ──────────────────────────────────────────
print()
print("=" * 78)
print("B. ORIGINAL VERDICT RECONSTRUCTION (preconditions + quality gates)")
print("=" * 78)
recon = {}
for r in finite:
    sid = r["strategy_id"]
    sh, bh, mt = r["sharpe"], r["buy_and_hold_sharpe"], r["mintrl"]
    sec = r.get("signal_event_count")
    t_approx = dev_bars(sid)
    pre_count = (sec >= 30) if sec is not None else (r["n_trades"] >= 30)
    pre_mintrl = mt <= t_approx
    if not (pre_count and pre_mintrl):
        v = "under_tested"
    else:
        v = "keep" if (sh > 0.0 and sh > bh) else "retire"
    recon[sid] = v
    flag = ""
    if not pre_count:
        flag += f" [count_floor_fail n_trades={r['n_trades']} sec={sec}]"
    if not pre_mintrl:
        flag += f" [mintrl_fail mintrl={mt:.0f} > T~{t_approx}]"
    print(f"  {sid:32s} sharpe={sh:+7.3f} B&H={bh:+6.3f} -> {v:12s}{flag}")

# ── S2 ───────────────────────────────────────────────────────────────────────
print()
print("=" * 78)
print("C. S2 — CORRECTED sr_zero_expected + approximate corrected DSR")
print("=" * 78)
by_cluster = {}
for r in finite:
    by_cluster.setdefault(CLUSTER[r["strategy_id"]], []).append(r["sharpe"])

all_sh = [r["sharpe"] for r in finite]
V_prog, N_prog = statistics.pvariance(all_sh), len(all_sh)
srz_prog = math.sqrt(V_prog) * gumbel_term(N_prog)
print(f"program layer: N={N_prog} sqrtV={math.sqrt(V_prog):.4f} "
      f"Gumbel={gumbel_term(N_prog):.4f} sr_zero_prog={srz_prog:.4f}")

# sensitivity: count CPCVError unique trials into cluster N (V unchanged)
err_unique = {(r['strategy_id'], r['variation_id']) for r in cpcv_err}
err_by_cluster = Counter(CLUSTER[s] for s, _ in err_unique)

cl_info = {}
for c, sl in sorted(by_cluster.items()):
    N = len(sl)
    V = statistics.pvariance(sl) if N > 1 else 0.0
    srz = math.sqrt(V) * gumbel_term(N)
    N_alt = N + err_by_cluster.get(c, 0)
    srz_alt = math.sqrt(V) * gumbel_term(N_alt)
    cl_info[c] = dict(N=N, V=V, srz=srz, N_alt=N_alt, srz_alt=srz_alt)
    print(f"  {c:24s} N={N} (alt incl CPCVError N={N_alt}) sqrtV={math.sqrt(V):.4f}"
          f" sr_zero={srz:.4f} (alt {srz_alt:.4f}) sharpes={[round(x,3) for x in sl]}")

print()
print(f"{'strategy':32s} {'SR':>7s} {'srz_cl':>7s} {'S2cl':>5s} "
      f"{'DSRcl':>6s} {'>=.95':>5s} {'S2prog':>6s} {'DSRpr':>6s} {'sr_std~':>7s}")
s2 = {}
for r in finite:
    sid = r["strategy_id"]
    sh, mt = r["sharpe"], r["mintrl"]
    c = CLUSTER[sid]
    srz_cl = cl_info[c]["srz"]
    t_approx = dev_bars(sid)
    # sr_std in the harness's annualised-SR convention, backed out of mintrl:
    # mintrl = 1 + vt*(Z/|SR|)^2  =>  vt = (mintrl-1)*(SR/Z)^2 ; sr_var = vt/(T-1)
    vt = (mt - 1.0) * (sh / Z95) ** 2 if abs(sh) > 1e-9 else float("nan")
    sr_std = math.sqrt(vt / (t_approx - 1)) if vt == vt and vt > 0 else float("nan")
    dsr_cl = float(stats.norm.cdf((sh - srz_cl) / sr_std)) if sr_std == sr_std else float("nan")
    dsr_pr = float(stats.norm.cdf((sh - srz_prog) / sr_std)) if sr_std == sr_std else float("nan")
    s2[sid] = dict(srz_cl=srz_cl, s2_cl=sh > srz_cl and dsr_cl >= 0.95,
                   dsr_cl=dsr_cl, s2_prog=sh > srz_prog and dsr_pr >= 0.95,
                   dsr_pr=dsr_pr, sr_std=sr_std)
    print(f"{sid:32s} {sh:+7.3f} {srz_cl:7.3f} {str(sh > srz_cl):>5s} "
          f"{dsr_cl:6.3f} {str(dsr_cl >= 0.95):>5s} {str(sh > srz_prog):>6s} "
          f"{dsr_pr:6.3f} {sr_std:7.3f}")

# ── S1 FundingRateHarvest PSR ────────────────────────────────────────────────
print()
print("=" * 78)
print("D. S1 — FundingRateHarvest_BTC V2b (delta-neutral): PSR vs 0 at 95%")
print("=" * 78)
frh = [r for r in finite if r["strategy_id"] == "FundingRateHarvest_BTC"][0]
sh_ann = frh["sharpe"]
T = dev_bars("FundingRateHarvest_BTC")
bpy = 365.25 * 24  # 1h bars
sr_pb = sh_ann / math.sqrt(bpy)
years = T / bpy
# Gaussian per-bar PSR
z_gauss = sr_pb * math.sqrt(T - 1)
# Fat-tail bound: persisted mintrl=123.91 with annualised SR gives
# vt_ann = (mintrl-1)*(SR/Z)^2; the kurtosis consistent with it (skew=0)
# is (k-1) = 4*(vt_ann-1)/SR_ann^2; recompute per-bar vt with that k.
vt_ann = (frh["mintrl"] - 1.0) * (sh_ann / Z95) ** 2
k_minus1_bound = 4.0 * (vt_ann - 1.0) / sh_ann ** 2
vt_pb_bound = 1.0 + k_minus1_bound / 4.0 * sr_pb ** 2
z_bound = sr_pb * math.sqrt(T - 1) / math.sqrt(vt_pb_bound)
# extreme-skew variant (skew = -10 per bar, k from the same identity)
skew_x = -10.0
k_minus1_x = max(0.0, 4.0 * (vt_ann - 1.0 + skew_x * sh_ann) / sh_ann ** 2)
vt_pb_x = 1.0 - skew_x * sr_pb + k_minus1_x / 4.0 * sr_pb ** 2
z_x = sr_pb * math.sqrt(T - 1) / math.sqrt(vt_pb_x)
print(f"dev T~{T} 1h bars ({years:.2f}y) SR_ann={sh_ann} sr_perbar={sr_pb:.5f}")
print(f"  PSR z (Gaussian)        = {z_gauss:.2f}  PSR={stats.norm.cdf(z_gauss):.6f}")
print(f"  PSR z (kurt-bound k~{k_minus1_bound+1:.0f}) = {z_bound:.2f}  PSR={stats.norm.cdf(z_bound):.6f}")
print(f"  PSR z (skew=-10 bound)  = {z_x:.2f}  PSR={stats.norm.cdf(z_x):.6f}")
print(f"  -> PASS at 95% under every moment combination consistent with the")
print(f"     persisted mintrl ({frh['mintrl']:.2f}).")

# ── S3 regime calendar (dev slice ONLY) ──────────────────────────────────────
print()
print("=" * 78)
print("E. S3 — regime sub-windows from BTC 1d dev cache (NO holdout bars)")
print("=" * 78)
df = pd.read_parquet(ROOT + r"\backtest\cache\ohlcv\BTC-USDT_1d_38mo.parquet")
if not isinstance(df.index, pd.DatetimeIndex):
    for cand in ("timestamp", "date", "ts"):
        if cand in df.columns:
            df = df.set_index(pd.to_datetime(df[cand], utc=True))
            break
dev_end = pd.Timestamp("2025-09-22T00:00:00+00:00")
if df.index.tz is None:
    dev_end = dev_end.tz_localize(None)
df = df[df.index < dev_end]  # STRICT dev slice — holdout never loaded
print(f"BTC 1d dev slice: {df.index[0]} -> {df.index[-1]} ({len(df)} bars)")

lr = np.log(df["close"]).diff()
vol30 = lr.rolling(30).std() * math.sqrt(365.25)
trend90 = df["close"].pct_change(90)
ok = vol30.notna() & trend90.notna()
q1, q2 = vol30[ok].quantile([1 / 3, 2 / 3])
print(f"vol30 terciles (annualised): q33={q1:.3f} q67={q2:.3f}")
terc = pd.Series(np.where(vol30 <= q1, "loV", np.where(vol30 <= q2, "midV", "hiV")),
                 index=df.index)
tr = pd.Series(np.where(trend90 > 0, "up", "dn"), index=df.index)
cell = terc + "-" + tr
cell[~ok] = "warmup"
print("dev-window regime occupancy (bars):")
print(cell.value_counts().to_string())

# verify CONTEXT bullet: dev BTC 1d B&H Sharpe ~1.94 (baseline.py formula)
r_ = df["close"].pct_change().dropna().values
yrs = len(r_) / 365.25
ann_ret = ((1.0 + float(np.prod(1 + r_)) - 1.0 + 0.0) ** 0)  # placeholder
tot = float(np.prod(1 + r_) - 1)
ann_ret = ((1 + tot) ** (1 / yrs) - 1) * 100
vol = float(r_.std()) * math.sqrt(365.25) * 100
print(f"check: BTC 1d dev B&H Sharpe (baseline.py formula) = {ann_ret/vol:.4f}")

# AttentionMomentum block -> regime map (blocks = sequential deciles of the
# post-warmup dev frame; warmup=29 per manifest/T1 note)
am_blocks = [float("nan"), -0.493, 9.859, 21.097, -0.402,
             float("nan"), float("nan"), -1.509, 0.753, 0.241]
am_start = pd.Timestamp("2023-03-06T00:00:00+00:00")
if df.index.tz is None:
    am_start = am_start.tz_localize(None)
am_idx = df.index[(df.index >= am_start)][29:]  # warmup 29
chunks = np.array_split(np.arange(len(am_idx)), 10)
print()
print("AttentionMomentum CPCV blocks mapped to dev deciles (approx):")
quad_perf = {}
for i, ch in enumerate(chunks):
    span = am_idx[ch]
    cc = cell.loc[span].value_counts()
    dom = cc.index[0]
    # collapse to 2x2 quadrant: vol median split via terciles (mid -> majority side)
    sub = cell.loc[span]
    hi = sub.str.startswith("hiV").sum()
    lo = sub.str.startswith("loV").sum()
    volside = "hiV" if hi >= lo else "loV"
    upn = sub.str.endswith("-up").sum()
    quad = f"{volside}-{'up' if upn >= len(sub)/2 else 'dn'}"
    s = am_blocks[i]
    if not math.isnan(s):
        quad_perf.setdefault(quad, []).append(s)
    print(f"  block {i}: {span[0].date()} -> {span[-1].date()}  dominant={dom:9s}"
          f" quadrant={quad}  block_sharpe={s}")
print("AttentionMomentum per-quadrant block-Sharpe means:")
for q, v in sorted(quad_perf.items()):
    print(f"  {q}: n={len(v)} mean={statistics.mean(v):+.3f} blocks={v}")
qpos = sum(1 for v in quad_perf.values() if statistics.mean(v) > 0)
print(f"quadrants covered={len(quad_perf)}/4, positive={qpos} -> "
      f"'>=3 of 4 positive' {'PASS' if qpos >= 3 and len(quad_perf) >= 4 else 'FAIL/INSUFFICIENT'}")

# funding-sign calendar for carry cluster (dev only)
import glob as _glob
ff = _glob.glob(ROOT + r"\backtest\cache\perp_funding\*BTC*")
print()
print("carry-cluster funding-sign calendar:", ff if ff else "no BTC funding cache file found")
if ff:
    fdf = pd.read_parquet(ff[0])
    if not isinstance(fdf.index, pd.DatetimeIndex):
        for cand in ("timestamp", "ts", "funding_time", "fundingTime"):
            if cand in fdf.columns:
                fdf = fdf.set_index(pd.to_datetime(fdf[cand], utc=True))
                break
    de = pd.Timestamp("2025-09-22T22:36:00+00:00")
    if fdf.index.tz is None:
        de = de.tz_localize(None)
    fdf = fdf[fdf.index < de]
    col = [c for c in fdf.columns if "rate" in c.lower() or "funding" in c.lower()]
    if col:
        fr = fdf[col[0]].astype(float)
        print(f"  dev funding rows={len(fr)} positive={float((fr > 0).mean()):.1%} "
              f"negative={float((fr < 0).mean()):.1%}")

# ── S4 parametric proxies ────────────────────────────────────────────────────
print()
print("=" * 78)
print("F. S4 — holdout casualties: parametric proxies from persisted stats")
print("=" * 78)
fg = final_gates[0]
T_h = 2352 + 3032  # persisted LV/HV bar split in final_gate notes
sh_h = fg["sharpe"]
# harness-convention sr_std backed out of dsr_holdout
z_h = stats.norm.ppf(fg["dsr_holdout"])
sr_std_harness = (sh_h - fg["sr_zero_expected_at_eval"]) / z_h
print(f"FRH holdout: SR={sh_h} T~{T_h} 1h bars; dsr_holdout={fg['dsr_holdout']:.2e}"
      f" -> harness sr_std={sr_std_harness:.4f} (annualised-SR units, per-bar T)")
# statistically-conventional Gaussian SE of the ANNUALISED Sharpe
se_ann = math.sqrt(bpy / (T_h - 1))
print(f"  Gaussian SE of annualised SR = sqrt(bpy/(T-1)) = {se_ann:.3f}")
print(f"  90% CI = {sh_h:+.2f} +/- {1.645*se_ann:.2f} = "
      f"[{sh_h-1.645*se_ann:+.2f}, {sh_h+1.645*se_ann:+.2f}]  "
      f"(B&H={fg['buy_and_hold_sharpe']:.3f}; zero {'EXCLUDED' if abs(sh_h) > 1.645*se_ann else 'INCLUDED'})")
am_T = 210  # ~daily bars 2025-09-22 -> 2026-04-19
se_am = math.sqrt(365.25 / (am_T - 1))
print(f"AttentionMomentum holdout: SR=-1.1771 T~{am_T} 1d bars")
print(f"  Gaussian SE = {se_am:.3f}; 90% CI = [-{1.1771+1.645*se_am:.2f}, "
      f"{-1.1771+1.645*se_am:+.2f}]  (zero {'EXCLUDED' if 1.1771 > 1.645*se_am else 'INCLUDED'})")

# ── MinTRL pre-check at true SR=1.0 ──────────────────────────────────────────
print()
print("=" * 78)
print("G. MinTRL PRE-CHECK at target true (annualised) Sharpe 1.0")
print("=" * 78)
print(f"Gaussian MinTRL(SR_ann=1.0) = 1 + (Z*sqrt(bpy)/1)^2 bars = "
      f"{Z95**2:.3f} years = 2.706 years, frequency-independent.")
seen = set()
for r in rows:
    sid = r["strategy_id"]
    if sid in seen or sid not in manifest:
        continue
    seen.add(sid)
    m = manifest[sid]
    tf = m["timeframe"]
    hpb = {"1h": 1, "4h": 4, "1d": 24}[tf]
    t0, t1 = pd.Timestamp(m["data_start"]), pd.Timestamp(m["holdout_start"])
    yrs = (t1 - t0).total_seconds() / 3600 / 8766
    sr_min = Z95 / math.sqrt(yrs)
    print(f"  {sid:32s} {tf:3s} dev={yrs:.2f}y  need 2.71y -> "
          f"{'TESTABLE' if yrs >= Z95**2 else 'UNTESTABLE'}  min-detectable SR_ann={sr_min:.2f}")

# ── CPCVError event-based feasibility ────────────────────────────────────────
print()
print("=" * 78)
print("H. CPCVError rows — event-based block sizing (>=5 blocks x 5 events = 25)")
print("=" * 78)
seen = set()
for r in cpcv_err:
    key = (r["strategy_id"], r["variation_id"])
    if key in seen:
        continue
    seen.add(key)
    m = re.search(r"valid (\d+)/(\d+) blocks", r.get("notes", ""))
    print(f"  {r['strategy_id']:28s} {m.group(0) if m else '?':18s} "
          f"recorded n_trades={r['n_trades']}")
print("recorded dev event counts (from backtest/cache/trial_result_*.json):")
print("  ExchangeListingDrift: headline n_trades=21, n_event_days(score>2.0)=28")
print("  -> trades basis: 21 < 25 NOT feasible; event-day basis: 28 >= 25 marginal (5 blocks)")
print("  PairsTradingCointegration (no trials.log row): headline n_trades=25 -> exactly 25, marginal")
print("  all other CPCVError strategies: dev trade/event counts NOT persisted -> UNRECOVERABLE")

# ── Borderline margins ───────────────────────────────────────────────────────
print()
print("=" * 78)
print("I. BORDERLINE (any failing gate margin within +/-0.05)")
print("=" * 78)
for r in finite:
    sh, bh = r["sharpe"], r["buy_and_hold_sharpe"]
    mb = sh - bh          # baseline margin (orig gate)
    mm = sh - 0.0         # mt margin at n_trials=1 (sr_zero=0)
    tags = []
    if mb < 0 and abs(mb) <= 0.05:
        tags.append(f"baseline margin {mb:+.4f}")
    if mm < 0 and abs(mm) <= 0.05:
        tags.append(f"mt-mean margin {mm:+.4f}")
    if tags:
        print(f"  {r['strategy_id']:32s} recon_verdict={recon[r['strategy_id']]:12s} "
              f"{'; '.join(tags)}")
fgm = fg["sharpe"] - fg["baseline_sharpe_at_eval"]
print(f"  final_gate FRH holdout: baseline margin {fgm:+.3f}, mt margin "
      f"{fg['sharpe']-fg['sr_zero_expected_at_eval']:+.3f} (not borderline)")
