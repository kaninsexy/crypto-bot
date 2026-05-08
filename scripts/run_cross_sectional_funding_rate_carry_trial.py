"""scripts/run_cross_sectional_funding_rate_carry_trial.py -- sq-038.

Runs the CrossSectionalFundingRateCarry long-only bottom-N
(lowest-funding) tercile-rotation strategy on a 7-symbol crypto
universe at 1D through dev_cpcv (run_cpcv_multi), computes DSR +
verdict, and appends one trial_type='full_cpcv' row to
backtest/trials.log via the schema-validating writer. No holdout
access, no commit, no deployment.

Funding-rate data source:

  Per-symbol 8-hour OKX USDT-M perpetual funding-rate history is
  fetched via data.okx_funding.load_or_fetch_funding_history
  (cache-wrapped; per-month archive parquets under
  backtest/cache/perp_funding/archive/{instId}/). The 8-hour series
  is then aggregated to a daily series by taking the mean of the
  three intra-day funding settlements ending at each UTC day-close.
  The daily funding series is reindexed onto each symbol's OHLCV
  daily index and forward-filled across rare missing settlements so
  the strategy receives a value at every trading bar.

  The strategy class itself is data-source agnostic; swapping in a
  different exchange (Binance, Bybit) or a different aggregation
  rule (last-rate, sum, predicted-rate) requires changing only
  ``_load_funding_for_symbol`` in this script. The rebalance /
  sizing / verdict logic is unaffected.

Output is ASCII-only (Windows cp1252 terminal compatibility).
"""

from __future__ import annotations

import json
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
from data import okx_funding
from strategies.cross_sectional_funding_rate_carry import (
    CrossSectionalFundingRateCarryStrategy,
)


STRATEGY_ID = "CrossSectionalFundingRateCarry"
VARIATION_ID = "cs-funding-rate-carry-v1"

HYPOTHESIS_TEXT = (
    "CrossSectionalFundingRateCarry sq-038 Variation #1: "
    "long-only cross-sectional funding-rate carry on a 7-symbol "
    "crypto basket (BTC/ETH/SOL/BNB/XRP/ADA/AVAX) at 1D. On each "
    "daily rebalance bar, rank symbols by their daily-aggregated "
    "OKX USDT-M perpetual funding rate ASCENDING (lowest / "
    "most-negative first); long the bottom tercile (top_n=2 of 7, "
    "equal weight) and hold for 1 day before the next rebalance. "
    "Long-only; short leg of the Bianchi et al. (2022) and "
    "Abedifar et al. (2023) dollar-neutral specifications dropped "
    "per Han et al. (2024) loser-rebound precedent (same rationale "
    "as sq-013/sq-016/sq-018/sq-020/news-sentiment-momentum). "
    "Sources: Bianchi/Babiak/Ciner (2022) JIFMIM; Abedifar/Fica/"
    "Imbierowicz (2023) SSRN; ryanczm (2024) GitHub Crypto-Stat-"
    "Arb; Han/Kang/Ryu (2024) SSRN."
)

PARAMS: dict = {
    "funding_window": 1,
    "top_n": 2,
    "holding_period": 1,
    "notional_capital": 10_000.0,
    "timeframe": "1d",
    "funding_source": "okx_usdt_swap",
    "funding_aggregation": "daily_mean_of_8h_settlements",
}

# Funding cache lookback buffer (months) past the manifest data window.
# data_start 2023-03-06 is ~38 months before now; round up so the
# fetch always covers the dev window with a small overlap to absorb
# OKX's ~94d live-API cutover boundary.
_FUNDING_LOOKBACK_MONTHS: int = 40


def _load_funding_for_symbol(
    sym: str,
    daily_index: pd.DatetimeIndex,
    holdout_start: pd.Timestamp,
) -> pd.Series:
    """Return a per-symbol daily funding-rate Series aligned to ``daily_index``.

    The 8-hour OKX funding history is mean-aggregated to a daily
    series by grouping settlement timestamps to their UTC date.
    Days with no settlement (rare) are forward-filled from the prior
    daily mean so the strategy receives a value at every trading bar.

    Returns a Series of NaN when okx_funding raises -- this lets the
    trial proceed and the strategy's _compute_funding_score short-
    circuit on missing data rather than crashing the trial.
    """
    try:
        funding_8h = okx_funding.load_or_fetch_funding_history(
            sym,
            months=_FUNDING_LOOKBACK_MONTHS,
            until_ts=holdout_start,
        )
    except Exception as exc:
        print(
            f"  WARN: funding fetch failed for {sym}: {exc}; "
            "falling back to NaN series."
        )
        return pd.Series(
            data=np.nan, index=daily_index, name="funding_rate", dtype=float,
        )

    if funding_8h is None or funding_8h.empty:
        print(
            f"  WARN: funding history empty for {sym}; "
            "falling back to NaN series."
        )
        return pd.Series(
            data=np.nan, index=daily_index, name="funding_rate", dtype=float,
        )

    rate_8h = funding_8h["funding_rate"].astype(float)
    # Group 8h settlements to UTC dates. Each daily index in the
    # OHLCV cache is at 00:00 UTC; the funding rate "for day D" is
    # the mean of the three settlements timestamped at 00/08/16 of
    # day D (i.e. settlements that occurred during day D).
    daily_mean = rate_8h.groupby(rate_8h.index.normalize()).mean()
    daily_mean.index = pd.DatetimeIndex(daily_mean.index, tz="UTC")

    aligned = daily_mean.reindex(daily_index).ffill()
    aligned.name = "funding_rate"
    return aligned.astype(float)


