"""scripts/run_on_chain_metric_models_trial.py — sq-001 trial.

Runs the OnChainMetricModels strategy through the dev_cpcv harness
with on-chain MVRV Z-score data injected as an `mvrv_zscore` column
on each per-symbol OHLCV frame.

Exit codes:
  0 — success (TRIAL SUMMARY JSON sentinel emitted, trials.log row appended)
  1 — harness or runtime error (manifest missing, CPCV failure, etc.)
  2 — on-chain data cache absent (actionable: run the fetcher)

The cache check fires BEFORE the manifest / load_dev step so the
operator gets the actionable "run the fetcher" message regardless of
whether a manifest entry exists yet.
"""

from __future__ import annotations

import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8',
                              errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8',
                              errors='replace')

import json
from pathlib import Path

import numpy as np
import pandas as pd

# Project root on sys.path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest import trials as _trials
from backtest.cpcv_common import CPCVConfig
from backtest.cpcv_multi import run_cpcv_multi
from backtest.dsr import dsr_from_cpcv_result
from backtest.engine_multi import run_engine_multi
from backtest.holdout import load_dev, load_manifest
from backtest.verdict import compute_verdict
from strategies.on_chain_metric_models import (
    OnChainMetricModelsStrategy,
)


STRATEGY_ID = "OnChainMetricModels"
VARIATION_ID = "onchain-macro-cycle-filter"

HYPOTHESIS_TEXT = (
    "sq-001 variation #1: on-chain macro cycle filter via MVRV "
    "Z-score. Long when mvrv_zscore < 1.0 (undervalued), flat when "
    "> 3.5 (overvalued). Long-only basket, single concurrent "
    "position per symbol. "
    "Source: Proposal-agent dry-run 2026-05-05 (quality 3.3)."
)

PARAMS: dict = {
    "mvrv_long_threshold": 3.5,
    "mvrv_short_threshold": 1.0,
    "timeframe": "1d",
}

ONCHAIN_DIR = ROOT / "data" / "onchain"
ONCHAIN_PARQUET_SUFFIX = "_mvrv.parquet"
ONCHAIN_RAW_COLUMN = "mvrv_zscore"
ONCHAIN_INJECTED_COLUMN = "mvrv_zscore"


def _build_strategy(symbols: list[str]) -> OnChainMetricModelsStrategy:
    return OnChainMetricModelsStrategy(
        symbols=symbols,
        timeframe=PARAMS["timeframe"],
        mvrv_long_threshold=PARAMS["mvrv_long_threshold"],
        mvrv_short_threshold=PARAMS["mvrv_short_threshold"],
    )


def _load_onchain_for(symbol: str) -> pd.Series | None:
    """Read the per-symbol on-chain parquet and return a tz-aware
    UTC-indexed Series of the mvrv_zscore value. Returns None when
    the file is absent or unreadable."""
    fname = symbol.replace("/", "-") + ONCHAIN_PARQUET_SUFFIX
    fpath = ONCHAIN_DIR / fname
    if not fpath.exists():
        return None
    try:
        df = pd.read_parquet(fpath)
    except Exception as exc:  # noqa: BLE001
        print(
            f"[onchain] WARNING: failed to read {fpath}: {exc}",
            file=sys.stderr,
        )
        return None
    if ONCHAIN_RAW_COLUMN not in df.columns:
        print(
            f"[onchain] WARNING: {fpath} missing column "
            f"'{ONCHAIN_RAW_COLUMN}'",
            file=sys.stderr,
        )
        return None
    if "timestamp" in df.columns:
        df = df.set_index(pd.to_datetime(df["timestamp"], utc=True))
    series = df[ONCHAIN_RAW_COLUMN].astype(float).sort_index()
    series.name = ONCHAIN_INJECTED_COLUMN
    return series


def _check_onchain_cache(symbols: list[str]) -> dict[str, pd.Series]:
    """Probe ONCHAIN_DIR for per-symbol mvrv caches.

    Returns the {symbol: Series} mapping for symbols that resolved.
    Symbols whose cache file is missing are warned-and-skipped.
    Returns empty dict when nothing resolves; caller should exit 2.
    """
    out: dict[str, pd.Series] = {}
    for sym in symbols:
        s = _load_onchain_for(sym)
        if s is None:
            print(
                f"[onchain] no cache for {sym} — skipping",
                file=sys.stderr,
            )
            continue
        out[sym] = s
    return out


