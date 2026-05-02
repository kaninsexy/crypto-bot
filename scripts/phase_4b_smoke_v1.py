"""Phase 4.B Variation #1 smoke trial.

Single end-to-end engine_perp.run_perp on the dev window for
FundingRateHarvest_BTC; records one `trial_type='smoke'` row to
trials.log via `record_trial`.

Cushion-threshold semantic mismatch flag (recorded in the row's
`notes` field for forensic visibility): the literature value
`exit_margin_breach_threshold = 0.01` is documented as an
*account margin ratio*, but `paper_trading.perp_simulator
.PerpSimulator.cushion_threshold` is interpreted as
`(equity − maintenance_margin) / maintenance_margin` — a
multiplier, not a ratio.  Per chat decision the smoke run passes
0.01 literally; the resolution belongs to the full_cpcv chat
scope.

Read-only with respect to project source (no edits to data/, no
manifest changes); writes a single row to backtest/trials.log via
the schema-validating writer.
"""

from __future__ import annotations

import json
import math
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from loguru import logger

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import backtest.holdout as holdout
from backtest.engine_perp import run_perp
from backtest import trials
from data import okx_funding
from strategies.funding_rate_harvest import (
    FundingRateHarvestStrategy,
    make_funding_settlement_counter,
)


VARIATION_ID = "phase4b-delta-neutral-singlepair-btc-v1"
STRATEGY_ID = "FundingRateHarvest_BTC"
LITERATURE_PATH = ROOT / "research" / "funding-rate-literature.md"


# ── 1. Manifest entry ────────────────────────────────────────────────────────

logger.info("[phase4b-smoke-v1] loading manifest")
manifest = holdout.load_manifest()
if STRATEGY_ID not in manifest:
    logger.error(
        f"manifest missing entry for {STRATEGY_ID}; aborting"
    )
    sys.exit(1)
entry = manifest[STRATEGY_ID]
funding_cadence_hours = int(entry.get("funding_cadence_hours", 8))
legs = entry["legs"]
holdout_start = pd.Timestamp(entry["holdout_start"])
data_start = pd.Timestamp(entry["data_start"])

logger.info(
    f"[phase4b-smoke-v1] manifest entry: timeframe={entry['timeframe']!r} "
    f"signal_cadence={entry.get('signal_cadence')!r} "
    f"funding_cadence_hours={funding_cadence_hours} "
    f"legs={legs} dev_window=[{data_start.isoformat()} → "
    f"{holdout_start.isoformat()})"
)


# ── 2. Pre-flight: zero pre-existing rows for this strategy ──────────────────

prior_rows = list(trials.read_trials(strategy_id=STRATEGY_ID))
if prior_rows:
    logger.warning(
        f"[phase4b-smoke-v1] DRIFT: {len(prior_rows)} pre-existing rows for "
        f"{STRATEGY_ID}: "
        + ", ".join(
            f"{r.get('variation_id')!r}/{r.get('trial_type')!r}"
            for r in prior_rows
        )
    )
    logger.error(
        "[phase4b-smoke-v1] aborting — first-row pre-state expected; "
        "drift suggests prior trial leaked into log"
    )
    sys.exit(1)
logger.info("[phase4b-smoke-v1] pre-flight clean: 0 prior rows")


# ── 3. Load dev legs via holdout accessor ────────────────────────────────────

logger.info("[phase4b-smoke-v1] loading dev legs via holdout.load_dev")
dev = holdout.load_dev(STRATEGY_ID)
if not isinstance(dev, dict) or "spot" not in dev or "perp" not in dev:
    logger.error(
        f"[phase4b-smoke-v1] holdout.load_dev returned unexpected shape "
        f"{type(dev).__name__}; aborting"
    )
    sys.exit(1)
df_spot = dev["spot"]
df_perp = dev["perp"]
logger.info(
    f"[phase4b-smoke-v1] dev legs: spot rows={len(df_spot)} "
    f"perp rows={len(df_perp)}"
)


