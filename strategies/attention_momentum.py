"""strategies/attention_momentum.py -- sq-018 strategy.

Cross-sectional Google Trends search-volume momentum strategy on a
5-symbol crypto basket at 1D.  Hypothesis: cryptocurrencies whose
recent (1-week) average Google search volume has accelerated above
their longer-term (4-week) average attract incoming flow and
out-perform peers over the subsequent 7-day holding window.  A
long-only equal-weight basket of the top-N attention-momentum
assets, rebalanced weekly, captures positive risk-adjusted returns
versus BTC buy-and-hold.

Algorithm per bar (daily):

  1. On each rebalance bar (every ``holding_period`` bars), for every
     symbol look up the latest search_volume value at df.index[-1]
     from a pre-fetched, OHLCV-aligned per-symbol Series passed at
     construction time, and compute the search-volume momentum score:

         sv_short = mean(SV over last ``short_window`` daily bars)
         sv_long  = mean(SV over last ``long_window`` daily bars)
         sv_mom   = (sv_short / sv_long) - 1.0

  2. Rank symbols by sv_mom DESCENDING (highest attention acceleration
     first); pick the top-N (default 1 of 5 = top quintile of the
     traded universe).
  3. BUY for symbols entering the winner portfolio with no position.
  4. SELL for symbols leaving the winner portfolio (rotation).
  5. Otherwise HOLD.

Between rebalance bars every symbol receives HOLD so existing
positions are kept for the full holding_period.

Long-only; equal weight across the held basket.  No short positions
are taken in losers (Han et al. 2024 -- crypto losers tend to
rebound and inflict significant losses on shorts), aligning with
the same precedent applied to CrossSectionalMomentum (sq-020) and
CrossSectionalSkewness (sq-016).

Citations:
- Lin, I-H.; Chiu, Y-C. (2022).  "The role of investor attention in
  the cryptocurrency markets." North American Journal of Economics
  and Finance.  Long-short Google-Trends attention index yields
  ~2.11% average monthly return.
- Bampinas, M.; Gkillas, P.K.; Loizos, C.K.; Main, A.C.N. (2022).
  "Forecasting cryptocurrency returns with Google Trends."
  Forecasting.  BTC return forecasting model augmented with Google
  Trends generates an annualised Sharpe of 1.25.
- You, W.; Yang, J. (2020).  "Investor Attention and Cryptocurrency
  Performance." SSRN.  Weekly-change Google SVI strategy generates
  an annualised Sharpe of 1.12.
- Han, C.; Kang, B.; Ryu, J. (2024). "Time-Series and
  Cross-Sectional Momentum in the Cryptocurrency Market: A
  Comprehensive Analysis under Realistic Assumptions." SSRN.
  Long-only winner baskets dominate long-short on a risk-adjusted
  basis -- justifies dropping the short leg of the original
  long-short specification.
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np
import pandas as pd

from strategies.base import Signal


class AttentionMomentumStrategy:
    """Long-only top-N cross-sectional Google-Trends attention momentum."""

    def __init__(
        self,
        symbols: list[str],
        sv_data: dict[str, pd.Series],
        timeframe: str = "1d",
        # CITATION: attention-momentum-literature
        # You & Yang (2020) and Lin & Chiu (2022) build the attention
        # signal from short-window vs long-window search-volume
        # averages; 1-week (7d) short / 4-week (28d) long is the
        # canonical configuration for daily Trends data resampled
        # from weekly via ffill.
        short_window: int = 7,
        long_window: int = 28,
        # CITATION: attention-momentum-literature
        # Top quintile of the 5-symbol traded universe = top 1.
        # Han et al. (2024) document that the momentum premium in
        # crypto is concentrated at the highest decile/quintile
        # tail; smaller traded universes implement quintile via
        # explicit top-N rotation.
        top_n: int = 1,
        # CITATION: attention-momentum-literature
        # Lin & Chiu (2022) and the user-provided implementation note
        # specify weekly rebalance.  Holding for the full window
        # avoids overtrading and lets the documented attention
        # persistence play out before the next ranking.
        holding_period: int = 7,
        # CITATION: attention-momentum-literature
        # Engine default initial_balance for the Phase 4 backtest harness.
        notional_capital: float = 10_000.0,
        name: str = "AttentionMomentum",
    ):
        if not symbols or len(symbols) < 2:
            raise ValueError("symbols must contain at least 2 entries")
        if short_window < 1:
            raise ValueError("short_window must be >= 1")
        if long_window <= short_window:
            raise ValueError(
                f"long_window ({long_window}) must be > "
                f"short_window ({short_window})"
            )
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
        self.short_window = int(short_window)
        self.long_window = int(long_window)
        self.top_n = int(top_n)
        self.holding_period = int(holding_period)
        self.notional_capital = float(notional_capital)

        # Per-symbol pre-fetched search volume Series (already aligned
        # to the OHLCV daily index via ffill at script setup time).
        self._sv_data: dict[str, pd.Series] = {
            sym: (sv_data.get(sym) if sv_data else None)
            for sym in self.symbols
        }

        # engine_multi.min_history_bars = strategy.lookback_days + 2.
        # Need long_window daily bars + 1 to form one well-defined
        # average ratio per symbol; pad via lookback_days so the
        # engine waits for at least one well-defined score per
        # symbol before the first signal.
        self.lookback_days: int = self.long_window + 1

        # Held set drives BUY / SELL / HOLD emission on rotation bars.
        self._held: set[str] = set()
        # Rebalance scheduler -- index of the most recent rebalance is
        # tracked via a counter incremented per generate_signals call.
        # `_first_signal` ensures the first call after engine warmup
        # is treated as a rebalance bar (counter starts at 0 then).
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
          - Subsequent calls: HOLD until ``_bars_since_rebalance``
            reaches ``holding_period``, then rebalance again and reset.
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

        # Rebalance path -------------------------------------------------------
        # 1. For every symbol resolve the current bar timestamp from
        #    its OHLCV slice and compute attention momentum.
        sv_mom_scores: dict[str, float] = {}
        for sym in self.symbols:
            df = prices.get(sym)
            if df is None or len(df) < self.long_window + 1:
                continue
            last_ts = df.index[-1]
            score = self._compute_sv_momentum(sym, last_ts)
            if score is not None and math.isfinite(score):
                sv_mom_scores[sym] = score

        out_rb: dict[str, Signal] = {}

        # 2. If we cannot rank yet, HOLD everything and exit.
        if len(sv_mom_scores) < self.top_n:
            for sym in self.symbols:
                out_rb[sym] = Signal(
                    action="HOLD", strategy=self.name,
                    price=prices_now.get(sym, 0.0),
                    reason=(
                        f"warmup | scored={len(sv_mom_scores)} < "
                        f"top_n={self.top_n}"
                    ),
                )
            return out_rb

        # 3. Rank DESCENDING (highest sv_mom first); pick top-N winners.
        ranked = sorted(
            sv_mom_scores.items(), key=lambda kv: kv[1], reverse=True,
        )
        target_set: set[str] = {sym for sym, _ in ranked[: self.top_n]}

        # 4. Emit signals.
        for sym in self.symbols:
            price = prices_now.get(sym, 0.0)
            score = sv_mom_scores.get(sym)
            score_str = (
                f"sv_mom={score * 100:+.3f}%"
                if score is not None else "sv_mom=NA"
            )

            in_target = sym in target_set
            in_held = sym in self._held

            if in_target and not in_held:
                self._held.add(sym)
                out_rb[sym] = Signal(
                    action="BUY", strategy=self.name, price=price,
                    reason=(
                        f"enter-attention-top-{self.top_n} | {score_str}"
                    ),
                    order_type="market",
                )
            elif (not in_target) and in_held:
                self._held.discard(sym)
                out_rb[sym] = Signal(
                    action="SELL", strategy=self.name, price=price,
                    reason=f"exit-rotation | {score_str}",
                    order_type="market",
                )
            else:
                if in_held:
                    reason = f"holding-attention-winner | {score_str}"
                else:
                    reason = f"flat | {score_str}"
                out_rb[sym] = Signal(
                    action="HOLD", strategy=self.name, price=price,
                    reason=reason,
                )

        return out_rb

    # -- Helpers --------------------------------------------------------------

    def _compute_sv_momentum(
        self,
        symbol: str,
        last_ts: pd.Timestamp,
    ) -> Optional[float]:
        """Compute (short_avg / long_avg) - 1 for the symbol at last_ts.

        Returns None when the search-volume series is missing,
        warmup is incomplete, the long-window mean is non-positive,
        or any value is non-finite.
        """
        sv = self._sv_data.get(symbol)
        if sv is None or len(sv) == 0:
            return None
        window = sv.loc[: last_ts]
        if len(window) < self.long_window:
            return None
        recent = window.iloc[-self.long_window:].astype(float)
        if recent.isna().any():
            return None
        short = recent.iloc[-self.short_window:]
        short_avg = float(short.mean())
        long_avg = float(recent.mean())
        if long_avg <= 0 or not math.isfinite(long_avg):
            return None
        if not math.isfinite(short_avg):
            return None
        return (short_avg / long_avg) - 1.0

    @staticmethod
    def _latest_close(df: Optional[pd.DataFrame]) -> float:
        if df is None or len(df) == 0:
            return 0.0
        return float(df["close"].iloc[-1])
