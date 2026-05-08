"""strategies/market_state_conditioned_momentum.py -- sq-031 strategy.

Market-state-conditioned time-series momentum on a 10-symbol crypto
basket at 1D. Conditions exposure to a per-asset TSMOM signal on the
broader market state, using BTC/USDT as the market-state benchmark.

Algorithm per bar t:

  1. Compute BTC market state by comparing two consecutive lookback
     windows of length L = market_state_lookback:
         prev_state_ret = log(close[t - L] / close[t - 2L])
         curr_state_ret = log(close[t]     / close[t - L])
     UP if return > 0, DOWN if return <= 0.
     trending = (prev_state == curr_state)  # both UP or both DOWN
     transition = (prev_state != curr_state) # UP/DOWN or DOWN/UP

  2. If trending UP (prev=UP and curr=UP), deploy long-only TSMOM on
     the alt basket: for each non-BTC symbol, compute the trailing
     tsmom_lookback log-return sum and go long if positive (long-only
     by construction; basket is spot in this codebase).

  3. If transition (UP/DOWN or DOWN/UP) OR trending DOWN, neutralize
     exposure -- close any open positions and skip new entries. This
     matches Cheema et al. (2017): TSMOM underperforms in market
     transitions; in crypto without a short-side harness the safest
     conditioning is "stay flat unless the market is trending up".

  4. BTC/USDT itself is the market-state benchmark and is never traded
     (always HOLD) -- the basket-level conditioning is the test, and
     trading BTC inside the same signal would conflate it.

Citations:
- Han, C.; Kang, B.; Ryu, J. (2024). "Time-Series and Cross-Sectional
  Momentum in the Cryptocurrency Market: A Comprehensive Analysis
  under Realistic Assumptions." SSRN. Time-series momentum in
  cryptocurrencies is strong; the effect concentrates among winners,
  so a long-only TSMOM tilt dominates a long-short construction.
- Cheema, M. A.; Nartea, G. V.; Man, Y. (2017). "Cross-Sectional and
  Time-Series Momentum Returns and Market States." MPRA Paper. TSMOM
  outperforms when the market continues in the same state (UP/UP or
  DN/DN) but underperforms in market transitions (UP/DN, DN/UP).
- Tzouvanas, P.; Kizys, R.; Tsend-Ayush, B. (2019). University of
  Southampton. Short-horizon momentum effects (7-day formation) are
  highly profitable in crypto and disappear at longer terms,
  motivating the choice of a 30-day per-asset TSMOM lookback rather
  than the equity-paper 12-month horizon.

Output is ASCII-only.
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np
import pandas as pd

from strategies.base import Signal


class MarketStateConditionedMomentumStrategy:
    """Long-only TSMOM on a crypto basket, gated by BTC market state.

    Multi-symbol duck-typed strategy (no BaseStrategy inheritance).
    Engine contract:
      - `symbols`: full basket list including the BTC benchmark leg.
      - `lookback_days`: warmup floor consumed by engine_multi via
        `min_history_bars = strategy.lookback_days + 2`.
      - `position_fraction(df, n_active)`: per-symbol equity fraction
        used by engine_multi when sizing a BUY at the close.
      - `generate_signals(prices)`: dict[symbol, Signal] for the
        current bar (one Signal per symbol).
    BTC always emits HOLD (benchmark leg, never traded).
    """

    def __init__(
        self,
        symbols: list[str],
        btc_symbol: str = "BTC/USDT",
        timeframe: str = "1d",
        # CITATION: market-state-conditioned-momentum-literature
        # Cheema et al. (2017) compare two consecutive lookback periods
        # to classify trending vs transitioning states. Their equity
        # paper uses 12 months on monthly data (12 observations);
        # Tzouvanas et al. (2019) and Han et al. (2024) document that
        # crypto momentum dominates at much shorter horizons. 60 daily
        # bars (~2 months) sits between the equity convention and the
        # crypto-short-horizon evidence and keeps the warmup
        # (2 * 60 + 1 = 121 bars) compatible with the dev window's
        # CPCV block-count budget.
        market_state_lookback: int = 60,
        # CITATION: market-state-conditioned-momentum-literature
        # Han et al. (2024) and Tzouvanas et al. (2019) report TSMOM
        # effects in crypto at 7- to 30-day horizons. 30 days sits at
        # the upper end of that band and matches the daily-1D TSMOM
        # convention used by VolatilityScaledTSMOMStrategy
        # (sq-021) on the same basket substrate.
        tsmom_lookback: int = 30,
        # CITATION: market-state-conditioned-momentum-literature
        # Han et al. (2024) document that the crypto TSMOM effect
        # concentrates among winners and that a long-only basket
        # dominates a long-short construction. Capping concurrent
        # positions at 5 of 9 alts (~half the basket) is the
        # equal-weight equivalent of holding the top 50% of TSMOM
        # winners on each trending-up bar; lower than the basket size
        # so cash is preserved when fewer alts have positive momentum.
        max_positions: int = 5,
        # CITATION: market-state-conditioned-momentum-literature
        # Engine default initial_balance for the Phase 4 backtest
        # harness; the simulator clamps amount_usdt to available
        # balance so this is the reference notional only.
        notional_capital: float = 10_000.0,
        name: str = "MarketStateConditionedMomentum",
    ):
        if not symbols:
            raise ValueError("symbols must be a non-empty list")
        if btc_symbol not in symbols:
            raise ValueError(
                f"btc_symbol={btc_symbol!r} must appear in "
                f"symbols={symbols!r}"
            )
        if market_state_lookback < 2:
            raise ValueError("market_state_lookback must be >= 2")
        if tsmom_lookback < 2:
            raise ValueError("tsmom_lookback must be >= 2")
        if max_positions < 1:
            raise ValueError("max_positions must be >= 1")

        self.name = name
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
        self.market_state_lookback = int(market_state_lookback)
        self.tsmom_lookback = int(tsmom_lookback)
        self.max_positions = int(max_positions)
        self.notional_capital = float(notional_capital)

        # engine_multi.min_history_bars = strategy.lookback_days + 2;
        # set lookback_days to the longest strict requirement: 2L bars
        # for the two BTC market-state windows. tsmom_lookback is
        # always smaller in practice but covered defensively.
        self.lookback_days = max(
            2 * self.market_state_lookback,
            self.tsmom_lookback,
        )

        # Per-instance position book. CPCV correctness relies on the
        # cpcv_multi runner deep-copying the strategy per block so this
        # state resets at every block boundary.
        self._open_positions: dict[str, dict] = {}

    # -- Engine sizing hook --------------------------------------------------

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
        """
        return float(1.0 / float(self.max_positions))

    # -- Signal generation ---------------------------------------------------

    def generate_signals(
        self,
        prices: dict[str, pd.DataFrame],
    ) -> dict[str, Signal]:
        """Map of symbol -> Signal for the current bar."""
        out: dict[str, Signal] = {}

        # BTC always HOLD -- benchmark leg, never traded.
        btc_df = prices.get(self.btc_symbol)
        btc_price = (
            float(btc_df["close"].iloc[-1])
            if btc_df is not None and len(btc_df) > 0 else 0.0
        )
        out[self.btc_symbol] = Signal(
            action="HOLD", strategy=self.name, price=btc_price,
            reason="btc-benchmark-leg",
        )

        # Insufficient BTC history for the two-period state classifier.
        min_btc_history = 2 * self.market_state_lookback + 1
        if btc_df is None or len(btc_df) < min_btc_history:
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

        # Compute the two-period BTC market state.
        btc_close = btc_df["close"].astype(float).sort_index()
        L = self.market_state_lookback
        c_now = float(btc_close.iloc[-1])
        c_mid = float(btc_close.iloc[-1 - L])
        c_far = float(btc_close.iloc[-1 - 2 * L])

        if c_now <= 0 or c_mid <= 0 or c_far <= 0:
            for sym in self.alt_symbols:
                df = prices.get(sym)
                price = (
                    float(df["close"].iloc[-1])
                    if df is not None and len(df) > 0 else 0.0
                )
                out[sym] = Signal(
                    action="HOLD", strategy=self.name, price=price,
                    reason="non-positive-btc-close",
                )
            return out

        prev_state_ret = math.log(c_mid / c_far)
        curr_state_ret = math.log(c_now / c_mid)
        prev_up = prev_state_ret > 0.0
        curr_up = curr_state_ret > 0.0

        trending_up = prev_up and curr_up
        # Per Cheema et al. (2017): TSMOM outperforms only in
        # continuation states. With long-only basket on spot the
        # safe-mode behaviour is "trade only on trending-up; flat
        # otherwise". DOWN/DOWN is also a continuation state but
        # would call for shorts, which the spot engine does not
        # support, so we treat it as flat.
        deploy_tsmom = trending_up

        if not deploy_tsmom:
            # Force-exit any open longs (transition or trending-down).
            reason_state = (
                "transition" if prev_up != curr_up
                else ("trending-down" if not curr_up else "trending-up")
            )
            return self._neutralize_all(prices, out, reason_state,
                                        prev_state_ret, curr_state_ret)

        # Trending UP -- compute TSMOM signal per alt.
        return self._tsmom_long_path(
            prices=prices,
            out=out,
            prev_state_ret=prev_state_ret,
            curr_state_ret=curr_state_ret,
        )

    # -- State helpers -------------------------------------------------------

    def _neutralize_all(
        self,
        prices: dict[str, pd.DataFrame],
        out: dict[str, Signal],
        reason_state: str,
        prev_state_ret: float,
        curr_state_ret: float,
    ) -> dict[str, Signal]:
        """Close any open longs; emit HOLD for everything else."""
        for sym in self.alt_symbols:
            df = prices.get(sym)
            price = (
                float(df["close"].iloc[-1])
                if df is not None and len(df) > 0 else 0.0
            )
            if sym in self._open_positions:
                out[sym] = Signal(
                    action="SELL", strategy=self.name, price=price,
                    reason=(
                        f"market-state-{reason_state} | "
                        f"prev={prev_state_ret:+.4f} | "
                        f"curr={curr_state_ret:+.4f} | neutralize"
                    ),
                    order_type="market",
                )
                self._open_positions.pop(sym, None)
            else:
                out[sym] = Signal(
                    action="HOLD", strategy=self.name, price=price,
                    reason=(
                        f"market-state-{reason_state} | flat | "
                        f"prev={prev_state_ret:+.4f} | "
                        f"curr={curr_state_ret:+.4f}"
                    ),
                )
        return out

    def _tsmom_long_path(
        self,
        prices: dict[str, pd.DataFrame],
        out: dict[str, Signal],
        prev_state_ret: float,
        curr_state_ret: float,
    ) -> dict[str, Signal]:
        """Compute alt TSMOM and emit BUY/SELL/HOLD per alt."""
        # Per-alt trailing log-return sum over tsmom_lookback bars
        # ending at bar t. Insufficient history -> HOLD.
        tsmom_by_sym: dict[str, Optional[float]] = {}
        for sym in self.alt_symbols:
            df = prices.get(sym)
            if df is None or len(df) < self.tsmom_lookback + 1:
                tsmom_by_sym[sym] = None
                continue
            close = df["close"].astype(float).sort_index()
            log_ret = np.log(close / close.shift(1)).dropna()
            window = log_ret.iloc[-self.tsmom_lookback:]
            if len(window) < self.tsmom_lookback:
                tsmom_by_sym[sym] = None
                continue
            val = float(window.sum())
            tsmom_by_sym[sym] = val if math.isfinite(val) else None

        # Process exits first (mirrors engine_multi closes-before-opens).
        for sym in list(self._open_positions.keys()):
            df = prices.get(sym)
            price = (
                float(df["close"].iloc[-1])
                if df is not None and len(df) > 0 else 0.0
            )
            mom = tsmom_by_sym.get(sym)
            # Exit when momentum turns non-positive even though the
            # market state still says trending-up: this is the
            # per-asset signal flipping flat.
            if mom is None or mom <= 0.0:
                out[sym] = Signal(
                    action="SELL", strategy=self.name, price=price,
                    reason=(
                        f"tsmom-flip-flat | mom="
                        f"{(mom if mom is not None else float('nan')):+.4f}"
                    ),
                    order_type="market",
                )
                self._open_positions.pop(sym, None)

        # Identify entry candidates: alts with positive TSMOM not
        # currently held; rank by momentum strength descending so the
        # max_positions cap selects the strongest signals.
        entry_candidates = [
            (sym, mom) for sym, mom in tsmom_by_sym.items()
            if mom is not None and mom > 0.0
            and sym not in self._open_positions
        ]
        entry_candidates.sort(key=lambda kv: kv[1], reverse=True)
        slots_open = self.max_positions - len(self._open_positions)
        entries = entry_candidates[: max(slots_open, 0)]

        entry_signals: dict[str, Signal] = {}
        for sym, mom in entries:
            df = prices.get(sym)
            if df is None or len(df) == 0:
                continue
            price = float(df["close"].iloc[-1])
            entry_signals[sym] = Signal(
                action="BUY", strategy=self.name, price=price,
                reason=(
                    f"market-state-trending-up | "
                    f"prev={prev_state_ret:+.4f} | "
                    f"curr={curr_state_ret:+.4f} | "
                    f"tsmom={mom:+.4f} | "
                    f"slot {len(self._open_positions) + len(entry_signals) + 1}"
                    f"/{self.max_positions}"
                ),
                order_type="market",
            )
            self._open_positions[sym] = {
                "entry_price": price,
            }

        # Default HOLD for any alt without an exit/entry signal yet.
        for sym in self.alt_symbols:
            if sym in out:
                continue
            if sym in entry_signals:
                out[sym] = entry_signals[sym]
                continue
            df = prices.get(sym)
            price = (
                float(df["close"].iloc[-1])
                if df is not None and len(df) > 0 else 0.0
            )
            mom = tsmom_by_sym.get(sym)
            held = sym in self._open_positions
            if held:
                reason = (
                    f"holding | tsmom="
                    f"{(mom if mom is not None else float('nan')):+.4f}"
                )
            elif mom is not None:
                reason = f"no-entry | tsmom={mom:+.4f}"
            else:
                reason = "no-signal | tsmom-warmup"
            out[sym] = Signal(
                action="HOLD", strategy=self.name, price=price,
                reason=reason,
            )

        return out
