"""
backtest/optimizer.py — Walk-forward parameter optimizer.

WHY WALK-FORWARD OPTIMIZATION?
───────────────────────────────
Normal grid search picks the parameters that maximized in-sample (training)
performance. This almost always overfits — the "best" params exploit random
noise in the training data and fall apart in live trading.

Walk-forward optimization fixes this:
  1. Split the data into N rolling windows (train+test pairs).
  2. For EACH parameter combination, run the strategy on EVERY window's
     test (out-of-sample) slice only.
  3. Rank by the AVERAGE of those out-of-sample scores.

The key insight: a parameter set that consistently works across multiple
disjoint out-of-sample periods has earned its results. One that worked on
a single in-sample period is just overfit.

USAGE
─────
    from backtest.optimizer import ParameterOptimizer
    from strategies.dca import DCAStrategy

    # 1. Define a factory: takes a param dict, returns a fresh strategy
    def dca_factory(params):
        return DCAStrategy(
            deviation_pct  = params['deviation_pct'],
            safety_scale   = params['safety_scale'],
            max_safety_orders = params['max_safety_orders'],
        )

    # 2. Define the search grid
    param_grid = {
        'deviation_pct':     [1.5, 2.0, 2.5, 3.0],
        'safety_scale':      [1.3, 1.5, 1.8],
        'max_safety_orders': [3, 5],
    }
    # → 4 × 3 × 2 = 24 combinations, each tested across N walk-forward windows

    # 3. Run
    optimizer = ParameterOptimizer(
        strategy_factory_fn = dca_factory,
        param_grid          = param_grid,
        n_windows           = 5,
        train_pct           = 0.70,
        scoring             = 'sharpe',   # or 'return', 'calmar', 'composite'
        max_combinations    = 100,        # Safety cap
        initial_balance     = 10_000,
    )
    report = optimizer.run(df)
    print(report.summary())
    print("Best params:", report.best_params)

SCORING MODES
─────────────
  'sharpe'    : Mean out-of-sample Sharpe ratio across windows. Good general
                purpose — rewards return per unit of risk.
  'return'    : Mean out-of-sample total return %. High score = good returns.
                Can be gamed by high-risk strategies.
  'calmar'    : Return / max drawdown. Rewards risk-adjusted returns well.
  'composite' : 0.5×Sharpe + 0.3×(return/10) + 0.2×(win_rate/100) − 0.1×dd.
                Balanced score that penalises high drawdown explicitly.

SAFETY CAPS
───────────
  max_combinations: Optimizer stops accepting new combinations once this
                    limit is reached (grid is truncated, not sampled).
                    Default 100 — enough for thorough search without
                    multi-hour runtimes on hourly data.
"""

import itertools
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
import numpy as np
import pandas as pd
from loguru import logger

from backtest.engine import BacktestEngine, WalkForwardBacktester, WalkForwardReport


# ── Result dataclasses ───────────────────────────────────────────────────────

@dataclass
class CandidateResult:
    """Result for one parameter combination across all walk-forward windows."""
    params:                dict
    score:                 float    # Composite score used for ranking
    mean_test_return_pct:  float
    mean_test_sharpe:      float
    mean_test_drawdown_pct: float
    mean_test_win_rate:    float
    pct_windows_profitable: float
    elapsed_sec:           float
    wf_report:             Optional[WalkForwardReport] = None

    def param_str(self) -> str:
        return " | ".join(f"{k}={v}" for k, v in self.params.items())


@dataclass
class OptimizationReport:
    """Full results from a parameter optimization run."""
    strategy_name:    str
    symbol:           str
    scoring:          str
    n_combinations:   int
    n_windows:        int
    train_pct:        float
    elapsed_sec:      float
    results:          list  # list[CandidateResult], sorted best→worst
    best_params:      dict
    best_score:       float

    # ── Convenience accessors ─────────────────────────────────────────────

    def top(self, n: int = 5) -> list:
        """Return the top-N CandidateResult objects."""
        return self.results[:n]

    def as_dataframe(self) -> pd.DataFrame:
        """Return all results as a pandas DataFrame for easy analysis."""
        rows = []
        for r in self.results:
            row = dict(r.params)
            row.update({
                "score":              round(r.score, 4),
                "return_pct":         round(r.mean_test_return_pct, 2),
                "sharpe":             round(r.mean_test_sharpe, 3),
                "max_dd_pct":         round(r.mean_test_drawdown_pct, 2),
                "win_rate_pct":       round(r.mean_test_win_rate, 1),
                "pct_windows_profit": round(r.pct_windows_profitable, 1),
                "elapsed_sec":        round(r.elapsed_sec, 1),
            })
            rows.append(row)
        return pd.DataFrame(rows)

    def summary(self) -> str:
        """Print a formatted leaderboard of the top 10 results."""
        lines = [
            "",
            "=" * 72,
            f"  PARAMETER OPTIMIZATION RESULTS  —  {self.strategy_name} / {self.symbol}",
            "=" * 72,
            f"  Scoring     : {self.scoring}",
            f"  Combinations: {self.n_combinations} tested",
            f"  WF Windows  : {self.n_windows}  (train {self.train_pct*100:.0f}% / test {(1-self.train_pct)*100:.0f}%)",
            f"  Total time  : {self.elapsed_sec:.1f}s",
            "",
            f"  {'Rank':<5} {'Score':>7} {'Return%':>9} {'Sharpe':>8} {'MaxDD%':>8} "
            f"{'WinR%':>7} {'Win Win%':>9}",
            "  " + "-" * 65,
        ]

        for rank, r in enumerate(self.results[:10], 1):
            lines.append(
                f"  {rank:<5} {r.score:>7.3f} {r.mean_test_return_pct:>+8.2f}% "
                f"{r.mean_test_sharpe:>8.3f} {r.mean_test_drawdown_pct:>7.2f}% "
                f"{r.mean_test_win_rate:>6.1f}% {r.pct_windows_profitable:>8.1f}%"
            )
            lines.append(f"        Params: {r.param_str()}")

        lines.extend([
            "",
            f"  BEST: {self.best_params}",
            f"  Score: {self.best_score:.4f}",
            "=" * 72,
        ])
        return "\n".join(lines)


