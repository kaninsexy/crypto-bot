"""scripts/run_exchange_netflow_reversal_trial.py -- sq-034 trial.

Runs the ExchangeNetflowReversal long-only bottom-quintile basket on
a 10-symbol crypto universe at 1D through dev_cpcv (run_cpcv_multi),
computes DSR + verdict, and appends one trial_type='full_cpcv' row to
backtest/trials.log via the schema-validating writer. No holdout
access, no commit, no deployment.

Per-symbol exchange netflow data is read from
`data/onchain/<symbol>_netflow.parquet` with a `netflow_native`
column. The trial script computes a 30-day rolling z-score from that
series and injects it onto each per-symbol OHLCV frame as the
`netflow_zscore` column the strategy consumes. If the cache is
absent, the script emits TRIAL_ERROR_TYPE: missing_data and exits 1
cleanly so the orchestrator records it as a deferred-no-data error.

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
from strategies.exchange_netflow_reversal import (
    ExchangeNetflowReversalStrategy,
)


STRATEGY_ID = "ExchangeNetflowReversal"
# Gate spec v2 (2026-06-11): explicit bar frequency for the
# units-correct DSR / MinTRL / verdict (manifest timeframe).
from backtest.dsr import bars_per_year_for_timeframe
BARS_PER_YEAR = bars_per_year_for_timeframe("1d")
VARIATION_ID = "cs-exchange-netflow-reversal"

HYPOTHESIS_TEXT = (
    "sq-034 variation #1: cross-sectional exchange-netflow reversal "
    "on a 10-symbol crypto basket at 1D. Each bar rank symbols by "
    "30-day rolling z-score of daily exchange netflow ascending; long "
    "the bottom quintile (top_n=2 of 10, equal weight) -- the "
    "'accumulation' tail with largest net outflows. Long-only "
    "adaptation of the published long-short hypothesis (engine_multi "
    "is long-only). Sources: Fantazzini/Li (2024); Kim/Ahn (2023); "
    "Chen et al. (2023)."
)

PARAMS: dict = {
    "zscore_window": 30,
    "top_n": 2,
    "notional_capital": 10_000.0,
    "timeframe": "1d",
}

ONCHAIN_DIR = ROOT / "data" / "onchain"
NETFLOW_PARQUET_SUFFIX = "_netflow.parquet"
NETFLOW_RAW_COLUMN = "netflow_native"
NETFLOW_INJECTED_COLUMN = "netflow_zscore"


def _build_strategy(symbols: list[str]) -> ExchangeNetflowReversalStrategy:
    """Strategy factory: fresh instance with reset internal `_held` set."""
    return ExchangeNetflowReversalStrategy(
        symbols=symbols,
        timeframe=PARAMS["timeframe"],
        zscore_window=PARAMS["zscore_window"],
        top_n=PARAMS["top_n"],
        notional_capital=PARAMS["notional_capital"],
    )


def _load_netflow_for(symbol: str) -> pd.Series | None:
    """Read per-symbol netflow parquet and return a tz-aware UTC-indexed
    Series of the raw `netflow_native` value. Returns None when the
    file is absent or unreadable."""
    fname = symbol.replace("/", "-") + NETFLOW_PARQUET_SUFFIX
    fpath = ONCHAIN_DIR / fname
    if not fpath.exists():
        return None
    try:
        df = pd.read_parquet(fpath)
    except Exception as exc:  # noqa: BLE001
        print(
            f"[netflow] WARNING: failed to read {fpath}: {exc}",
            file=sys.stderr,
        )
        return None
    if NETFLOW_RAW_COLUMN not in df.columns:
        print(
            f"[netflow] WARNING: {fpath} missing column "
            f"'{NETFLOW_RAW_COLUMN}'",
            file=sys.stderr,
        )
        return None
    if "timestamp" in df.columns:
        df = df.set_index(pd.to_datetime(df["timestamp"], utc=True))
    series = df[NETFLOW_RAW_COLUMN].astype(float).sort_index()
    series.name = NETFLOW_RAW_COLUMN
    return series


def _check_netflow_cache(symbols: list[str]) -> dict[str, pd.Series]:
    """Probe ONCHAIN_DIR for per-symbol netflow caches.

    Returns the {symbol: Series} mapping for symbols that resolved.
    Symbols whose cache file is missing are warned-and-skipped.
    Returns empty dict when nothing resolves; caller should exit 1
    with the missing_data sentinel.
    """
    out: dict[str, pd.Series] = {}
    for sym in symbols:
        s = _load_netflow_for(sym)
        if s is None:
            print(
                f"[netflow] no cache for {sym} -- skipping",
                file=sys.stderr,
            )
            continue
        out[sym] = s
    return out


def main() -> int:
    print("=" * 72)
    print(
        "sq-034 -- ExchangeNetflowReversal trial #1 (dev_cpcv only)"
    )
    print("=" * 72)

    # -- -1. Netflow cache pre-check (before manifest load) ------------------
    if not ONCHAIN_DIR.exists():
        print("TRIAL_ERROR_TYPE: missing_data")
        print("TRIAL_ERROR_FETCH: scripts/fetch_onchain_data.py")
        print(
            "TRIAL_ERROR_MSG: Exchange netflow cache directory absent. "
            "Populate data/onchain/<sym>_netflow.parquet with a "
            "netflow_native column from CryptoQuant / Glassnode."
        )
        sys.exit(1)

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

    # -- -0.5. Probe per-symbol netflow caches -------------------------------
    netflow_by_sym = _check_netflow_cache(symbols)
    if not netflow_by_sym:
        print("TRIAL_ERROR_TYPE: missing_data")
        print("TRIAL_ERROR_FETCH: scripts/fetch_onchain_data.py")
        print(
            "TRIAL_ERROR_MSG: No per-symbol exchange netflow caches "
            "resolved. Populate data/onchain/<sym>_netflow.parquet "
            "with a netflow_native column from CryptoQuant / Glassnode."
        )
        sys.exit(1)

    active_symbols = [s for s in symbols if s in netflow_by_sym]
    if len(active_symbols) < len(symbols):
        skipped = [s for s in symbols if s not in netflow_by_sym]
        print(
            f"[netflow] basket reduced from {len(symbols)} to "
            f"{len(active_symbols)} (skipped: {skipped})"
        )

    # -- 1. Load dev frame + pivot into per-symbol dict ----------------------
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

    data: dict[str, pd.DataFrame] = {}
    for sym in active_symbols:
        df = (
            raw[raw["symbol"] == sym]
            .drop(columns=["symbol"])
            .sort_index()
        )
        # Forward-fill netflow onto the OHLCV index, then compute the
        # 30-day rolling z-score on that aligned series. The z-score
        # uses the prior zscore_window observations strictly before
        # the current bar (closed=left semantics emulated via a
        # shifted rolling window) to avoid look-ahead.
        netflow = (
            netflow_by_sym[sym]
            .reindex(df.index, method="ffill")
            .astype(float)
        )
        roll = netflow.rolling(
            window=PARAMS["zscore_window"], min_periods=PARAMS["zscore_window"]
        )
        roll_mean = roll.mean()
        roll_std = roll.std(ddof=0)
        zscore = (netflow - roll_mean) / (roll_std + 1e-9)
        df = df.copy()
        df[NETFLOW_INJECTED_COLUMN] = zscore.astype(float)
        data[sym] = df

    missing = [
        sym for sym in active_symbols if sym not in data or len(data[sym]) == 0
    ]
    if missing:
        print(
            f"FAIL: missing or empty per-symbol slices for: {missing}",
            file=sys.stderr,
        )
        return 1
    print("\nper-symbol dev coverage (with netflow_zscore injected):")
    for sym, df in data.items():
        nan_count = int(df[NETFLOW_INJECTED_COLUMN].isna().sum())
        print(
            f"  {sym:<12} rows={len(df):>5} "
            f"netflow_z_nan={nan_count} "
            f"range={df.index.min()} -> {df.index.max()}"
        )

    # -- 2. Headline backtest on the full dev window -------------------------
    print("\n--- Headline run on full dev window ---")
    headline_strategy = _build_strategy(active_symbols)
    headline_result = run_engine_multi(
        data=data,
        strategy=headline_strategy,
        period_label="sq-034-cs-netflow-reversal-dev-headline",
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

    # -- 3. CPCV block-Sharpe distribution -----------------------------------
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
    cpcv_strategy = _build_strategy(active_symbols)
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
            "symbols": active_symbols,
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

    # -- 4. DSR on validation (dev) ------------------------------------------
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

    # -- 5. Verdict tree -----------------------------------------------------
    valid_returns = [r for r in cpcv_result.per_block_returns if r.size > 0]
    concat_returns = (
        np.concatenate(valid_returns) if valid_returns else np.array([])
    )
    # Baseline = BTC/USDT B&H. The hypothesis claims the long-only
    # bottom-quintile basket outperforms passive crypto exposure;
    # BTC is the natural counterfactual.
    baseline_df = data.get("BTC/USDT")
    if baseline_df is None:
        first_sym = next(iter(data))
        print(
            f"[verdict] BTC/USDT absent -- using {first_sym} as baseline",
            file=sys.stderr,
        )
        baseline_df = data[first_sym]
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

    # -- 6. Append trial row -------------------------------------------------
    notes = (
        "sq-034 variation #1. Long-only cross-sectional exchange-"
        "netflow reversal on 10-symbol 1D universe. Each bar rank "
        "symbols by 30-day rolling z-score of daily exchange netflow "
        "ascending; hold bottom-2 (accumulation tail, equal weight). "
        "Engine handles 1/N sizing via position_fraction(). Baseline "
        "= BTC/USDT B&H. Long-only adaptation of the published long-"
        "short hypothesis (engine_multi is long-only). "
        f"Warmup-aware CPCV: strategy_warmup_candles="
        f"{config.strategy_warmup_candles}, effective_n_blocks="
        f"{effective_n_blocks}. "
        "Sources: Fantazzini/Li (2024); Kim/Ahn (2023); Chen et al. "
        "(2023)."
    )
    event = {
        "strategy_id": STRATEGY_ID,
        "variation_id": VARIATION_ID,
        "trial_type": "full_cpcv",
        "params": PARAMS,
        "hypothesis": HYPOTHESIS_TEXT,
        "split_holdout_start": entry["holdout_start"],
        "symbols": active_symbols,
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

    # -- 7. Final summary block ----------------------------------------------
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
