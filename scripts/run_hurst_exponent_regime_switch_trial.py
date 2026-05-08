"""scripts/run_hurst_exponent_regime_switch_trial.py -- sq-027 trial.

Runs the HurstExponentRegimeSwitch strategy on BTC/USDT 4H through the
single-symbol dev_cpcv harness (run_cpcv) -- mirrors the structural
shape of run_volatility_scaled_tsmom_trial.py (constants block, fresh
strategy_factory per block, monkeypatched n_trials lookup, CPCVError
handler with sentinel-bearing retire row, TRIAL SUMMARY JSON sentinel)
routed through the single-symbol engine so it matches the manifest's
`symbol` (singular) shape.

No holdout access, no commit, no deployment.

Output is ASCII-only (Windows cp1252 terminal compatibility).
"""

from __future__ import annotations

import os
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest import trials as _trials
from backtest.cpcv import run_cpcv
from backtest.cpcv_common import CPCVConfig, CPCVError
from backtest.dsr import dsr_from_cpcv_result
from backtest.engine import BacktestEngine
from backtest.holdout import load_dev, load_manifest
from backtest.verdict import compute_verdict
from strategies.hurst_exponent_regime_switch import (
    HurstExponentRegimeSwitchStrategy,
)


STRATEGY_ID = "HurstExponentRegimeSwitch"
VARIATION_ID = "hurst-regime-trend-mr-switch"

HYPOTHESIS_TEXT = (
    "sq-027 variation #1: Hurst-exponent regime-switch on BTC/USDT 4H. "
    "Rolling 100-bar Hurst classifies the regime as trending (H>0.55) "
    "or mean-reverting (H<0.45) or random-walk (otherwise). When "
    "trending, run a 30-bar momentum trend-follower (long when "
    "trailing log-return sum > 0). When mean-reverting, run a 30-bar "
    "z-score MR (long at z<=-1.5, exit at z>=0 or -8% stop). "
    "Random-walk band is flat (no new entries). Long-only, single "
    "concurrent position. Sources: Calef & Kucinic (2021) SSRN; "
    "Begusic, Velickovic, Lio (2022) Applied Sciences; Kyriazis "
    "(2020) Eurasian Economic Review."
)

PARAMS: dict = {
    "hurst_window": 100,
    "h_upper": 0.55,
    "h_lower": 0.45,
    "momentum_lookback": 30,
    "zscore_window": 30,
    "entry_z": -1.5,
    "exit_z": 0.0,
    "mr_stop_loss_pct": 0.08,
    "notional_capital": 10_000.0,
    "timeframe": "4h",
    "symbol": "BTC/USDT",
}