# ── 4. Funding history covering dev window ───────────────────────────────────

dev_span_days = (holdout_start - data_start).days
months = int(math.ceil(dev_span_days / 30.44)) + 1
logger.info(
    f"[phase4b-smoke-v1] requesting funding months={months} "
    f"(dev span {dev_span_days}d)"
)
funding_full = okx_funding.load_or_fetch_funding_history(
    legs["perp"], months=months,
)
funding_dev = funding_full[funding_full.index < holdout_start]
logger.info(
    f"[phase4b-smoke-v1] funding rows in dev window: {len(funding_dev)} "
    f"(full fetch {len(funding_full)})"
)


# ── 5. Build strategy + run engine_perp ──────────────────────────────────────

strategy = FundingRateHarvestStrategy(
    symbol=legs["spot"], timeframe=entry["timeframe"],
)
logger.info("[phase4b-smoke-v1] running engine_perp.run_perp")
result = run_perp(
    df_spot=df_spot,
    df_perp=df_perp,
    funding_history=funding_dev,
    strategy=strategy,
    period_label="phase4b-smoke-v1",
    initial_balance=10_000.0,
    leverage=5.0,
    margin_mode="cross",
    spot_symbol=legs["spot"],
    perp_symbol=legs["perp"],
    flip_exit_n=4,                # literature exit_funding_flip_n_settlements
    flip_exit_threshold=0.0,
    cushion_threshold=0.01,       # literature exit_margin_breach_threshold
                                  # (semantic-mismatch flag below)
)


# ── 6. Exit-reason histogram ─────────────────────────────────────────────────

exit_reasons = Counter(t.exit_reason for t in result.trade_history)
logger.info(f"[phase4b-smoke-v1] exit_reasons={dict(exit_reasons)}")


# ── 7. Signal-event count for the smoke trial ────────────────────────────────

counter = make_funding_settlement_counter(funding_cadence_hours)
n_funding_settlements = counter(result, dev)
logger.info(
    f"[phase4b-smoke-v1] funding settlements counted in dev window: "
    f"{n_funding_settlements}"
)


# ── 8. Build trials event payload + record ───────────────────────────────────

# Read the Hypothesis section verbatim from literature.md so the
# trials.log row mirrors the canonical hypothesis-of-record.
lit_text = LITERATURE_PATH.read_text(encoding="utf-8")
m = re.search(
    r"\*\*Hypothesis\.\*\*\s*(.*?)\n\n\*\*Substrate\.\*\*",
    lit_text, flags=re.DOTALL,
)
if m is None:
    logger.error(
        "[phase4b-smoke-v1] could not extract Hypothesis section from "
        f"{LITERATURE_PATH}; aborting"
    )
    sys.exit(1)
hypothesis_text = m.group(1).strip()

equity = result.equity_curve
eq_start = float(equity.iloc[0]) if not equity.empty else float("nan")
eq_end = float(equity.iloc[-1]) if not equity.empty else float("nan")
eq_min = float(equity.min()) if not equity.empty else float("nan")
eq_max = float(equity.max()) if not equity.empty else float("nan")

# Combined-position sanity at exit (per risk-model § 5).
sanity_pct: float | None = None
sanity_violation = False
if result.trade_history:
    last_trade = result.trade_history[-1]
    # `quantity` on TradeRecord is perp_qty; we approximate the
    # combined-position sanity using the per-leg notional symmetry —
    # PerpSimulator already records explicit violations on its
    # `combined_position_sanity_violations` ledger which engine_perp
    # logs in its run summary; for the smoke row we surface whether
    # the simulator-side ledger has any entries.
    # The ledger is on the simulator instance, not on the
    # BacktestResult, so we infer from the engine log line below.
    sanity_pct = None  # explicit "computed by simulator, not here"
    # See logs for combined_position_violations count.

