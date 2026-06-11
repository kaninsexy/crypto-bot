"""scripts/run_exchange_listing_drift_trial.py -- sq-037 trial.

Runs the ExchangeListingDrift long-only event-driven strategy on
BTC/USDT 1D through the single-symbol dev_cpcv harness (run_cpcv)
with a deterministic OHLCV-derived event-score proxy held in-
strategy. No holdout access, no commit, no deployment.

Event-score data source:

  No live Coinbase / Binance announcement feed is wired into this
  repo, so the trial uses a deterministic OHLCV-derived proxy: a
  rolling abnormal-volume z-score gated on positive-return bars.
  Specifically, for each daily bar:

      log_ret[t]    = log(close[t] / close[t-1])
      vol_baseline  = mean(volume over baseline_window=30 prior bars)
      vol_std       = std(volume over baseline_window=30 prior bars)
      vol_z[t]      = (volume[t] - vol_baseline) / max(vol_std, eps)
      event_score[t] = vol_z[t] if log_ret[t] > 0 else 0.0

  Volume z-score on positive-return days is the standard event-
  study proxy for "an announcement-like news event hit today" when
  no announcement feed is available: listing announcements drive
  abnormal volume + positive price movement, while flat or negative
  return bars are not announcement-driven (Corbet et al. 2020 use a
  2-sigma jump filter to identify listing-news-driven jumps; Le et
  al. 2021 document significant abnormal volume on Coinbase
  announcement days). The 30-day baseline is the conventional
  event-study window in the related equity literature.

  The strategy class itself is data-source agnostic; swapping in a
  real announcement feed (Coinbase blog scrape, Binance announcement
  RSS, Twitter API on @binance / @coinbase) requires changing only
  ``_compute_event_score_proxy`` below. The verdict / verdict-tree
  logic is unaffected.

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
from strategies.exchange_listing_drift import (
    ExchangeListingDriftStrategy,
)


STRATEGY_ID = "ExchangeListingDrift"
# Gate spec v2 (2026-06-11): explicit bar frequency for the
# units-correct DSR / MinTRL / verdict (manifest timeframe).
from backtest.dsr import bars_per_year_for_timeframe
BARS_PER_YEAR = bars_per_year_for_timeframe("1d")
VARIATION_ID = "major-exchange-listing-announcement"

# Window over which the rolling baseline volume mean and standard
# deviation are computed. 30 daily bars matches the conventional
# event-study baseline window (one trading month) in the cited
# equity-event-study literature.
EVENT_BASELINE_WINDOW = 30
# Numerical-stability floor for the volume-std denominator.
_VOL_STD_FLOOR = 1e-9

HYPOTHESIS_TEXT = (
    "sq-037 variation #1 (major-exchange-listing-announcement): "
    "cryptocurrencies experience a significant positive price drift "
    "in the days immediately following a listing announcement on a "
    "top-tier exchange. Captured on BTC/USDT 1D via an OHLCV-derived "
    "event-score proxy (no announcement feed wired): rolling 30-day "
    "abnormal-volume z-score gated on positive-return bars; long "
    "when score > +2.0; exit after 5 days (Le et al. 2021 5-day "
    "post-announcement window). Long-only on spot per project "
    "conventions. Sources: Le, Nguyen, Park (2021) Finance Research "
    "Letters; Mazabel, Sciandra (2022) SSRN; Corbet et al. (2020) "
    "Review of Financial Studies; Han, Kang, Ryu (2024) SSRN "
    "(long-only adaptation precedent)."
)

PARAMS: dict = {
    "entry_threshold": 2.0,
    "holding_period": 5,
    "event_baseline_window": EVENT_BASELINE_WINDOW,
    "event_score_proxy": "abnormal_volume_zscore_positive_return_gated",
    "timeframe": "1d",
    "symbol": "BTC/USDT",
}


def _compute_event_score_proxy(
    df: pd.DataFrame,
    baseline_window: int,
) -> pd.Series:
    """Return a daily event-score proxy series aligned to df.index.

    Algorithm:
      log_ret[t]    = log(close[t] / close[t-1])
      vol_mean[t]   = mean(volume[t-window..t-1])
      vol_std[t]    = std(volume[t-window..t-1])
      vol_z[t]      = (volume[t] - vol_mean[t]) / max(vol_std[t], eps)
      event[t]      = vol_z[t] if log_ret[t] > 0 else 0.0

    NaN for the first ``baseline_window`` rows (insufficient history).
    """
    if not {"open", "close", "volume"}.issubset(df.columns):
        raise ValueError(
            "event-score proxy requires open/close/volume columns in df"
        )
    close = df["close"].astype(float)
    volume = df["volume"].astype(float)

    log_ret = np.log(close / close.shift(1))

    # Trailing baseline excluding the current bar so the z-score at t
    # reflects volume vs. the prior baseline_window bars, not a window
    # that includes t itself (Le et al. 2021 estimation-window
    # convention for event studies).
    baseline_mean = (
        volume.shift(1)
        .rolling(window=baseline_window, min_periods=baseline_window)
        .mean()
    )
    baseline_std = (
        volume.shift(1)
        .rolling(window=baseline_window, min_periods=baseline_window)
        .std(ddof=0)
    )
    denom = baseline_std.where(
        baseline_std > _VOL_STD_FLOOR, _VOL_STD_FLOOR,
    )
    vol_z = (volume - baseline_mean) / denom

    score = vol_z.where(log_ret > 0, 0.0)
    score = score.replace([np.inf, -np.inf], np.nan)
    return score.astype(float)


def main() -> int:
    print("=" * 72)
    print("sq-037 -- ExchangeListingDrift trial #1 (dev_cpcv only)")
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

    # 1. Load dev OHLCV.
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

    # 2. Compute the OHLCV-derived event-score proxy.
    event_series = _compute_event_score_proxy(
        dev_df, baseline_window=EVENT_BASELINE_WINDOW,
    )
    valid_event = event_series.dropna()
    nonzero_event = valid_event[valid_event != 0.0]
    n_event_days = int((valid_event > PARAMS["entry_threshold"]).sum())
    print(
        f"event-score proxy: rows={len(event_series)} "
        f"valid={len(valid_event)} nonzero={len(nonzero_event)} "
        f"baseline_window={EVENT_BASELINE_WINDOW} | "
        f"min={valid_event.min():+.3f} median={valid_event.median():+.3f} "
        f"max={valid_event.max():+.3f} | "
        f"n_event_days(score>{PARAMS['entry_threshold']})={n_event_days}"
    )

    # 3. Strategy factory: fresh instance per call so per-instance
    #    state resets across CPCV blocks. The event series is captured
    #    by closure and shared read-only across instances.
    def make_strategy() -> ExchangeListingDriftStrategy:
        return ExchangeListingDriftStrategy(
            symbol=symbol,
            timeframe=PARAMS["timeframe"],
            event_score_series=event_series,
            entry_threshold=PARAMS["entry_threshold"],
            holding_period=PARAMS["holding_period"],
        )

    # 4. Headline backtest on the full dev window.
    print("\n--- Headline run on full dev window ---")
    headline_engine = BacktestEngine(
        initial_balance=10_000.0,
        warm_up_candles=int(entry["strategy_warmup_candles"]),
        verbose=False,
    )
    headline_result = headline_engine.run(
        df=dev_df,
        strategy=make_strategy(),
        period_label="sq-037-listing-drift-dev-headline",
    )
    sr_observed = float(headline_result.metrics.sharpe_ratio)
    n_trades_headline = int(headline_result.metrics.total_trades)
    print(
        f"headline Sharpe = {sr_observed:.4f} | "
        f"n_trades = {n_trades_headline} | "
        f"return_pct = {headline_result.metrics.total_return_pct:+.2f}% | "
        f"max_dd = {headline_result.metrics.max_drawdown_pct:.2f}%"
    )

    # 5. CPCV block-Sharpe distribution.
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
                f"verdict=retire | CPCVError: {exc}. Listing-drift "
                "event-score signal too sparse to meet the per-block "
                "trade-count floor."
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

    # 6. DSR on validation (dev). First full_cpcv -> monkeypatch
    #    n_trials_for_dsr to max(N, 1) so the deflation runs.
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

    # 7. Verdict tree (dev-side preview).
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
    print(f"sr_observed             = {verdict.sr_observed:.4f}")
    print(
        f"baseline_sharpe_at_eval = {verdict.baseline_sharpe_at_eval:.4f}"
    )
    print(f"mt_mean_pass            = {verdict.mt_mean_pass}")
    print(f"baseline_pass           = {verdict.baseline_pass}")
    print(f"dsr                     = {verdict.dsr}")
    print(f"n_trials                = {verdict.n_trials}")

    notes = (
        "sq-037 variation #1 (major-exchange-listing-announcement). "
        "Single-symbol BTC/USDT 1D long-only. Event-score proxy: "
        f"rolling {EVENT_BASELINE_WINDOW}-day abnormal-volume z-score "
        "gated on positive-return bars (no announcement feed wired). "
        "Entry score>+2.0; time-based exit after 5 days (Le et al. "
        "2021 5-day post-announcement abnormal-return window). "
        "Baseline = BTC/USDT B&H over the same dev window. Sources: "
        "Le, Nguyen, Park (2021) FRL; Mazabel, Sciandra (2022) SSRN; "
        "Corbet et al. (2020) RFS; Han, Kang, Ryu (2024) SSRN "
        "(long-only adaptation)."
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
