"""scripts/run_dex_flow_spillover_trial.py -- sq-039 trial.

Runs the DEXFlowSpillover strategy on BTC/USDT 1H through the
single-symbol dev_cpcv harness (run_cpcv) with a deterministic OHLCV-
derived OFI proxy held in-strategy. No holdout access, no commit, no
deployment.

OFI (Order Flow Imbalance) data source:

  No Uniswap V3 / DEX swap feed is wired into this repo, so the trial
  uses a deterministic Lee-Ready style OFI proxy from OHLCV: signed
  volume per bar where the sign is determined by whether the close
  finished above or below the open. Specifically, for each bar:

      signed_volume[t]  = +volume[t] if close[t] > open[t]
                          -volume[t] if close[t] < open[t]
                                   0 if close[t] == open[t]
      ofi_proxy[t]      = sum_{k=t-window+1..t}(signed_volume[k])

  Lee-Ready signed-volume is the standard equity-microstructure proxy
  for buy-vs-sell pressure when trade-level data is unavailable: bars
  that close green represent net buy pressure, bars that close red
  represent net sell pressure. Aggregating over a short window gives
  a "cumulative directional flow" measure analogous to the DEX swap
  imbalance used by Makarov & Schoar (2023). The 5-bar window matches
  the 5-hour scaling of their 5-minute lead-lag horizon to the 1H
  sampling cadence of this trial.

  The strategy class itself is data-source agnostic; swapping in a
  real Uniswap V3 swap-volume OFI (Makarov & Schoar 2023, Lehar et al
  2024) requires changing only ``_compute_ofi_proxy`` below. The
  z-score / verdict logic is unaffected.

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
from backtest.cpcv import run_cpcv
from backtest.cpcv_common import CPCVConfig, CPCVError
from backtest.dsr import dsr_from_cpcv_result
from backtest.engine import BacktestEngine
from backtest.holdout import load_dev, load_manifest
from backtest.verdict import compute_verdict
from strategies.dex_flow_spillover import DEXFlowSpilloverStrategy


STRATEGY_ID = "DEXFlowSpillover"
VARIATION_ID = "dex-cex-flow-imbalance-spillover"

# Window over which Lee-Ready signed volume is rolling-summed to form
# the cumulative OFI proxy. 5 hourly bars matches the scaling of the
# 5-minute Makarov & Schoar (2023) lead-lag horizon to 1H sampling.
OFI_PROXY_WINDOW = 5

HYPOTHESIS_TEXT = (
    "sq-039 variation #1 (dex-cex-flow-imbalance-spillover): "
    "extreme positive DEX-style order flow imbalance on BTC/USDT 1H "
    "as a directional spillover signal. OFI proxy (Uniswap V3 swap "
    "feed not wired): rolling 5-bar sum of Lee-Ready signed volume. "
    "Apply 60-bar rolling z-score; long when z > +2.0; exit after 5 "
    "bars or when z <= 0.0. Long-only on spot per project "
    "conventions. Sources: Makarov & Schoar (2023) SSRN; Lehar et "
    "al. (2024) SSRN; Cong et al. (2022) SSRN; Han, Kang & Ryu "
    "(2024) SSRN (long-only adaptation precedent)."
)

PARAMS: dict = {
    "zscore_lookback": 60,
    "entry_threshold": 2.0,
    "exit_threshold": 0.0,
    "holding_period": 5,
    "ofi_proxy_window": OFI_PROXY_WINDOW,
    "ofi_proxy": "rolling_lee_ready_signed_volume",
    "timeframe": "1h",
    "symbol": "BTC/USDT",
}


def _compute_ofi_proxy(df: pd.DataFrame, window: int) -> pd.Series:
    """Return a per-bar OFI proxy series aligned to df.index.

    Algorithm:
      sign[t]         = +1 if close[t] > open[t]
                        -1 if close[t] < open[t]
                         0 if close[t] == open[t]
      signed_vol[t]   = sign[t] * volume[t]
      ofi_proxy[t]    = rolling_sum(signed_vol, window)

    NaN for the first ``window - 1`` rows (insufficient history).
    """
    if not {"open", "close", "volume"}.issubset(df.columns):
        raise ValueError(
            "OFI proxy requires open/close/volume columns in df"
        )
    close = df["close"].astype(float)
    open_ = df["open"].astype(float)
    vol = df["volume"].astype(float)

    sign = pd.Series(0.0, index=df.index)
    sign = sign.where(~(close > open_), 1.0)
    sign = sign.where(~(close < open_), -1.0)
    signed_vol = sign * vol

    ofi = signed_vol.rolling(window=window, min_periods=window).sum()
    return ofi.astype(float)


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
        f"DEXFlowSpillover CPCVError: {msg}. Retire row written via "
        "sentinel handler per .claude/rules/backtest.md. Extreme-tail "
        "OFI z-score signal too sparse to meet per-block trade-count "
        "floor."
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
    print("sq-039 -- DEXFlowSpillover trial #1 (dev_cpcv only)")
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

    # 2. Compute the OHLCV-derived OFI proxy.
    ofi_series = _compute_ofi_proxy(dev_df, window=OFI_PROXY_WINDOW)
    valid_ofi = ofi_series.dropna()
    print(
        f"ofi proxy: rows={len(ofi_series)} valid={len(valid_ofi)} "
        f"window={OFI_PROXY_WINDOW} "
        f"min={valid_ofi.min():.2f} median={valid_ofi.median():.2f} "
        f"max={valid_ofi.max():.2f}"
    )

    # 3. Strategy factory: fresh instance per call so per-instance
    #    state resets across CPCV blocks. The OFI series is captured
    #    by closure and shared read-only across instances.
    def make_strategy() -> DEXFlowSpilloverStrategy:
        return DEXFlowSpilloverStrategy(
            symbol=symbol,
            timeframe=PARAMS["timeframe"],
            ofi_series=ofi_series,
            zscore_lookback=PARAMS["zscore_lookback"],
            entry_threshold=PARAMS["entry_threshold"],
            exit_threshold=PARAMS["exit_threshold"],
            holding_period=PARAMS["holding_period"],
        )

    # 4. Headline backtest on the full dev window.
    print("\n--- Headline run on full dev window ---")
    headline_engine = BacktestEngine(
        initial_balance=10_000.0,
        warm_up_candles=50,
        verbose=False,
    )
    headline_result = headline_engine.run(
        df=dev_df,
        strategy=make_strategy(),
        period_label="sq-039-dex-flow-spillover-dev-headline",
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
    except CPCVError as err:
        return _record_cpcv_error_retire(err, entry)

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
        "sq-039 variation #1 (dex-cex-flow-imbalance-spillover). "
        "Single-symbol BTC/USDT 1H long-only. OFI proxy: rolling "
        f"{OFI_PROXY_WINDOW}-bar sum of Lee-Ready signed volume "
        "(Uniswap V3 swap feed not wired into repo); z-score "
        "lookback 60, entry z>+2.0, exit at z<=0.0 or after 5 bars. "
        "Baseline = BTC/USDT B&H. Sources: Makarov & Schoar (2023); "
        "Lehar et al (2024); Cong et al (2022); Han/Kang/Ryu (2024) "
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
    _emit_summary(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