# Notes string with full forensic context.
notes_lines = [
    "Smoke trial. Cushion-threshold semantic mismatch flagged: "
    "literature exit_margin_breach_threshold=0.01 is account margin "
    "ratio, PerpSimulator.cushion_threshold is (equity-MM)/MM "
    "multiplier. Passing 0.01 literally; expect zero margin_breach "
    "exits. Resolve in full_cpcv chat scope.",
    f"Exit-reason histogram: {dict(exit_reasons)}.",
    f"Equity curve: start={eq_start:.2f} end={eq_end:.2f} "
    f"min={eq_min:.2f} max={eq_max:.2f}.",
    f"Funding settlements in dev window: {n_funding_settlements}.",
]
notes = " ".join(notes_lines)

event = {
    "strategy_id": STRATEGY_ID,
    "variation_id": VARIATION_ID,
    "trial_type": "smoke",
    "params": {
        "signal_cadence": "8h",
        "timeframe": "1h",
        "target_vol_annual": 0.05,
        "notional_capital_per_leg": 10000,
        "exit_funding_flip_n_settlements": 4,
        "exit_margin_breach_threshold": 0.01,
        "leverage": 5.0,
        "margin_mode": "cross",
    },
    "hypothesis": hypothesis_text,
    "split_holdout_start": entry["holdout_start"],
    "symbols": [legs["spot"]],
    "n_trades": int(result.metrics.total_trades),
    "sharpe": float(result.metrics.sharpe_ratio),
    "signal_event_count": int(n_funding_settlements),
    "notes": notes,
}

logger.info("[phase4b-smoke-v1] recording trial row via trials.record_trial")
trials.record_trial(event)


# ── 9. Summary block to stdout ───────────────────────────────────────────────

# Anomaly flags.
anomalies: list[str] = [
    "cushion_threshold semantic mismatch (literature ratio vs simulator "
    "(equity-MM)/MM multiplier; passing 0.01 literally)",
]
if int(result.metrics.total_trades) == 0:
    anomalies.append("n_trades == 0 (strategy never opened/closed)")
if math.isnan(float(result.metrics.sharpe_ratio)):
    anomalies.append("sharpe == NaN (degenerate equity curve)")
if (
    len(exit_reasons) > 0
    and set(exit_reasons.keys()) == {"funding_flip"}
):
    anomalies.append(
        "all exits = funding_flip (regime-decline confirmation)"
    )
if (
    len(exit_reasons) > 0
    and set(exit_reasons.keys()) == {"backtest_end"}
):
    anomalies.append(
        "all exits = backtest_end (no in-window exit fired; flip & "
        "margin gates inactive on this dev window)"
    )

print()
print("=" * 70)
print("Phase 4.B Variation #1 smoke trial — summary")
print("=" * 70)
print(f"Strategy        : {STRATEGY_ID}")
print(f"Variation       : {VARIATION_ID}")
print(f"Dev window      : {data_start.isoformat()} → "
      f"{holdout_start.isoformat()}")
print(f"Sharpe (head)   : {result.metrics.sharpe_ratio:.4f}")
print(f"Total return %  : {result.metrics.total_return_pct:+.3f}")
print(f"Max drawdown %  : {result.metrics.max_drawdown_pct:.3f}")
print(f"n_trades        : {result.metrics.total_trades}")
print(f"n_funding_evts  : {n_funding_settlements}")
print(f"Exit reasons    : {dict(exit_reasons)}")
print(
    f"Equity curve    : start={eq_start:,.2f}  end={eq_end:,.2f}  "
    f"min={eq_min:,.2f}  max={eq_max:,.2f}"
)
print()
if anomalies:
    print("Anomalies:")
    for a in anomalies:
        print(f"  - {a}")
else:
    print("Anomalies: none")
print("=" * 70)
print()
print(
    f"Trial row appended via record_trial; check "
    f"backtest/trials.log for the schema-validated record."
)
