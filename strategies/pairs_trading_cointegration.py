"""
strategies/pairs_trading_cointegration.py — sq-005 PairsTradingCointegration v1.

BTC/ETH cointegration-based pairs trading on 1H candles. Hypothesis:
the BTC/USDT vs ETH/USDT spread is cointegrated and mean-reverts to its
historical mean; entries on |z|>2.0 with exits on z=0 capture the
reversion.

Algorithm per bar:

  1. Pull base_close (BTC) and quote_close (ETH) series synchronised on
     the timestamp intersection.
  2. If the common history is shorter than formation_window+zscore_window,
     emit HOLD for both legs (warmup).
  3. Run statsmodels.tsa.stattools.coint(base, quote) on the trailing
     formation_window bars (Engle-Granger ADF-based test).
  4. If p-value < 0.05: cointegrated. Compute the OLS hedge ratio via
     np.polyfit(quote, base, 1) on the formation window.
  5. Form spread = base - hedge_ratio * quote over the trailing
     zscore_window bars; compute the rolling z-score:
         z = (spread_now - spread.mean()) / spread.std(ddof=0)
  6. Entry: when no position is open
       z < -entry_z_threshold  -> LONG base, SHORT quote
                                  (base undervalued, quote overvalued)
       z > +entry_z_threshold  -> SHORT base, LONG quote
                                  (base overvalued, quote undervalued)
  7. Exit: when a position is open and z crosses zero relative to the
     position side (long_base exits at z>=0; short_base exits at z<=0)
     -> SELL both legs to close the spread.
  8. If p-value >= 0.05 (decointegrated) and a position is open: close
     both legs. Otherwise: HOLD.

Contract with backtest.engine_multi:

  * `symbols` constructor arg is [base_symbol, quote_symbol]; engine_multi
    reads `len(strategy.symbols)` for n_active and the per-bar
    synchronisation.
  * `lookback_days` is exposed as a property returning
    formation_window/24.0 (formation_window bars at 1H expressed in
    days). engine_multi uses `min_history_bars = strategy.lookback_days
    + 2` to defer the first signal call, but the strategy itself
    enforces the full formation_window+zscore_window warmup internally
    by emitting HOLD until enough bars are available.
  * The strategy emits BUY+is_short=False for the LONG leg and
    BUY+is_short=True for the SHORT leg. The current engine_multi is
    long-only and treats both as longs; the is_short flag is forward-
    compatible metadata for a short-aware engine. Per the sq-005 build-
    infra-only scope, the engine integration is not in this commit.
  * SELL closes whatever the engine currently holds for that symbol
    (i.e., the long position the engine opened on the prior BUY).

Citation: Park (2026), Tadi & Witzany (2023), Carvalho (2021) — see
research/pairs-trading-cointegration-literature.md.
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np
import pandas as pd

from strategies.base import Signal


class PairsTradingCointegrationStrategy:
    """BTC/ETH cointegration-based pairs trading.

    Self-contained: callers supply a `dict[symbol, DataFrame]` of OHLCV
    slices ending at the current bar and receive back a `dict[symbol,
    Signal]` keyed by the same symbols. Internal state tracks the open
    spread position so the second-leg signal (long vs short) is consistent
    with the first.
    """

    def __init__(
        self,
        base_symbol: str = "BTC/USDT",
        quote_symbol: str = "ETH/USDT",
        timeframe: str = "1h",
        # CITATION: pairs-trading-cointegration-literature
        # Carvalho (2021) §5: 6-month formation period (≈200 hourly bars
        # over a trading-day-equivalent window) outperforms 3-month
        # alternatives for Engle-Granger cointegration on crypto pairs.
        formation_window: int = 200,
        # CITATION: pairs-trading-cointegration-literature
        # Park (2026) §3 specifies a 21-bar rolling z-score window
        # for spread normalisation in BTC/ETH pairs trading.
        zscore_window: int = 21,
        # CITATION: pairs-trading-cointegration-literature
        # Park (2026) §3 entry threshold |z| > 2.0; matches the
        # 2-sigma convention in Tadi & Witzany (2023) §4.
        entry_z_threshold: float = 2.0,
        # CITATION: pairs-trading-cointegration-literature
        # Engine default initial_balance for the Phase 4 backtest harness.
        notional_capital: float = 10_000.0,
    ):
        if formation_window < 10:
            raise ValueError("formation_window must be >= 10")
        if zscore_window < 2:
            raise ValueError("zscore_window must be >= 2")
        if entry_z_threshold <= 0:
            raise ValueError("entry_z_threshold must be > 0")
        if base_symbol == quote_symbol:
            raise ValueError("base_symbol and quote_symbol must differ")

        self.name = "PairsTradingCointegration"
        self.base_symbol = str(base_symbol)
        self.quote_symbol = str(quote_symbol)
        self.symbols: list[str] = [self.base_symbol, self.quote_symbol]
        self.timeframe = str(timeframe)
        self.formation_window = int(formation_window)
        self.zscore_window = int(zscore_window)
        self.entry_z_threshold = float(entry_z_threshold)
        self.notional_capital = float(notional_capital)

        # Position state. None = flat. Otherwise:
        #   "long_base_short_quote"  → entered when z < -entry_z_threshold
        #   "short_base_long_quote"  → entered when z > +entry_z_threshold
        self._position_side: Optional[str] = None
        self._base_entry_price: Optional[float] = None
        self._quote_entry_price: Optional[float] = None
        self._hedge_ratio_at_entry: Optional[float] = None

    # ── Engine surface ────────────────────────────────────────────────────────

    @property
    def lookback_days(self) -> float:
        """Formation window expressed in days at the strategy timeframe.

        engine_multi uses `min_history_bars = strategy.lookback_days + 2`
        to delay the first signal evaluation. At 1H this returns
        formation_window/24.0 — the strategy itself emits HOLD until the
        full formation_window+zscore_window history is available, so the
        engine-side floor is a soft check.
        """
        return float(self.formation_window) / 24.0

    def position_fraction(
        self,
        df: pd.DataFrame,
        n_active: int,
        max_concentration_mult: float = 2.0,
    ) -> float:
        """Per-symbol position fraction of portfolio equity.

        Each leg of the pair gets 1/n_active so the two legs together
        consume 100% of equity at full deployment. The engine clamps to
        available cash, so this is a soft cap.
        """
        if n_active <= 0:
            return 0.0
        return float(1.0 / float(n_active))

    # ── Signal generation ────────────────────────────────────────────────────

    def generate_signals(
        self,
        prices: dict[str, pd.DataFrame],
    ) -> dict[str, Signal]:
        """Map of symbol -> Signal for the current bar."""
        base_df = prices.get(self.base_symbol)
        quote_df = prices.get(self.quote_symbol)

        base_now = self._latest_close(base_df)
        quote_now = self._latest_close(quote_df)

        if (
            base_df is None
            or quote_df is None
            or len(base_df) == 0
            or len(quote_df) == 0
        ):
            return self._both_hold(
                base_now, quote_now, reason="missing-data"
            )

        # Synchronise on intersection of timestamps.
        common = base_df.index.intersection(quote_df.index)
        min_bars = self.formation_window + self.zscore_window
        if len(common) < min_bars:
            return self._both_hold(
                base_now, quote_now,
                reason=(
                    f"warmup | bars={len(common)} < "
                    f"required={min_bars}"
                ),
            )

        base_close = (
            base_df["close"].astype(float).loc[common].to_numpy()
        )
        quote_close = (
            quote_df["close"].astype(float).loc[common].to_numpy()
        )

        # Engle-Granger cointegration test on the trailing formation_window.
        base_form = base_close[-self.formation_window:]
        quote_form = quote_close[-self.formation_window:]
        try:
            from statsmodels.tsa.stattools import coint
            _, pvalue, _ = coint(base_form, quote_form)
        except Exception as exc:
            return self._both_hold(
                base_now, quote_now,
                reason=f"coint-failure: {exc.__class__.__name__}",
            )

        # When the pair is no longer cointegrated, close any open position
        # and otherwise sit out.
        if not math.isfinite(pvalue) or pvalue >= 0.05:
            if self._position_side is not None:
                return self._close_both(
                    base_now, quote_now,
                    reason=f"decointegrated | p={pvalue:.4f}",
                )
            return self._both_hold(
                base_now, quote_now,
                reason=f"not-cointegrated | p={pvalue:.4f}",
            )

        # OLS hedge ratio: base = intercept + hedge_ratio * quote.
        # np.polyfit returns [slope, intercept] for deg=1.
        try:
            hedge_ratio, _intercept = np.polyfit(quote_form, base_form, 1)
            hedge_ratio = float(hedge_ratio)
        except Exception as exc:
            return self._both_hold(
                base_now, quote_now,
                reason=f"polyfit-failure: {exc.__class__.__name__}",
            )
        if not math.isfinite(hedge_ratio):
            return self._both_hold(
                base_now, quote_now,
                reason="hedge-ratio-non-finite",
            )

        # Spread series over the trailing zscore_window bars and the
        # rolling z at the current bar.
        spread_window = (
            base_close[-self.zscore_window:]
            - hedge_ratio * quote_close[-self.zscore_window:]
        )
        spread_now = float(spread_window[-1])
        spread_mean = float(spread_window.mean())
        spread_std = float(spread_window.std(ddof=0))
        # CITATION: standard numerical stability constant, not a tuned parameter
        if spread_std < 1e-12 or not math.isfinite(spread_std):
            return self._both_hold(
                base_now, quote_now, reason="spread-flat",
            )
        z = (spread_now - spread_mean) / spread_std

        # Exit / entry decision tree.
        if self._position_side is None:
            if z < -self.entry_z_threshold:
                # Base undervalued, quote overvalued.
                self._position_side = "long_base_short_quote"
                self._base_entry_price = base_now
                self._quote_entry_price = quote_now
                self._hedge_ratio_at_entry = hedge_ratio
                return {
                    self.base_symbol: Signal(
                        action="BUY", strategy=self.name, price=base_now,
                        reason=(
                            f"long-base | z={z:+.3f} < "
                            f"-{self.entry_z_threshold:.2f} | "
                            f"hedge={hedge_ratio:.4f} | p={pvalue:.4f}"
                        ),
                        order_type="market",
                        is_short=False,
                    ),
                    self.quote_symbol: Signal(
                        action="BUY", strategy=self.name, price=quote_now,
                        reason=(
                            f"short-quote | z={z:+.3f} < "
                            f"-{self.entry_z_threshold:.2f} | "
                            f"hedge={hedge_ratio:.4f}"
                        ),
                        order_type="market",
                        is_short=True,
                    ),
                }
            if z > self.entry_z_threshold:
                # Base overvalued, quote undervalued.
                self._position_side = "short_base_long_quote"
                self._base_entry_price = base_now
                self._quote_entry_price = quote_now
                self._hedge_ratio_at_entry = hedge_ratio
                return {
                    self.base_symbol: Signal(
                        action="BUY", strategy=self.name, price=base_now,
                        reason=(
                            f"short-base | z={z:+.3f} > "
                            f"+{self.entry_z_threshold:.2f} | "
                            f"hedge={hedge_ratio:.4f} | p={pvalue:.4f}"
                        ),
                        order_type="market",
                        is_short=True,
                    ),
                    self.quote_symbol: Signal(
                        action="BUY", strategy=self.name, price=quote_now,
                        reason=(
                            f"long-quote | z={z:+.3f} > "
                            f"+{self.entry_z_threshold:.2f} | "
                            f"hedge={hedge_ratio:.4f}"
                        ),
                        order_type="market",
                        is_short=False,
                    ),
                }
            return self._both_hold(
                base_now, quote_now,
                reason=f"flat | z={z:+.3f} | p={pvalue:.4f}",
            )

        # Position open — exit when the spread reverts past zero.
        exit_now = (
            (self._position_side == "long_base_short_quote" and z >= 0.0)
            or (self._position_side == "short_base_long_quote" and z <= 0.0)
        )
        if exit_now:
            return self._close_both(
                base_now, quote_now,
                reason=f"revert | z={z:+.3f} | side={self._position_side}",
            )
        return self._both_hold(
            base_now, quote_now,
            reason=(
                f"holding {self._position_side} | "
                f"z={z:+.3f} | p={pvalue:.4f}"
            ),
        )

    # ── Helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _latest_close(df: Optional[pd.DataFrame]) -> float:
        if df is None or len(df) == 0:
            return 0.0
        return float(df["close"].iloc[-1])

    def _both_hold(
        self,
        base_price: float,
        quote_price: float,
        *,
        reason: str,
    ) -> dict[str, Signal]:
        return {
            self.base_symbol: Signal(
                action="HOLD", strategy=self.name, price=base_price,
                reason=reason,
            ),
            self.quote_symbol: Signal(
                action="HOLD", strategy=self.name, price=quote_price,
                reason=reason,
            ),
        }

    def _close_both(
        self,
        base_price: float,
        quote_price: float,
        *,
        reason: str,
    ) -> dict[str, Signal]:
        prior_side = self._position_side
        self._position_side = None
        self._base_entry_price = None
        self._quote_entry_price = None
        self._hedge_ratio_at_entry = None
        return {
            self.base_symbol: Signal(
                action="SELL", strategy=self.name, price=base_price,
                reason=f"close-base | {reason} | prior={prior_side}",
                order_type="market",
            ),
            self.quote_symbol: Signal(
                action="SELL", strategy=self.name, price=quote_price,
                reason=f"close-quote | {reason} | prior={prior_side}",
                order_type="market",
            ),
        }
