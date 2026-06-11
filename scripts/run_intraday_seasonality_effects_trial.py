"""scripts/run_intraday_seasonality_effects_trial.py — sq-003 full_cpcv trial.

Runs the IntradaySeasonalityEffects (21-23 UTC long-only window) variation
through dev_cpcv only, computes the verdict, and appends one
trial_type="full_cpcv" row to backtest/trials.log via the schema-validating
writer.  No holdout access — that's the human-only step after review.

Mirror of scripts/run_supertrend_phase4a_trial.py with:
  - headline BacktestEngine warm_up_candles=0 (pure time filter; no warm-up)
  - CPCV default _ENGINE_WARM_UP_CANDLES=50 accepted (~2 days at 1H);
    boundary artefact documented in the trial-row notes
  - DSR monkeypatch (first full_cpcv for this strategy → count=0; max(N,1))
  - Sentinel + JSON summary at script tail for the orchestrator parser
"""

from __future__ import annotations

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
from backtest.dsr import dsr_from_cpcv_result
from backtest.engine import BacktestEngine
from backtest.holdout import load_dev, load_manifest
from backtest.verdict import compute_verdict
from strategies.intraday_seasonality import IntradaySeasonalityEffects


STRATEGY_ID = "IntradaySeasonalityEffects"
# Gate spec v2 (2026-06-11): explicit bar frequency for the
# units-correct DSR / MinTRL / verdict (manifest timeframe).
from backtest.dsr import bars_per_year_for_timeframe
BARS_PER_YEAR = bars_per_year_for_timeframe("1h")
VARIATION_ID = "intraday-hourly-long-21-23utc"

HYPOTHESIS_TEXT = (
    "Phase 4.C sq-003: pure time-of-day filter — enter BTC/USDT long at "
    "21:00 UTC, exit at 23:00 UTC on 1H candles. Hypothesis: crypto perp "
    "futures exhibit statistically significant positive returns in this "
    "UTC window. Source: trial_queue.json sq-003 (quality 3.5). "
    "Pre-trial gates: single-pair BTC/USDT, long-only, no indicators."
)

PARAMS: dict = {
    "entry_hour": 21,
    "exit_hour": 23,
    "timeframe": "1h",
    "symbol": "BTC/USDT",
    "notional_capital": 10000.0,
}


def make_strategy() -> IntradaySeasonalityEffects:
    """Fresh instance per call so CPCV block boundaries reset
    `_position_open` to False (gate #7)."""
    return IntradaySeasonalityEffects(
        symbol="BTC/USDT",
        timeframe="1h",
        entry_hour=21,
        exit_hour=23,
    )


def main() -> int:
    print("=" * 72)
    print(
        "Phase 4.C sq-003 — IntradaySeasonalityEffects trial (dev_cpcv only)"
    )
    print("=" * 72)

    manifest = load_manifest()
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
        f"range={dev_df.index[0]} → {dev_df.index[-1]}"
    )

    headline_engine = BacktestEngine(
        initial_balance=10_000.0,
        warm_up_candles=0,  # pure time filter — no indicator warm-up
        verbose=False,
    )
    headline_strategy = make_strategy()
    print("\n--- Headline run on full dev window ---")
    headline_result = headline_engine.run(
        df=dev_df.drop(columns=["symbol"], errors="ignore"),
        strategy=headline_strategy,
        period_label="phase4c-sq003-dev-headline",
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
    cpcv_result = run_cpcv(
        strategy_id=STRATEGY_ID,
        params=PARAMS,
        config=config,
        strategy_factory=make_strategy,
    )

    print(f"per-block Sharpes: {cpcv_result.per_block_sharpes}")
    print(f"per-block trade counts: {cpcv_result.trades_per_path}")
    print(f"distribution: {cpcv_result.sharpe_distribution}")

    # 3. DSR on validation (dev).  First full_cpcv for this strategy →
    # count_trials_for_dsr returns 0 (smoke excluded).  Mirror the
    # phase_4b_full_cpcv_v1 pattern: monkeypatch the lookup to
    # max(count, 1) so the deflation runs against n_trials=1.
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
        "sq-003 first full_cpcv. Single-pair BTC/USDT 1H, long-only "
        "(_position_open guard verified: no short entries). Pure "
        "time-of-day filter, entry 21:00 UTC, exit 23:00 UTC. Headline "
        "warm_up_candles=0 (no indicators). CPCV default warm-up "
        "(_ENGINE_WARM_UP_CANDLES=50) wastes first ~2 days of each "
        "block (no warm-up needed for pure time filter); affects at "
        "most 1 of ~73 trade opportunities per block. "
        "See research/intraday-seasonality-effects-literature.md."
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

    # 6. Final summary.
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
    print("--- TRIAL SUMMARY JSON ---")
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
