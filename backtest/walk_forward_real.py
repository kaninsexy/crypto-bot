"""
backtest/walk_forward_real.py — Walk-forward backtest using real Binance data.

WHAT THIS DOES
──────────────
1. Loads real historical OHLCV data for each strategy's assigned symbol
   (from data/cache/ — downloaded by data.historical_fetcher)
2. Runs walk-forward validation:
     - Splits the full history into N rolling windows
     - Each window: 6-month train + 1-month out-of-sample test
     - Rolls forward by 1 month each step
     - Collects OOS metrics from each window
3. Reports: per-window P&L, aggregate stats (mean/std Sharpe, win rate),
   and a cross-strategy ranking

WHY WALK-FORWARD BEATS SIMPLE IS/OOS SPLIT
────────────────────────────────────────────
A single 9m/3m split gives you ONE data point. Walk-forward gives you ~30.
A strategy that scores well on 1 specific window might be lucky.
One that consistently scores well across 30 independent windows has earned it.

USAGE
─────
  # Run all strategies on their assigned symbols (3 years of data)
  cd crypto_bot
  python -m backtest.walk_forward_real

  # Override years of history
  BACKTEST_YEARS=4 python -m backtest.walk_forward_real

  # Single strategy test
  BACKTEST_STRATEGY=MeanReversion python -m backtest.walk_forward_real

  # Force re-download data even if cache is fresh
  BACKTEST_FORCE_REFRESH=1 python -m backtest.walk_forward_real
"""

from __future__ import annotations

import os
import sys
import math
import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime, timezone

# Allow running as python -m backtest.walk_forward_real from crypto_bot/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtest.run_improved import (
    generate_ohlcv,       # synthetic fallback if no real data
    simulate, compute_metrics, Metrics,
    signals_dca_v2, signals_meanrev_v2, signals_grid_v2, signals_vwap,
    signals_supertrend, signals_trend_following, signals_breakout,
    BALANCE, WARM_UP,
)

try:
    from data.historical_fetcher import load_or_fetch, get_cache_info
    _FETCHER_AVAILABLE = True
except ImportError:
    _FETCHER_AVAILABLE = False

import config


# ── Config from env ───────────────────────────────────────────────────────────

YEARS          = float(os.getenv("BACKTEST_YEARS", "3"))
TRAIN_MONTHS   = int(os.getenv("BACKTEST_TRAIN_MONTHS", "6"))
TEST_MONTHS    = int(os.getenv("BACKTEST_TEST_MONTHS", "1"))
FORCE_REFRESH  = bool(os.getenv("BACKTEST_FORCE_REFRESH", ""))
ONLY_STRATEGY  = os.getenv("BACKTEST_STRATEGY", "")   # empty = all

HOURS_PER_MONTH = 30 * 24

G = "\033[92m"; R = "\033[91m"; Y = "\033[93m"
C = "\033[96m"; B = "\033[1m"; RESET = "\033[0m"


# ── Strategy registry ─────────────────────────────────────────────────────────
# Maps strategy name → (signal_function, per-trade USDT or None)

STRATEGY_REGISTRY = {
    "DCA":            (signals_dca_v2,          200.0),
    "MeanReversion":  (signals_meanrev_v2,       None),
    "GridTrading":    (signals_grid_v2,          200.0),
    "VWAP":           (signals_vwap,             None),
    "Supertrend":     (signals_supertrend,       None),
    "TrendFollowing": (signals_trend_following,  None),
    "Breakout":       (signals_breakout,         None),
}


# ── Walk-forward engine ───────────────────────────────────────────────────────

@dataclass
class WindowResult:
    window_idx:      int
    train_start:     str
    train_end:       str
    test_start:      str
    test_end:        str
    metrics:         Metrics

@dataclass
class StrategyWFResult:
    strategy:        str
    symbol:          str
    windows:         list[WindowResult]

    @property
    def oos_sharpes(self) -> list[float]:
        return [w.metrics.sharpe for w in self.windows]

    @property
    def oos_returns(self) -> list[float]:
        return [w.metrics.total_return_pct for w in self.windows]

    @property
    def oos_drawdowns(self) -> list[float]:
        return [w.metrics.max_drawdown_pct for w in self.windows]

    @property
    def mean_sharpe(self) -> float:
        return float(np.mean(self.oos_sharpes)) if self.oos_sharpes else 0.0

    @property
    def std_sharpe(self) -> float:
        return float(np.std(self.oos_sharpes)) if self.oos_sharpes else 0.0

    @property
    def positive_windows(self) -> int:
        return sum(1 for r in self.oos_returns if r > 0)

    @property
    def win_pct(self) -> float:
        return self.positive_windows / len(self.windows) * 100 if self.windows else 0.0

    @property
    def mean_return(self) -> float:
        return float(np.mean(self.oos_returns)) if self.oos_returns else 0.0

    @property
    def mean_drawdown(self) -> float:
        return float(np.mean(self.oos_drawdowns)) if self.oos_drawdowns else 0.0


