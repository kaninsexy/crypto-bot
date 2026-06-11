"""scripts/run_pairs_trading_cointegration_trial.py -- Phase 4.C
PairsTradingCointegration trial #1 (variation stat-arb-coint-rotation-v1).

Runs the BTC/USDT vs ETH/USDT cointegration **rotation** strategy
(Option B2 reframe, 2026-05-06: long-only, at most one asset held at
a time -- engine_multi compatible without short-side support) through
the dev_cpcv harness (run_cpcv_multi), computes DSR + verdict, and
appends one trial_type='full_cpcv' row to backtest/trials.log via the
schema-validating writer. No holdout access, no commit, no deployment.

Structure mirrors run_meanreversion_btc_residual_phase4a_trial.py --
the authoritative two-symbol Phase 4 template.

Output is ASCII-only (Windows cp1252 terminal compatibility).
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
from strategies.pairs_trading_cointegration import (
    PairsTradingCointegrationStrategy,
)


STRATEGY_ID = "PairsTradingCointegration"
# Gate spec v2 (2026-06-11): explicit bar frequency for the
# units-correct DSR / MinTRL / verdict (manifest timeframe).
from backtest.dsr import bars_per_year_for_timeframe
BARS_PER_YEAR = bars_per_year_for_timeframe("1h")
VARIATION_ID = "stat-arb-coint-rotation-v1"

HYPOTHESIS_TEXT = (
    "Phase 4.C sq-005 Variation #1: cointegration-based rotation between "
    "BTC/USDT and ETH/USDT at 1H. Rolling 720-bar OLS hedge ratio, "
    "168-bar spread z-score, entry |z|>2.0, exit |z|<0.5, ADF p<0.05 "
    "cointegration filter. Long-only rotation -- no short positions. "
    "Sources: Park (2026) IJSRA; Carvalho (2021) UCP dissertation; "
    "Tadi & Witzany (2023) arXiv."
)

# PARAMS dict: keys mirror PairsTradingCointegrationStrategy.__init__
# keyword arguments exactly. symbols / symbol_a / symbol_b / timeframe
# come from the manifest at runtime and are not tuned, so they are not
# in PARAMS -- same convention as the MeanReversion_BTC_Residual script
# excludes `symbols` and `btc_symbol`.
PARAMS: dict = {
    "hedge_ratio_window": 720,
    "zscore_window": 168,
    "entry_z_threshold": 2.0,
    "exit_z_threshold": 0.5,
    "cointegration_pvalue_threshold": 0.05,
    "notional_capital": 10_000.0,
    "timeframe": "1h",
}


def _build_strategy(
    symbol_a: str, symbol_b: str,
) -> PairsTradingCointegrationStrategy:
    """Strategy factory.

    Per pre-trial gate #9: returns a FRESH
    PairsTradingCointegrationStrategy instance on every call so internal
    state (`_current_position`) resets between callers. run_cpcv_multi
    additionally deepcopies the instance per block via `_clone_strategy`,
    but constructing fresh here keeps the factory contract explicit at
    every call site.
    """
    return PairsTradingCointegrationStrategy(
        symbols=[symbol_a, symbol_b],
        symbol_a=symbol_a,
        symbol_b=symbol_b,
        timeframe=PARAMS["timeframe"],
        hedge_ratio_window=PARAMS["hedge_ratio_window"],
        zscore_window=PARAMS["zscore_window"],
        entry_z_threshold=PARAMS["entry_z_threshold"],
        exit_z_threshold=PARAMS["exit_z_threshold"],
        cointegration_pvalue_threshold=PARAMS[
            "cointegration_pvalue_threshold"
        ],
        notional_capital=PARAMS["notional_capital"],
    )


def main() -> int:
    print("=" * 72)
    print(
        "Phase 4.C -- PairsTradingCointegration trial #1 (dev_cpcv only)"
    )
    print("=" * 72)

    # -- 0. Sanity checks on the manifest entry --------------------------------
    manifest = load_manifest()
    if STRATEGY_ID not in manifest:
        print(f"FAIL: '{STRATEGY_ID}' not in manifest.", file=sys.stderr)
        return 1
    entry = manifest[STRATEGY_ID]
    symbols: list[str] = list(entry["symbols"])
    if len(symbols) != 2:
        print(
            f"FAIL: PairsTradingCointegration manifest must have exactly "
            f"2 symbols; got {symbols}.",
            file=sys.stderr,
        )
        return 1
    symbol_a, symbol_b = symbols[0], symbols[1]
    print(
        f"manifest entry: timeframe={entry['timeframe']} "
        f"holdout_start={entry['holdout_start']} "
        f"symbol_a={symbol_a} symbol_b={symbol_b}"
    )
    print(
        f"  strategy_warmup_candles={entry['strategy_warmup_candles']} "
        f"min_tradeable_candles_per_block="
        f"{entry['min_tradeable_candles_per_block']}"
    )

    # -- 1. Load dev frame + pivot into per-symbol dict ------------------------
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
            f"  {sym:<12} rows={len(df):>6} "
            f"range={df.index.min()} -> {df.index.max()}"
        )

    # -- 2. Headline backtest on the full dev window ---------------------------
    print("\n--- Headline run on full dev window ---")
    headline_strategy = _build_strategy(symbol_a, symbol_b)
    headline_result = run_engine_multi(
        data=data,
        strategy=headline_strategy,
        period_label="phase4c-pairs-coint-rotation-dev-headline",
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

    # -- 3. CPCV block-Sharpe distribution -------------------------------------
    print(
        "\n--- CPCV: requested n_blocks=10, "
        "warmup-aware downshift via compute_effective_n_blocks ---"
    )
    config = CPCVConfig(
        n_blocks=10,
        k_held_out=2,
        purge_periods=0,
        embargo_periods=0,
        strategy_warmup_candles=int(entry["strategy_warmup_candles"]),
        min_tradeable_candles_per_block=int(
            entry["min_tradeable_candles_per_block"]
        ),
    )
    cpcv_strategy = _build_strategy(symbol_a, symbol_b)
    try:
        cpcv_result = run_cpcv_multi(
            data=data,
            strategy=cpcv_strategy,
            config=config,
            initial_balance=10_000.0,
        )
    except CPCVError as exc:
        # Mandatory CPCVError handler per CLAUDE.md: insufficient-trades
        # failures must record a clean retire row and exit 0 rather than
        # crash the orchestrator into an `error` queue status that needs
        # manual cleanup. The cpcv block is filled with placeholder
        # zeros / NaN; the verdict is carried in `notes` since the
        # full_cpcv schema does not have a top-level `verdict` field.
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
            "symbols": symbols,
            "n_trades": 0,
            "sharpe": nan,
            "cpcv": {
                "n_paths": 0,
                "n_blocks": 0,
                "k_held_out": int(config.k_held_out),
                "purge_periods": int(config.purge_periods),
                "embargo_periods": int(config.embargo_periods),
                "strategy_warmup_candles": int(
                    config.strategy_warmup_candles
                ),
                "min_tradeable_candles_per_block": int(
                    config.min_tradeable_candles_per_block
                ),
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
                f"verdict=retire | CPCVError: {exc}. Insufficient "
                "trades; filter too strict or signal too sparse."
            ),
        }
        _trials.record_trial(cpcv_error_event)
        print(
            f"Trial row recorded. trial_id={cpcv_error_event['trial_id']} | "
            f"params_hash={cpcv_error_event['params_hash']} | "
            "Exiting cleanly."
        )
        # Emit the TRIAL SUMMARY JSON sentinel so the orchestrator's
        # parser sees a well-formed summary block even on the error path.
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

    # -- 4. DSR on validation (dev) --------------------------------------------
    # count_trials_for_dsr is computed BEFORE this trial is appended.
    # First full_cpcv for PairsTradingCointegration -> returns 0
    # (smoke excluded). dsr_from_cpcv_result calls that helper internally
    # and the DSRError raised at n_trials < 1 short-circuits. Mirror the
    # MeanReversion_BTC_Residual pattern and monkeypatch the lookup so
    # the deflation runs against n_trials=1.
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

    # -- 5. Verdict tree -------------------------------------------------------
    valid_returns = [r for r in cpcv_result.per_block_returns if r.size > 0]
    concat_returns = (
        np.concatenate(valid_returns) if valid_returns else np.array([])
    )
    # Baseline: BTC/USDT B&H. The strategy rotates between BTC and ETH;
    # the relevant counterfactual is passive BTC B&H (the default crypto
    # allocation that the rotation is trying to outperform).
    baseline_df = data[symbol_a]
    verdict = compute_verdict(
        strategy_id=STRATEGY_ID,
        sr_candidate=sr_observed,
        returns=concat_returns,
        total_trades=int(sum(cpcv_result.trades_per_path)),
        baseline_df=baseline_df,
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

    # -- 6. Append trial row ---------------------------------------------------
    notes = (
        "Phase 4.C variation #1 (B2 reframe approved 2026-05-06). "
        "BTC/USDT vs ETH/USDT cointegration-based **rotation** at 1H -- "
        "long-only by construction (at most one asset held at a time). "
        "This is directional rotation, NOT delta-neutral pairs trading: "
        "engine_multi is long-only and silently drops short legs, so the "
        "classical two-leg variant cannot be tested without engine "
        "changes. Algorithm: 720-bar OLS hedge ratio (cov/var in log "
        "space), 168-bar spread series, ADF p<0.05 cointegration filter, "
        "z<-2.0 -> hold BTC, z>+2.0 -> hold ETH, exit at |z|<0.5. "
        f"Warmup-aware CPCV: strategy_warmup_candles="
        f"{config.strategy_warmup_candles}, effective_n_blocks="
        f"{effective_n_blocks}. "
        "Sources: Park (2026); Carvalho (2021); Tadi & Witzany (2023)."
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
            "k_held_out": int(config.k_held_out),
            "purge_periods": int(config.purge_periods),
            "embargo_periods": int(config.embargo_periods),
            "strategy_warmup_candles": int(config.strategy_warmup_candles),
            "min_tradeable_candles_per_block": int(
                config.min_tradeable_candles_per_block
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
    _trials.record_trial(
        event,
        # Gate spec v2 (2026-06-11): persist the per-bar series the
        # verdict ran on (audit: never saved -> S1/bootstrap blocked).
        per_bar_returns=concat_returns,
        per_bar_benchmark=(
            baseline_df["close"].pct_change().dropna().values.astype(float)
        ),
    )
    print(
        f"\nappended trial row to backtest/trials.log | "
        f"trial_id={event['trial_id']} | "
        f"params_hash={event['params_hash']}"
    )

    n_trials_post = _trials.count_trials_for_dsr(STRATEGY_ID)
    print(
        f"n_trials_for_dsr({STRATEGY_ID}) after append  = {n_trials_post}"
    )

    # -- 7. Final summary block ------------------------------------------------
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
