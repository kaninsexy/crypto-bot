"""scripts/run_attention_momentum_trial.py -- sq-018 trial.

Runs the AttentionMomentum long-only top-N attention-rotation
strategy on a 5-symbol crypto universe at 1D through dev_cpcv
(run_cpcv_multi), computes DSR + verdict, and appends one
trial_type='full_cpcv' row to backtest/trials.log via the
schema-validating writer.  No holdout access, no commit, no
deployment.

Google Trends weekly search-volume series is fetched per symbol via
the existing data.google_trends loader (24h cache, exponential
backoff on 429), resampled to daily via ffill at the source, then
reindexed onto each symbol's OHLCV index inside this script before
being handed to the strategy.

If any pytrends fetch fails after retries the script prints the
error and exits 1 cleanly; the orchestrator records the failure as
an error status, not a partial trial.

Output is ASCII-only (Windows cp1252 terminal compatibility).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

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
from data.google_trends import GoogleTrendsError, load_or_fetch_trends
from strategies.attention_momentum import AttentionMomentumStrategy


STRATEGY_ID = "AttentionMomentum"
# Gate spec v2 (2026-06-11): explicit bar frequency for the
# units-correct DSR / MinTRL / verdict (manifest timeframe).
from backtest.dsr import bars_per_year_for_timeframe
BARS_PER_YEAR = bars_per_year_for_timeframe("1d")
VARIATION_ID = "search-volume-momentum"

TRENDS_HISTORY_MONTHS = 38

# Per-symbol Google Trends keywords.  Local to the trial script so we
# do not modify the shared data/google_trends.py mapping (which the
# retired sq-011 trial established for BTC + ETH).  Additive
# extensions only.
SYMBOL_TO_KEYWORD: dict[str, str] = {
    "BTC/USDT": "bitcoin",
    "ETH/USDT": "ethereum",
    "SOL/USDT": "solana",
    "BNB/USDT": "binance coin",
    "XRP/USDT": "xrp",
}

HYPOTHESIS_TEXT = (
    "Phase 4 sq-018 Variation #1: cross-sectional Google Trends "
    "attention momentum on a 5-symbol crypto basket "
    "(BTC/ETH/SOL/BNB/XRP) at 1D. On each rebalance bar, rank "
    "symbols by attention-momentum score (1-week SV avg / 4-week SV "
    "avg - 1) descending; long the top quintile (top_n=1 of 5, "
    "equal weight) and hold for 7 days before the next rebalance. "
    "Long-only; short leg of the original long-short specification "
    "dropped per Han et al. (2024) loser-rebound precedent (same "
    "rationale as sq-016/sq-020). Sources: Lin/Chiu (2022) "
    "NAJEF; Bampinas et al (2022) Forecasting; You/Yang (2020) "
    "SSRN."
)

PARAMS: dict = {
    "short_window": 7,
    "long_window": 28,
    "top_n": 1,
    "holding_period": 7,
    "notional_capital": 10_000.0,
    "timeframe": "1d",
}


def _load_trends_for_symbol(symbol: str) -> pd.Series:
    """Fetch + cache the Google Trends daily search_volume series.

    Returns a pd.Series indexed by tz-aware UTC daily timestamps,
    values in [0, 100].
    """
    keyword = SYMBOL_TO_KEYWORD[symbol]
    df = load_or_fetch_trends(
        keyword=keyword, months=TRENDS_HISTORY_MONTHS,
        # 2026-06-11: dev re-run only needs coverage to dev_end;
        # coverage-first check avoids a stale-mtime refetch (and
        # the Trends 429 risk) when the cached span already covers.
        required_end_date=load_manifest()[STRATEGY_ID]["dev_end"],
    )
    if "search_volume" not in df.columns:
        raise GoogleTrendsError(
            f"Google Trends payload for {symbol} ({keyword!r}) "
            f"missing 'search_volume' column."
        )
    return df["search_volume"].astype(float).sort_index()


def _build_strategy(
    symbols: list[str],
    sv_data: dict[str, pd.Series],
) -> AttentionMomentumStrategy:
    """Strategy factory: fresh instance with reset internal state."""
    return AttentionMomentumStrategy(
        symbols=symbols,
        sv_data=sv_data,
        timeframe=PARAMS["timeframe"],
        short_window=PARAMS["short_window"],
        long_window=PARAMS["long_window"],
        top_n=PARAMS["top_n"],
        holding_period=PARAMS["holding_period"],
        notional_capital=PARAMS["notional_capital"],
    )


def main() -> int:
    print("=" * 72)
    print(
        "Phase 4 sq-018 -- AttentionMomentum trial #1 (dev_cpcv only)"
    )
    print("=" * 72)

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

    # Verify every manifest symbol has a Google Trends keyword.
    missing_kw = [s for s in symbols if s not in SYMBOL_TO_KEYWORD]
    if missing_kw:
        print(
            f"FAIL: no Google Trends keyword mapping for: {missing_kw}",
            file=sys.stderr,
        )
        return 1

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

    # ── Fetch Google Trends per symbol and align to OHLCV index ─────────────
    print("\n--- Fetching Google Trends search-volume per symbol ---")
    sv_data: dict[str, pd.Series] = {}
    for sym in symbols:
        try:
            sv_raw = _load_trends_for_symbol(sym)
        except GoogleTrendsError as exc:
            print(
                f"FAIL: GoogleTrendsError for {sym}: {exc}",
                file=sys.stderr,
            )
            return 1
        ohlcv_idx = data[sym].index
        aligned = (
            sv_raw.sort_index()
            .reindex(ohlcv_idx, method="ffill")
            .astype(float)
        )
        nan_count = int(aligned.isna().sum())
        sv_data[sym] = aligned
        print(
            f"  {sym:<12} keyword={SYMBOL_TO_KEYWORD[sym]!r} "
            f"sv_rows={len(sv_raw)} aligned_nan={nan_count}"
        )

    print("\n--- Headline run on full dev window ---")
    headline_strategy = _build_strategy(symbols, sv_data)
    headline_result = run_engine_multi(
        data=data,
        strategy=headline_strategy,
        period_label="phase4-attention-momentum-dev-headline",
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
    cpcv_strategy = _build_strategy(symbols, sv_data)
    try:
        cpcv_result = run_cpcv_multi(
            data=data,
            strategy=cpcv_strategy,
            config=config,
            initial_balance=10_000.0,
        )
    except CPCVError as exc:
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
                "rotation events across blocks."
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

    valid_returns = [r for r in cpcv_result.per_block_returns if r.size > 0]
    concat_returns = (
        np.concatenate(valid_returns) if valid_returns else np.array([])
    )
    # Baseline = BTC/USDT B&H over the same dev window.  Lin & Chiu
    # (2022) and Bampinas et al (2022) frame attention-momentum
    # against BTC buy-and-hold; BTC is the explicit counterfactual.
    baseline_df = data["BTC/USDT"]
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

    notes = (
        "Phase 4 sq-018 variation #1. Long-only top-N attention "
        "momentum basket on 5-symbol 1D universe. Each rebalance "
        "bar (every 7 days) ranks symbols by sv_mom = (mean(SV last "
        "7d) / mean(SV last 28d) - 1) descending and holds top-1 "
        "(equal weight) for the next 7 days. Engine handles 1/N "
        "sizing via position_fraction(). Google Trends weekly data "
        "ffilled to daily then reindexed per symbol's OHLCV index. "
        "Baseline = BTC/USDT B&H. Short leg of the proposal "
        "specification dropped per Han et al. (2024) loser-rebound "
        "precedent. "
        f"Warmup-aware CPCV: strategy_warmup_candles="
        f"{config.strategy_warmup_candles}, effective_n_blocks="
        f"{effective_n_blocks}. "
        "Sources: Lin/Chiu (2022); Bampinas et al (2022); "
        "You/Yang (2020); Han/Kang/Ryu (2024)."
    )
    if os.environ.get("GATE_V2_RERUN_2026_06_11") == "1":
        notes = (
            "Extended-window re-test under gate spec v2 (2026-06-11 "
            "work order): same hypothesis + params as the original "
            "trial, extended substrate window (BNB backfilled from "
            "Binance 2021-01-01->2022-12-21, see manifest notes), "
            "units-correct DSR/MinTRL, family-scaled eq.7, alpha/IR "
            "baseline gate. Consumes no new variation slot. " + notes
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
