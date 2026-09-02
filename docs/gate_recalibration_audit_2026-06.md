# Gate-recalibration audit — 2026-06

**Date:** 2026-06-11
**Scope:** READ-ONLY recomputation of all 38 `backtest/trials.log` verdicts
under the v2-candidate gate spec (fixed ex ante in the audit prompt; outcomes
computed, not tuned). No production edits, no trials.log appends, no holdout
access, no strategy re-runs.
**Data sources:** `backtest/trials.log` (38 rows), `backtest/holdout_manifest.json`,
`backtest/trial_queue_state.json` + `backtest/trial_queue.json`,
`backtest/cache/` (dev-window slices only; every cache read was truncated
strictly before each strategy's `holdout_start`), `backtest/cache/trial_result_sq-*.json`,
`research/*-literature.md` outcome rows, `.memory/T1_episodic/2026-05-08/sq-018-keep-verdict-tree.md`.
**Analysis scripts:** `scratch/gate_recalc.py` (pre-existing), `scratch/gate_recalc_v2.py`
(this audit). Both throwaway; outputs reproduced below.

---

## 1. Verification of CONTEXT bullets against code

| # | Claim | Verdict | Evidence |
|---|-------|---------|----------|
| 1 | `dsr.py` computes `sr_zero_expected` via BLP eq.7 Gumbel term WITHOUT the `sqrt(V[{SR_n}])` cross-trial variance scaling (implicit variance = 1.0) | **CONFIRMED** | `backtest/dsr.py:181-185` — `sr_zero_expected = (1-γ)·Φ⁻¹(1-1/N) + γ·Φ⁻¹(1-1/(N·e))`, no variance factor. At N=1 it is hard-set to 0.0 (`dsr.py:174-179`). |
| 2a | n_trials is per-strategy via `count_trials_for_dsr(strategy_id)` at runner.py:749 | **CONFIRMED** | `backtest/runner.py:749`; counting logic at `backtest/trials.py:607-632` (per-strategy, full_cpcv+final_gate, smoke and superseded excluded). |
| 2b | Phase 3c rescue path used fixed `RESCUE_TRIAL_BUDGET=20` | **CONFIRMED** | `rescue/policy.py:51`; applied at `backtest/runner.py:1126-1127`. |
| 2c | May 2026 trial scripts used per-strategy count + 1 (= 1 for first variations ⇒ `sr_zero_expected = 0.0`, MT gate a no-op) | **CONFIRMED** | `scripts/run_attention_momentum_trial.py:332,371` (`n_trials=n_trials_pre + 1`); `scripts/phase_4b_full_cpcv_v1.py:410,419`; `scripts/phase_4b_holdout_v1.py:338-339`. Cross-checked numerically: the FRH final_gate row persists `sr_zero_expected_at_eval = 0.519755` = Gumbel(N=2) exactly (pre-count 1 + 1). Note: the T1 memo `.memory/T1_episodic/2026-05-08/sq-018-keep-verdict-tree.md` (line 49) claims `n_trials_for_dsr = 13` for sq-018 — that memo is **wrong**; the script source and the saturated dsr both confirm the per-strategy count. |
| 3 | verdict.py: keep iff `mt_mean_pass AND baseline_pass`; `beats_baseline()` strict raw-Sharpe comparison | **CONFIRMED** | `backtest/verdict.py:276-279`; `backtest/baseline.py:184-197` (`strategy_sr > baseline_sr`, strict). Preconditions (trade-count floor 30 / signal-event floor 30, MinTRL) route to `under_tested` first (`verdict.py:246`). |
| 4 | Dev-window BTC B&H Sharpe ≈ 1.94 | **CONFIRMED** | Recomputed from the dev slice of `backtest/cache/ohlcv/BTC-USDT_1d_38mo.parquet` (2023-03-07 → 2025-09-21, 930 bars) with the `baseline.py` formula: **1.9391**, matching the `buy_and_hold_sharpe` field persisted in every 1d trial row. Minor correction: the 1d dev window starts 2023-03, not 2023-04 (2023-04 applies to the 1h substrates). |
| 5 | ~12/38 trials died as CPCVError, all low-frequency designs | **PARTIALLY WRONG (count)** | Actual: **15 of 38 rows** have NaN sharpe / n_trades=0, covering **8 unique (strategy, variation) pairs** — ExchangeListingDrift alone has **8 duplicate CPCVError rows** (2026-05-08 21:08 → 2026-05-09 00:03, a cron retry loop; queue status is still `needs_rerun`, not retired). "~12" matches neither figure. "All low-frequency" is confirmed: all 8 unique failures are 1d designs. The 8 duplicate rows also inflate any program-level N by 7 junk draws — flagged for (human-gated) supersession review. |
| 6 | Two dev-passers retired on holdout, each n_trades=14: AttentionMomentum (dev KEEP, holdout −1.1771) and FundingRateHarvest_BTC V2b (dev +2.90, holdout −1.14 vs B&H −0.90) | **CONFIRMED** | FRH: trials.log final_gate row (trial_id `5bedac34…`, sharpe −1.14, n_trades 14, `baseline_sharpe_at_eval` −0.8958). AttentionMomentum: holdout sharpe −1.1771 / n_trades 14 is persisted **only** in `research/attention-momentum-literature.md:93` — there is **no final_gate row in trials.log** despite holdout access being consumed (`backtest/holdout_access.log` lines 1–3). Record-keeping gap flagged below. |

**Additional finding (units inconsistency, affects every persisted dsr/mintrl):**
`deflated_sharpe` and `min_track_record_length` receive the engine's
**annualised** headline Sharpe (`engine.py:93`, "Annualised Sharpe (rf = 0)")
but compute the BLP eq.9 variance from **per-bar** returns and per-bar T
(`dsr.py:161-165`, `dsr.py:375`). BLP eq.9/13 require SR in the same
per-period units as the returns. Plugging an annualised SR into a per-bar
formula inflates the z-score by roughly √(bars-per-year) (≈19× at 1d, ≈94×
at 1h). This is why `dsr_validation` saturates to 1.0 on every positive-Sharpe
trial and why persisted MinTRL values are absurdly small (e.g. 5.95–20 bars).
Verifiable from persisted data alone: the FRH final_gate `dsr_holdout =
1.36e-13` back-solves to sr_std = 0.227, i.e. vt ≈ 275 — only reachable by
mixing annualised SR into the per-bar moment formula. Consequence: the
original MT gate was doubly inert (N=1 ⇒ sr_zero=0, AND saturated z), and
`mintrl_pass` was trivially true for every trial. Both the original gates and
the harness-convention S2 columns below inherit this; the units-correct
columns are the statistically meaningful ones.

---

## 2. Flip table

### Method and recoverability

- **Original verdict** = recorded verdict (trial_queue_state.json /
  literature outcome rows / final_gate row). Reconstruction from persisted
  fields (preconditions + quality gates) matches all 22 recorded verdicts
  bit-for-bit (scratch/gate_recalc_v2.py §B), including the two
  `under_tested` rows (DailyCrossSectionalReversal via MinTRL,
  CryptoDualMomentum via the 30-trade floor).
- **S1** (OLS alpha at 95% + IR ≥ 0.5 vs same-instrument B&H): requires
  per-bar strategy return series. These are **not persisted anywhere**
  (trials.log keeps only distribution quantiles; backtest/reports/ holds
  only pre-Phase-4 runs). **UNRECOVERABLE for all 20 directional rows**
  without re-running backtests (forbidden — would be new trials).
  Exception: FundingRateHarvest_BTC (delta-neutral track) needs only
  PSR > 0 at 95%, computable from persisted SR + window length:
  z = 4.48 Gaussian; still z = 3.86–4.39 under every skew/kurtosis
  combination consistent with the persisted mintrl ⇒ **PASS** (robust).
- **S2** = `sr_zero = sqrt(V[{SR_n}]) × Gumbel(N)`, V over observed Sharpes
  in the family cluster (taxonomy in Appendix A; 21 finite non-superseded
  full_cpcv rows). Two layers per spec: cluster-N and program layer
  (N_eff = 21, sr_zero_prog = 2.644). DSR ≥ 0.95 evaluated with the
  units-correct Gaussian SE of the annualised Sharpe, SE = sqrt(bpy/(T−1))
  ≈ 0.63–0.65 (per-bar moment corrections are O(sr_pb) ≈ 0.03 and ignorable
  at this precision). Single-member clusters (seasonality, micro/flow,
  other) have V = 0 ⇒ sr_zero = 0 — the corrected gate is degenerate there
  by construction; flagged per row.
- **S3** (vol-tercile × trend-sign sub-windows): per-block CPCV Sharpes are
  persisted **only for AttentionMomentum** (`research/attention-momentum-literature.md:98`);
  all other rows persist only distribution quantiles ⇒ **UNRECOVERABLE**.
  AttentionMomentum result below. Regime calendar itself: Appendix C.
- **Net verdict** legend: `RETIRE` (fails S2-cluster — confirmed dead under
  v2), `COND-FLIP` (passes S2-cluster; S1/S3 unrecoverable, flip cannot be
  confirmed from persisted data), `UNDER_TESTED` (precondition fail —
  unchanged), `RETIRE-CPCV` (no statistics exist; see §3), `RETIRE-HOLDOUT`
  (holdout result stands; see §6/S4 caveat).

### Finite full_cpcv rows (21, non-superseded)

| strategy (cluster) | SR | orig verdict | S1 | S2 srz-cl / DSR-cl | S2-cl | S2 prog DSR | S2-prog | S3 | net (v2) |
|---|---|---|---|---|---|---|---|---|---|
| MeanReversion_BTC_Residual (rev) | +0.851 | retire | UNREC | 1.718 / 0.091 | fail | 0.003 | fail | UNREC | RETIRE |
| IdiosyncraticResidualTSMOM (tsmom) | +0.386 | retire | UNREC | 0.259 / 0.578 | fail | 0.000 | fail | UNREC | RETIRE |
| CrossSectionalMomentum (csmom) | +1.642 | retire | UNREC | 0.525 / 0.963 | **PASS** | 0.055 | fail | UNREC | **COND-FLIP** |
| CrossSectionalReversal (rev) | −0.108 | retire | UNREC | 1.718 / 0.002 | fail | 0.000 | fail | UNREC | RETIRE |
| IntradayMomentumReversal (rev) | −3.050 | retire | UNREC | 1.718 / 0.000 | fail | 0.000 | fail | UNREC | RETIRE |
| IntradayJumpReversal (rev) | −1.097 | retire | UNREC | 1.718 / 0.000 | fail | 0.000 | fail | UNREC | RETIRE |
| AttentionMomentum (sent) | +2.106 | keep→retire_holdout | UNREC | 0.162 / 0.999 | **PASS** | 0.195 | fail | FAIL¹ | RETIRE-HOLDOUT² |
| ShortTermCrossSectionalMomentum (csmom) | +0.961 | retire | UNREC | 0.525 / 0.756 | fail | 0.004 | fail | UNREC | RETIRE |
| OvernightSessionReversal (rev) | −1.877 | retire | UNREC | 1.718 / 0.000 | fail | 0.000 | fail | UNREC | RETIRE |
| DayOfWeekSeasonality (seas, N=1³) | +0.757 | retire | UNREC | 0.000³ / 0.886 | fail | 0.001 | fail | UNREC | RETIRE |
| DailyCrossSectionalReversal (rev) | −0.017 | under_tested | UNREC | 1.718 / 0.003 | fail | 0.000 | fail | UNREC | UNDER_TESTED |
| HurstExponentRegimeSwitch (tsmom) | −0.610 | retire | UNREC | 0.259 / 0.090 | fail | 0.000 | fail | UNREC | RETIRE |
| CrossSectionalResidualReversal (rev) | −0.354 | retire | UNREC | 1.718 / 0.000 | fail | 0.000 | fail | UNREC | RETIRE |
| AltcoinSeasonRotation (csmom) | +1.677 | retire | UNREC | 0.525 / 0.967 | **PASS** | 0.061 | fail | UNREC | **COND-FLIP** |
| CryptoSectorRotation (csmom) | +0.774 | retire | UNREC | 0.525 / 0.654 | fail | 0.001 | fail | UNREC | RETIRE |
| FundingRateHarvest_BTC V2b (carry) | +2.899 | keep→retire (final_gate) | **PASS** | 0.469 / 1.000 | **PASS** | 0.653 | fail | UNREC⁴ | RETIRE-HOLDOUT² |
| CryptoDualMomentum (csmom) | +1.901 | under_tested | UNREC | 0.525 / 0.986 | PASS⁵ | 0.118 | fail | UNREC | UNDER_TESTED⁵ |
| LiquidityConditionedReversal (rev) | +0.129 | retire | UNREC | 1.718 / 0.006 | fail | 0.000 | fail | UNREC | RETIRE |
| NewsSentimentMomentum (sent) | +1.484 | retire | UNREC | 0.162 / 0.983 | **PASS** | 0.032 | fail | UNREC | **COND-FLIP** |
| DEXFlowSpillover (micro, N=1³) | −0.742 | retire | UNREC | 0.000³ / 0.125 | fail | 0.000 | fail | UNREC | RETIRE |
| CrossSectionalFundingRateCarry (carry) | +1.096 | retire | UNREC | 0.469 / 0.842 | fail | 0.007 | fail | UNREC | RETIRE |

¹ AttentionMomentum S3 (only evaluable row): its 10 CPCV blocks map to dev
deciles covering only 2 of the 4 vol×trend quadrants (hiV-up mean +6.40 over
3 blocks, driven by one +21.1 block; loV-up mean +2.59 over 4 blocks; both
down-trend quadrants have no finite block). "Positive in ≥ 3 of 4
sub-windows" **cannot be cleared — insufficient regime coverage**, itself a
finding: the dev window is so bull-dominated (614 of 840 post-warmup BTC 1d
bars are trend-up; see Appendix C) that the S3 gate is structurally
unsatisfiable on this dev window for most designs.
² Holdout retire stands as recorded, but see §6/S4: neither holdout Sharpe is
statistically distinguishable from zero on the persisted summary stats.
³ Single-member cluster ⇒ V=0 ⇒ corrected sr_zero degenerates to 0; the
cluster-layer S2 for these rows is a Sharpe>0-with-DSR≥0.95 test only.
⁴ FRH S3-carry (funding-sign split) needs per-settlement strategy returns —
not persisted. Dev funding-sign calendar: 78.7% positive / 21.3% negative
(2,696 dev settlements, cache, dev slice only).
⁵ Passes S2-cluster on the recorded Sharpe but the under_tested precondition
(15 trades < 30 floor) is unchanged by the v2 spec; stays UNDER_TESTED.

### Non-finite rows (17)

| rows | strategy | orig verdict | S1/S2/S3 | net (v2) |
|---|---|---|---|---|
| 1 | IlliquidityPremium | retire (CPCVError, 2/10 valid blocks) | no statistics exist | RETIRE-CPCV → see §3 |
| 1 | SocialSentimentMomentum | retire (CPCVError, 0/10) | — | RETIRE-CPCV |
| 1 | ContrarianSearchVolume | retire (CPCVError, 1/10) | — | RETIRE-CPCV |
| 1 | VolumeWeightedTSMOM | retire (CPCVError, 0/10) | — | RETIRE-CPCV |
| 1 | CrossSectionalSkewness | retire (CPCVError, 0/7) | — | RETIRE-CPCV |
| 1 | VolatilityScaledTSMOM | retire (CPCVError, 1/10) | — | RETIRE-CPCV |
| 1 | PutCallRatioContrarian | retire (CPCVError, 0/10) | — | RETIRE-CPCV |
| 8 | ExchangeListingDrift (7 duplicates) | queue `needs_rerun` | — | RETIRE-CPCV; duplicates flagged for supersession review (human-gated) |
| 1 | FundingRateHarvest_BTC V2b dup (row 22 of 38, `superseded_by` set) | superseded | excluded from all N | n/a |
| 1 | FundingRateHarvest_BTC V2b final_gate | retire | S4: see §6 | RETIRE-HOLDOUT |

---

## 3. CPCVError section — event-based block sizing feasibility

Per-block validity rule: a block contributes only if its event count ≥
`_MIN_EVENTS_PER_BLOCK = 5` (`backtest/cpcv_common.py:62`, applied at
`cpcv.py:327-330`); CPCVError fires when valid blocks ≤ 50%
(`cpcv.py:341-349`). Under event-based sizing (block = fixed signal-event
count, min 5 blocks), a strategy is testable iff its **total dev events ≥
5 blocks × 5 events = 25**.

The blocker: trials.log records `n_trades = 0` for all CPCVError rows (the
trial scripts record the CPCVError, not the headline run), and the
literature outcome rows echo the zero. Headline dev trade counts survive
only where a `trial_result_*.json` was cached:

| strategy | valid blocks | recorded dev events | ≥ 25? | feasibility |
|---|---|---|---|---|
| ExchangeListingDrift | 0/10 | **21 trades; 28 event-days (score>2.0)** (`backtest/cache/trial_result_sq-037.json`) | trades: NO (21) / event-days: marginal (28) | Trade basis: NOT testable (would give 4 blocks of 5). Signal-event basis: **marginally testable** — exactly 5 blocks of 5 with 3 spare; distribution statistics on 5 blocks would be extremely weak. |
| IlliquidityPremium | 2/10 | not persisted | ? | **UNRECOVERABLE.** Lower bound from block structure: ≥ 2 blocks held ≥ 5 events ⇒ ≥ 10+; insufficient to decide vs 25. |
| ContrarianSearchVolume | 1/10 | not persisted | ? | UNRECOVERABLE (lower bound ≥ 5) |
| VolatilityScaledTSMOM | 1/10 | not persisted | ? | UNRECOVERABLE (lower bound ≥ 5) |
| SocialSentimentMomentum | 0/10 | not persisted | ? | UNRECOVERABLE; 0 valid blocks ⇒ every decile had < 5 events ⇒ total < 50, could still be ≥ 25 |
| VolumeWeightedTSMOM | 0/10 | not persisted | ? | UNRECOVERABLE (same bound: 25 ≤ ? < 50 undecidable) |
| CrossSectionalSkewness | 0/7 | not persisted | ? | UNRECOVERABLE (total < 35 implied; vs 25 undecidable) |
| PutCallRatioContrarian | 0/10 | not persisted | ? | UNRECOVERABLE |

Note: 0/10 valid blocks DOES bound total events < 50 (each of 10 deciles
< 5); 25 ≤ total < 50 cases would become testable event-based, but the
persisted record cannot distinguish them from total < 25. Determining the
actual counts requires re-running the signal computation — out of scope for
this audit (would be new trials under the discipline rules; flagged as a
candidate for a human-approved diagnostic batch, since signal-event counting
alone does not produce a Sharpe and arguably consumes no trial budget — a
decision for the human, not this audit).

Bonus recovery: `trial_result_sq-005.json` (PairsTradingCointegration,
crashed before any trials.log row) records headline n_trades = 25 — exactly
the event-based minimum.

---

## 4. MinTRL pre-check at target true Sharpe 1.0

With SR expressed per-bar (units-correct), BLP eq.13 at annualised SR 1.0
reduces to MinTRL ≈ (1.6449)² × bars-per-year = **2.706 years of history,
frequency-independent** (per-bar moment corrections are negligible at
sr_pb ≤ 0.05). The check is therefore a pure calendar-length test:

| substrate group | dev span | years | MinTRL @ SR 1.0 | verdict | min detectable SR_ann |
|---|---|---|---|---|---|
| 1d (2023-03-06/07 → 2025-09-22): all 19 1d strategies | 931 d | 2.55 | 2.71 y | **UNTESTABLE** | 1.03 |
| 1h legacy (2023-04-20 → 2025-09-12): IntradayMomentumReversal, IntradayJumpReversal, OvernightSessionReversal, DEXFlowSpillover | 876 d | 2.40 | 2.71 y | **UNTESTABLE** | 1.06 |
| 1h (2023-05-03 → 2025-09-22): FundingRateHarvest_BTC | 873 d | 2.39 | 2.71 y | **UNTESTABLE** | 1.06 |
| 1h (2023-04-30 → 2025-09-13): IdiosyncraticResidualTSMOM | 866 d | 2.37 | 2.71 y | **UNTESTABLE** | 1.07 |
| 4h (2023-04-30 → 2025-09-13): MeanReversion_BTC_Residual, HurstExponentRegimeSwitch | 866 d | 2.37 | 2.71 y | **UNTESTABLE** | 1.07 |

**Every strategy in the program is structurally untestable at true Sharpe
1.0 on its ~29-month dev window at 95% confidence.** The dev windows can
confidently detect only true annualised Sharpes ≥ 1.03–1.07. Designs whose
realistic edge is below ~1.0 annualised cannot be validated on this data
length no matter the frequency — more bars at higher frequency do not help,
because the requirement is calendar time. (The persisted per-row `mintrl`
values of 6–286 bars are artifacts of the units mismatch in §1 and say
nothing about this.)

---

## 5. Borderline-consult violations (±0.05 failing margin)

CLAUDE.md requires consulting the human on borderline retire/keep calls.
Scanning every failing gate margin on rows that reached (or would have
reached) the quality gates:

| strategy | recorded verdict | gate | margin | violation? |
|---|---|---|---|---|
| **MeanReversion_BTC_Residual** | retire (2026-05-05) | baseline (0.8505 vs B&H 0.8916) | **−0.0411** | **YES — new finding.** Retired without consult on a margin inside ±0.05. Same class as the known CryptoDualMomentum case. |
| CryptoDualMomentum | **under_tested** (sq-032) | baseline (1.9006 vs 1.9391) | −0.0385 | Known case from the prompt. Nuance: the recorded verdict was `under_tested` (15 trades < 30 floor), not retire — the borderline baseline margin was never the operative gate. If treated as retired in MASTER_PLAN/strategies.md, that doc-level retire is the borderline call that skipped consult. |
| DailyCrossSectionalReversal | under_tested (sq-026) | mt-mean (−0.0166 vs sr_zero 0) | −0.0166 | Margin inside ±0.05 on the MT gate, but the operative verdict was `under_tested` via MinTRL (9,824 > T≈931), which is not margin-based. No consult violation under the rule as written; listed for completeness. |
| FundingRateHarvest_BTC final_gate | retire | baseline −0.244; mt −1.660 | — | Not borderline. |
| AttentionMomentum holdout | retire_holdout | raw SR −1.1771 | — | Not borderline (also analysed in the T1 memo). |

Net: **one clear unreported violation (MeanReversion_BTC_Residual)** plus
the known CryptoDualMomentum case (with the verdict-status nuance above).

---

## 6. One-page summary

**What flips (conditionally).** Three strategies retired purely for not
beating BTC buy-and-hold (1.94) in a bull dev window pass the corrected
multiple-testing gate at the family-cluster layer (sr_zero scaled by
realized cluster Sharpe variance, DSR ≥ 0.95 units-correct):
**CrossSectionalMomentum (DSR 0.963), AltcoinSeasonRotation (0.967),
NewsSentimentMomentum (0.983)**. None can be promoted from persisted data
alone: S1 (alpha/IR vs B&H) requires per-bar strategy returns that were
never persisted — confirming a flip requires a human-approved re-run, which
would be a new trial. They are **candidate resurrections, not flips-in-fact**.
CryptoDualMomentum also clears S2-cluster (0.986) but remains `under_tested`
on its 15-trade precondition.

**What stays retired.** All 11 negative-or-near-zero-Sharpe rows fail S2 at
both layers and stay retired. CryptoSectorRotation (0.654),
ShortTermCrossSectionalMomentum (0.756), CrossSectionalFundingRateCarry
(0.842), DayOfWeekSeasonality (0.886), IdiosyncraticResidualTSMOM (0.578),
MeanReversion_BTC_Residual (0.091) fail the DSR ≥ 0.95 bar at the cluster
layer. At the **program layer (N_eff = 21, sr_zero = 2.644) every strategy
in the book fails, including both dev-keeps** — under a program-wide
correction the entire Phase 4 batch produced zero survivors.

**Holdout casualties (S4, report-only).** Per-bar holdout return series are
not persisted for either casualty ⇒ stationary-bootstrap CI is
**BLOCKED: requires holdout re-access (human approval needed)**. What the
bootstrap would need: the per-bar holdout equity curves (~5,384 1h bars FRH;
~210 1d bars AttentionMomentum), Politis–Romano expected block length ~n^(1/3)
(≈17 and ≈6 bars), 90% percentile CI on the annualised Sharpe and on the
Sharpe-minus-B&H difference. Parametric Gaussian proxy from persisted
summary stats alone: FRH holdout SR −1.14 ± 2.10 (90% CI [−3.24, +0.96]);
AttentionMomentum −1.18 ± 2.17 (CI [−3.35, +1.00]). **Neither holdout
Sharpe is distinguishable from zero** — the 7-month holdout window is far
too short to retire a strategy on its own statistical evidence at these
frequencies; the retirements were effectively sign-of-Sharpe decisions on
14 trades each.

**Blocked on missing persisted data (UNRECOVERABLE without new runs).**
S1 for all 20 directional finite rows; S3 for all rows except
AttentionMomentum (per-block Sharpes persisted only there — and its S3
fails on regime coverage: the dev window has essentially no trend-down
sub-windows, making the ≥3-of-4 criterion unsatisfiable as specced);
dev event counts for 7 of 8 CPCVError strategies (§3). Only
ExchangeListingDrift is decidable: NOT testable on trades (21 < 25),
marginal on event-days (28 ≥ 25).

**Record-keeping defects surfaced.**
(1) AttentionMomentum's holdout evaluation consumed holdout access but
appended **no final_gate row to trials.log** — the result lives only in a
literature table; the multiple-testing count is silently understated by one.
(2) Eight duplicate ExchangeListingDrift CPCVError rows from a cron retry
loop inflate any program-level N; candidates for supersession tagging
(human-gated).
(3) Trial scripts discard the headline run's trade count when CPCV errors —
one extra field in the CPCVError row would have made §3 fully decidable.
(4) The dsr/mintrl units mismatch (§1, additional finding) means every
persisted `dsr_validation`/`mintrl` value is uninterpretable as a
probability/sample-size; any future gate spec should fix units before
recalibrating thresholds.

**Numbers at a glance.** 38 rows = 21 finite full_cpcv (non-superseded)
+ 1 superseded duplicate + 15 CPCVError rows (8 unique) + 1 final_gate.
Original verdicts: 2 dev-keep (both then failed holdout), 2 under_tested,
rest retire. v2-candidate: 3 COND-FLIP, 2 UNDER_TESTED unchanged,
14 RETIRE confirmed at cluster layer, 8 RETIRE-CPCV, 2 RETIRE-HOLDOUT
(statistically uninformative holdouts), 0 program-layer survivors.

---

## Appendix A — family-cluster taxonomy (ex ante, assigned by hand)

| cluster | members (finite-Sharpe trials in parens) |
|---|---|
| cs-momentum/rotation (N=5) | CrossSectionalMomentum, ShortTermCrossSectionalMomentum, AltcoinSeasonRotation, CryptoSectorRotation, CryptoDualMomentum |
| reversal (N=8) | MeanReversion_BTC_Residual, CrossSectionalReversal, DailyCrossSectionalReversal, CrossSectionalResidualReversal, IntradayMomentumReversal, IntradayJumpReversal, OvernightSessionReversal, LiquidityConditionedReversal, (PutCallRatioContrarian — CPCVError, no Sharpe) |
| carry (N=2) | FundingRateHarvest_BTC, CrossSectionalFundingRateCarry |
| trend/TSMOM (N=2 finite) | IdiosyncraticResidualTSMOM, HurstExponentRegimeSwitch, (VolumeWeightedTSMOM, VolatilityScaledTSMOM — CPCVError) |
| seasonality (N=1) | DayOfWeekSeasonality |
| sentiment/attention (N=2 finite) | AttentionMomentum, NewsSentimentMomentum, (SocialSentimentMomentum, ContrarianSearchVolume — CPCVError) |
| microstructure/flow (N=1 finite) | DEXFlowSpillover, (IlliquidityPremium, ExchangeListingDrift — CPCVError) |
| other (N=0 finite) | CrossSectionalSkewness (CPCVError) |

Cluster sr_zero (primary, finite-N): carry 0.469; cs-mom 0.525; reversal
1.718; trend 0.259; sentiment 0.162; seasonality/micro/other 0 (degenerate).
Sensitivity including unique CPCVError trials in N (V unchanged): reversal
1.791, sentiment 0.327, trend 0.524 — no row's pass/fail flips under the
sensitivity variant. Cross-family layer: 7 clusters with finite trials
(8 attempted): Gumbel(7)=1.387 / Gumbel(8)=1.459; with cross-family variance
of cluster means (√V=1.069): sr_zero = 1.48 / 1.56.

## Appendix B — S2 computation notes

sr_std for the DSR ≥ 0.95 test uses the Gaussian SE of the annualised
Sharpe, SE = sqrt(bars_per_year/(T−1)) with T = manifest dev-window bar
count (warmup ignored; ≤ 3% effect). Per-bar skew/kurt corrections cannot
be recovered exactly (the persisted mintrl mixes units, §1) but enter the
SE at O(skew·sr_pb) ≈ 0.03 relative — far below decision relevance except
for rows already at the 0.95 boundary (none within ±0.01 except
CrossSectionalMomentum at 0.963 and AltcoinSeasonRotation at 0.967, which
would need per-bar skew < −2 to flip; flagged as approximation-sensitive).
The harness-convention recomputation (annualised SR over per-bar T, matching
the production code's own units) saturates to DSR ≈ 1.0 for every
positive-margin row and is reported only in scratch output — it cannot
discriminate and is not used for the verdict columns.

## Appendix C — S3 regime calendar (dev window, BTC 1d, no holdout bars)

Ex-ante observables: 30-bar realised vol (annualised) terciles
(q33 = 0.378, q67 = 0.487) × sign of trailing 90-bar return. Occupancy over
2023-03-07 → 2025-09-21 (930 bars, 90 warmup): midV-up 236, loV-up 205,
hiV-up 173, hiV-dn 107, loV-dn 75, midV-dn 44. Trend-up bars: 614/840
(73%); the two down-trend corner quadrants together hold only 182 bars
(22%), in two short clusters (mid-2023, mid/late-2024). Consequence: a
10-block CPCV at 1d yields at most ~2 blocks dominated by down-trend
regimes, so the "positive in ≥ 3 of 4 sub-windows" criterion is
structurally out of reach on this dev window even with full per-block data.
Carry overlay: BTC perp funding sign over dev = 78.7% positive / 21.3%
negative (2,696 settlements) — a negative-funding sub-window exists and is
material; FRH per-regime performance is unrecoverable (no per-settlement
returns persisted), but the final_gate notes' persisted holdout vol split
(LV 43.7% / HV 56.3%) confirms the dev-calibrated LV gate was active for
under half the holdout window.

## Appendix D — AttentionMomentum block→regime map (approximation)

CPCV blocks assumed sequential deciles of the post-warmup dev frame
(warmup 29; manifest data_start 2023-03-06). Block spans and quadrant
assignment in scratch/gate_recalc_v2.py §E. Finite blocks: hiV-up
{+21.10, −0.40, −1.51}, loV-up {−0.49, +9.86, +0.75, +0.24}. The +2.11
headline is carried by two blocks (2023-10→2024-03 bull leg); 5 of 7
finite blocks are ≤ +0.75. Down-trend quadrants: no finite blocks (the
three NaN blocks include both hiV-dn-dominated spans — the strategy did
not trade enough there to produce a block Sharpe, consistent with the
holdout failure mode recorded in the T1 memo).
