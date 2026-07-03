"""scripts/phase4e_trial_common.py -- shared Phase 4.E trial runner.

One parameterised runner for the 7 microstructure strategies.  Each
per-strategy script (scripts/run_<slug>_trial.py) calls `run(strategy_id)`.

What it does per strategy:
  1. Load the dev window via holdout.load_dev (signal-timeframe frame with
     delta/cum_delta columns; strictly < holdout_start).
  2. Build the strategy's precomputed features from the 1m substrate,
     truncated at holdout_start exactly like the price frame:
       - profile strategies -> build_profile_features(1m, dev.index)
       - VWAPInstitutionalBand -> session_vwap + vwap_bands on the dev frame
       - delta strategies -> none
  3. FEE-REALISM GATE (Gate 1, verbatim): run the full headline + CPCV +
     DSR + verdict evaluation TWICE -- once at the engine's standard taker
     fee + slippage model, once at 2x fees (slippage unchanged, per the
     "2x fees" gate wording).  The edge must survive BOTH or the verdict is
     retire.
  4. Persist the per-bar return series (gate spec v2 per-bar store) and
     append one full_cpcv row to trials.log.

Fee model (documented in each research/<strategy>-literature.md):
  standard taker fee  = FEE_MARKET  = 0.0004 (0.04%)   [paper_trading.simulator]
  standard slippage   = SLIPPAGE_MARKET = 0.0005 (0.05%) [backtest.engine]
  2x-fee run          = FEE_MARKET x2 = 0.0008 (0.08%); slippage unchanged.
Strategy entries/exits are market (taker) orders, so FEE_MARKET is the
relevant fee.  Fees are simulator module globals read at fill time, so the
2x run is applied by temporarily doubling them around the evaluation.

CPCVError handling (mandatory, verbatim per .claude/rules/backtest.md):
Every call to run_cpcv_multi()/run_cpcv() is wrapped in try/except CPCVError.
On catch: call _trials.record_trial() with verdict='retire', sr_observed=nan,
n_trades=0, and a notes string containing the CPCVError message, then
return 0.

ASCII-only stdout for Windows cp1252 terminals.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import paper_trading.simulator as _sim
from backtest import trials as _trials
from backtest.cpcv import CPCVConfig, run_cpcv
from backtest.cpcv_common import CPCVError
from backtest.dsr import bars_per_year_for_timeframe, dsr_from_cpcv_result
from backtest.engine import BacktestEngine
from backtest.holdout import load_dev, load_manifest
from backtest.verdict import compute_verdict
from data.microstructure import session_vwap, vwap_bands
from data.microstructure_features import build_profile_features

from strategies.breakout_delta_confirmed import BreakoutDeltaConfirmedStrategy
from strategies.delta_divergence import DeltaDivergenceStrategy
from strategies.hvn_mean_reversion import HVNMeanReversionStrategy
from strategies.liquidity_sweep_reversal import LiquiditySweepReversalStrategy
from strategies.lvn_traversal import LVNTraversalStrategy
from strategies.volume_profile_acceptance import VolumeProfileAcceptanceStrategy
from strategies.vwap_institutional_band import VWAPInstitutionalBandStrategy


# strategy_id -> (class, feature_kind, variation_id, literature slug)
REGISTRY: dict[str, tuple] = {
    "VolumeProfileAcceptance": (
        VolumeProfileAcceptanceStrategy, "profile",
        "phase4e-volume-profile-acceptance-v1", "volume-profile-acceptance",
    ),
    "LiquiditySweepReversal": (
        LiquiditySweepReversalStrategy, "none",
        "phase4e-liquidity-sweep-reversal-v1", "liquidity-sweep-reversal",
    ),
    "LVNTraversal": (
        LVNTraversalStrategy, "profile",
        "phase4e-lvn-traversal-v1", "lvn-traversal",
    ),
    "HVNMeanReversion": (
        HVNMeanReversionStrategy, "profile",
        "phase4e-hvn-mean-reversion-v1", "hvn-mean-reversion",
    ),
    "DeltaDivergence": (
        DeltaDivergenceStrategy, "none",
        "phase4e-delta-divergence-v1", "delta-divergence",
    ),
    "VWAPInstitutionalBand": (
        VWAPInstitutionalBandStrategy, "vwap",
        "phase4e-vwap-institutional-band-v1", "vwap-institutional-band",
    ),
    "BreakoutDeltaConfirmed": (
        BreakoutDeltaConfirmedStrategy, "none",
        "phase4e-breakout-delta-confirmed-v1", "breakout-delta-confirmed",
    ),
}

# Locked Variation #1 parameters per strategy (mirrors the module constants
# in each strategy file; recorded on the trial row so it is self-describing).
PARAMS: dict[str, dict] = {
    "VolumeProfileAcceptance": {
        "profile_days": 5, "n_bins": 100, "value_area_pct": 0.70,
        "delta_median_lookback": 30, "time_stop_bars": 24,
    },
    "LiquiditySweepReversal": {
        "swing_lookback": 20, "sweep_atr_k": 0.5, "target_atr_m": 2.0,
        "stop_atr": 0.10, "time_stop_bars": 16, "atr_period": 14,
    },
    "LVNTraversal": {
        "profile_days": 5, "n_bins": 100, "smooth_bins": 5,
        "min_rel_prominence": 0.25, "stop_atr": 0.25, "time_stop_bars": 8,
        "atr_period": 14,
    },
    "HVNMeanReversion": {
        "profile_days": 5, "n_bins": 100, "smooth_bins": 5,
        "min_rel_prominence": 0.25, "touch_tol": 0.001, "target_atr_r": 1.5,
        "stop_atr_s": 0.75, "time_stop_bars": 24, "atr_period": 14,
    },
    "DeltaDivergence": {
        "pivot_lookback": 20, "target_atr_d": 2.0, "stop_atr": 0.5,
        "time_stop_bars": 16, "atr_period": 14,
    },
    "VWAPInstitutionalBand": {
        "band_window": 60, "band_sigma": 2, "stop_atr": 0.5,
        "time_stop_bars": 12, "atr_period": 14, "side": "reversion_long",
    },
    "BreakoutDeltaConfirmed": {
        "range_lookback": 24, "delta_window": 100, "delta_quantile": 75,
        "target_atr_b": 3.0, "trail_atr_a": 2.0, "time_stop_bars": 48,
        "atr_period": 14,
    },
}

# Standard cost model (documented in the literature files).
_BASE_FEE_MARKET = _sim.FEE_MARKET   # 0.0004
_BASE_FEE_LIMIT = _sim.FEE_LIMIT     # 0.0002


def _set_fee_multiplier(mult: float) -> None:
    """Scale the simulator taker/maker fees (read as module globals at fill
    time, so this affects run_cpcv's internally-built engines)."""
    _sim.FEE_MARKET = _BASE_FEE_MARKET * mult
    _sim.FEE_LIMIT = _BASE_FEE_LIMIT * mult


def _restore_fees() -> None:
    _sim.FEE_MARKET = _BASE_FEE_MARKET
    _sim.FEE_LIMIT = _BASE_FEE_LIMIT


def _build_feature_kwargs(kind: str, symbol: str, timeframe: str,
                          dev_df, holdout_start) -> dict:
    """Precompute the strategy's injected features from the 1m substrate,
    truncated strictly before holdout_start (dev)."""
    if kind == "none":
        return {}
    if kind == "vwap":
        vw = session_vwap(dev_df, "1D")
        vb = vwap_bands(dev_df, vw, window=60)
        return {"vwap_features": vb}
    if kind == "profile":
        # Local import: cache helper reads the on-disk 1m month range.
        from backtest.cache import _bv_cached_month_range, _bv_symbol
        from data.binance_vision import load_klines
        import pandas as pd

        sym = _bv_symbol(symbol)
        start_m, end_m = _bv_cached_month_range(sym)
        k1m = load_klines(sym, start_m, end_m, interval="1m")
        k1m = k1m[k1m.index < pd.Timestamp(holdout_start)]   # dev truncation
        feats = build_profile_features(k1m, dev_df.index)
        return {"profile_features": feats}
    raise ValueError(f"unknown feature kind {kind!r}")


def _evaluate(strategy_id, cls, feature_kwargs, symbol, timeframe,
              dev_df, bars_per_year, fee_mult, params):
    """Full headline + CPCV + DSR + verdict at a given fee multiplier.

    Returns a dict with sr_observed, verdict object, cpcv_result, dsr, and the
    concatenated per-bar returns.  Raises CPCVError up to the caller.
    """
    def factory():
        return cls(symbol=symbol, timeframe=timeframe, **feature_kwargs)

    _set_fee_multiplier(fee_mult)
    try:
        headline = BacktestEngine(
            initial_balance=10_000.0, warm_up_candles=50, verbose=False,
        ).run(df=dev_df, strategy=factory(),
              period_label=f"{strategy_id}-dev-headline-fee{fee_mult:g}")
        sr_observed = float(headline.metrics.sharpe_ratio)
        n_trades_headline = int(headline.metrics.total_trades)

        config = CPCVConfig(
            n_blocks=10, k_held_out=2, purge_periods=0, embargo_periods=0,
        )
        warm = int(os.environ.get("TRIAL_WARM_UP_CANDLES", 50))
        cpcv_result = run_cpcv(
            strategy_id=strategy_id, params=params, config=config,
            strategy_factory=factory, warm_up_candles=warm,
        )
    finally:
        _restore_fees()

    n_trials_pre = _trials.count_trials_for_dsr(strategy_id)
    import backtest.trials as _t_mod
    _orig = _t_mod.count_trials_for_dsr
    _t_mod.count_trials_for_dsr = lambda sid: max(_orig(sid), 1)
    try:
        dsr_result = dsr_from_cpcv_result(
            result=cpcv_result, strategy_id=strategy_id,
            sr_candidate=sr_observed, bars_per_year=bars_per_year,
        )
    finally:
        _t_mod.count_trials_for_dsr = _orig

    valid = [r for r in cpcv_result.per_block_returns if r.size > 0]
    concat = np.concatenate(valid) if valid else np.array([])
    verdict = compute_verdict(
        strategy_id=strategy_id, sr_candidate=sr_observed, returns=concat,
        total_trades=int(sum(cpcv_result.trades_per_path)), baseline_df=dev_df,
        n_trials=n_trials_pre + 1, min_trade_count=30, confidence=0.95,
        bars_per_year=bars_per_year,
    )
    return {
        "sr_observed": sr_observed,
        "n_trades_headline": n_trades_headline,
        "cpcv_result": cpcv_result,
        "dsr": dsr_result,
        "verdict": verdict,
        "concat_returns": concat,
        "n_trials_pre": n_trials_pre,
    }


def _emit(summary: dict) -> None:
    print("--- TRIAL SUMMARY JSON ---")
    print(json.dumps(summary, indent=2, default=str))


def _record_cpcv_error_retire(strategy_id, variation_id, params, err, entry) -> int:
    msg = str(err)
    print(f"CPCVError caught: {msg}")
    event = {
        "strategy_id": strategy_id, "variation_id": variation_id,
        "trial_type": "full_cpcv", "params": params,
        "hypothesis": f"{strategy_id} Phase 4.E Variation #1 (locked spec).",
        "split_holdout_start": entry["holdout_start"],
        "symbols": [entry["symbol"]], "n_trades": 0, "sharpe": float("nan"),
        "cpcv": {
            "n_paths": 0, "n_blocks": 0, "k_held_out": 2,
            "purge_periods": 0, "embargo_periods": 0,
            "sharpe_distribution": {
                "mean": float("nan"), "std": float("nan"),
                "quantiles": {q: float("nan") for q in
                              ("p05", "p25", "p50", "p75", "p95")},
            },
        },
        "dsr_validation": float("nan"), "mintrl": None,
        "buy_and_hold_sharpe": float("nan"),
        "notes": (
            f"{strategy_id} CPCVError: {msg}. Retire row written via sentinel "
            "handler per .claude/rules/backtest.md."
        ),
        "verdict": "retire",
    }
    _trials.record_trial(event)
    _emit({"strategy": strategy_id, "verdict": "retire",
           "sr_observed": None, "n_trades_total": 0, "cpcv_error": msg})
    return 0


def run(strategy_id: str) -> int:
    if strategy_id not in REGISTRY:
        print(f"FAIL: {strategy_id} not in Phase 4.E registry.", file=sys.stderr)
        return 1
    cls, kind, variation_id, slug = REGISTRY[strategy_id]

    print("=" * 72)
    print(f"Phase 4.E -- {strategy_id} trial (dev_cpcv only, dual-fee gate)")
    print("=" * 72)

    manifest = load_manifest()
    if strategy_id not in manifest:
        print(f"FAIL: {strategy_id} not in manifest.", file=sys.stderr)
        return 1
    entry = manifest[strategy_id]
    symbol = entry["symbol"]
    timeframe = entry["timeframe"]
    bars_per_year = bars_per_year_for_timeframe(timeframe)
    print(f"manifest: tf={timeframe} symbol={symbol} "
          f"holdout_start={entry['holdout_start']}")

    dev_raw = load_dev(strategy_id)
    dev_df = dev_raw.drop(columns=["symbol"], errors="ignore")
    print(f"dev frame: rows={len(dev_df)} "
          f"range={dev_df.index[0]} -> {dev_df.index[-1]}")

    feature_kwargs = _build_feature_kwargs(
        kind, symbol, timeframe, dev_df, entry["holdout_start"],
    )
    params = {**PARAMS[strategy_id], "symbol": symbol, "timeframe": timeframe}

    # ── FEE-REALISM GATE: evaluate at 1x and 2x fees ─────────────────────
    results = {}
    for fee_mult in (1.0, 2.0):
        try:
            results[fee_mult] = _evaluate(
                strategy_id, cls, feature_kwargs, symbol, timeframe,
                dev_df, bars_per_year, fee_mult, params,
            )
        except CPCVError as err:
            return _record_cpcv_error_retire(
                strategy_id, variation_id, params, err, entry,
            )

    std = results[1.0]
    dbl = results[2.0]
    survives_both = (
        std["verdict"].verdict == "keep" and dbl["verdict"].verdict == "keep"
    )
    final_verdict = "keep" if survives_both else "retire"
    print(f"\n--- Fee-realism gate ---")
    print(f"standard fee verdict = {std['verdict'].verdict} "
          f"(sr={std['sr_observed']:.4f}, dsr={std['dsr'].dsr:.4f})")
    print(f"2x fee verdict       = {dbl['verdict'].verdict} "
          f"(sr={dbl['sr_observed']:.4f}, dsr={dbl['dsr'].dsr:.4f})")
    print(f"FINAL (survive both) = {final_verdict}")

    # ── Record ONE full_cpcv row (standard-run metrics) with the dual-fee
    #    outcome documented; verdict is retire unless the edge survives both.
    cpcv_result = std["cpcv_result"]
    verdict = std["verdict"]
    notes = (
        f"{strategy_id} Phase 4.E Variation #1 (locked spec, single-pair "
        f"{symbol} {timeframe}, long-only). Substrate Binance spot 1m via "
        "data/binance_vision.py; execution venue OKX (cross-venue provenance "
        "per 2026-06-11 BNB precedent). FEE-REALISM GATE (Gate 1): standard "
        f"taker fee 0.04% + slippage 0.05% -> verdict {std['verdict'].verdict} "
        f"(sr {std['sr_observed']:.4f}); 2x fee 0.08% -> verdict "
        f"{dbl['verdict'].verdict} (sr {dbl['sr_observed']:.4f}); edge "
        f"survives both = {survives_both}. See research/{slug}-literature.md."
    )
    event = {
        "strategy_id": strategy_id, "variation_id": variation_id,
        "trial_type": "full_cpcv", "params": params,
        "hypothesis": f"{strategy_id} Phase 4.E Variation #1 (locked spec).",
        "split_holdout_start": entry["holdout_start"],
        "symbols": [symbol],
        "n_trades": int(sum(cpcv_result.trades_per_path)),
        "sharpe": std["sr_observed"],
        "cpcv": {
            "n_paths": 10, "n_blocks": 10, "k_held_out": 2,
            "purge_periods": 0, "embargo_periods": 0,
            "sharpe_distribution": cpcv_result.sharpe_distribution,
        },
        "dsr_validation": float(std["dsr"].dsr),
        "mintrl": (
            float(verdict.mintrl_required_at_eval)
            if np.isfinite(verdict.mintrl_required_at_eval) else None
        ),
        "buy_and_hold_sharpe": float(verdict.baseline_sharpe_at_eval),
        "notes": notes,
        "verdict": final_verdict,
    }
    _trials.record_trial(
        event,
        per_bar_returns=std["concat_returns"],
        per_bar_benchmark=(
            dev_df["close"].pct_change().dropna().values.astype(float)
        ),
    )
    print(f"\nappended trial row | trial_id={event['trial_id']} | "
          f"params_hash={event['params_hash']}")
    _emit({
        "strategy": strategy_id, "variation": variation_id,
        "verdict": final_verdict,
        "sr_observed_standard": std["sr_observed"],
        "sr_observed_2x_fee": dbl["sr_observed"],
        "dsr_standard": float(std["dsr"].dsr),
        "dsr_2x_fee": float(dbl["dsr"].dsr),
        "survives_both_fee_regimes": survives_both,
        "n_trades_total": int(sum(cpcv_result.trades_per_path)),
    })
    return 0