def main() -> int:
    print("=" * 72)
    print("sq-027 -- HurstExponentRegimeSwitch trial #1 (dev_cpcv only)")
    print("=" * 72)

    manifest = load_manifest()
    if STRATEGY_ID not in manifest:
        print(f"FAIL: '{STRATEGY_ID}' not in manifest.", file=sys.stderr)
        return 1
    entry = manifest[STRATEGY_ID]
    symbol: str = entry["symbol"]
    print(
        f"manifest entry: timeframe={entry['timeframe']} "
        f"symbol={symbol} "
        f"holdout_start={entry['holdout_start']}"
    )
    print(
        f"  strategy_warmup_candles={entry['strategy_warmup_candles']} "
        f"min_tradeable_candles_per_block="
        f"{entry['min_tradeable_candles_per_block']}"
    )

    # 1. Load dev OHLCV. Single-symbol manifest entry returns a
    #    DataFrame with a 'symbol' column per backtest.holdout.load_dev.
    raw = load_dev(STRATEGY_ID)
    if isinstance(raw, pd.DataFrame) and "symbol" in raw.columns:
        dev_df = raw.drop(columns=["symbol"]).sort_index()
    else:
        dev_df = raw if isinstance(raw, pd.DataFrame) else None
    if dev_df is None or len(dev_df) == 0:
        print(
            f"FAIL: load_dev returned empty for {STRATEGY_ID}.",
            file=sys.stderr,
        )
        return 1
    print(
        f"\ndev frame: rows={len(dev_df):,} "
        f"range={dev_df.index.min()} -> {dev_df.index.max()}"
    )

    # 2. Strategy factory: fresh instance per call so _position_open
    #    and _active_mode reset across CPCV blocks.
    def make_strategy() -> HurstExponentRegimeSwitchStrategy:
        return HurstExponentRegimeSwitchStrategy(
            symbol=symbol,
            timeframe=PARAMS["timeframe"],
            hurst_window=PARAMS["hurst_window"],
            h_upper=PARAMS["h_upper"],
            h_lower=PARAMS["h_lower"],
            momentum_lookback=PARAMS["momentum_lookback"],
            zscore_window=PARAMS["zscore_window"],
            entry_z=PARAMS["entry_z"],
            exit_z=PARAMS["exit_z"],
            mr_stop_loss_pct=PARAMS["mr_stop_loss_pct"],
            notional_capital=PARAMS["notional_capital"],
        )

    # 3. Headline backtest on the full dev window.
    print("\n--- Headline run on full dev window ---")
    headline_engine = BacktestEngine(
        initial_balance=10_000.0,
        warm_up_candles=int(entry["strategy_warmup_candles"]),
        verbose=False,
    )
    headline_result = headline_engine.run(
        df=dev_df,
        strategy=make_strategy(),
        period_label="sq-027-hurst-regime-dev-headline",
    )
    sr_observed = float(headline_result.metrics.sharpe_ratio)
    n_trades_headline = int(headline_result.metrics.total_trades)
    print(
        f"headline Sharpe = {sr_observed:.4f} | "
        f"n_trades = {n_trades_headline} | "
        f"return_pct = {headline_result.metrics.total_return_pct:+.2f}% | "
        f"max_dd = {headline_result.metrics.max_drawdown_pct:.2f}%"
    )

    # 4. CPCV block-Sharpe distribution.
    print("\n--- CPCV: 10 blocks, full_cpcv ---")
    config = CPCVConfig(
        n_blocks=10, k_held_out=2, purge_periods=0, embargo_periods=0,
    )
    # TRIAL_WARM_UP_CANDLES env var: orchestrator pre-run gate
    # injects this when the manifest's strategy_warmup_candles is
    # significantly larger than the cpcv harness default (50). The
    # Hurst-regime strategy needs hurst_window + zscore_window =
    # 130 bars before any signal can fire, so the trial defaults to
    # the manifest's strategy_warmup_candles value (132) when the
    # env var is unset.
    _warm = int(os.environ.get(
        "TRIAL_WARM_UP_CANDLES",
        int(entry["strategy_warmup_candles"]),
    ))
    try:
        cpcv_result = run_cpcv(
            strategy_id=STRATEGY_ID,
            params=PARAMS,
            config=config,
            strategy_factory=make_strategy,
            warm_up_candles=_warm,
        )
    except CPCVError as exc:
        # CPCVError handler -- mandatory per CLAUDE.md "Pre-justified
        # test batch execution / CPCVError handling".
        print(f"\nCPCVError: {exc}")
        print(
            "Insufficient trades across CPCV blocks -- recording as "
            "retire/under_tested."
        )
        nan = float("nan")
        cpcv_error_event = {
            "strategy_id": STRATEGY_ID,
            "variation_id": VARIATION_ID,
            "trial_type": "full_cpcv",
            "params": PARAMS,
            "hypothesis": HYPOTHESIS_TEXT,
            "split_holdout_start": entry["holdout_start"],
            "symbols": [symbol],
            "n_trades": 0,
            "sharpe": nan,
            "cpcv": {
                "n_paths": 0,
                "n_blocks": 0,
                "k_held_out": int(config.k_held_out),
                "purge_periods": int(config.purge_periods),
                "embargo_periods": int(config.embargo_periods),
                "sharpe_distribution": {
                    "mean": nan, "std": nan,
                    "quantiles": {
                        "p05": nan, "p25": nan, "p50": nan,
                        "p75": nan, "p95": nan,
                    },
                },
            },
            "dsr_validation": nan,
            "mintrl": None,
            "buy_and_hold_sharpe": nan,
            "notes": (
                f"verdict=retire | CPCVError: {exc}. "
                "Hurst-regime-switch signal too sparse to meet the "
                "per-block trade-count floor. The 100-bar Hurst "
                "warmup plus the regime-gate neutral band reduces "
                "the trade rate; if the dev window cannot support "
                "the per-block minimum the verdict is retire."
            ),
        }
        _trials.record_trial(cpcv_error_event)
        print(
            f"Trial row recorded. trial_id={cpcv_error_event['trial_id']} | "
            f"params_hash={cpcv_error_event['params_hash']} | "
            "Exiting cleanly."
        )
        print("\n--- TRIAL SUMMARY JSON ---")
        print(json.dumps({
            "strategy": STRATEGY_ID,
            "variation": VARIATION_ID,
            "verdict": "retire",
            "sr_observed": None,
            "dsr_validation": None,
            "n_trades_total": 0,
            "cpcv_error": str(exc),
        }, indent=2))
        return 0

    print(f"per-block Sharpes: {cpcv_result.per_block_sharpes}")
    print(f"per-block trade counts: {cpcv_result.trades_per_path}")
    print(f"distribution: {cpcv_result.sharpe_distribution}")

    # 5. DSR on validation (dev). Monkeypatch n_trials lookup to
    #    max(N, 1) for the first full_cpcv (count_trials_for_dsr
    #    returns 0 prior to append).
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

    # 6. Verdict tree (dev-side preview).
    valid_returns = [r for r in cpcv_result.per_block_returns if r.size > 0]
    concat_returns = (
        np.concatenate(valid_returns) if valid_returns else np.array([])
    )
    verdict = compute_verdict(
        strategy_id=STRATEGY_ID,
        sr_candidate=sr_observed,
        returns=concat_returns,
        total_trades=int(sum(cpcv_result.trades_per_path)),
        baseline_df=dev_df,
        n_trials=n_trials_pre + 1,
        min_trade_count=30,
        confidence=0.95,
    )

    print("\n--- Verdict (dev-side preview) ---")
    print(f"verdict                 = {verdict.verdict}")
    print(
        f"trade_count_pass        = {verdict.trade_count_pass} "
        f"(total_trades={verdict.total_trades})"
    )
    print(f"mintrl_pass             = {verdict.mintrl_pass}")
    print(f"mintrl_required_at_eval = {verdict.mintrl_required_at_eval}")
    print(f"sr_observed             = {verdict.sr_observed:.4f}")
    print(
        f"baseline_sharpe_at_eval = {verdict.baseline_sharpe_at_eval:.4f}"
    )
    print(f"mt_mean_pass            = {verdict.mt_mean_pass}")
    print(f"baseline_pass           = {verdict.baseline_pass}")
    print(f"dsr                     = {verdict.dsr}")
    print(f"n_trials                = {verdict.n_trials}")

    notes = (
        "sq-027 variation #1. Single-symbol BTC/USDT 4H long-only. "
        "Hurst-exponent regime-switch: rolling 100-bar R/S Hurst "
        "classifies the regime; H>0.55 -> 30-bar momentum "
        "trend-follower; H<0.45 -> 30-bar z-score mean-reversion "
        "(entry z<=-1.5, exit z>=0, -8% stop); 0.45<=H<=0.55 "
        "neutral band (no new entries). Long-only, single "
        "concurrent position. No look-ahead. Baseline = BTC/USDT "
        "B&H. Sources: Calef & Kucinic (2021) SSRN; Begusic, "
        "Velickovic, Lio (2022) Applied Sciences; Kyriazis (2020) "
        "Eurasian Economic Review."
    )
    event = {
        "strategy_id": STRATEGY_ID,
        "variation_id": VARIATION_ID,
        "trial_type": "full_cpcv",
        "params": PARAMS,
        "hypothesis": HYPOTHESIS_TEXT,
        "split_holdout_start": entry["holdout_start"],
        "symbols": [symbol],
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

    print("\n--- Final summary ---")
    summary = {
        "strategy": STRATEGY_ID,
        "variation": VARIATION_ID,
        "verdict": verdict.verdict,
        "sr_observed": sr_observed,
        "sr_zero_expected_at_eval": verdict.sr_zero_expected_at_eval,
        "baseline_sharpe_at_eval": verdict.baseline_sharpe_at_eval,
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