def main() -> int:
    print("=" * 72)
    print("sq-001 — OnChainMetricModels trial #1 (dev_cpcv only)")
    print("=" * 72)

    # ── -1. On-chain cache pre-check (fires before manifest load) ───────────
    if not ONCHAIN_DIR.exists():
        print("TRIAL_ERROR_TYPE: missing_data")
        print("TRIAL_ERROR_FETCH: scripts/fetch_onchain_data.py")
        print("TRIAL_ERROR_MSG: On-chain history not found. Run fetch script first.")
        sys.exit(1)

    # ── 0. Sanity checks on the manifest entry ──────────────────────────────
    manifest = load_manifest()
    if STRATEGY_ID not in manifest:
        print(f"FAIL: '{STRATEGY_ID}' not in manifest.", file=sys.stderr)
        return 1
    entry = manifest[STRATEGY_ID]
    symbols: list[str] = list(entry.get("symbols") or [entry.get("symbol")])
    print(
        f"manifest entry: timeframe={entry['timeframe']} "
        f"holdout_start={entry['holdout_start']} "
        f"n_symbols={len(symbols)}"
    )
    print(f"  symbols: {symbols}")

    # ── -0.5. Probe per-symbol on-chain caches ──────────────────────────────
    onchain_by_sym = _check_onchain_cache(symbols)
    if not onchain_by_sym:
        print("TRIAL_ERROR_TYPE: missing_data")
        print("TRIAL_ERROR_FETCH: scripts/fetch_onchain_data.py")
        print("TRIAL_ERROR_MSG: On-chain history not found. Run fetch script first.")
        sys.exit(1)

    active_symbols = [s for s in symbols if s in onchain_by_sym]
    if len(active_symbols) < len(symbols):
        skipped = [s for s in symbols if s not in onchain_by_sym]
        print(
            f"[onchain] basket reduced from {len(symbols)} to "
            f"{len(active_symbols)} (skipped: {skipped})"
        )

    # ── 1. Load dev frame + pivot into per-symbol dict ──────────────────────
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
        f"range={raw.index.min()} → {raw.index.max()}"
    )

    data: dict[str, pd.DataFrame] = {}
    for sym in active_symbols:
        df = (
            raw[raw["symbol"] == sym]
            .drop(columns=["symbol"])
            .sort_index()
        )
        mvrv = onchain_by_sym[sym].reindex(df.index, method="ffill")
        df = df.copy()
        df[ONCHAIN_INJECTED_COLUMN] = mvrv.astype(float)
        data[sym] = df

    missing = [sym for sym in active_symbols if sym not in data or len(data[sym]) == 0]
    if missing:
        print(
            f"FAIL: missing or empty per-symbol slices for: {missing}",
            file=sys.stderr,
        )
        return 1
    print("\nper-symbol dev coverage (with mvrv_zscore injected):")
    for sym, df in data.items():
        nan_count = int(df[ONCHAIN_INJECTED_COLUMN].isna().sum())
        print(
            f"  {sym:<12} rows={len(df):>5} "
            f"mvrv_nan={nan_count} "
            f"range={df.index.min()} → {df.index.max()}"
        )

    # ── 2. Headline backtest on the full dev window ─────────────────────────
    print("\n--- Headline run on full dev window ---")
    headline_strategy = _build_strategy(active_symbols)
    headline_result = run_engine_multi(
        data=data,
        strategy=headline_strategy,
        period_label="sq-001-onchain-dev-headline",
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

    # ── 3. CPCV block-Sharpe distribution ───────────────────────────────────
    print(
        "\n--- CPCV: requested n_blocks=10, "
        "warmup-aware downshift via compute_effective_n_blocks ---"
    )
    config = CPCVConfig(
        n_blocks=10,
        k_held_out=2,
        purge_periods=0,
        embargo_periods=0,
        strategy_warmup_candles=int(
            entry.get("strategy_warmup_candles", 30)
        ),
        min_tradeable_candles_per_block=int(
            entry.get("min_tradeable_candles_per_block", 10)
        ),
    )
    cpcv_strategy = _build_strategy(active_symbols)
    cpcv_result = run_cpcv_multi(
        data=data,
        strategy=cpcv_strategy,
        config=config,
        initial_balance=10_000.0,
    )
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

    # ── 4. DSR on validation (dev) ──────────────────────────────────────────
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

    # ── 5. Verdict tree ─────────────────────────────────────────────────────
    valid_returns = [r for r in cpcv_result.per_block_returns if r.size > 0]
    concat_returns = (
        np.concatenate(valid_returns) if valid_returns else np.array([])
    )
    # Baseline = BTC/USDT B&H. MVRV is a Bitcoin-cycle metric and BTC
    # is the natural counterfactual for "did the on-chain overlay add
    # value over passive BTC exposure".
    baseline_df = data.get("BTC/USDT")
    if baseline_df is None:
        first_sym = next(iter(data))
        print(
            f"[verdict] BTC/USDT absent — using {first_sym} as baseline",
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

    # ── 6. Append trial row ─────────────────────────────────────────────────
    notes = (
        "sq-001 variation #1. On-chain macro cycle filter via MVRV "
        "Z-score on a crypto basket. Long when mvrv_zscore < 1.0 "
        "(undervalued), flat when > 3.5 (overvalued). Long-only "
        "basket, single concurrent position per symbol. Baseline: "
        "BTC/USDT B&H. "
        f"Warmup-aware CPCV: strategy_warmup_candles="
        f"{config.strategy_warmup_candles}, effective_n_blocks="
        f"{effective_n_blocks}. "
        "Source: Proposal-agent dry-run 2026-05-05 (quality 3.3)."
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

    # ── 7. Final summary block ──────────────────────────────────────────────
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
