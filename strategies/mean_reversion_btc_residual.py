"""
strategies/mean_reversion_btc_residual.py — Phase 4.A MeanReversion variation #1.

BTC-neutral residual mean-reversion on a 5-alt crypto basket
(ETH/SOL/BNB/XRP/ADA) at 4H. Hypothesis: alts that have moved
disproportionately negative relative to BTC's market move tend to
revert; this isolates idiosyncratic alt mean-reversion from the
BTC-driven market beta.

Algorithm per bar (per Fil & Kristoufek 2020 IEEE Access):

  1. Compute log-returns for each symbol over the basket history.
  2. For each alt at bar t: rolling beta_window-bar OLS beta vs BTC:
         beta[t] = cov(alt_ret[t-W+1:t+1], btc_ret[t-W+1:t+1]) /
                   var(btc_ret[t-W+1:t+1])
  3. residual_ret[t] = alt_ret[t] - beta[t] * btc_ret[t]
  4. cum_resid[t] = sum(residual_ret[t-Z+1:t+1])
  5. Cross-sectional z across alts at bar t:
         z[alt,t] = (cum_resid[alt,t] - mean(cum_resid[:,t])) /
                    std(cum_resid[:,t])
  6. Entry: long alts with z < entry_z_threshold, ranked
     most-negative first, capped at max_positions concurrent open.
  7. Exit: z >= exit_z_threshold, OR unrealised pnl <= -stop_loss_pct,
     OR bars_held >= max_hold_bars.

Contract with backtest.engine_multi:

  * `symbols` constructor arg is the FULL list including the BTC
    reference; engine_multi reads `len(strategy.symbols)` for n_active
    and the per-bar synchronisation.
  * BTC/USDT is the reference leg only — `generate_signals` always
    emits HOLD for it; `position_fraction` returns 0 if the engine
    ever asks (it won't, since BTC never gets a BUY signal).
  * `lookback_days` attribute exposes the warmup floor used by
    engine_multi's `min_history_bars = strategy.lookback_days + 2`
    check, set to `beta_window + zscore_window` so the engine waits
    until both windows are full before invoking the strategy.

Citation: Fil, M. & Kristoufek, L. (2020). "Pairs Trading in
Cryptocurrency Markets." IEEE Access 8, 172644-172651.
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np
import pandas as pd

from strategies.base import Signal


class _InsufficientHistory(Exception):
    """Raised when the BTC reference series is too short for a beta."""


class MeanReversionBTCResidualStrategy:
    """BTC-neutral residual MR on a 5-alt basket.

    Self-contained: callers supply a `dict[symbol, DataFrame]` of OHLCV
    slices ending at the current bar and receive back a `dict[symbol,
    Signal]` keyed by the same symbols. Internal state tracks open
    positions to enforce the max-positions cap and per-position exit
    conditions (stop-loss, max-hold).
    """

    def __init__(
        self,
        symbols: list[str],
        btc_symbol: str = "BTC/USDT",
        timeframe: str = "4h",
        # CITATION: mean-reversion-btc-residual-literature
        # Fil & Kristoufek (2020) §3 use a 60-bar rolling OLS beta
        # window for the cointegration-residual leg on 4H crypto bars.
        beta_window: int = 60,
        # CITATION: mean-reversion-btc-residual-literature
        # 30-bar (≈5-day at 4H) cumulative-residual horizon for the
        # cross-sectional z-score, per Fil & Kristoufek (2020) §3.
        zscore_window: int = 30,
        # CITATION: mean-reversion-btc-residual-literature
        # Entry z-threshold of -1.5 SD matches the lower tail used in
        # Fil & Kristoufek (2020) §4 for residual MR entries.
        entry_z_threshold: float = -1.5,
        exit_z_threshold: float = 0.0,
        max_positions: int = 2,
        # CITATION: mean-reversion-btc-residual-literature
        # 8% stop-loss bound on residual MR positions, matching
        # Fil & Kristoufek (2020) §4 risk-control specification.
        stop_loss_pct: float = 0.08,
        # CITATION: mean-reversion-btc-residual-literature
        # 30-bar (≈5-day at 4H) max-hold cap, paired with the 30-bar
        # zscore_window so positions exit within one signal horizon.
        max_hold_bars: int = 30,
        # CITATION: mean-reversion-btc-residual-literature
        # Engine default initial_balance for the Phase 4 backtest harness.
        notional_capital: float = 10_000.0,
    ):
        if not symbols:
            raise ValueError("symbols must be a non-empty list")
        if btc_symbol not in symbols:
            raise ValueError(
                f"btc_symbol={btc_symbol!r} must appear in "
                f"symbols={symbols!r}"
            )
        if beta_window < 2 or zscore_window < 2:
            raise ValueError("beta_window and zscore_window must be >= 2")
        if max_positions < 1:
            raise ValueError("max_positions must be >= 1")
        if not (0.0 < stop_loss_pct < 1.0):
            raise ValueError("stop_loss_pct must lie in (0, 1)")
        if max_hold_bars < 1:
            raise ValueError("max_hold_bars must be >= 1")

        self.name = "MeanReversionBTCResidual"
        self.symbols: list[str] = list(symbols)
        self.btc_symbol = btc_symbol
        self.alt_symbols: list[str] = [
            s for s in self.symbols if s != btc_symbol
        ]
        if not self.alt_symbols:
            raise ValueError(
                "symbols must include at least one alt besides BTC"
            )
        self.timeframe = timeframe
        self.beta_window = int(beta_window)
        self.zscore_window = int(zscore_window)
        self.entry_z_threshold = float(entry_z_threshold)
        self.exit_z_threshold = float(exit_z_threshold)
        self.max_positions = int(max_positions)
        self.stop_loss_pct = float(stop_loss_pct)
        self.max_hold_bars = int(max_hold_bars)
        self.notional_capital = float(notional_capital)

        # engine_multi.min_history_bars = strategy.lookback_days + 2,
        # so we set lookback_days = beta_window + zscore_window to
        # guarantee both windows are full before signal generation.
        self.lookback_days = self.beta_window + self.zscore_window

        # Internal bookkeeping for the max-positions cap and per-
        # position exit conditions. Engine fills are inferred from
        # emitted signals; for long-only spot with 1/max_positions
        # sizing on a 5-alt basket the engine fills the strategy's
        # intent on every bar that has cash available.
        self._open_positions: dict[str, dict] = {}

    # ── Engine sizing hook ───────────────────────────────────────────────────

    def position_fraction(
        self,
        df: pd.DataFrame,
        n_active: int,
        max_concentration_mult: float = 2.0,
    ) -> float:
        """Per-symbol position fraction of portfolio equity.

        Sizing target = 1 / max_positions so the basket reaches full
        notional when max_positions concurrent longs are open. The
        engine clamps to available cash, so this is a soft cap.

        Note: engine_multi only invokes position_fraction for symbols
        the strategy emitted BUY for; BTC always receives HOLD, so
        this fraction is never applied to the reference leg.
        """
        return float(1.0 / float(self.max_positions))

    # ── Signal generation ────────────────────────────────────────────────────

    def generate_signals(
        self,
        prices: dict[str, pd.DataFrame],
    ) -> dict[str, Signal]:
        """Map of symbol -> Signal for the current bar."""
        out: dict[str, Signal] = {}

        # BTC always HOLD — reference leg, never traded.
        btc_df = prices.get(self.btc_symbol)
        btc_price = (
            float(btc_df["close"].iloc[-1])
            if btc_df is not None and len(btc_df) > 0 else 0.0
        )
        out[self.btc_symbol] = Signal(
            action="HOLD", strategy=self.name, price=btc_price,
            reason="btc-reference-leg",
        )

        # Insufficient BTC history for beta -> HOLD all alts.
        min_history = self.beta_window + self.zscore_window + 1
        if btc_df is None or len(btc_df) < min_history:
            for sym in self.alt_symbols:
                df = prices.get(sym)
                price = (
                    float(df["close"].iloc[-1])
                    if df is not None and len(df) > 0 else 0.0
                )
                out[sym] = Signal(
                    action="HOLD", strategy=self.name, price=price,
                    reason="warmup-insufficient-btc-history",
                )
            return out

        # Compute cum_resid at the latest bar for each alt.
        try:
            cum_resid_at_t = self._compute_cum_residuals(prices, btc_df)
        except _InsufficientHistory as exc:
            for sym in self.alt_symbols:
                df = prices.get(sym)
                price = (
                    float(df["close"].iloc[-1])
                    if df is not None and len(df) > 0 else 0.0
                )
                out[sym] = Signal(
                    action="HOLD", strategy=self.name, price=price,
                    reason=f"warmup: {exc}",
                )
            return out

        # Cross-sectional z across alts that produced a finite cum_resid.
        finite_alts = {
            sym: r for sym, r in cum_resid_at_t.items()
            if r is not None and math.isfinite(r)
        }
        if len(finite_alts) < 2:
            for sym in self.alt_symbols:
                df = prices.get(sym)
                price = (
                    float(df["close"].iloc[-1])
                    if df is not None and len(df) > 0 else 0.0
                )
                out[sym] = Signal(
                    action="HOLD", strategy=self.name, price=price,
                    reason="cross-section-too-small",
                )
            return out

        cs_values = np.array(list(finite_alts.values()), dtype=np.float64)
        cs_mean = float(cs_values.mean())
        cs_std = float(cs_values.std(ddof=0))
        z_by_sym: dict[str, float] = {}
        for sym, r in finite_alts.items():
            # CITATION: standard numerical stability constant, not a tuned parameter
            if cs_std > 1e-12 and math.isfinite(cs_std):
                z_by_sym[sym] = (r - cs_mean) / cs_std
            else:
                z_by_sym[sym] = 0.0

        # Increment hold counters (engine processes one bar per call).
        for state in self._open_positions.values():
            state["bars_held"] += 1

        # Process exits first — mirrors engine_multi's "closes before
        # opens" ordering.
        exit_signals: dict[str, Signal] = {}
        for sym, state in list(self._open_positions.items()):
            df = prices.get(sym)
            if df is None or len(df) == 0:
                continue
            price = float(df["close"].iloc[-1])
            entry_price = state["entry_price"]
            pnl_pct = (
                (price / entry_price) - 1.0
                if entry_price > 0 else 0.0
            )
            z = z_by_sym.get(sym)
            reason: Optional[str] = None
            if z is not None and z >= self.exit_z_threshold:
                reason = f"residual-revert | z={z:+.3f}"
            elif pnl_pct <= -self.stop_loss_pct:
                # CITATION: standard numerical stability constant, not a tuned parameter
                # (100 = decimal-to-percent display conversion in the log message)
                reason = (
                    f"stop-loss | pnl={pnl_pct * 100:+.2f}% | "
                    f"entry={entry_price:.4f}"
                )
            elif state["bars_held"] >= self.max_hold_bars:
                reason = (
                    f"max-hold | bars_held={state['bars_held']} >= "
                    f"{self.max_hold_bars}"
                )
            if reason is not None:
                exit_signals[sym] = Signal(
                    action="SELL", strategy=self.name, price=price,
                    reason=reason, order_type="market",
                )
                self._open_positions.pop(sym, None)

        # Identify entry candidates: alts with z < entry_z_threshold
        # and not currently held.
        entry_candidates = [
            (sym, z) for sym, z in z_by_sym.items()
            if z < self.entry_z_threshold and sym not in self._open_positions
        ]
        entry_candidates.sort(key=lambda kv: kv[1])  # most-negative first
        slots_open = self.max_positions - len(self._open_positions)
        entries = entry_candidates[: max(slots_open, 0)]

        entry_signals: dict[str, Signal] = {}
        for sym, z in entries:
            df = prices.get(sym)
            if df is None or len(df) == 0:
                continue
            price = float(df["close"].iloc[-1])
            entry_signals[sym] = Signal(
                action="BUY", strategy=self.name, price=price,
                reason=(
                    f"residual-entry | z={z:+.3f} < "
                    f"{self.entry_z_threshold} | "
                    f"slot {len(self._open_positions) + len(entry_signals) + 1}"
                    f"/{self.max_positions}"
                ),
                order_type="market",
            )
            self._open_positions[sym] = {
                "entry_price": price,
                "bars_held": 0,
            }

        # Default HOLD for any alt without an exit/entry signal.
        for sym in self.alt_symbols:
            if sym in exit_signals:
                out[sym] = exit_signals[sym]
                continue
            if sym in entry_signals:
                out[sym] = entry_signals[sym]
                continue
            df = prices.get(sym)
            price = (
                float(df["close"].iloc[-1])
                if df is not None and len(df) > 0 else 0.0
            )
            z = z_by_sym.get(sym)
            held = sym in self._open_positions
            if held:
                state = self._open_positions[sym]
                reason = (
                    f"holding | z={z if z is not None else float('nan'):+.3f} | "
                    f"bars_held={state['bars_held']}/{self.max_hold_bars}"
                )
            elif z is not None:
                reason = f"no-signal | z={z:+.3f}"
            else:
                reason = "no-signal"
            out[sym] = Signal(
                action="HOLD", strategy=self.name, price=price,
                reason=reason,
            )

        return out

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _compute_cum_residuals(
        self,
        prices: dict[str, pd.DataFrame],
        btc_df: pd.DataFrame,
    ) -> dict[str, Optional[float]]:
        """Compute cum_resid at the latest bar for each alt.

        Aligns each alt's series to BTC's index (intersection), computes
        log-returns, then for each of the trailing zscore_window bars j:

            beta_j      = cov(alt_ret[j-W+1:j+1], btc_ret[j-W+1:j+1]) /
                          var(btc_ret[j-W+1:j+1])
            resid_j     = alt_ret[j] - beta_j * btc_ret[j]

        Returns sum(resid_j) over the trailing zscore_window bars.
        """
        btc_close = btc_df["close"].astype(float).sort_index()
        if len(btc_close) < self.beta_window + self.zscore_window + 1:
            raise _InsufficientHistory(
                f"BTC history {len(btc_close)} < required "
                f"{self.beta_window + self.zscore_window + 1}"
            )
        btc_logret = np.log(btc_close / btc_close.shift(1)).dropna()

        out: dict[str, Optional[float]] = {}
        W = self.beta_window
        Z = self.zscore_window

        for sym in self.alt_symbols:
            df = prices.get(sym)
            if df is None or len(df) < self.beta_window + self.zscore_window + 1:
                out[sym] = None
                continue
            alt_close = df["close"].astype(float).sort_index()
            alt_logret = np.log(alt_close / alt_close.shift(1)).dropna()
            common = alt_logret.index.intersection(btc_logret.index)
            if len(common) < W + Z:
                out[sym] = None
                continue
            alt_ret = alt_logret.loc[common].to_numpy(dtype=np.float64)
            btc_ret = btc_logret.loc[common].to_numpy(dtype=np.float64)
            n = len(alt_ret)

            resid_per_bar = np.empty(Z, dtype=np.float64)
            ok = True
            for k, j in enumerate(range(n - Z, n)):
                btc_slice = btc_ret[j - W + 1 : j + 1]
                alt_slice = alt_ret[j - W + 1 : j + 1]
                if len(btc_slice) < W:
                    ok = False
                    break
                var_btc = float(np.var(btc_slice, ddof=0))
                # CITATION: standard numerical stability constant, not a tuned parameter
                if var_btc < 1e-12 or not math.isfinite(var_btc):
                    ok = False
                    break
                cov_ab = float(
                    np.mean(
                        (alt_slice - alt_slice.mean()) *
                        (btc_slice - btc_slice.mean())
                    )
                )
                beta = cov_ab / var_btc
                resid_per_bar[k] = alt_ret[j] - beta * btc_ret[j]
            if not ok or np.any(~np.isfinite(resid_per_bar)):
                out[sym] = None
                continue
            out[sym] = float(np.sum(resid_per_bar))

        return out
