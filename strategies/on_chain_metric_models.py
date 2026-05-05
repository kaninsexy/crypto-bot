"""
strategies/on_chain_metric_models.py — sq-001 strategy.

On-chain macro cycle indicators (MVRV Z-score primary) used as a
position filter on a crypto basket. Reads an `mvrv_zscore` column
injected alongside OHLCV by the trial script.

Algorithm per bar:
  1. Read each symbol's `mvrv_zscore` column.
  2. If column absent for any symbol, that symbol receives HOLD
     (data not loaded yet — orchestrator should exit 2 upstream).
  3. Long signal (BUY) when mvrv_zscore < `mvrv_short_threshold`
     (undervalued — historically a long entry zone).
  4. Flat signal (SELL) when mvrv_zscore > `mvrv_long_threshold`
     (overvalued — historically an exit zone).
  5. Otherwise HOLD.

Single concurrent position per symbol, long-only — the engine
enforces the per-symbol cap.

Contract with backtest.engine_multi:

  * `lookback_days = 30` is a minimal warmup floor — MVRV is a daily
    on-chain signal and does not need a long rolling window.
  * `position_fraction(df, n_active)` returns 1 / n_active so the
    basket sizes equal-weight.

Citation: Proposal-agent dry-run 2026-05-05 (overall_quality 3.3);
literature compendium pending in research/on-chain-metric-models-
literature.md.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from strategies.base import Signal


MVRV_COLUMN = "mvrv_zscore"
# CITATION: on-chain-metric-models-literature
# MVRV is a daily on-chain signal; 30-day lookback is the minimal
# warmup floor (engine_multi.min_history_bars = lookback_days + 2).
_DEFAULT_LOOKBACK_DAYS = 30


class OnChainMetricModelsStrategy:
    """On-chain MVRV-Z-score band strategy on a crypto basket."""

    def __init__(
        self,
        symbols: list[str],
        timeframe: str = "1d",
        # CITATION: on-chain-metric-models-literature
        # Glassnode MVRV Z-score historical "overvalued" band (>3.5)
        # marks cycle-top entries → exit zone for long-only spot.
        mvrv_long_threshold: float = 3.5,
        # CITATION: on-chain-metric-models-literature
        # MVRV Z-score "undervalued" band (<1.0) marks cycle-bottom
        # accumulation zones → long-entry signal.
        mvrv_short_threshold: float = 1.0,
        # CITATION: on-chain-metric-models-literature
        # Engine default initial_balance for the Phase 4 backtest harness.
        notional_capital: float = 10_000.0,
    ):
        if not symbols:
            raise ValueError("symbols must be a non-empty list")
        if not (mvrv_short_threshold < mvrv_long_threshold):
            raise ValueError(
                f"mvrv_short_threshold ({mvrv_short_threshold}) must be < "
                f"mvrv_long_threshold ({mvrv_long_threshold})"
            )

        self.name = "OnChainMetricModels"
        self.symbols: list[str] = list(symbols)
        self.timeframe = timeframe
        self.mvrv_long_threshold = float(mvrv_long_threshold)
        self.mvrv_short_threshold = float(mvrv_short_threshold)
        self.notional_capital = float(notional_capital)

        # Engine warmup floor; MVRV is a daily signal and does not
        # need a rolling lookback beyond a single value at bar t.
        self.lookback_days = _DEFAULT_LOOKBACK_DAYS

    # ── Engine sizing hook ───────────────────────────────────────────────────

    def position_fraction(
        self,
        df: pd.DataFrame,
        n_active: int,
        max_concentration_mult: float = 2.0,
    ) -> float:
        """1 / n_active sizing — equal-weight basket."""
        if n_active <= 0:
            return 0.0
        return float(1.0 / float(n_active))

    # ── Signal generation ────────────────────────────────────────────────────

    def generate_signals(
        self,
        prices: dict[str, pd.DataFrame],
    ) -> dict[str, Signal]:
        """Map of symbol -> Signal for the current bar."""
        out: dict[str, Signal] = {}

        for sym in self.symbols:
            df = prices.get(sym)
            if df is None or len(df) == 0:
                out[sym] = Signal(
                    action="HOLD", strategy=self.name, price=0.0,
                    reason="missing-data",
                )
                continue

            price = float(df["close"].iloc[-1])

            if MVRV_COLUMN not in df.columns:
                out[sym] = Signal(
                    action="HOLD", strategy=self.name, price=price,
                    reason="mvrv_zscore-column-absent",
                )
                continue

            mvrv = float(df[MVRV_COLUMN].iloc[-1])
            if not math.isfinite(mvrv):
                out[sym] = Signal(
                    action="HOLD", strategy=self.name, price=price,
                    reason="mvrv-not-finite",
                )
                continue

            if mvrv < self.mvrv_short_threshold:
                out[sym] = Signal(
                    action="BUY", strategy=self.name, price=price,
                    reason=(
                        f"mvrv-undervalued | mvrv={mvrv:+.3f} < "
                        f"{self.mvrv_short_threshold}"
                    ),
                    order_type="market",
                )
            elif mvrv > self.mvrv_long_threshold:
                out[sym] = Signal(
                    action="SELL", strategy=self.name, price=price,
                    reason=(
                        f"mvrv-overvalued | mvrv={mvrv:+.3f} > "
                        f"{self.mvrv_long_threshold}"
                    ),
                    order_type="market",
                )
            else:
                out[sym] = Signal(
                    action="HOLD", strategy=self.name, price=price,
                    reason=(
                        f"mvrv-mid | mvrv={mvrv:+.3f} in "
                        f"[{self.mvrv_short_threshold}, "
                        f"{self.mvrv_long_threshold}]"
                    ),
                )

        return out
