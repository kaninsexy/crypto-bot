"""scripts/run_overnight_session_reversal_trial.py -- sq-023 full_cpcv trial.

Runs the OvernightSessionReversal long-only NYSE-overnight reversal
variation through dev_cpcv only on BTC/USDT 1H, computes the
verdict, and appends one trial_type='full_cpcv' row to
backtest/trials.log via the schema-validating writer.  No holdout
access -- that's the human-only step after review.

Mirror of scripts/run_intraday_momentum_reversal_trial.py: single
symbol, BacktestEngine.run + run_cpcv (the manifest entry has the
singular `symbol` field, not plural `symbols`).

CPCVError handling: wrapped in try/except per .claude/rules/backtest.md;
on catch the trial appends a sentinel-bearing retire row and exits 0
so the orchestrator records a clean retire status instead of a crash.

ASCII-only stdout: every print() in this file uses ASCII characters
only for Windows cp1252 terminal compatibility.
"""

from __future__ import annotations

import os
import json
import sys
from pathlib import Path

import numpy as np

# Project root on sys.path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest import trials as _trials
from backtest.cpcv import CPCVConfig, run_cpcv
from backtest.cpcv_common import CPCVError
from backtest.dsr import dsr_from_cpcv_result
from backtest.engine import BacktestEngine
from backtest.holdout import load_dev, load_manifest
from backtest.verdict import compute_verdict
from strategies.overnight_session_reversal import (
    OvernightSessionReversalStrategy,
)


STRATEGY_ID = "OvernightSessionReversal"
# Gate spec v2 (2026-06-11): explicit bar frequency for the
# units-correct DSR / MinTRL / verdict (manifest timeframe).
from backtest.dsr import bars_per_year_for_timeframe
BARS_PER_YEAR = bars_per_year_for_timeframe("1h")
VARIATION_ID = "us-session-overnight-reversal"

HYPOTHESIS_TEXT = (
    "sq-023: long-only NYSE-overnight reversal on BTC/USDT 1H. The "
    "overnight-session return (NYSE-closed window, ~21:00 UTC prior "
    "day to 14:00 UTC current day) is negatively correlated with the "
    "subsequent day-session return (14:00-21:00 UTC). When the "
    "overnight return is negative, BUY at the day-session start "
    "(close of bar 14:00 UTC, fill ~15:00 UTC) and SELL at the day-"
    "session end (close of bar 20:00 UTC, fill ~21:00 UTC). When the "
    "overnight return is positive, the specification calls for a "
    "short leg; the engine is long-only on spot, so HOLD. Sources: "
    "Ham, Ryu, Webb (2022) IRFA; Lou, Polk, Skouras (2019) JFE; "
    "Zaremba et al. (2021) IRFA; Han, Kang, Ryu (2024) SSRN."
)

PARAMS: dict = {
    "entry_hour": 14,
    "exit_hour": 20,
    "timeframe": "1h",
    "symbol": "BTC/USDT",
    "notional_capital": 10000.0,
}


def make_strategy() -> OvernightSessionReversalStrategy:
    """Fresh instance per call so CPCV block boundaries reset
    `_position_open` to False (CPCV correctness gate)."""
    return OvernightSessionReversalStrategy(
        symbol="BTC/USDT",
        timeframe="1h",
        entry_hour=PARAMS["entry_hour"],
        exit_hour=PARAMS["exit_hour"],
    )


def _emit_summary(summary: dict) -> None:
    """Print the orchestrator-parsed sentinel + JSON block."""
    print("--- TRIAL SUMMARY JSON ---")
    print(json.dumps(summary, indent=2, default=str))


def _record_cpcv_error_retire(
    err: CPCVError, manifest_entry: dict,
) -> int:
    """Append a sentinel-bearing retire row when CPCV cannot run.

    Mirrors the CPCVError pattern documented in
    .claude/rules/backtest.md: log the failure, append a retire row
    with sr_observed=nan and n_trades=0, emit the JSON sentinel, and
    return 0 so the orchestrator records `done` rather than `error`.
    """
    msg = str(err)
    print(f"CPCVError caught: {msg}")
    notes = (
        f"sq-023 CPCVError: {msg}.  Retire row written via "
        "sentinel handler per .claude/rules/backtest.md."
    )
    event = {
        "strategy_id": STRATEGY_ID,
        "variation_id": VARIATION_ID,
        "trial_type": "full_cpcv",
        "params": PARAMS,
        "hypothesis": HYPOTHESIS_TEXT,
        "split_holdout_start": manifest_entry["holdout_start"],
        "symbols": [manifest_entry["symbol"]],
        "n_trades": 0,
        "sharpe": float("nan"),
        "cpcv": {
            "n_paths": 0,
            "n_blocks": 0,
            "k_held_out": 2,
            "purge_periods": 0,
            "embargo_periods": 0,
            "sharpe_distribution": {
                "mean": float("nan"),
                "std": float("nan"),
                "quantiles": {
                    "p05": float("nan"),
                    "p25": float("nan"),
                    "p50": float("nan"),
                    "p75": float("nan"),
                    "p95": float("nan"),
                },
            },
        },
        "dsr_validation": float("nan"),
        "mintrl": None,
        "buy_and_hold_sharpe": float("nan"),
        "notes": notes,
        "verdict": "retire",
    }
    _trials.record_trial(event)
    print(
        f"appended retire trial row | "
        f"trial_id={event['trial_id']} | "
        f"params_hash={event['params_hash']}"
    )
    summary = {
        "strategy": STRATEGY_ID,
        "variation": VARIATION_ID,
        "verdict": "retire",
        "sr_observed": None,
        "n_trades_total": 0,
        "cpcv_error": msg,
    }
    _emit_summary(summary)
    return 0