def run_walk_forward(
    df:        pd.DataFrame,
    sig_func,
    usdt:      Optional[float],
    strategy:  str,
    symbol:    str,
    train_h:   int,
    test_h:    int,
) -> StrategyWFResult:
    """
    Run walk-forward on a single strategy over the full DataFrame.

    Windows slide forward by test_h hours each step.
    Each window: train on [i : i+train_h], test on [i+train_h : i+train_h+test_h].
    Warm-up candles overlap: we include WARM_UP candles before each test window.
    """
    results = []
    n       = len(df)
    step    = test_h
    start   = train_h

    window_idx = 0
    while start + test_h <= n:
        test_end = start + test_h

        # OOS test window: include warm-up for indicator calculation
        oos_start_with_wu = max(0, start - WARM_UP)
        df_test = df.iloc[oos_start_with_wu : test_end].copy()

        try:
            sigs     = sig_func(df_test)
            sim      = simulate(df_test, sigs, BALANCE, usdt)
            m        = compute_metrics(sim, df_test, strategy, "oos")
        except Exception as e:
            print(f"  [WF] Window {window_idx} error: {e}")
            start += step
            window_idx += 1
            continue

        results.append(WindowResult(
            window_idx  = window_idx,
            train_start = str(df.index[max(0, start - train_h)])[:10],
            train_end   = str(df.index[start - 1])[:10],
            test_start  = str(df_test.index[0])[:10],
            test_end    = str(df_test.index[-1])[:10],
            metrics     = m,
        ))

        start      += step
        window_idx += 1

    return StrategyWFResult(strategy=strategy, symbol=symbol, windows=results)


# ── Data loading ──────────────────────────────────────────────────────────────

def _load_data(symbol: str, years: float, force_refresh: bool) -> pd.DataFrame:
    """Load real data from cache/Binance, fall back to synthetic if unavailable."""
    if _FETCHER_AVAILABLE:
        try:
            df = load_or_fetch(
                symbol=symbol,
                timeframe="1h",
                years=years,
                force_refresh=force_refresh,
            )
            print(f"  {G}✓{RESET} {symbol}: {len(df):,} real candles "
                  f"({df.index[0].date()} → {df.index[-1].date()})")
            return df
        except Exception as e:
            print(f"  {Y}⚠{RESET} {symbol}: real data unavailable ({e}) — using synthetic")

    # Synthetic fallback
    from backtest.run_improved import generate_ohlcv
    n_hours = int(years * 365.25 * 24)
    df = generate_ohlcv(n_hours=n_hours)
    print(f"  {Y}(synthetic){RESET} {symbol}: {len(df):,} GBM candles")
    return df


# ── Reporter ──────────────────────────────────────────────────────────────────

def _pct(v, pos_good=True):
    col = (G if v > 0 else R) if pos_good else (R if v > 0 else G)
    return f"{col}{'+' if v>0 else ''}{v:.2f}%{RESET}"

def _val(v, pos_good=True):
    col = (G if v > 0 else R) if pos_good else (R if v > 0 else G)
    return f"{col}{'+' if v>0 else ''}{v:.3f}{RESET}"


def print_strategy_detail(r: StrategyWFResult):
    """Print per-window OOS results for one strategy."""
    print(f"\n  {C}{B}{r.strategy}  —  {r.symbol}{RESET}")
    print(f"  {'Win#':<6} {'Test period':<22} {'Return':>10}  "
          f"{'MaxDD':>8}  {'Sharpe':>8}  {'WinRate':>9}  {'Trades':>7}")
    print("  " + "─" * 72)
    for w in r.windows:
        print(f"  {w.window_idx:<6} {w.test_start} → {w.test_end}  "
              f"{_pct(w.metrics.total_return_pct):>19}  "
              f"{R}-{w.metrics.max_drawdown_pct:.1f}%{RESET:>4}  "
              f"{_val(w.metrics.sharpe):>17}  "
              f"{w.metrics.win_rate_pct:>6.1f}%  "
              f"{w.metrics.total_trades:>6}")
    print(f"\n  Summary: {len(r.windows)} windows | "
          f"Mean Sharpe {_val(r.mean_sharpe)} (σ={r.std_sharpe:.3f}) | "
          f"Mean Return {_pct(r.mean_return)} | "
          f"Win {r.win_pct:.0f}% of windows")


