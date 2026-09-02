"""
scratch/gate_recalc.py — Gate-recalibration audit (READ-ONLY).

Recomputes trials.log verdicts under the v2-candidate gate spec using
ONLY data persisted in trials.log. No holdout access, no strategy
re-runs, no new trial rows. Throwaway analysis per the 2026-06 audit.

What is computable from persisted data:
  - Original verdict reconstruction (baseline_pass via sharpe vs B&H;
    mt_mean_pass via sharpe>0 since every first variation ran n_trials=1).
  - S2 corrected sr_zero_expected = sqrt(V[{SR_n}]) * Gumbel(N), both at
    family-cluster level and program level (N_eff).
  - sr_std back-out from the original (n_trials=1) dsr_validation:
    dsr_orig = Phi(sharpe / sr_std)  =>  sr_std = sharpe / Phi^-1(dsr_orig).
  - Borderline ±0.05 baseline margins.
  - MinTRL back-out (variance_term from persisted mintrl + sharpe).

What is NOT computable (per-bar series absent):
  - S1 (alpha/IR OLS): needs per-bar strategy & B&H returns -> UNRECOVERABLE.
  - S3 (regime sub-windows): CPCV block Sharpes are not regime-labelled and
    only quantiles persist -> UNRECOVERABLE.
  - S4 (holdout bootstrap): per-bar holdout returns absent -> BLOCKED.
"""
import json
import math
import statistics
from scipy import stats

TRIALS = r"C:\crypto-bot\backtest\trials.log"
EULER = 0.5772156649015329


def gumbel_term(n: int) -> float:
    """BLP eq.7 bracket: (1-g)Z^-1(1-1/N) + g Z^-1(1-1/(N e)). N>=2."""
    if n < 2:
        return 0.0  # no multiplicity at N=1
    n = float(n)
    return ((1.0 - EULER) * stats.norm.ppf(1.0 - 1.0 / n)
            + EULER * stats.norm.ppf(1.0 - 1.0 / (n * math.e)))


# Ex-ante anomaly-mechanism taxonomy (documented; assigned by hand).
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


