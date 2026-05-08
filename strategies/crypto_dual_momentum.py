"""strategies/crypto_dual_momentum.py -- Phase 4 sq-032 strategy.

Crypto Dual Momentum (TSMOM filter + CS rotation) on a 10-symbol
crypto basket at 1D.  Hypothesis: rotating into the top-quintile
cross-sectional winners ONLY when the broad crypto market itself is
in a positive time-series momentum regime delivers positive
risk-adjusted returns versus BTC buy-and-hold.

Algorithm per bar (daily):

  1. Compute the TSMOM market filter on the reference symbol
     (BTC/USDT): is the latest close above the prior
     `tsmom_lookback` SMA?  This is the dual-momentum first leg --
     "is the market in an uptrend at all?"

  2. On a rebalance bar (every `holding_period` bars):
        a. If TSMOM filter is OFF  --> SELL all held; emit HOLD on
           the rest.  No new entries.
        b. If TSMOM filter is ON   --> rank symbols by prior
           `cs_lookback` return DESCENDING; hold the top-N (the
           winner quintile per Han et al. 2024) equal-weight.
                - BUY symbols entering the winner set.
                - SELL symbols leaving the winner set.
                - HOLD symbols whose membership did not change.

  3. Between rebalance bars: HOLD every symbol -- existing
     positions are preserved through the full `holding_period`.

Long-only; equal weight (1 / top_n) across held symbols.  No short
positions in losers per Han et al. (2024) -- crypto losers rebound
and inflict losses on shorts.

The TSMOM filter is the "absolute momentum" leg in the dual-
momentum framework (Antonacci 2014 / Faber 2007 in equities; Han
et al. 2024 / Borgards 2021 in crypto).  Without it, the cross-
sectional rotation would buy the top winners even mid-bear-market;
the absolute filter forces flat exposure when the broad market is
trending down.

Citations:
- Han, C.; Kang, B.; Ryu, J. (2024).  "Time-Series and
  Cross-Sectional Momentum in the Cryptocurrency Market: A
  Comprehensive Analysis under Realistic Assumptions." SSRN.
  Key finding: time-series momentum remains strong and profitable
  under realistic costs; long-only winners dominate (losers
  rebound, hurting shorts).
- Borgards, O. (2021).  "Dynamic time series momentum of
  cryptocurrencies." North American Journal of Economics and
  Finance.  Key finding: TSMOM significantly outperforms
  buy-and-hold for cryptocurrencies with higher risk-adjusted
  returns and lower downside risk.
- Huang, Z.; Sangiorgi, I.; Urquhart, A. (2024).  "Cryptocurrency
  Volume-Weighted Time Series Momentum." SSRN.  Key finding: a
  volume-weighted TSMOM winner-minus-loser portfolio achieves an
  annualised Sharpe ratio of 2.17.
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np
import pandas as pd

from strategies.base import Signal


class CryptoDualMomentumStrategy:
    """Long-only TSMOM-filtered cross-sectional momentum rotation."""

    def __init__(
        self,
        symbols: list[str],
        timeframe: str = "1d",
        market_filter_symbol: str = "BTC/USDT",
        # CITATION: crypto-dual-momentum-literature
        # Faber (2007) 10-month MA filter on monthly bars ~= 200
        # trading days at daily cadence.  The 200-day MA is the
        # widely-used crypto trend filter; Han et al. (2024) confirm
        # that TSMOM filters at multi-month lookbacks remain
        # profitable post-cost in crypto.
        tsmom_lookback: int = 200,
        # CITATION: crypto-dual-momentum-literature
        # Borgards (2021) and Han et al. (2024) document a 6-month
        # (180-day) formation window as a robust cross-sectional
        # ranking signal -- long enough to capture persistence,
        # short enough to avoid stale information.
        cs_lookback: int = 180,
        # CITATION: crypto-dual-momentum-literature
        # Han et al. (2024) document that the momentum premium is
        # concentrated in the top-quintile winner portfolio; top_n=2
        # of 10 = top quintile.  Long-only because losers rebound
        # and inflict losses on shorts.
        top_n: int = 2,
        # CITATION: crypto-dual-momentum-literature
        # Faber (2007) and the proposal-agent implementation note
        # specify monthly rebalancing (~30 calendar days) -- long
        # enough for the documented persistence to play out without
        # overtrading.
        holding_period: int = 30,
        # CITATION: crypto-dual-momentum-literature
        # Engine default initial_balance for the Phase 4 backtest harness.
        notional_capital: float = 10_000.0,
        name: str = "CryptoDualMomentum",
    ):
        if not symbols or len(symbols) < 2:
            raise ValueError("symbols must contain at least 2 entries")
        if market_filter_symbol not in symbols:
            raise ValueError(
                f"market_filter_symbol={market_filter_symbol} must be in "
                f"the symbols basket; got symbols={symbols}"
            )
        if tsmom_lookback < 1:
            raise ValueError("tsmom_lookback must be >= 1")
        if cs_lookback < 1:
            raise ValueError("cs_lookback must be >= 1")
        if top_n < 1 or top_n > len(symbols):
            raise ValueError(
                f"top_n must satisfy 1 <= top_n <= len(symbols)={len(symbols)}; "
                f"got {top_n}"
            )
        if holding_period < 1:
            raise ValueError("holding_period must be >= 1")

        self.name = str(name)
        self.symbols: list[str] = list(symbols)
        self.timeframe = str(timeframe)
        self.market_filter_symbol = str(market_filter_symbol)
        self.tsmom_lookback = int(tsmom_lookback)
        self.cs_lookback = int(cs_lookback)
        self.top_n = int(top_n)
        self.holding_period = int(holding_period)
        self.notional_capital = float(notional_capital)

        # engine_multi.min_history_bars = strategy.lookback_days + 2.
        # The TSMOM filter needs `tsmom_lookback` closes for the SMA;
        # the CS ranking needs `cs_lookback + 1` closes for one
        # well-defined return.  Take the max so the engine waits for
        # both signals to be defined before the first generate_signals
        # call.
        self.lookback_days: int = max(
            self.tsmom_lookback, self.cs_lookback + 1
        )

        # Held set drives BUY / SELL / HOLD emission on rotation bars.
        self._held: set[str] = set()
        # Rebalance scheduler.  `_first_signal` ensures the first call
        # after engine warmup is treated as a rebalance bar (counter
        # starts at 0 then).
        self._bars_since_rebalance: int = 0
        self._first_signal: bool = True

    # -- Engine sizing hook ---------------------------------------------------

    def position_fraction(
        self,
        df: pd.DataFrame,
        n_active: int,
        max_concentration_mult: float = 2.0,
    ) -> float:
        """Equal weight across the top_n held symbols -- 1 / top_n.

        engine_multi only invokes position_fraction on symbols that
        receive a BUY this bar, so 1 / top_n correctly sizes each new
        leg as one-Nth of equity.
        """
        if self.top_n <= 0:
            return 0.0
        return float(1.0 / float(self.top_n))

    # -- Signal generation ----------------------------------------------------

    def generate_signals(
        self,
        prices: dict[str, pd.DataFrame],
    ) -> dict[str, Signal]:
        """Map of symbol -> Signal for the current bar.

        Rebalance logic:
          - First call after engine warmup: rebalance.
          - Subsequent calls: HOLD until `_bars_since_rebalance`
            reaches `holding_period`, then rebalance again and reset.
          - On every rebalance bar, the TSMOM filter gates entries:
            filter OFF -> SELL all held + HOLD rest; filter ON ->
            cross-sectional rotation into top_n winners.
        """
        prices_now: dict[str, float] = {
            sym: self._latest_close(prices.get(sym)) for sym in self.symbols
        }

        # Decide whether this bar is a rebalance bar.
        if self._first_signal:
            self._first_signal = False
            self._bars_since_rebalance = 0
            is_rebalance = True
        else:
            self._bars_since_rebalance += 1
            if self._bars_since_rebalance >= self.holding_period:
                self._bars_since_rebalance = 0
                is_rebalance = True
            else:
                is_rebalance = False

        if not is_rebalance:
            # Mid-holding bar: HOLD every symbol.  Engine treats HOLD
            # as no-op so existing positions are preserved through
            # the holding_period without further trades.
            out: dict[str, Signal] = {}
            for sym in self.symbols:
                in_held = sym in self._held
                reason = (
                    f"holding | bars_since_rebalance="
                    f"{self._bars_since_rebalance}/{self.holding_period}"
                ) if in_held else (
                    f"flat | bars_since_rebalance="
                    f"{self._bars_since_rebalance}/{self.holding_period}"
                )
                out[sym] = Signal(
                    action="HOLD", strategy=self.name,
                    price=prices_now.get(sym, 0.0),
                    reason=reason,
                )
            return out

        # -- Rebalance path ---------------------------------------------------

        # 1. TSMOM market filter on the reference symbol.
        tsmom_on, tsmom_detail = self._evaluate_tsmom_filter(
            prices.get(self.market_filter_symbol),
        )

        # 2. If filter is OFF: liquidate held; HOLD rest.  No entries.
        if not tsmom_on:
            out_off: dict[str, Signal] = {}
            for sym in self.symbols:
                price = prices_now.get(sym, 0.0)
                if sym in self._held:
                    self._held.discard(sym)
                    out_off[sym] = Signal(
                        action="SELL", strategy=self.name, price=price,
                        reason=(
                            f"tsmom-filter-off | exit-to-flat | {tsmom_detail}"
                        ),
                        order_type="market",
                    )
                else:
                    out_off[sym] = Signal(
                        action="HOLD", strategy=self.name, price=price,
                        reason=f"tsmom-filter-off | flat | {tsmom_detail}",
                    )
            return out_off

        # 3. TSMOM filter ON: compute cross-sectional ranking.
        prior_returns: dict[str, float] = {}
        for sym in self.symbols:
            df = prices.get(sym)
            if df is None or len(df) < self.cs_lookback + 1:
                continue
            close = df["close"].astype(float).to_numpy()
            window = close[-(self.cs_lookback + 1):]
            if np.any(window <= 0) or not np.all(np.isfinite(window)):
                continue
            prior_close = float(window[0])
            current_close = float(window[-1])
            if prior_close <= 0:
                continue
            ret = (current_close / prior_close) - 1.0
            if math.isfinite(ret):
                prior_returns[sym] = ret

        out_rb: dict[str, Signal] = {}

        # 3a. If we cannot rank yet, HOLD everything and exit.
        if len(prior_returns) < self.top_n:
            for sym in self.symbols:
                out_rb[sym] = Signal(
                    action="HOLD", strategy=self.name,
                    price=prices_now.get(sym, 0.0),
                    reason=(
                        f"warmup | scored={len(prior_returns)} < "
                        f"top_n={self.top_n} | {tsmom_detail}"
                    ),
                )
            return out_rb

        # 3b. Rank DESCENDING (best-performing first); pick top-N winners.
        ranked = sorted(
            prior_returns.items(), key=lambda kv: kv[1], reverse=True,
        )
        target_set: set[str] = {sym for sym, _ in ranked[: self.top_n]}

        # 3c. Emit signals.
        for sym in self.symbols:
            price = prices_now.get(sym, 0.0)
            score = prior_returns.get(sym)
            score_str = (
                f"prior_ret={score * 100:+.3f}%"
                if score is not None else "prior_ret=NA"
            )

            in_target = sym in target_set
            in_held = sym in self._held

            if in_target and not in_held:
                self._held.add(sym)
                out_rb[sym] = Signal(
                    action="BUY", strategy=self.name, price=price,
                    reason=(
                        f"tsmom-on | enter-winner-top-{self.top_n} | "
                        f"{score_str} | {tsmom_detail}"
                    ),
                    order_type="market",
                )
            elif (not in_target) and in_held:
                self._held.discard(sym)
                out_rb[sym] = Signal(
                    action="SELL", strategy=self.name, price=price,
                    reason=(
                        f"tsmom-on | exit-rotation | {score_str} | "
                        f"{tsmom_detail}"
                    ),
                    order_type="market",
                )
            else:
                if in_held:
                    reason = (
                        f"tsmom-on | holding-winner | {score_str} | "
                        f"{tsmom_detail}"
                    )
                else:
                    reason = (
                        f"tsmom-on | flat | {score_str} | {tsmom_detail}"
                    )
                out_rb[sym] = Signal(
                    action="HOLD", strategy=self.name, price=price,
                    reason=reason,
                )

        return out_rb

    # -- Helpers --------------------------------------------------------------

    def _evaluate_tsmom_filter(
        self,
        market_df: Optional[pd.DataFrame],
    ) -> tuple[bool, str]:
        """Return (filter_on, human-readable detail string).

        Filter is ON when the latest market close is above the prior
        `tsmom_lookback`-day SMA.  During warmup (insufficient
        history) the filter defaults to OFF -- a conservative
        no-entry stance until the trend signal is well-defined.
        """
        if market_df is None or len(market_df) < self.tsmom_lookback:
            return False, (
                f"tsmom-warmup | "
                f"have={(0 if market_df is None else len(market_df))} "
                f"need={self.tsmom_lookback}"
            )
        close = market_df["close"].astype(float).to_numpy()
        window = close[-self.tsmom_lookback:]
        if not np.all(np.isfinite(window)) or np.any(window <= 0):
            return False, "tsmom-bad-data"
        sma = float(window.mean())
        latest = float(close[-1])
        if not math.isfinite(sma) or sma <= 0 or not math.isfinite(latest):
            return False, "tsmom-bad-sma"
        is_on = latest > sma
        detail = (
            f"market={self.market_filter_symbol} "
            f"close={latest:.2f} sma{self.tsmom_lookback}={sma:.2f} "
            f"({'ABOVE' if is_on else 'BELOW'})"
        )
        return is_on, detail

    @staticmethod
    def _latest_close(df: Optional[pd.DataFrame]) -> float:
        if df is None or len(df) == 0:
            return 0.0
        return float(df["close"].iloc[-1])
