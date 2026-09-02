"""
scratch/backfill_and_supersede_2026_06_11.py — work-order item 7a + 7b.

7a: Backfill the missing AttentionMomentum final_gate row via
    backtest.trials.record_trial (the sanctioned writer; NOT a manual
    file edit).  The 2026-05-08 holdout evaluation consumed holdout
    access (holdout_access.log lines 1-3) but never appended a
    final_gate row — the result lived only in
    research/attention-momentum-literature.md (audit §6 defect 1).

7b: Tag the 7 duplicate ExchangeListingDrift CPCVError rows (keeping
    the first occurrence, ts=2026-05-08T21:08) with
    superseded_by="duplicate-cron-retry-2026-05-08".  Scoped
    trials.log content edit pre-authorized in the 2026-06-11 work
    order.  String-level insertion before the closing brace so every
    untouched line stays byte-identical; each modified line is
    re-parsed to verify validity.

Run once from the repo root.  Idempotence guards on both steps.
"""
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backtest import trials as _trials  # noqa: E402

LOG = Path("backtest/trials.log")
SUPERSEDE_TAG = "duplicate-cron-retry-2026-05-08"

rows = [json.loads(l) for l in LOG.read_text(encoding="utf-8").splitlines() if l.strip()]

# ── 7a. Backfill AttentionMomentum final_gate ────────────────────────────────

already = [
    r for r in rows
    if r["strategy_id"] == "AttentionMomentum"
    and r["trial_type"] == "final_gate"
]
if already:
    print(f"7a SKIP: final_gate row already present ({already[0]['trial_id']})")
else:
    dev = [
        r for r in rows
        if r["strategy_id"] == "AttentionMomentum"
        and r["trial_type"] == "full_cpcv"
    ]
    assert len(dev) == 1, f"expected exactly 1 dev row, got {len(dev)}"
    dev = dev[0]
    assert dev["trial_id"] == "8c1278b5d259411b969260a61d5cea28"

    event = {
        "strategy_id": "AttentionMomentum",
        "variation_id": dev["variation_id"],          # search-volume-momentum
        "trial_type": "final_gate",
        "params": dev["params"],
        "hypothesis": dev["hypothesis"],
        "split_holdout_start": dev["split_holdout_start"],
        "symbols": dev["symbols"],
        "n_trades": 14,
        "sharpe": -1.1771,
        # cpcv block + dsr_validation inherited verbatim from the prior
        # dev full_cpcv row, mirroring runner._run_strategy_final_gate.
        "cpcv": dev["cpcv"],
        "dsr_validation": dev["dsr_validation"],
        # dsr_holdout was never computed on the 2026-05-08 run
        # (literature records "n/a"); NaN is the honest value.
        "dsr_holdout": float("nan"),
        "verdict": "retire",
        # Preconditions: 14 trades < 30 floor -> False.  MinTRL was not
        # computed on the holdout run; False mirrors verdict.py's
        # not-computable convention (DSRError fold), not a computed fail.
        "trade_count_pass": False,
        "mintrl_pass": False,
        # Quality bools (schema requires bools for verdict=retire):
        # mt_mean_pass False is true under any sr_zero >= 0 (sharpe is
        # negative).  baseline_pass False basis: BTC B&H over the
        # overlapping FRH holdout window persisted at -0.8958 and
        # -1.1771 < -0.8958; the AttentionMomentum-window B&H itself
        # was never persisted.
        "mt_mean_pass": False,
        "baseline_pass": False,
        "sr_zero_expected_at_eval": float("nan"),
        "mintrl_required_at_eval": float("nan"),
        "baseline_sharpe_at_eval": float("nan"),
        "total_trades": 14,
        "buy_and_hold_sharpe": None,
        "mintrl": None,
        "notes": (
            "BACKFILL 2026-06-11 (gate-recalibration audit §6, "
            "record-keeping defect 1): the 2026-05-08 holdout "
            "evaluation of search-volume-momentum consumed holdout "
            "access but appended no final_gate row; the result was "
            "persisted only in research/attention-momentum-literature.md "
            "(outcome row: retire_holdout, sr -1.1771, n_trades 14, "
            "2026-05-08). Sources: backtest/holdout_access.log "
            "AttentionMomentum entries ts=2026-05-08T03:04:07.743093 "
            "(first attempt, aborted on Google-Trends 429), "
            "ts=2026-05-08T03:12:05.743402 (regenerated=true, "
            "old==new holdout_start, access-flag reset), "
            "ts=2026-05-08T03:13:03.624368 (successful re-run, "
            "n_rows=1130, git_commit=a2940b6); "
            ".memory/T1_episodic/2026-05-08/sq-018-keep-verdict-tree.md. "
            "cpcv + dsr_validation inherited verbatim from dev trial "
            "8c1278b5d259411b969260a61d5cea28 per the final_gate "
            "convention (NOTE: dsr_validation is units-invalid per "
            "validation_framework.md § Gate spec v2). dsr_holdout NaN: "
            "never computed on the original run. mintrl_pass=False "
            "records not-computed (verdict.py DSRError convention), "
            "not a computed fail. baseline_pass=False basis: holdout "
            "sharpe -1.1771 < BTC B&H -0.8958 persisted for the "
            "overlapping FRH holdout window; the exact same-window B&H "
            "was never persisted. ts of this row is the backfill "
            "append time, NOT the original run time (2026-05-08) — "
            "deliberate, so the append history stays honest."
        ),
    }
    _trials.record_trial(event)
    print(f"7a DONE: final_gate backfill appended, trial_id={event['trial_id']}")

# ── 7b. Supersession tags on the 7 duplicate ELD rows ───────────────────────

lines = LOG.read_text(encoding="utf-8").splitlines(keepends=False)
eld_first_seen = False
modified = 0
out_lines: list[str] = []
for line in lines:
    if not line.strip():
        out_lines.append(line)
        continue
    row = json.loads(line)
    is_eld_err = (
        row.get("strategy_id") == "ExchangeListingDrift"
        and row.get("variation_id") == "major-exchange-listing-announcement"
        and row.get("trial_type") == "full_cpcv"
        and isinstance(row.get("sharpe"), float)
        and math.isnan(row["sharpe"])
    )
    if not is_eld_err:
        out_lines.append(line)
        continue
    if not eld_first_seen:
        eld_first_seen = True          # keep the first occurrence untagged
        out_lines.append(line)
        continue
    if row.get("superseded_by"):
        out_lines.append(line)          # idempotence
        continue
    assert line.rstrip().endswith("}"), "unexpected line shape"
    new_line = (
        line.rstrip()[:-1]
        + f', "superseded_by": "{SUPERSEDE_TAG}"}}'
    )
    parsed = json.loads(new_line)       # verify the edit parses
    assert parsed["superseded_by"] == SUPERSEDE_TAG
    out_lines.append(new_line)
    modified += 1

if modified:
    LOG.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
print(f"7b DONE: tagged {modified} duplicate ExchangeListingDrift rows "
      f"(expected 7 on first run, 0 after)")

# Post-check.
rows2 = [json.loads(l) for l in LOG.read_text(encoding="utf-8").splitlines() if l.strip()]
n_fg = sum(1 for r in rows2 if r["strategy_id"] == "AttentionMomentum"
           and r["trial_type"] == "final_gate")
n_tagged = sum(1 for r in rows2 if r.get("superseded_by") == SUPERSEDE_TAG)
print(f"post-check: AttentionMomentum final_gate rows={n_fg} (want 1); "
      f"rows tagged {SUPERSEDE_TAG!r}={n_tagged} (want 7); "
      f"total rows={len(rows2)} (want 39)")
