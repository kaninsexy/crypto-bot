"""scripts/run_market_state_conditioned_momentum_trial.py -- sq-031 trial.

Runs the MarketStateConditionedMomentum variation (sq-031) through
dev_cpcv only via run_engine_multi + run_cpcv_multi, computes DSR +
verdict, and appends one trial_type='full_cpcv' row to
backtest/trials.log via the schema-validating writer. No holdout
access -- that is the human-only step after review.

Mirror of scripts/run_meanreversion_btc_residual_phase4a_trial.py
(authoritative multi-symbol Phase 4 template) with:
  - 10-symbol basket (BTC + 9 alts) at 1D
  - BTC is the market-state benchmark (never traded)
  - DSR monkeypatch (first full_cpcv -> count_trials_for_dsr returns 0;
    max(N, 1) so the deflation runs against n_trials=1)
  - Sentinel + JSON summary at script tail for the orchestrator parser
  - CPCVError handler emits a sentinel-bearing retire row
  - ASCII-only stdout (cp1252-safe)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# Project root on sys.path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest import trials as _trials
from backtest.cpcv_common import CPCVConfig, CPCVError
from backtest.cpcv_multi import run_cpcv_multi
from backtest.dsr import dsr_from_cpcv_result
from backtest.engine_multi import run_engine_multi
from backtest.holdout import load_dev, load_manifest
from backtest.verdict import compute_verdict
from strategies.market_state_conditioned_momentum import (
    MarketStateConditionedMomentumStrategy,
)


STRATEGY_ID = "MarketStateConditionedMomentum"
# Gate spec v2 (2026-06-11): explicit bar frequency for the
# units-correct DSR / MinTRL / verdict (manifest timeframe).
from backtest.dsr import bars_per_year_for_timeframe
BARS_PER_YEAR = bars_per_year_for_timeframe("1d")
VARIATION_ID = "market-state-conditioned-momentum"

HYPOTHESIS_TEXT = (
    "sq-031: market-state-conditioned time-series momentum on a "
    "10-symbol crypto basket at 1D. BTC/USDT defines the market state "
    "by comparing two consecutive 60-day log returns; deploy long-only "
    "TSMOM (30-day lookback) on the 9-alt basket only when both "
    "windows are UP (continuation state per Cheema et al. 2017); "
    "neutralize exposure during transitions or trending-down states. "
    "BTC is benchmark-only (never traded). Sources: Han et al. (2024) "
    "SSRN; Cheema et al. (2017) MPRA; Tzouvanas et al. (2019) "
    "University of Southampton."
)

PARAMS: dict = {
    "market_state_lookback": 60,
    "tsmom_lookback": 30,
    "max_positions": 5,
    "notional_capital": 10_000.0,
    "timeframe": "1d",
}


def _build_strategy(symbols: list[str]) -> MarketStateConditionedMomentumStrategy:
    return MarketStateConditionedMomentumStrategy(
        symbols=symbols,
        btc_symbol="BTC/USDT",
        timeframe=PARAMS["timeframe"],
        market_state_lookback=PARAMS["market_state_lookback"],
        tsmom_lookback=PARAMS["tsmom_lookback"],
        max_positions=PARAMS["max_positions"],
        notional_capital=PARAMS["notional_capital"],
    )


def _record_cpcv_error_retire(
    entry: dict,
    symbols: list[str],
    err_msg: str,
) -> dict:
    """Append a sentinel retire row when CPCV fails (e.g. insufficient
    trades / blocks too small). Mirrors the .claude/rules/backtest.md
    CPCVError handler so the orchestrator parses a clean done queue
    status instead of an error one."""
    notes = (
        "sq-031 CPCVError on first full_cpcv: " + str(err_msg)
        + " | strategy retired without surviving CPCV. See "
        "research/market-state-conditioned-momentum-literature.md."
    )
    event = {
        "strategy_id": STRATEGY_ID,
        "variation_id": VARIATION_ID,
        "trial_type": "full_cpcv",
        "params": PARAMS,
        "hypothesis": HYPOTHESIS_TEXT,
        "split_holdout_start": entry["holdout_start"],
        "symbols": symbols,
        "n_trades": 0,
        "sharpe": float("nan"),
        "cpcv": {
            "n_paths": 0,
            "n_blocks": 0,
            "k_held_out": 2,
            "purge_periods": 0,
            "embargo_periods": 0,
            "strategy_warmup_candles": int(entry["strategy_warmup_candles"]),
            "min_tradeable_candles_per_block": int(
                entry["min_tradeable_candles_per_block"]
            ),
            "sharpe_distribution": {},
        },
        "dsr_validation": float("nan"),
        "mintrl": None,
        "buy_and_hold_sharpe": float("nan"),
        "notes": notes,
        "verdict": "retire",
    }
    _trials.record_trial(event)
    return event


def main() -> int:
    print("=" * 72)
    print(
        "sq-031 -- MarketStateConditionedMomentum trial (dev_cpcv only)"
    )
    print("=" * 72)

    # -- 0. Sanity checks on the manifest entry -----------------------------
    manifest = load_manifest()
    if STRATEGY_ID not in manifest:
        print(f"FAIL: '{STRATEGY_ID}' not in manifest.", file=sys.stderr)
        return 1
    entry = manifest[STRATEGY_ID]
    symbols: list[str] = list(entry["symbols"])
    print(
        f"manifest entry: timeframe={entry['timeframe']} "
        f"holdout_start={entry['holdout_start']} "
        f"n_symbols={len(symbols)}"
    )
    print(f"  symbols: {symbols}")
    print(
        f"  strategy_warmup_candles={entry['strategy_warmup_candles']} "
        f"min_tradeable_candles_per_block="
        f"{entry['min_tradeable_candles_per_block']}"
    )

    # -- 1. Load dev frame + pivot into per-symbol dict ---------------------
    raw = load_dev(STRATEGY_ID)
    if not isinstance(raw, pd.DataFrame) or "symbol" not in raw.columns:
        print(
            "FAIL: load_dev did not return a stacked DataFrame with a "
            f"'symbol' column; got type={type(raw).__name__}.",
            file=sys.stderr,
        )
        return 1
    print(
        f"\ndev frame: rows={len(raw):,} "
        f"range={raw.index.min()} -> {raw.index.max()}"
    )

    data: dict[str, pd.DataFrame] = {
        sym: raw[raw["symbol"] == sym]
            .drop(columns=["symbol"])
            .sort_index()
        for sym in symbols
    }
    missing = [sym for sym in symbols if sym not in data or len(data[sym]) == 0]
    if missing:
        print(
            f"FAIL: missing or empty per-symbol slices for: {missing}",
            file=sys.stderr,
        )
        return 1
    print("\nper-symbol dev coverage:")
    for sym, df in data.items():
        print(
            f"  {sym:<12} rows={len(df):>5} "
            f"range={df.index.min()} -> {df.index.max()}"
        )

    # -- 2. Headline backtest on the full dev window ------------------------
    print("\n--- Headline run on full dev window ---")
    headline_strategy = _build_strategy(symbols)
    headline_result = run_engine_multi(
        data=data,
        strategy=headline_strategy,
        period_label="sq-031-msc-momentum-dev-headline",
        initial_balance=10_000.0,
    )
    sr_observed = float(headline_result.metrics.sharpe_ratio)
    n_trades_headline = int(headline_result.metrics.total_trades)
    print(
        f"headline Sharpe = {sr_observed:.4f} | "
        f"n_trades = {n_trades_headline} | "
        f"return_pct = {headline_result.metrics.total_return_pct:+.2f}% | "
        f"max_dd = {headline_result.metrics.max_drawdown_pct:.2f}%"
    )

    # -- 3. CPCV block-Sharpe distribution ----------------------------------
    print(
        "\n--- CPCV: requested n_blocks=10, "
        "warmup-aware downshift via compute_effective_n_blocks ---"
    )
    cpcv_config = CPCVConfig(
        n_blocks=10,
        k_held_out=2,
        purge_periods=0,
        embargo_periods=0,
        strategy_warmup_candles=int(entry["strategy_warmup_candles"]),
        min_tradeable_candles_per_block=int(
            entry["min_tradeable_candles_per_block"]
        ),
    )
    cpcv_strategy = _build_strategy(symbols)  # fresh instance for CPCV

    try:
        cpcv_result = run_cpcv_multi(
            data=data,
            strategy=cpcv_strategy,
            config=cpcv_config,
            initial_balance=10_000.0,
        )
    except CPCVError as exc:
        # Sentinel-bearing retire row + clean exit per backtest.md.
        print(f"CPCVError caught: {exc}", file=sys.stderr)
        retire_event = _record_cpcv_error_retire(entry, symbols, str(exc))
        print(
            f"\nappended retire trial row to backtest/trials.log | "
            f"trial_id={retire_event['trial_id']} | "
            f"params_hash={retire_event['params_hash']}"
        )
        summary = {
            "strategy": STRATEGY_ID,
            "variation": VARIATION_ID,
            "verdict": "retire",
            "sr_observed": float("nan"),
            "n_trades_total": 0,
            "n_trades_headline_run": n_trades_headline,
            "cpcv_error": str(exc),
        }
        print("\n--- Final summary ---")
        print("--- TRIAL SUMMARY JSON ---")
        print(json.dumps(summary, indent=2, default=str))
        return 0

    effective_n_blocks = int(cpcv_result.n_paths)
    if effective_n_blocks < 2:
        print(
            f"FAIL: effective_n_blocks={effective_n_blocks} < 2; dev "
            f"window too short for warmup-aware splitting. Aborting "
            f"before record_trial.",
            file=sys.stderr,
        )
        return 1

    print(f"effective_n_blocks      = {effective_n_blocks}")
    print(f"per-block Sharpes       : {cpcv_result.per_block_sharpes}")
    print(f"per-block trade counts  : {cpcv_result.trades_per_path}")
    print(f"sharpe_distribution     : {cpcv_result.sharpe_distribution}")

    # -- 4. DSR on validation (dev) -----------------------------------------
    n_trials_pre = _trials.count_trials_for_dsr(STRATEGY_ID)
    print(
        f"\nn_trials_for_dsr({STRATEGY_ID}) before append = {n_trials_pre}"
    )

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

    # -- 5. Verdict tree ----------------------------------------------------
    valid_returns = [r for r in cpcv_result.per_block_returns if r.size > 0]
    concat_returns = (
        np.concatenate(valid_returns) if valid_returns else np.array([])
    )
    # Baseline = BTC/USDT B&H over the same dev window. BTC is the
    # market-state benchmark and is never traded by the strategy, so
    # buy-and-hold BTC is the natural "did conditioned momentum beat
    # just holding the market" counterfactual.
    baseline_df = data["BTC/USDT"]
    verdict = compute_verdict(
        strategy_id=STRATEGY_ID,
        sr_candidate=sr_observed,
        returns=concat_returns,
        total_trades=int(sum(cpcv_result.trades_per_path)),
        baseline_df=baseline_df,
        n_trials=n_trials_pre + 1,  # this trial included in the budget
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
    print(
        f"sr_zero_expected_at_eval= {verdict.sr_zero_expected_at_eval}"
    )
    print(
        f"baseline_sharpe_at_eval = {verdict.baseline_sharpe_at_eval:.4f}"
    )
    print(f"mt_mean_pass            = {verdict.mt_mean_pass}")
    print(f"baseline_pass           = {verdict.baseline_pass}")
    print(f"sr_margin_vs_mt_mean    = {verdict.sr_margin_vs_mt_mean}")
    print(f"sr_margin_vs_baseline   = {verdict.sr_margin_vs_baseline}")
    print(f"dsr                     = {verdict.dsr}")
    print(f"n_trials                = {verdict.n_trials}")

    # -- 6. Append trial row ------------------------------------------------
    notes = (
        "sq-031 first full_cpcv. Long-only TSMOM on 9-alt basket gated "
        "by BTC two-period market state (60-day windows). Deploy "
        "TSMOM only when prev=UP and curr=UP (continuation state per "
        "Cheema et al. 2017); neutralize on transitions or "
        "trending-down. BTC is benchmark-only (never traded); "
        "baseline = BTC/USDT B&H. "
        f"Warmup-aware CPCV: strategy_warmup_candles="
        f"{cpcv_config.strategy_warmup_candles}, effective_n_blocks="
        f"{effective_n_blocks}. "
        "Sources: Han et al. (2024) SSRN; Cheema et al. (2017) MPRA; "
        "Tzouvanas et al. (2019) Univ. of Southampton."
    )
    event = {
        "strategy_id": STRATEGY_ID,
        "variation_id": VARIATION_ID,
        "trial_type": "full_cpcv",
        "params": PARAMS,
        "hypothesis": HYPOTHESIS_TEXT,
        "split_holdout_start": entry["holdout_start"],
        "symbols": symbols,
        "n_trades": int(sum(cpcv_result.trades_per_path)),
        "sharpe": sr_observed,
        "cpcv": {
            "n_paths": effective_n_blocks,
            "n_blocks": effective_n_blocks,
            "k_held_out": int(cpcv_config.k_held_out),
            "purge_periods": int(cpcv_config.purge_periods),
            "embargo_periods": int(cpcv_config.embargo_periods),
            "strategy_warmup_candles": int(cpcv_config.strategy_warmup_candles),
            "min_tradeable_candles_per_block": int(
                cpcv_config.min_tradeable_candles_per_block
            ),
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
    print(
        f"n_trials_for_dsr({STRATEGY_ID}) after append  = {n_trials_post}"
    )

    # -- 7. Final summary block ---------------------------------------------
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
        "effective_n_blocks": effective_n_blocks,
        "dsr_validation": float(dsr_result.dsr),
        "n_trials_post_append": n_trials_post,
    }
    print("--- TRIAL SUMMARY JSON ---")
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