def main() -> int:
    print("=" * 72)
    print(
        "sq-023 -- OvernightSessionReversal trial (dev_cpcv only)"
    )
    print("=" * 72)

    manifest = load_manifest()
    if STRATEGY_ID not in manifest:
        print(
            f"FAIL: '{STRATEGY_ID}' not in manifest.", file=sys.stderr,
        )
        return 1
    entry = manifest[STRATEGY_ID]
    print(
        f"manifest entry: timeframe={entry['timeframe']} "
        f"symbol={entry['symbol']} "
        f"holdout_start={entry['holdout_start']}"
    )

    # 1. Headline backtest on the full dev window for the candidate Sharpe.
    dev_df = load_dev(STRATEGY_ID)
    print(
        f"dev frame: rows={len(dev_df)} "
        f"range={dev_df.index[0]} -> {dev_df.index[-1]}"
    )

    headline_engine = BacktestEngine(
        initial_balance=10_000.0,
        warm_up_candles=50,
        verbose=False,
    )
    headline_strategy = make_strategy()
    print("\n--- Headline run on full dev window ---")
    headline_result = headline_engine.run(
        df=dev_df.drop(columns=["symbol"], errors="ignore"),
        strategy=headline_strategy,
        period_label="sq023-dev-headline",
    )
    sr_observed = float(headline_result.metrics.sharpe_ratio)
    n_trades_headline = int(headline_result.metrics.total_trades)
    print(
        f"headline Sharpe = {sr_observed:.4f} | "
        f"n_trades = {n_trades_headline} | "
        f"return_pct = {headline_result.metrics.total_return_pct:+.2f}% | "
        f"max_dd = {headline_result.metrics.max_drawdown_pct:.2f}%"
    )

    # 2. CPCV block-Sharpe distribution on the same dev window.
    print("\n--- CPCV: 10 blocks, full_cpcv ---")
    config = CPCVConfig(
        n_blocks=10, k_held_out=2, purge_periods=0, embargo_periods=0,
    )
    # TRIAL_WARM_UP_CANDLES env var: orchestrator pre-run gate
    # injects this when manifest strategy_warmup_candles is
    # significantly smaller than cpcv harness default (50).
    _warm = int(os.environ.get("TRIAL_WARM_UP_CANDLES", 50))
    try:
        cpcv_result = run_cpcv(
            strategy_id=STRATEGY_ID,
            params=PARAMS,
            config=config,
            strategy_factory=make_strategy,
            warm_up_candles=_warm,
        )
    except CPCVError as err:
        return _record_cpcv_error_retire(err, entry)

    print(f"per-block Sharpes: {cpcv_result.per_block_sharpes}")
    print(f"per-block trade counts: {cpcv_result.trades_per_path}")
    print(f"distribution: {cpcv_result.sharpe_distribution}")

    # 3. DSR on validation (dev).  First full_cpcv for this strategy
    # -> count_trials_for_dsr returns 0 (smoke excluded).  Mirror the
    # phase_4b_full_cpcv_v1 / sq-003 / sq-019 pattern: monkeypatch
    # the lookup to max(N, 1) so the deflation runs against
    # n_trials=1.
    n_trials_pre = _trials.count_trials_for_dsr(STRATEGY_ID)
    print(f"\nn_trials_for_dsr({STRATEGY_ID}) before append = {n_trials_pre}")

    import backtest.trials as _t_mod
    _orig_count = _t_mod.count_trials_for_dsr
    _t_mod.count_trials_for_dsr = lambda sid: max(_orig_count(sid), 1)
    try:
        dsr_result = dsr_from_cpcv_result(
            result=cpcv_result,
            strategy_id=STRATEGY_ID,
            sr_candidate=sr_observed,
            bars_per_year=BARS_PER_YEAR,
        )
    finally:
        _t_mod.count_trials_for_dsr = _orig_count

    print(
        f"DSR validation: dsr={dsr_result.dsr:.4f} | "
        f"sr_candidate={dsr_result.sr_candidate:.4f} | "
        f"sr_zero_expected={dsr_result.sr_zero_expected:.4f} | "
        f"sr_std={dsr_result.sr_std:.4f} | "
        f"T={dsr_result.t} | n_trials={dsr_result.n_trials}"
    )

    # 4. Verdict-tree preview (dev-side).
    valid_returns = [r for r in cpcv_result.per_block_returns if r.size > 0]
    concat_returns = (
        np.concatenate(valid_returns) if valid_returns else np.array([])
    )
    primary_baseline_df = dev_df.drop(columns=["symbol"], errors="ignore")
    verdict = compute_verdict(
        strategy_id=STRATEGY_ID,
        sr_candidate=sr_observed,
        returns=concat_returns,
        total_trades=int(sum(cpcv_result.trades_per_path)),
        baseline_df=primary_baseline_df,
        n_trials=n_trials_pre + 1,
        min_trade_count=30,
        confidence=0.95,
        bars_per_year=BARS_PER_YEAR,
    )

    print("\n--- Verdict (dev-side preview) ---")
    print(f"verdict                 = {verdict.verdict}")
    print(
        f"trade_count_pass        = {verdict.trade_count_pass} "
        f"(total_trades={verdict.total_trades})"
    )
    print(f"mintrl_pass             = {verdict.mintrl_pass}")
    print(f"mintrl_required_at_eval = {verdict.mintrl_required_at_eval}")
    print(f"t_observed              = {verdict.t_observed}")
    print(f"sr_observed             = {verdict.sr_observed:.4f}")
    print(f"sr_zero_expected_at_eval= {verdict.sr_zero_expected_at_eval}")
    print(
        f"baseline_sharpe_at_eval = {verdict.baseline_sharpe_at_eval:.4f}"
    )
    print(f"mt_mean_pass            = {verdict.mt_mean_pass}")
    print(f"baseline_pass           = {verdict.baseline_pass}")
    print(f"sr_margin_vs_mt_mean    = {verdict.sr_margin_vs_mt_mean}")
    print(f"sr_margin_vs_baseline   = {verdict.sr_margin_vs_baseline}")
    print(f"dsr                     = {verdict.dsr}")
    print(f"n_trials                = {verdict.n_trials}")

    # 5. Append trial row.
    notes = (
        "sq-023 first full_cpcv. Single-pair BTC/USDT 1H, long-only. "
        "NYSE-overnight reversal: overnight return (close of bar "
        "20:00 UTC yesterday to open of bar 14:00 UTC today) "
        "predicts day-session return (14:00-21:00 UTC). BUY at close "
        "of bar 14:00 UTC when overnight return < 0; SELL at close "
        "of bar 20:00 UTC. The short-leg branch (overnight > 0) is "
        "HOLD on the long-only spot engine. _position_open guard: "
        "SELL only fires when a prior BUY flipped state to True. "
        "See research/overnight-session-reversal-literature.md."
    )
    event = {
        "strategy_id": STRATEGY_ID,
        "variation_id": VARIATION_ID,
        "trial_type": "full_cpcv",
        "params": PARAMS,
        "hypothesis": HYPOTHESIS_TEXT,
        "split_holdout_start": entry["holdout_start"],
        "symbols": [entry["symbol"]],
        "n_trades": int(sum(cpcv_result.trades_per_path)),
        "sharpe": sr_observed,
        "cpcv": {
            "n_paths": int(config.n_blocks),
            "n_blocks": int(config.n_blocks),
            "k_held_out": int(config.k_held_out),
            "purge_periods": int(config.purge_periods),
            "embargo_periods": int(config.embargo_periods),
            "sharpe_distribution": cpcv_result.sharpe_distribution,
        },
        "dsr_validation": float(dsr_result.dsr),
        "mintrl": (
            float(verdict.mintrl_required_at_eval)
            if np.isfinite(verdict.mintrl_required_at_eval)
            else None
        ),
        "buy_and_hold_sharpe": float(verdict.baseline_sharpe_at_eval),
        "notes": notes,
    }
    _trials.record_trial(event)
    print(
        f"\nappended trial row to backtest/trials.log | "
        f"trial_id={event['trial_id']} | "
        f"params_hash={event['params_hash']}"
    )

    n_trials_post = _trials.count_trials_for_dsr(STRATEGY_ID)
    print(f"n_trials_for_dsr({STRATEGY_ID}) after append  = {n_trials_post}")

    # 6. Final summary block.
    print("\n--- Final summary ---")
    summary = {
        "strategy": STRATEGY_ID,
        "variation": VARIATION_ID,
        "verdict": verdict.verdict,
        "sr_observed": sr_observed,
        "sr_zero_expected_at_eval": verdict.sr_zero_expected_at_eval,
        "baseline_sharpe_at_eval": verdict.baseline_sharpe_at_eval,
        "baseline_sharpe": float(verdict.baseline_sharpe_at_eval),
        "trade_count_pass": verdict.trade_count_pass,
        "mintrl_pass": verdict.mintrl_pass,
        "mt_mean_pass": verdict.mt_mean_pass,
        "baseline_pass": verdict.baseline_pass,
        "n_trades_total": int(sum(cpcv_result.trades_per_path)),
        "n_trades_headline_run": n_trades_headline,
        "block_sharpes": cpcv_result.per_block_sharpes,
        "block_trades": cpcv_result.trades_per_path,
        "sharpe_distribution": cpcv_result.sharpe_distribution,
        "dsr_validation": float(dsr_result.dsr),
        "n_trials_post_append": n_trials_post,
    }
    _emit_summary(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
