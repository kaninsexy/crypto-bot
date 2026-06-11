"""scripts/run_news_sentiment_momentum_trial.py -- NewsSentimentMomentum trial.

Runs the NewsSentimentMomentum long-only top-N tercile-rotation
strategy on a 7-symbol crypto universe at 1D through dev_cpcv
(run_cpcv_multi), computes DSR + verdict, and appends one
trial_type='full_cpcv' row to backtest/trials.log via the
schema-validating writer. No holdout access, no commit, no
deployment.

News-sentiment data source:

  No paid news-sentiment API is currently wired into this repo, so
  the trial uses a deterministic, OHLCV-derived proxy: the
  volume-weighted log-return per daily bar. Specifically, for each
  symbol:

      log_ret[t]    = log(close[t] / close[t-1])
      vol_baseline  = mean(volume over baseline_window=30 prior bars)
      vol_ratio[t]  = volume[t] / vol_baseline (clipped to [0.1, 10])
      news_score[t] = log_ret[t] * vol_ratio[t]

  Volume-weighted return is the standard equity-finance proxy for
  news-driven price moves (Engelberg & Parsons 2011; Tetlock 2007):
  high-volume up moves coincide with positive news, high-volume
  down moves with negative news, while low-volume drift is treated
  as noise. Burggraf (2022) explicitly uses volume-amplified return
  signals as a news-sentiment proxy when extracting the directional
  component for the long-only Bitcoin trading simulation.

  The strategy class itself is data-source agnostic; swapping in a
  paid news-sentiment feed (e.g. CryptoCompare News, CoinDesk
  Sentiment, RavenPack) requires changing only the
  ``_compute_news_score_for_symbol`` function in this script. The
  rebalance / sizing / verdict logic is unaffected.

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
from strategies.news_sentiment_momentum import NewsSentimentMomentumStrategy


STRATEGY_ID = "NewsSentimentMomentum"
# Gate spec v2 (2026-06-11): explicit bar frequency for the
# units-correct DSR / MinTRL / verdict (manifest timeframe).
from backtest.dsr import bars_per_year_for_timeframe
BARS_PER_YEAR = bars_per_year_for_timeframe("1d")
VARIATION_ID = "news-sentiment-momentum"

HYPOTHESIS_TEXT = (
    "NewsSentimentMomentum Variation #1: cross-sectional news "
    "sentiment momentum on a 7-symbol crypto basket "
    "(BTC/ETH/SOL/BNB/XRP/ADA/AVAX) at 1D. On each daily rebalance "
    "bar, rank symbols by their 24-hour news-sentiment proxy "
    "(volume-weighted log return) descending; long the top tercile "
    "(top_n=2 of 7, equal weight) and hold for 1 day before the "
    "next rebalance. Long-only; short leg of the Chen/Hafner/Weber "
    "(2023) and Kalamara et al (2022) long-short tercile / decile "
    "specifications dropped per Han et al. (2024) loser-rebound "
    "precedent (same rationale as sq-013/sq-016/sq-018/sq-020). "
    "Sources: Chen/Hafner/Weber (2023) JIFMIM; Kalamara et al "
    "(2022) JIFMIM; Burggraf (2022) FRL; Han/Kang/Ryu (2024) SSRN."
)

PARAMS: dict = {
    "sentiment_window": 1,
    "top_n": 2,
    "holding_period": 1,
    "notional_capital": 10_000.0,
    "timeframe": "1d",
    "news_score_proxy": "volume_weighted_log_return",
    "baseline_window": 30,
}

# Volume-ratio clip bounds keep the news-score proxy finite when a
# symbol's baseline volume is artificially small (e.g. exchange
# listing day, low-liquidity regime). Symmetric in log space at
# log(10) ~ 2.30; chosen so a 10x volume spike is the maximum
# amplification factor and a 10x volume crash the minimum.
_VOLUME_RATIO_CLIP_LOW = 0.1
_VOLUME_RATIO_CLIP_HIGH = 10.0


def _compute_news_score_for_symbol(
    df: pd.DataFrame,
    baseline_window: int,
) -> pd.Series:
    """Volume-weighted log-return news-sentiment proxy.

    Returns a pd.Series of news_score values indexed identically to
    ``df.index``. Values are NaN until ``baseline_window + 1`` bars
    of history are available.
    """
    close = df["close"].astype(float)
    volume = df["volume"].astype(float)

    log_ret = np.log(close / close.shift(1))

    # Trailing baseline volume excluding the current bar (so the
    # ratio at t reflects volume vs. the prior baseline_window bars,
    # not a window that includes t itself).
    baseline_vol = (
        volume.shift(1)
        .rolling(window=baseline_window, min_periods=baseline_window)
        .mean()
    )

    vol_ratio = (volume / baseline_vol).clip(
        lower=_VOLUME_RATIO_CLIP_LOW, upper=_VOLUME_RATIO_CLIP_HIGH,
    )

    score = log_ret * vol_ratio
    score = score.replace([np.inf, -np.inf], np.nan)
    return score


def _build_strategy(
    symbols: list[str],
    sentiment_data: dict[str, pd.Series],
) -> NewsSentimentMomentumStrategy:
    """Strategy factory: fresh instance with reset internal state."""
    return NewsSentimentMomentumStrategy(
        symbols=symbols,
        sentiment_data=sentiment_data,
        timeframe=PARAMS["timeframe"],
        sentiment_window=PARAMS["sentiment_window"],
        top_n=PARAMS["top_n"],
        holding_period=PARAMS["holding_period"],
        notional_capital=PARAMS["notional_capital"],
    )


def main() -> int:
    print("=" * 72)
    print(
        "NewsSentimentMomentum trial #1 (dev_cpcv only)"
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

    # -- Build per-symbol news-sentiment proxy series ------------------------
    print("\n--- Computing news-sentiment proxy per symbol ---")
    print(
        f"  proxy = volume_weighted_log_return | "
        f"baseline_window={PARAMS['baseline_window']} | "
        f"vol_ratio_clip=[{_VOLUME_RATIO_CLIP_LOW}, {_VOLUME_RATIO_CLIP_HIGH}]"
    )
    sentiment_data: dict[str, pd.Series] = {}
    for sym in symbols:
        score = _compute_news_score_for_symbol(
            data[sym], baseline_window=PARAMS["baseline_window"],
        )
        nan_count = int(score.isna().sum())
        finite_count = int(np.isfinite(score).sum())
        sentiment_data[sym] = score
        print(
            f"  {sym:<12} score_rows={len(score)} "
            f"nan={nan_count} finite={finite_count} "
            f"min={float(score.min()):+.4f} max={float(score.max()):+.4f}"
        )

    print("\n--- Headline run on full dev window ---")
    headline_strategy = _build_strategy(symbols, sentiment_data)
    headline_result = run_engine_multi(
        data=data,
        strategy=headline_strategy,
        period_label="news-sentiment-momentum-dev-headline",
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
    cpcv_strategy = _build_strategy(symbols, sentiment_data)
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
    # Baseline = BTC/USDT B&H over the same dev window. Chen/Hafner/
    # Weber (2023) and Burggraf (2022) frame the news-sentiment
    # strategy against BTC buy-and-hold; BTC is the explicit
    # counterfactual.
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
        "NewsSentimentMomentum variation #1. Long-only top-tercile "
        "news-sentiment momentum basket on 7-symbol 1D universe. "
        "Each daily rebalance bar ranks symbols by 24-hour "
        "volume-weighted log return (proxy for news shock) "
        "descending and holds top-2 (equal weight) for the next "
        "day. Engine handles 1/N sizing via position_fraction(). "
        "Baseline = BTC/USDT B&H. Short leg of the proposal "
        "specification dropped per Han et al. (2024) loser-rebound "
        "precedent. News-sentiment data source: "
        "volume-weighted-log-return proxy (Engelberg/Parsons 2011 "
        "equity precedent; Burggraf 2022 directional sentiment "
        "proxy). "
        f"Warmup-aware CPCV: strategy_warmup_candles="
        f"{config.strategy_warmup_candles}, effective_n_blocks="
        f"{effective_n_blocks}. "
        "Sources: Chen/Hafner/Weber (2023); Kalamara et al (2022); "
        "Burggraf (2022); Han/Kang/Ryu (2024)."
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