def print_summary_table(results: list[StrategyWFResult]):
    """Ranked comparison of all strategies by mean OOS Sharpe."""
    ranked = sorted(results, key=lambda r: r.mean_sharpe, reverse=True)

    print(f"\n\n{'═'*95}")
    print(f"  {B}WALK-FORWARD SUMMARY — {YEARS:.0f} years data | "
          f"{TRAIN_MONTHS}m train / {TEST_MONTHS}m test windows{RESET}")
    print(f"{'═'*95}")
    print(f"  {'Strategy':<18} {'Symbol':<12} {'Windows':>8}  "
          f"{'MeanSharpe':>12}  {'SharpeStd':>11}  "
          f"{'MeanReturn':>12}  {'AvgDD':>8}  {'WinPct':>8}")
    print("  " + "─" * 91)

    for r in ranked:
        consistency = "✓" if r.std_sharpe < 0.5 and r.mean_sharpe > 0.5 else (
                      "△" if r.mean_sharpe > 0 else "✗")
        col = G if consistency == "✓" else (Y if consistency == "△" else R)
        print(
            f"  {r.strategy:<18} {r.symbol:<12} {len(r.windows):>8}  "
            f"{_val(r.mean_sharpe):>21}  "
            f"{r.std_sharpe:>9.3f}  "
            f"{_pct(r.mean_return):>21}  "
            f"{R}-{r.mean_drawdown:.1f}%{RESET:>4}  "
            f"{r.win_pct:>6.0f}%  {col}{consistency}{RESET}"
        )

    print(f"\n{'═'*95}")
    if ranked:
        best = ranked[0]
        print(f"\n  {B}Best strategy:{RESET} {G}{best.strategy}{RESET} on {best.symbol}")
        print(f"  Mean OOS Sharpe: {_val(best.mean_sharpe)} | "
              f"Profitable in {best.win_pct:.0f}% of windows")

    print(f"""
  {B}HOW TO READ{RESET}
  Windows    — Number of independent OOS test periods (more = more reliable)
  MeanSharpe — Average OOS Sharpe across all windows (>1 good, >2 excellent)
  SharpeStd  — Consistency: lower = more reliable. <0.5 with MeanSharpe>0.5 = ✓
  MeanReturn — Average OOS return per window ({TEST_MONTHS}m period)
  AvgDD      — Average max drawdown per window
  WinPct     — % of windows with positive return (>60% is solid)
  ✓ = Consistent + positive  △ = Positive but inconsistent  ✗ = Negative mean
""")


# ── Main ──────────────────────────────────────────────────────────────────────

def run_all():
    train_h = TRAIN_MONTHS * HOURS_PER_MONTH
    test_h  = TEST_MONTHS  * HOURS_PER_MONTH

    print(f"\n{'═'*95}")
    print(f"  {B}WALK-FORWARD BACKTEST — REAL DATA{RESET}")
    print(f"  History: {YEARS:.0f} years  |  Train: {TRAIN_MONTHS}m  |  Test: {TEST_MONTHS}m")
    print(f"  Expected windows ≈ {int((YEARS * 12 - TRAIN_MONTHS) / TEST_MONTHS)}")
    print(f"{'═'*95}\n")

    # Filter to single strategy if requested
    registry = STRATEGY_REGISTRY
    if ONLY_STRATEGY:
        registry = {k: v for k, v in STRATEGY_REGISTRY.items() if k == ONLY_STRATEGY}
        if not registry:
            print(f"Unknown strategy '{ONLY_STRATEGY}'. Available: {list(STRATEGY_REGISTRY.keys())}")
            return

    # Show cache status
    if _FETCHER_AVAILABLE:
        info = get_cache_info()
        if info:
            print("  Cached data files:")
            for i in info:
                if "error" not in i:
                    print(f"    {i['file']:<30} {i['rows']:>7,} rows  "
                          f"{i['start']} → {i['end']}  (age {i['age_hours']}h)")
        print()

    # Load data per unique symbol
    print("  Loading historical data …")
    data_cache: dict[str, pd.DataFrame] = {}
    for sname in registry:
        sym = config.STRATEGY_SYMBOLS.get(sname, "BTC/USDT")
        if sym not in data_cache:
            data_cache[sym] = _load_data(sym, YEARS, FORCE_REFRESH)
    print()

    # Run walk-forward for each strategy
    all_results: list[StrategyWFResult] = []

    for sname, (sig_func, usdt) in registry.items():
        sym = config.STRATEGY_SYMBOLS.get(sname, "BTC/USDT")
        df  = data_cache[sym]

        if len(df) < train_h + test_h + WARM_UP:
            print(f"  {Y}⚠{RESET} {sname}: not enough data "
                  f"({len(df)} candles < {train_h + test_h + WARM_UP} needed)")
            continue

        print(f"  Running {sname} on {sym} …", end="", flush=True)
        wf = run_walk_forward(df, sig_func, usdt, sname, sym, train_h, test_h)
        all_results.append(wf)
        print(f"  {len(wf.windows)} windows | "
              f"Mean Sharpe {_val(wf.mean_sharpe)} | "
              f"Win {wf.win_pct:.0f}%")

    # Detailed per-strategy report
    print(f"\n\n{'═'*95}")
    print(f"  {B}PER-STRATEGY WINDOW DETAIL{RESET}")
    print(f"{'═'*95}")
    for r in all_results:
        print_strategy_detail(r)

    # Summary ranking
    print_summary_table(all_results)
    return all_results


if __name__ == "__main__":
    run_all()
