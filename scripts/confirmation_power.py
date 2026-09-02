#!/usr/bin/env python
"""I2 — can the CONFIRMATION stage even conclude?

The pre-flight power gate (`.claude/rules/backtest.md`, discovery split item 5)
covers SCREENS. It has never been applied downstream. This asks the same
question of the confirmation and holdout windows: what true Sharpe would a
Phase 4.F strategy need before those windows are capable of validating it?

Computes nothing new about any strategy. It combines window lengths with the
project's OWN formulas, imported rather than reimplemented:

  - `backtest.dsr` for the BLP eq.7 expected-maximum (Gumbel) null;
  - `backtest.families` for the per-family trial count and V[{SR_n}].

Two bars a confirmation trial must clear:

  1. **MinTRL** — the minimum track record length. Inverted here: given the
     window, the minimum annualised Sharpe validatable at 95 % is
     `1.645 / sqrt(years)`. Below that, no amount of good luck in the data can
     make the record long enough to be significant.
  2. **The family multiple-testing null** `sr_zero = sqrt(V[SR]) x Gumbel(N)`.
     A candidate's Sharpe must exceed this before deflation leaves anything.

The binding constraint is the larger of the two. Writes no trials.log row and
touches no holdout data — it reads only window boundaries from the manifest.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pandas as pd
from scipy import stats

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from backtest.dsr import _EULER_MASCHERONI  # noqa: E402
from backtest.families import family_sharpe_stats  # noqa: E402
from backtest.holdout import load_manifest  # noqa: E402

PHASE_4F = ("FundingDispersionCarry", "DeleveragingReversal", "ListingFlow")
Z95 = 1.645


def gumbel_sr_zero(n_trials: int, sr_var: float) -> float:
    """BLP eq.7 expected maximum under the null — the SAME expression
    `backtest.dsr.deflated_sharpe` uses, lifted so it can be evaluated at
    hypothetical N without running a trial."""
    if n_trials <= 1:
        return 0.0
    n = float(n_trials)
    gumbel = ((1.0 - _EULER_MASCHERONI) * stats.norm.ppf(1.0 - 1.0 / n)
              + _EULER_MASCHERONI * stats.norm.ppf(1.0 - 1.0 / (n * math.e)))
    return math.sqrt(sr_var) * gumbel


def bars_per_year(timeframe: str) -> float:
    return {"1d": 365.0, "1h": 365.0 * 24, "4h": 365.0 * 6,
            "15m": 365.0 * 96, "1m": 365.0 * 1440}[timeframe]


def window_years(a: str, b: str) -> float:
    """Length in years. Both ends are coerced to UTC: manifest timestamps are
    tz-aware, the literal confirmation-window start is not."""
    ta, tb = pd.Timestamp(a), pd.Timestamp(b)
    ta = ta.tz_localize("UTC") if ta.tzinfo is None else ta.tz_convert("UTC")
    tb = tb.tz_localize("UTC") if tb.tzinfo is None else tb.tz_convert("UTC")
    return (tb - ta).days / 365.25


def main() -> int:
    man = load_manifest()
    fam_stats = family_sharpe_stats(PHASE_4F[0])
    sr_var = fam_stats.sr_var

    print("I2 — confirmation-stage power. No trials.log row; no holdout read.\n")
    print(f"family                : {fam_stats.family}")
    print(f"existing family trials: {fam_stats.n_trials}")
    print(f"V[{{SR_n}}]             : {sr_var:.4f}"
          f"{'  (1.0 FALLBACK — see caveat)' if fam_stats.used_fallback else ''}\n")

    rows = []
    for sid in PHASE_4F:
        e = man[sid]
        tf = e["timeframe"]
        dev_y = window_years("2023-01-01", e["dev_end"])
        hold_y = window_years(e["holdout_start"], e["data_end"])
        rows.append({
            "strategy": sid, "timeframe": tf,
            "dev_years": dev_y,
            "dev_bars": int(dev_y * bars_per_year(tf)),
            "dev_mintrl_sr": Z95 / math.sqrt(dev_y),
            "holdout_years": hold_y,
            "holdout_bars": int(hold_y * bars_per_year(tf)),
            "holdout_mintrl_sr": Z95 / math.sqrt(hold_y),
        })

    print("Window lengths and the MinTRL floor (min annualised SR validatable at 95 %):\n")
    print(f"  {'strategy':24s} {'tf':>4s} {'dev y':>6s} {'dev bars':>9s} "
          f"{'dev SR':>7s} {'hold y':>7s} {'hold bars':>10s} {'hold SR':>8s}")
    for r in rows:
        print(f"  {r['strategy']:24s} {r['timeframe']:>4s} {r['dev_years']:6.2f} "
              f"{r['dev_bars']:9,d} {r['dev_mintrl_sr']:7.2f} "
              f"{r['holdout_years']:7.2f} {r['holdout_bars']:10,d} "
              f"{r['holdout_mintrl_sr']:8.2f}")

    print("\nFamily multiple-testing null sr_zero = sqrt(V[SR]) x Gumbel(N):\n")
    print(f"  {'N trials':>9s} {'sr_zero':>9s}   min SR to clear BOTH bars "
          f"(dev, 1d strategies)")
    dev_floor = rows[0]["dev_mintrl_sr"]
    nulls = {}
    for n in range(1, 6):
        z = gumbel_sr_zero(n, sr_var)
        nulls[n] = z
        print(f"  {n:9d} {z:9.3f}   {max(z, dev_floor):.2f}")

    out = {
        "family": fam_stats.family, "sr_var": sr_var,
        "used_fallback": fam_stats.used_fallback,
        "existing_family_trials": fam_stats.n_trials,
        "windows": rows,
        "sr_zero_by_n": {str(k): v for k, v in nulls.items()},
    }
    Path("docs/confirmation_power_2026-09.json").write_text(
        json.dumps(out, indent=2), encoding="utf-8")
    print("\n[json] docs/confirmation_power_2026-09.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