# ── Optimizer ────────────────────────────────────────────────────────────────

class ParameterOptimizer:
    """
    Walk-forward parameter optimizer for any BaseStrategy subclass.

    Each parameter combination is evaluated purely on out-of-sample
    walk-forward windows — not on training data — so the results are
    not overfit to any single historical period.

    Args:
        strategy_factory_fn: Callable[[dict], BaseStrategy].
                             Takes a param dict, returns a fresh strategy.
                             Called many times — keep it cheap to construct.
        param_grid:          Dict of {param_name: [list of values to try]}.
                             All combinations are tested (grid search).
        n_windows:           Number of walk-forward windows. Default 5.
        train_pct:           Fraction of each window used for "training"
                             (warm-up only — no signal quality extracted).
                             Default 0.70.
        scoring:             Metric to rank by. One of:
                               'sharpe'    (default)
                               'return'
                               'calmar'
                               'composite'
        max_combinations:    Hard cap on tested combinations. If the grid
                             has more, they are truncated (first N tested).
                             Default 100.
        initial_balance:     Paper trading starting balance. Default 10,000.
        warm_up_candles:     Warm-up candles per window run. Default 50.
        next_candle_fill:    Pass True for realistic BUY fill simulation.
    """

    VALID_SCORING = {"sharpe", "return", "calmar", "composite"}

    def __init__(
        self,
        strategy_factory_fn: Callable[[dict], Any],
        param_grid:          dict,
        n_windows:           int   = 5,
        train_pct:           float = 0.70,
        scoring:             str   = "sharpe",
        max_combinations:    int   = 100,
        initial_balance:     float = 10_000.0,
        warm_up_candles:     int   = 50,
        next_candle_fill:    bool  = False,
    ):
        if scoring not in self.VALID_SCORING:
            raise ValueError(
                f"scoring must be one of {self.VALID_SCORING}, got '{scoring}'"
            )

        self.strategy_factory_fn = strategy_factory_fn
        self.param_grid          = param_grid
        self.n_windows           = n_windows
        self.train_pct           = train_pct
        self.scoring             = scoring
        self.max_combinations    = max_combinations
        self.initial_balance     = initial_balance
        self.warm_up_candles     = warm_up_candles
        self.next_candle_fill    = next_candle_fill

    # ── Public API ────────────────────────────────────────────────────────────

    def run(self, df: pd.DataFrame) -> OptimizationReport:
        """
        Run the full walk-forward optimization over the given OHLCV data.

        Args:
            df: Full historical OHLCV DataFrame. Should cover at least
                n_windows × (warm_up_candles + 20) candles.

        Returns:
            OptimizationReport sorted by score descending (best first).
        """
        combinations = self._build_combinations()
        n_total      = len(combinations)

        # Get strategy metadata from a test instance
        test_strategy = self.strategy_factory_fn(combinations[0])
        strategy_name = test_strategy.name
        symbol        = test_strategy.symbol

        logger.info(
            f"[Optimizer] Starting walk-forward optimization | "
            f"Strategy: {strategy_name} | Symbol: {symbol} | "
            f"Combinations: {n_total} | Windows: {self.n_windows} | "
            f"Scoring: {self.scoring}"
        )

        engine = BacktestEngine(
            initial_balance  = self.initial_balance,
            warm_up_candles  = self.warm_up_candles,
            next_candle_fill = self.next_candle_fill,
            verbose          = False,
        )

        overall_start = time.time()
        candidate_results: list[CandidateResult] = []

        for idx, params in enumerate(combinations, 1):
            t0 = time.time()

            logger.info(
                f"[Optimizer] {idx}/{n_total} | Params: "
                + " | ".join(f"{k}={v}" for k, v in params.items())
            )

            # Build a walk-forward backtester using a factory closure that
            # injects the current params into every fresh strategy instance.
            captured_params = dict(params)  # capture by value, not reference

            def make_strategy(p=captured_params):
                return self.strategy_factory_fn(p)

            wf = WalkForwardBacktester(
                strategy_factory = make_strategy,
                initial_balance  = self.initial_balance,
                n_windows        = self.n_windows,
                train_pct        = self.train_pct,
                warm_up_candles  = self.warm_up_candles,
            )

            try:
                report = wf.run(df)
            except Exception as exc:
                logger.warning(f"[Optimizer] Params {params} failed: {exc} — skipping.")
                continue

            score = self._compute_score(report)
            elapsed = time.time() - t0

            candidate_results.append(CandidateResult(
                params                 = params,
                score                  = score,
                mean_test_return_pct   = report.mean_test_return_pct,
                mean_test_sharpe       = report.mean_test_sharpe,
                mean_test_drawdown_pct = report.mean_test_drawdown_pct,
                mean_test_win_rate     = report.mean_test_win_rate,
                pct_windows_profitable = report.pct_windows_profitable,
                elapsed_sec            = elapsed,
                wf_report              = report,
            ))

            logger.info(
                f"[Optimizer]   Score={score:.4f} | "
                f"Return={report.mean_test_return_pct:+.2f}% | "
                f"Sharpe={report.mean_test_sharpe:.3f} | "
                f"DD={report.mean_test_drawdown_pct:.2f}% | "
                f"Win%={report.mean_test_win_rate:.1f}% | "
                f"({elapsed:.1f}s)"
            )

        if not candidate_results:
            raise RuntimeError(
                "Optimizer produced no valid results. Check that df has "
                "enough candles and the strategy factory is correct."
            )

        # Sort best → worst
        candidate_results.sort(key=lambda r: r.score, reverse=True)

        total_elapsed = time.time() - overall_start
        best = candidate_results[0]

        final_report = OptimizationReport(
            strategy_name  = strategy_name,
            symbol         = symbol,
            scoring        = self.scoring,
            n_combinations = len(candidate_results),
            n_windows      = self.n_windows,
            train_pct      = self.train_pct,
            elapsed_sec    = total_elapsed,
            results        = candidate_results,
            best_params    = best.params,
            best_score     = best.score,
        )

        logger.info(f"\n{final_report.summary()}")
        return final_report

    # ── Private helpers ───────────────────────────────────────────────────────

    def _build_combinations(self) -> list[dict]:
        """
        Expand param_grid into a flat list of parameter dicts.

        Example:
            {'a': [1, 2], 'b': [10, 20]} →
            [{'a': 1, 'b': 10}, {'a': 1, 'b': 20},
             {'a': 2, 'b': 10}, {'a': 2, 'b': 20}]
        """
        keys   = list(self.param_grid.keys())
        values = list(self.param_grid.values())

        all_combos = [
            dict(zip(keys, combo))
            for combo in itertools.product(*values)
        ]

        total = len(all_combos)
        if total > self.max_combinations:
            logger.warning(
                f"[Optimizer] Grid has {total} combinations but max_combinations="
                f"{self.max_combinations}. Truncating to first {self.max_combinations}. "
                f"Increase max_combinations or reduce grid size to test all."
            )
            all_combos = all_combos[:self.max_combinations]

        logger.info(
            f"[Optimizer] Grid: "
            + " × ".join(f"{k}({len(v)})" for k, v in self.param_grid.items())
            + f" = {total} total, testing {len(all_combos)}"
        )
        return all_combos

    def _compute_score(self, report: WalkForwardReport) -> float:
        """
        Convert a WalkForwardReport into a single scalar score for ranking.

        Higher is always better for all scoring modes.
        """
        ret    = report.mean_test_return_pct
        sharpe = report.mean_test_sharpe
        dd     = report.mean_test_drawdown_pct   # positive number, worse is higher
        wr     = report.mean_test_win_rate        # 0-100

        if self.scoring == "sharpe":
            return sharpe

        elif self.scoring == "return":
            return ret

        elif self.scoring == "calmar":
            # Calmar = annualised return / max drawdown
            # We approximate with test-period return / drawdown
            if dd <= 0:
                return ret * 10.0   # No drawdown — very high score
            return ret / dd

        elif self.scoring == "composite":
            # Balanced score penalising high drawdown explicitly.
            # Normalised so each component contributes comparably:
            #   Sharpe     → typically in [-1, 3]   weight 0.50
            #   Return/10  → typically in [-2, 5]   weight 0.30
            #   WinRate/100→ in [0, 1]              weight 0.20
            #   DD penalty → subtract 0.1 per 1% of drawdown
            return (
                0.50 * sharpe
                + 0.30 * (ret / 10.0)
                + 0.20 * (wr / 100.0)
                - 0.10 * (dd / 10.0)
            )

        return sharpe   # fallback
