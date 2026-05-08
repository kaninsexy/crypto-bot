"""scripts/run_put_call_ratio_contrarian_trial.py -- sq-036 trial.

Runs the PutCallRatioContrarian strategy on BTC/USDT 1D through the
single-symbol dev_cpcv harness (run_cpcv) with a deterministic OHLCV-
derived Put/Call Ratio (PCR) proxy held in-strategy. No holdout
access, no commit, no deployment.

PCR data source:

  No Deribit options feed is wired into this repo, so the trial uses
  a deterministic OHLCV-derived proxy: the rolling ratio of down-day
  volume to up-day volume. Specifically, for each daily bar:

      up_flow[d]    = volume[d] if close[d] > open[d] else 0
      down_flow[d]  = volume[d] if close[d] < open[d] else 0
      pcr_proxy[t]  = sum_{k=t-13..t}(down_flow[k]) /
                      max(sum_{k=t-13..t}(up_flow[k]), 1e-9)

  Down-vs-up volume ratio is the standard equity-microstructure proxy
  for bearish-vs-bullish options-style sentiment when no options feed
  is available: surges of trading volume on red bars indicate
  protective / hedging flow analogous to put-buying, while surges on
  green bars indicate speculative / bullish flow analogous to call-
  buying. The 14-bar smoothing window mirrors the typical PCR
  reporting window in the literature.

  The strategy class itself is data-source agnostic; swapping in a
  real Deribit OI-based PCR (Kyriazis 2022, Akyildirim 2024, Chen
  2023) requires changing only ``_compute_pcr_proxy`` below. The
  z-score / verdict logic is unaffected.

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
from strategies.put_call_ratio_contrarian import (
    PutCallRatioContrarianStrategy,
)


STRATEGY_ID = "PutCallRatioContrarian"
VARIATION_ID = "pcr-contrarian-zscore-reversal"

# Window over which the rolling sums of up- vs. down-volume are
# accumulated to form the PCR proxy. 14 daily bars matches the
# typical PCR reporting cadence in the cited literature.
PCR_PROXY_WINDOW = 14
# Numerical-stability floor for the up-volume denominator.
_PCR_DENOM_FLOOR = 1e-9

HYPOTHESIS_TEXT = (
    "sq-036 variation #1 (pcr-contrarian-zscore-reversal): extreme "
    "high Put/Call Ratio on BTC/USDT 1D as a contrarian long signal. "
    "PCR proxy (Deribit feed not wired): rolling 14-day ratio of "
    "down-day volume to up-day volume. Apply 60-day rolling z-score; "
    "long when z > +2.0; exit after 3 days or when z <= 0.0. Long-"
    "only on spot per project conventions. Sources: Kyriazis et al. "
    "(2022) Global Finance Journal; Akyildirim et al. (2024) "
    "Finance Research Letters; Chen et al. (2023) JIFMIM; Han, Kang "
    "& Ryu (2024) SSRN (long-only adaptation precedent)."
)

PARAMS: dict = {
    "zscore_lookback": 60,
    "entry_threshold": 2.0,
    "exit_threshold": 0.0,
    "holding_period": 3,
    "pcr_proxy_window": PCR_PROXY_WINDOW,
    "pcr_proxy": "rolling_down_vs_up_volume",
    "timeframe": "1d",
    "symbol": "BTC/USDT",
}


def _compute_pcr_proxy(df: pd.DataFrame, window: int) -> pd.Series:
    """Return a daily PCR proxy series aligned to df.index.

    Algorithm:
      up_flow[d]   = volume[d] when close[d] > open[d] else 0
      down_flow[d] = volume[d] when close[d] < open[d] else 0
      pcr[t]       = rolling_sum(down_flow, window) /
                     max(rolling_sum(up_flow, window), 1e-9)

    NaN for the first ``window - 1`` rows (insufficient history).
    """
    if not {"open", "close", "volume"}.issubset(df.columns):
        raise ValueError(
            "PCR proxy requires open/close/volume columns in df"
        )
    close = df["close"].astype(float)
    open_ = df["open"].astype(float)
    vol = df["volume"].astype(float)

    up_flow = vol.where(close > open_, 0.0)
    down_flow = vol.where(close < open_, 0.0)

    up_sum = up_flow.rolling(window=window, min_periods=window).sum()
    down_sum = down_flow.rolling(window=window, min_periods=window).sum()

    denom = up_sum.where(up_sum > _PCR_DENOM_FLOOR, _PCR_DENOM_FLOOR)
    pcr = down_sum / denom
    return pcr.astype(float)


def main() -> int:
    print("=" * 72)
    print("sq-036 -- PutCallRatioContrarian trial #1 (dev_cpcv only)")
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

    # 2. Compute the OHLCV-derived PCR proxy.
    pcr_series = _compute_pcr_proxy(dev_df, window=PCR_PROXY_WINDOW)
    valid_pcr = pcr_series.dropna()
    print(
        f"pcr proxy: rows={len(pcr_series)} valid={len(valid_pcr)} "
        f"window={PCR_PROXY_WINDOW} "
        f"min={valid_pcr.min():.4f} median={valid_pcr.median():.4f} "
        f"max={valid_pcr.max():.4f}"
    )

    # 3. Strategy factory: fresh instance per call so per-instance
    #    state resets across CPCV blocks. The PCR series is captured
    #    by closure and shared read-only across instances.
    def make_strategy() -> PutCallRatioContrarianStrategy:
        return PutCallRatioContrarianStrategy(
            symbol=symbol,
            timeframe=PARAMS["timeframe"],
            pcr_series=pcr_series,
            zscore_lookback=PARAMS["zscore_lookback"],
            entry_threshold=PARAMS["entry_threshold"],
            exit_threshold=PARAMS["exit_threshold"],
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
        period_label="sq-036-pcr-contrarian-dev-headline",
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
                f"verdict=retire | CPCVError: {exc}. PCR z-score "
                "extreme-tail signal too sparse to meet the per-"
                "block trade-count floor."
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
        "sq-036 variation #1 (pcr-contrarian-zscore-reversal). "
        "Single-symbol BTC/USDT 1D long-only. PCR proxy: rolling "
        f"{PCR_PROXY_WINDOW}-day ratio of down-volume to up-volume "
        "(Deribit OI feed not wired into repo); z-score lookback "
        "60, entry z>+2.0, exit at z<=0.0 or after 3 bars. "
        "Baseline = BTC/USDT B&H. Sources: Kyriazis et al (2022); "
        "Akyildirim et al (2024); Chen et al (2023); Han/Kang/Ryu "
        "(2024) (long-only adaptation)."
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
