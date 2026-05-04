"""
scripts/phase_4a_trendfollowing_smoke.py — smoke trial for the
Phase 4.A TrendFollowing daily multi-asset substrate.

Single full-dev-window run (no CPCV) of the new harness components
(cpcv_common warmup-aware sizer, manifest entry TrendFollowing_multi,
engine_multi, cpcv_multi, strategies/trend_following_multi).
Verifies end-to-end pipeline plumbing produces sensible output before
a full_cpcv trial slot is committed.

This script:
  * does NOT access the holdout window — only `holdout.load_dev`.
  * does NOT append to `backtest/trials.log`.
  * writes its run artefact to `scripts/phase_4a_tf_smoke_output.json`
    (gitignored — exploratory output, not a sacred-harness trial row).

Run it manually after the harness commit lands and review the output
before queueing a full_cpcv trial:

    cd ~/dev/crypto-bot && python scripts/phase_4a_trendfollowing_smoke.py
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest.engine_multi import run_engine_multi
from backtest.holdout import load_dev, load_manifest
from strategies.trend_following_multi import TrendFollowingMultiStrategy


STRATEGY_ID = "TrendFollowing_multi"
OUTPUT_PATH = ROOT / "scripts" / "phase_4a_tf_smoke_output.json"


def _pivot_dev_df(dev_df: pd.DataFrame, symbols: list[str]) -> dict[str, pd.DataFrame]:
    """Option-C pivot: holdout.load_dev returns a concatenated frame
    with a 'symbol' column; engine_multi consumes a per-symbol dict.
    The pivot is one line of business logic and lives at the caller
    boundary so holdout.py's contract stays untouched (per the
    chat 2026-05-04 design decision)."""
    out: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        sub = dev_df[dev_df["symbol"] == sym].drop(columns=["symbol"]).sort_index()
        out[sym] = sub
    return out


def _per_symbol_trade_counts(trades) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for t in trades:
        counter[t.symbol] += 1
    return dict(counter)


def main() -> int:
    manifest = load_manifest()
    if STRATEGY_ID not in manifest:
        print(f"FAIL: '{STRATEGY_ID}' not in manifest.", file=sys.stderr)
        return 1
    entry = manifest[STRATEGY_ID]
    symbols = list(entry["symbols"])
    timeframe = entry["timeframe"]
    lookback = int(entry.get("strategy_warmup_candles", 126))

    print(f"[smoke] loading dev OHLCV for {STRATEGY_ID} "
          f"({len(symbols)} symbols, timeframe={timeframe})")
    dev_df = load_dev(STRATEGY_ID)
    if not isinstance(dev_df, pd.DataFrame) or "symbol" not in dev_df.columns:
        print(
            "FAIL: load_dev did not return a concatenated frame with "
            f"a 'symbol' column; got type={type(dev_df).__name__}",
            file=sys.stderr,
        )
        return 1

    data = _pivot_dev_df(dev_df, symbols)
    rows_per_symbol = {sym: len(df) for sym, df in data.items()}
    print(f"[smoke] pivoted dev frame -> per-symbol rows: {rows_per_symbol}")

    strategy = TrendFollowingMultiStrategy(
        symbols=symbols,
        timeframe=timeframe,
        lookback_days=lookback,
    )

    print("[smoke] running engine_multi over the full dev window …")
    result = run_engine_multi(
        data=data,
        strategy=strategy,
        period_label="phase4a-tf-smoke",
    )

    metrics = result.metrics
    per_symbol_counts = _per_symbol_trade_counts(result.trade_history)

    summary = {
        "strategy_id": STRATEGY_ID,
        "timeframe": timeframe,
        "symbols": symbols,
        "lookback_days": lookback,
        "rows_per_symbol": rows_per_symbol,
        "common_bars": int(len(result.equity_curve)),
        "total_return_pct": float(metrics.total_return_pct),
        "sharpe_ratio": float(metrics.sharpe_ratio),
        "max_drawdown_pct": float(metrics.max_drawdown_pct),
        "total_trades": int(metrics.total_trades),
        "win_rate_pct": float(metrics.win_rate_pct),
        "trades_per_symbol": per_symbol_counts,
    }

    print()
    print("=== Phase 4.A TrendFollowing smoke output ===")
    print(f"  common bars       : {summary['common_bars']}")
    print(f"  total_return_pct  : {summary['total_return_pct']:+.3f}%")
    print(f"  sharpe_ratio      : {summary['sharpe_ratio']:+.4f}")
    print(f"  max_drawdown_pct  : {summary['max_drawdown_pct']:.3f}%")
    print(f"  total_trades      : {summary['total_trades']}")
    print(f"  win_rate_pct      : {summary['win_rate_pct']:.2f}")
    print(f"  trades_per_symbol : {summary['trades_per_symbol']}")
    print()

    OUTPUT_PATH.write_text(json.dumps(summary, indent=2, sort_keys=True))
    print(f"[smoke] wrote summary to {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
