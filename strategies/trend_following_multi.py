"""
strategies/trend_following_multi.py — Phase 4.A multi-asset TSMOM strategy.

Time-series momentum (TSMOM) on a daily multi-instrument crypto basket.
Long-only, vol-targeted per instrument.  Signal cadence is daily — the
sign of the trailing `lookback_days` return drives the next-bar long
or flat decision per symbol; vol-targeting per Barroso & Santa-Clara
(2015) scales each per-instrument long to a constant per-bar
volatility contribution.

This module is the strategy surface only — block construction, CPCV,
and the engine replay live in `backtest.engine_multi` and
`backtest.cpcv_multi`.  Symbols come from the manifest entry at
runtime (see `research/trendfollowing-literature.md` § Variation
discipline) — they are NEVER hardcoded inside the class.

Citations
─────────
- Moskowitz, Ooi & Pedersen (2012), "Time Series Momentum",
  Journal of Financial Economics 104.
- Hurst, Ooi & Pedersen (2017), "A Century of Evidence on
  Trend-Following Investing", Journal of Portfolio Management.
- Barroso & Santa-Clara (2015), "Momentum has its moments",
  Journal of Financial Economics 116.

Default parameter values are sourced from these papers; see
`research/trendfollowing-literature.md` for the per-knob mapping
and the rationale for the defaults below.
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np
import pandas as pd

from strategies.base import Signal


class TrendFollowingMultiStrategy:
    """Multi-instrument time-series momentum.

    The class is self-contained: callers supply a `dict[symbol,
    DataFrame]` of OHLCV slices ending at the current bar and receive
    back a `dict[symbol, Signal]` keyed by the same symbols.  N (the
    instrument count) is read from `len(self.symbols)` at every call,
    so an updated manifest list immediately drives the new behaviour
    with no code change.
    """

    def __init__(
        self,
        symbols: list[str],
        timeframe: str = "1d",
        # CITATION: trendfollowing-literature
        # Hurst/Ooi/Pedersen (2017) 6-month formation window.
        lookback_days: int = 126,
        # CITATION: trendfollowing-literature
        # Barroso & Santa-Clara (2015) vol-targeting calibration.
        target_vol_annual: float = 0.15,
    ):
        if not symbols:
            raise ValueError("symbols must be a non-empty list")
        if lookback_days < 2:
            raise ValueError(
                f"lookback_days must be ≥ 2; got {lookback_days}"
            )
        if not (0.0 < target_vol_annual <= 1.0):
            raise ValueError(
                "target_vol_annual must lie in (0, 1]; "
                f"got {target_vol_annual}"
            )

        self.name = "TrendFollowingMulti"
        self.symbols: list[str] = list(symbols)
        self.timeframe = timeframe
        self.lookback_days = lookback_days
        self.target_vol_annual = target_vol_annual

    # ── Signal generation ────────────────────────────────────────────────────

    def generate_signals(
        self,
        prices: dict[str, pd.DataFrame],
    ) -> dict[str, Signal]:
        """Map of symbol -> Signal for the current bar.

        For each known symbol:

          * If the slice has fewer than `lookback_days + 1` rows the
            signal is HOLD (insufficient history for a formation-window
            return).
          * Otherwise compute `ret = close[-1] / close[-lookback - 1] - 1`.
            Sign > 0  → BUY (long).
            Sign < 0  → SELL (close any long, stay flat per long-only).
            Sign == 0 → HOLD (degenerate case; do nothing).

        Symbols absent from `prices` get a HOLD signal at price 0.0
        with a "missing data" reason — the engine treats this as
        no-op, identical to insufficient history.
        """
        out: dict[str, Signal] = {}
        for sym in self.symbols:
            df = prices.get(sym)
            if df is None or len(df) == 0:
                out[sym] = Signal(
                    action="HOLD", strategy=self.name, price=0.0,
                    reason="missing-data",
                )
                continue

            close = df["close"]
            price = float(close.iloc[-1])
            if len(close) < self.lookback_days + 1:
                out[sym] = Signal(
                    action="HOLD", strategy=self.name, price=price,
                    reason="insufficient-history",
                )
                continue

            ref = float(close.iloc[-self.lookback_days - 1])
            if ref <= 0:
                out[sym] = Signal(
                    action="HOLD", strategy=self.name, price=price,
                    reason="reference-price-non-positive",
                )
                continue

            ret = (price / ref) - 1.0
            if ret > 0:
                out[sym] = Signal(
                    action="BUY", strategy=self.name, price=price,
                    reason=f"tsmom-{self.lookback_days}d-ret={ret:+.4f}",
                    order_type="market",
                )
            elif ret < 0:
                out[sym] = Signal(
                    action="SELL", strategy=self.name, price=price,
                    reason=f"tsmom-{self.lookback_days}d-ret={ret:+.4f}",
                    order_type="market",
                )
            else:
                out[sym] = Signal(
                    action="HOLD", strategy=self.name, price=price,
                    reason="tsmom-zero-return",
                )
        return out

    # ── Vol-targeting helper ────────────────────────────────────────────────

    def realized_vol(self, df: pd.DataFrame) -> float:
        """Annualised realised volatility from the last `lookback_days`
        daily log-returns.

        Returns `target_vol_annual` (the conservative fallback) when
        the slice is shorter than `lookback_days + 1` rows so the
        caller's `target_vol / realized_vol` ratio resolves to 1.0
        and the position size collapses to the `1/N` baseline rather
        than blowing up via division-by-near-zero.

        Annualisation uses the standard 252 √-rule for daily data per
        Barroso & Santa-Clara (2015) §3.
        """
        if df is None or len(df) < self.lookback_days + 1:
            return self.target_vol_annual

        close = df["close"].astype(float)
        # CITATION: trendfollowing-literature
        # 252 trading-day annualisation per Barroso & Santa-Clara
        # (2015) §3 — daily realised vol scaled by sqrt(252) into
        # an annualised figure for the vol-targeting denominator.
        log_ret = np.log(close / close.shift(1)).dropna().tail(
            self.lookback_days
        )
        if len(log_ret) < self.lookback_days:
            return self.target_vol_annual
        sigma_daily = float(log_ret.std())
        if sigma_daily <= 0 or not math.isfinite(sigma_daily):
            return self.target_vol_annual
        # CITATION: trendfollowing-literature
        sigma_annual = sigma_daily * math.sqrt(252)
        if not math.isfinite(sigma_annual) or sigma_annual <= 0:
            return self.target_vol_annual
        return sigma_annual

    # ── Sizing helper (used by engine_multi) ────────────────────────────────

    def position_fraction(
        self,
        df: pd.DataFrame,
        n_active: int,
        max_concentration_mult: float = 2.0,
    ) -> float:
        """Per-instrument position fraction of portfolio equity.

        size = (target_vol / realized_vol) × (1 / n_active),
        capped at `max_concentration_mult × (1 / n_active)` to prevent
        a single low-vol instrument from dominating the basket per
        Barroso & Santa-Clara (2015) §3.

        `n_active` is the basket size — the engine passes
        `len(self.symbols)` so updates flow from the manifest.
        """
        if n_active <= 0:
            return 0.0
        baseline = 1.0 / n_active
        rv = self.realized_vol(df)
        if rv <= 0 or not math.isfinite(rv):
            return baseline
        scaled = (self.target_vol_annual / rv) * baseline
        cap = max_concentration_mult * baseline
        return float(min(max(scaled, 0.0), cap))