def load_rows():
    rows = []
    with open(TRIALS, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def main():
    rows = load_rows()
    print(f"total rows: {len(rows)}")

    # Classify rows.
    cpcv_err = [r for r in rows if (isinstance(r.get("sharpe"), float)
                                    and math.isnan(r["sharpe"]))
                or r.get("n_trades") == 0]
    finite_full = [r for r in rows
                   if r.get("trial_type") == "full_cpcv"
                   and isinstance(r.get("sharpe"), (int, float))
                   and not (isinstance(r["sharpe"], float) and math.isnan(r["sharpe"]))
                   and not r.get("superseded_by")]
    final_gate = [r for r in rows if r.get("trial_type") == "final_gate"]

    print(f"CPCVError rows (n_trades==0 / NaN sharpe): {len(cpcv_err)}")
    print("  by strategy:", {})
    from collections import Counter
    print("  ", dict(Counter(r["strategy_id"] for r in cpcv_err)))
    print(f"finite full_cpcv rows (non-superseded): {len(finite_full)}")
    print(f"final_gate rows: {len(final_gate)}")

    # --- Cluster Sharpe variance V[{SR_n}] ---
    by_cluster = {}
    for r in finite_full:
        c = CLUSTER[r["strategy_id"]]
        by_cluster.setdefault(c, []).append(r["sharpe"])

    all_sharpes = [r["sharpe"] for r in finite_full]
    V_prog = statistics.pvariance(all_sharpes)  # realized (population) variance
    N_eff = len(all_sharpes)
    srzero_prog = math.sqrt(V_prog) * gumbel_term(N_eff)

    # Cross-family reading of "N_eff = cluster count across the whole
    # program": N_eff = number of distinct clusters. Two variants reported.
    n_clusters_finite = len({CLUSTER[r["strategy_id"]] for r in finite_full})
    n_clusters_all = len(set(CLUSTER.values()))
    cluster_means = [statistics.mean(v) for v in by_cluster.values()]
    V_xfam = statistics.pvariance(cluster_means) if len(cluster_means) > 1 else 0.0
    print("\n=== CROSS-FAMILY N_eff variants ===")
    print(f"clusters with finite-sharpe trials = {n_clusters_finite}; "
          f"clusters attempted (incl CPCVError-only) = {n_clusters_all}")
    for ne in (n_clusters_finite, n_clusters_all):
        print(f"  N_eff={ne}: Gumbel={gumbel_term(ne):.4f}  "
              f"sr_zero(sqrtV_prog={math.sqrt(V_prog):.3f})={math.sqrt(V_prog)*gumbel_term(ne):.4f}  "
              f"sr_zero(sqrtV_xfam={math.sqrt(V_xfam):.3f})={math.sqrt(V_xfam)*gumbel_term(ne):.4f}")

    print("\n=== CLUSTER VARIANCE & sr_zero_expected (corrected) ===")
    print(f"program: N_eff={N_eff}  V={V_prog:.4f}  sqrtV={math.sqrt(V_prog):.4f}"
          f"  Gumbel(N_eff)={gumbel_term(N_eff):.4f}  sr_zero_prog={srzero_prog:.4f}")
    cluster_srzero = {}
    for c, sl in sorted(by_cluster.items()):
        N = len(sl)
        V = statistics.pvariance(sl) if N > 1 else 0.0
        g = gumbel_term(N)
        srz = math.sqrt(V) * g
        cluster_srzero[c] = (srz, N, V, g)
        print(f"  {c:24s} N={N} sharpes={[round(x,3) for x in sl]}")
        print(f"        V={V:.4f} sqrtV={math.sqrt(V):.4f} Gumbel(N)={g:.4f} "
              f"-> sr_zero_cluster={srz:.4f}")

    # --- Per-row recomputation ---
    print("\n=== FLIP TABLE (finite full_cpcv) ===")
    hdr = ("strat", "sharpe", "B&H", "dsr_orig", "orig_keep",
           "S2cl_z", "S2cl", "S2prog_z", "S2prog", "sr_std", "DSR_cl")
    print("{:28s} {:>7s} {:>6s} {:>9s} {:>9s} {:>7s} {:>5s} {:>8s} {:>6s} {:>7s} {:>7s}".format(*hdr))
    results = []
    for r in finite_full:
        sid = r["strategy_id"]
        sh = r["sharpe"]
        bh = r["buy_and_hold_sharpe"]
        dsr_orig = r.get("dsr_validation")
        c = CLUSTER[sid]
        srz_cl = cluster_srzero[c][0]
        # original verdict (n_trials=1 -> sr_zero=0): keep = sharpe>0 AND sharpe>bh
        orig_baseline = sh > bh
        orig_mt = sh > 0.0
        orig_keep = orig_baseline and orig_mt
        # S2 corrected mt-pass
        s2_cl = sh > srz_cl
        s2_prog = sh > srzero_prog
        # recover sr_std
        sr_std = None
        dsr_cl = None
        if dsr_orig is not None and 0.0 < dsr_orig < 1.0 and abs(sh) > 1e-12:
            z = stats.norm.ppf(dsr_orig)
            if abs(z) > 1e-9:
                sr_std = sh / z
                dsr_cl = float(stats.norm.cdf((sh - srz_cl) / sr_std))
        results.append(dict(sid=sid, cluster=c, sharpe=sh, bh=bh,
                            dsr_orig=dsr_orig, orig_keep=orig_keep,
                            orig_baseline=orig_baseline, orig_mt=orig_mt,
                            srz_cl=srz_cl, srz_prog=srzero_prog,
                            s2_cl=s2_cl, s2_prog=s2_prog,
                            sr_std=sr_std, dsr_cl=dsr_cl,
                            n_trades=r.get("n_trades"),
                            mintrl=r.get("mintrl")))
        print("{:28s} {:>7.3f} {:>6.3f} {:>9} {:>9} {:>7.3f} {:>5} {:>8.3f} {:>6} {:>7} {:>7}".format(
            sid[:28], sh, bh,
            ("sat" if dsr_orig in (1.0,) else (f"{dsr_orig:.1e}" if dsr_orig is not None else "NA")),
            str(orig_keep), srz_cl, str(s2_cl), srzero_prog, str(s2_prog),
            (f"{sr_std:.3f}" if sr_std is not None else "UNREC"),
            (f"{dsr_cl:.2f}" if dsr_cl is not None else "bound")))

    # --- Borderline ±0.05 on baseline margin ---
    print("\n=== BORDERLINE (|sharpe - B&H| <= 0.05) — finite full_cpcv ===")
    for r in finite_full:
        m = r["sharpe"] - r["buy_and_hold_sharpe"]
        if abs(m) <= 0.05:
            print(f"  {r['strategy_id']:30s} sharpe={r['sharpe']:.4f} "
                  f"B&H={r['buy_and_hold_sharpe']:.4f} margin={m:+.4f}")

    # --- MinTRL back-out: variance_term from persisted mintrl + sharpe ---
    # mintrl = 1 + vt*(Zalpha/|SR|)^2 ; Zalpha=1.6449 ; solve vt.
    print("\n=== MinTRL back-out (Z=1.6449) + rescale-to-SR=1.0 (Gaussian-bounded) ===")
    Z = stats.norm.ppf(0.95)
    for r in finite_full:
        sh = r["sharpe"]
        mt = r.get("mintrl")
        if mt is None or abs(sh) < 1e-9:
            continue
        vt = (mt - 1.0) / (Z / abs(sh)) ** 2
        # At target SR=1.0, vt depends on skew/kurt which we can't separate;
        # report Gaussian-floor MinTRL(1.0) = 1 + 1*(Z/1)^2 and the back-out vt.
        mintrl_at1_gauss = 1.0 + 1.0 * (Z / 1.0) ** 2
        print(f"  {r['strategy_id']:30s} SR={sh:+.3f} mintrl_persist={mt:9.2f} "
              f"vt_backed={vt:7.3f}  MinTRL@SR1(gauss)={mintrl_at1_gauss:.2f}  "
              f"n_trades={r.get('n_trades')}")

    # --- CPCVError block-sizing feasibility ---
    print("\n=== CPCVError rows — valid-block counts (event-based feasibility) ===")
    import re
    for r in cpcv_err:
        notes = r.get("notes", "")
        m = re.search(r"valid (\d+)/(\d+) blocks", notes)
        vb = m.group(0) if m else "?"
        print(f"  {r['strategy_id']:28s} {r['variation_id'][:34]:34s} n_trades={r.get('n_trades')} {vb}")


if __name__ == "__main__":
    main()
