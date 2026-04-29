"""Phase 4.A drop-in #3 — GridTrading regime-conditional trial #1.

Mirrors backtest/runner.py:_run_strategy_dev_cpcv but with explicit
variation_id, params, and hypothesis for the variation #1 row.
n_trials = RESCUE_TRIAL_BUDGET (20) for DSR symmetry with Phase 3c.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path("/Users/kanin/Documents/crypto-bot")
sys.path.insert(0, str(ROOT))

from backtest import holdout as _holdout
from backtest import trials as _trials
from backtest.baseline import buy_and_hold_sharpe
from backtest.cpcv import CPCVConfig, run_cpcv
from backtest.dsr import deflated_sharpe, min_track_record_length
from backtest.engine import BacktestEngine
from backtest.runner import WARM_UP, _concat_per_block_returns, make_strategies
from backtest.verdict import compute_verdict
from rescue.policy import RESCUE_TRIAL_BUDGET
from strategies.grid_trading import GridTradingStrategy

STRATEGY_ID = "GridTrading"
VARIATION_ID = "phase4a-regime-conditional-v1"
TIMEFRAME = "1h"
INITIAL_BALANCE = 10_000.0

PARAMS: dict = {
    "regime_gate": "RANGE_only",
    "regime_detector_path": "portfolio.regime_detector.RegimeDetector",
    "regime_detector_data_source": "strategy_pair_df",
    "regime_min_warmup_candles": 210,
    # Phase 3c factory params (held constant; regime gate is the sole
    # structural change).  Mirrors make_strategies("1h")["GridTrading"]
    # default args for explicit auditability of the params_hash.
    "bb_period": 20,
    "bb_std": 2.0,
    "atr_period": 14,
    "atr_step_mult": 0.75,
    "atr_trend_threshold": 2.5,
    "grid_levels": 10,
    "usdt_per_trade": 200.0,
    "recalibrate_every": 24,
    "btd_mode": False,
    "trailing_grid": False,
}

HYPOTHESIS = (
    "Phase 4.A variation #1: RANGE-only regime gate added to GridTrading. "
    "Source: Chen/Chen/Jang (2025) — unconditional grid trading has zero EV; "
    "conditional firing on the regime that satisfies range AND low-trend AND "
    "mid-vol is the structural change.  In the 6-regime detector "
    "(portfolio/regime_detector.py), REGIME_RANGE is the unique label that "
    "matches all three criteria.  Detector reads strategy pair df "
    "(asset-specific regime), not BTC.  All Phase 3c grid params held "
    "constant; gate is the sole structural change.  See "
    "research/gridtrading-literature.md (committed bf4b9ca)."
)


def _grid_factory():
    """Phase 3c factory params + the regime gate (built into the class)."""
    syms = __import__("config").STRATEGY_SYMBOLS
    return GridTradingStrategy(
        symbol=syms["GridTrading"],
        timeframe=TIMEFRAME,
        grid_levels=10,
        usdt_per_trade=200.0,
        recalibrate_every=24,
    )


def main() -> int:
    print("=" * 88)
    print(f"GridTrading Phase 4.A trial #1 — {VARIATION_ID}")
    print("=" * 88)

    manifest = _holdout.load_manifest()
    entry = manifest[STRATEGY_ID]
    print(f"manifest entry: {entry}\n")

    sample = _grid_factory()
    primary_symbol = sample.symbol
    print(f"primary symbol: {primary_symbol}\n")

    # 1. CPCV.
    cpcv_config = CPCVConfig()  # Phase 3c defaults: 10 blocks, k_held_out=2, purge=0, embargo=0
    print(f"CPCVConfig: n_blocks={cpcv_config.n_blocks}, k_held_out={cpcv_config.k_held_out}, "
          f"purge={cpcv_config.purge_periods}, embargo={cpcv_config.embargo_periods}")
    print("Running CPCV...")
    cpcv_result = run_cpcv(
        strategy_id=STRATEGY_ID,
        params=PARAMS,
        config=cpcv_config,
        strategy_factory=_grid_factory,
    )
    print(f"per-block trades: {cpcv_result.trades_per_path}")
    print(f"per-block sharpes: {[f'{s:+.3f}' if not (s != s) else 'NaN' for s in cpcv_result.per_block_sharpes]}")
    print(f"sharpe distribution: {cpcv_result.sharpe_distribution}")

    # 2. Headline engine run.
    dev_df = _holdout.load_dev(STRATEGY_ID)
    primary_dev_df = dev_df.drop(columns=["symbol"], errors="ignore")
    engine = BacktestEngine(
        initial_balance=INITIAL_BALANCE,
        warm_up_candles=WARM_UP,
        verbose=False,
    )
    print("\nRunning headline backtest on full dev window...")
    headline_result = engine.run(
        primary_dev_df,
        _grid_factory(),
        period_label=f"phase4a-{STRATEGY_ID}-headline",
    )
    observed_sharpe = float(headline_result.metrics.sharpe_ratio)
    total_trades = int(headline_result.metrics.total_trades)
    print(f"\nheadline: Sharpe={observed_sharpe:+.4f}  trades={total_trades}  "
          f"return={headline_result.metrics.total_return_pct:+.2f}%  "
          f"maxdd={headline_result.metrics.max_drawdown_pct:.2f}%  "
          f"win={headline_result.metrics.win_rate_pct:.1f}%")

    # 3. Baseline.
    baseline_result = buy_and_hold_sharpe(primary_dev_df)
    baseline_sharpe = float(baseline_result.sharpe)
    print(f"\nbaseline (SOL/USDT B&H): Sharpe={baseline_sharpe:+.4f}  "
          f"return={baseline_result.total_return*100:+.2f}%  bars={baseline_result.n_bars}")

    # 4. DSR / MinTRL inputs.
    returns_for_dsr = _concat_per_block_returns(cpcv_result)

    # 5. DSR — n_trials=RESCUE_TRIAL_BUDGET (20) per Phase 3c convention.
    n_trials = RESCUE_TRIAL_BUDGET
    dsr_result = deflated_sharpe(
        sr_candidate=observed_sharpe,
        returns=returns_for_dsr,
        n_trials=n_trials,
    )
    print(f"\nDSR: dsr={dsr_result.dsr:.6f}  "
          f"sr_zero_expected (N={n_trials})={dsr_result.sr_zero_expected:+.4f}  "
          f"sr_std={dsr_result.sr_std:.4f}  T={dsr_result.t}")

    # 6. MinTRL.
    mintrl_result = min_track_record_length(
        sr_candidate=observed_sharpe,
        returns=returns_for_dsr,
    )
    print(f"MinTRL: required={mintrl_result.min_trl:.0f} bars  "
          f"observed={mintrl_result.t_observed} bars  "
          f"under_tested={mintrl_result.under_tested}")

    # 7. Verdict.
    verdict = compute_verdict(
        strategy_id=STRATEGY_ID,
        sr_candidate=observed_sharpe,
        returns=returns_for_dsr,
        total_trades=total_trades,
        baseline_df=primary_dev_df,
        n_trials=n_trials,
    )

    # 8. Build the row with explicit Phase 4.A variation params + hypothesis.
    is_multi_symbol = "symbols" in entry
    symbols = list(entry["symbols"]) if is_multi_symbol else [entry["symbol"]]

    row = {
        "strategy_id": STRATEGY_ID,
        "variation_id": VARIATION_ID,
        "trial_type": "full_cpcv",
        "params": PARAMS,
        "hypothesis": HYPOTHESIS,
        "split_holdout_start": entry["holdout_start"],
        "symbols": symbols,
        "n_trades": int(headline_result.metrics.total_trades),
        "sharpe": float(headline_result.metrics.sharpe_ratio),
        "cpcv": {
            "n_paths": int(cpcv_result.n_paths),
            "n_blocks": int(cpcv_config.n_blocks),
            "k_held_out": int(cpcv_config.k_held_out),
            "purge_periods": int(cpcv_config.purge_periods),
            "embargo_periods": int(cpcv_config.embargo_periods),
            "sharpe_distribution": cpcv_result.sharpe_distribution,
        },
        "dsr_validation": float(dsr_result.dsr),
        "n_trials": int(n_trials),
        "mintrl": float(mintrl_result.min_trl),
        "buy_and_hold_sharpe": baseline_sharpe,
        "notes": (
            f"Phase 4.A variation #1 — RANGE-only regime gate added to "
            f"GridTrading per research/gridtrading-literature.md "
            f"(Chen/Chen/Jang 2025). All Phase 3c grid params held constant. "
            f"Detector reads strategy pair df (SOL/USDT), not BTC. "
            f"Phase 3c rescue-default for comparison: sharpe=+1.5004, "
            f"n_trades=1035; this trial reduces n_trades by gating entries "
            f"to RANGE only."
        ),
    }

    # 9. Append (atomic — last side-effect before return).
    print(f"\ncount_trials_for_dsr({STRATEGY_ID}) before append = "
          f"{_trials.count_trials_for_dsr(STRATEGY_ID)}")
    _trials.record_trial(row)
    print(f"count_trials_for_dsr({STRATEGY_ID}) after append  = "
          f"{_trials.count_trials_for_dsr(STRATEGY_ID)}")

    # 10. Verdict block.
    sep = "═" * 70
    sub = "─" * 70
    print()
    print(sep)
    print(f"  DEV_CPCV — {STRATEGY_ID} ({VARIATION_ID})")
    print(sep)
    print(f"  VERDICT: {verdict.verdict.upper()}")
    print(sub)
    print(f"  observed_sharpe         {observed_sharpe:>+10.4f}")
    print(f"  sr_zero_expected (N=20) {dsr_result.sr_zero_expected:>+10.4f}")
    print(f"  dsr_pvalue              {float(dsr_result.dsr):>10.4f}")
    print(f"  mintrl (bars)           {float(mintrl_result.min_trl):>10.2f}")
    print(f"  baseline_sharpe         {baseline_sharpe:>+10.4f}")
    print(sub)
    print(f"  trade_count_pass        {verdict.trade_count_pass}")
    print(f"  mintrl_pass             {verdict.mintrl_pass}")
    print(f"  mt_mean_pass            {verdict.mt_mean_pass}")
    print(f"  baseline_pass           {verdict.baseline_pass}")
    print(f"  total_trades            {verdict.total_trades}")
    print(f"  t_observed              {verdict.t_observed}")
    print(f"  sr_margin_vs_mt_mean    {verdict.sr_margin_vs_mt_mean}")
    print(f"  sr_margin_vs_baseline   {verdict.sr_margin_vs_baseline}")
    print(sep)

    # Persist a slim summary for downstream doc edits.
    Path("/tmp/grid_phase4a_summary.json").write_text(json.dumps({
        "trial_id": row["trial_id"],
        "params_hash": row["params_hash"],
        "git_commit": row["git_commit"],
        "ts": row["ts"],
        "verdict": verdict.verdict,
        "observed_sharpe": observed_sharpe,
        "n_trades": total_trades,
        "dist_mean": cpcv_result.sharpe_distribution["mean"],
        "dist_std": cpcv_result.sharpe_distribution["std"],
        "dsr_validation": float(dsr_result.dsr),
        "sr_zero_expected": float(dsr_result.sr_zero_expected),
        "mintrl": float(mintrl_result.min_trl),
        "baseline_sharpe": baseline_sharpe,
        "headline_return_pct": float(headline_result.metrics.total_return_pct),
        "headline_max_dd_pct": float(headline_result.metrics.max_drawdown_pct),
        "headline_win_rate_pct": float(headline_result.metrics.win_rate_pct),
        "headline_profit_factor": float(headline_result.metrics.profit_factor),
        "block_trades": list(cpcv_result.trades_per_path),
        "block_sharpes": list(cpcv_result.per_block_sharpes),
        "trade_count_pass": verdict.trade_count_pass,
        "mintrl_pass": verdict.mintrl_pass,
        "mt_mean_pass": verdict.mt_mean_pass,
        "baseline_pass": verdict.baseline_pass,
        "sr_margin_vs_mt_mean": verdict.sr_margin_vs_mt_mean,
        "sr_margin_vs_baseline": verdict.sr_margin_vs_baseline,
    }, indent=2, default=str))
    print(f"\nSummary persisted to /tmp/grid_phase4a_summary.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