def _build_strategy(
    symbols: list[str],
    funding_data: dict[str, pd.Series],
) -> CrossSectionalFundingRateCarryStrategy:
    """Strategy factory: fresh instance with reset internal state."""
    return CrossSectionalFundingRateCarryStrategy(
        symbols=symbols,
        funding_data=funding_data,
        timeframe=PARAMS["timeframe"],
        funding_window=PARAMS["funding_window"],
        top_n=PARAMS["top_n"],
        holding_period=PARAMS["holding_period"],
        notional_capital=PARAMS["notional_capital"],
    )


def main() -> int:
    print("=" * 72)
    print(
        "sq-038 -- CrossSectionalFundingRateCarry trial #1 (dev_cpcv only)"
    )
    print("=" * 72)

    manifest = load_manifest()
    if STRATEGY_ID not in manifest:
        print(f"FAIL: '{STRATEGY_ID}' not in manifest.", file=sys.stderr)
        return 1
    entry = manifest[STRATEGY_ID]
    symbols: list[str] = list(entry["symbols"])
    holdout_start = pd.Timestamp(entry["holdout_start"])
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

    # -- Fetch + align per-symbol funding-rate series ------------------------
    print("\n--- Loading per-symbol funding-rate history ---")
    print(
        f"  source = okx_usdt_swap | "
        f"months={_FUNDING_LOOKBACK_MONTHS} | "
        f"aggregation = daily_mean_of_8h_settlements"
    )
    funding_data: dict[str, pd.Series] = {}
    for sym in symbols:
        daily_index = data[sym].index
        series = _load_funding_for_symbol(sym, daily_index, holdout_start)
        nan_count = int(series.isna().sum())
        finite_count = int(np.isfinite(series).sum())
        funding_data[sym] = series
        if finite_count > 0:
            min_v = float(series.min(skipna=True))
            max_v = float(series.max(skipna=True))
            mean_v = float(series.mean(skipna=True))
            print(
                f"  {sym:<12} rows={len(series)} "
                f"nan={nan_count} finite={finite_count} "
                f"mean={mean_v:+.6f} min={min_v:+.6f} max={max_v:+.6f}"
            )
        else:
            print(
                f"  {sym:<12} rows={len(series)} "
                f"nan={nan_count} finite={finite_count} "
                f"(all-NaN; symbol will be excluded from ranking)"
            )

    print("\n--- Headline run on full dev window ---")
    headline_strategy = _build_strategy(symbols, funding_data)
    headline_result = run_engine_multi(
        data=data,
        strategy=headline_strategy,
        period_label="cs-funding-rate-carry-dev-headline",
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
    cpcv_strategy = _build_strategy(symbols, funding_data)
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
    # Baseline = BTC/USDT B&H over the same dev window. Bianchi et
    # al. (2022) frame the carry portfolio against the cap-weighted
    # crypto market index; BTC is the closest available proxy and
    # the explicit counterfactual used by every other cross-sectional
    # trial in this codebase.
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
        "sq-038 variation #1. Long-only bottom-tercile cross-"
        "sectional funding-rate carry basket on 7-symbol 1D "
        "universe. Each daily rebalance bar ranks symbols by "
        "daily-mean OKX USDT-M perpetual funding rate ascending "
        "and holds bottom-2 (lowest funding, equal weight) for the "
        "next day. Engine handles 1/N sizing via "
        "position_fraction(). Baseline = BTC/USDT B&H. Short leg "
        "of the proposal specification dropped per Han et al. "
        "(2024) loser-rebound precedent. Funding-rate data source: "
        "OKX USDT-M perpetual 8-hour funding-rate history "
        "(data.okx_funding.load_or_fetch_funding_history), "
        "aggregated to daily via mean of 3 intra-day settlements. "
        f"Warmup-aware CPCV: strategy_warmup_candles="
        f"{config.strategy_warmup_candles}, effective_n_blocks="
        f"{effective_n_blocks}. "
        "Sources: Bianchi/Babiak/Ciner (2022); Abedifar/Fica/"
        "Imbierowicz (2023); ryanczm (2024); Han/Kang/Ryu (2024)."
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
