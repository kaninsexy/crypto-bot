"""strategies/altcoin_season_rotation.py -- AltcoinSeasonRotation strategy.

Long-only altcoin-season-vs-Bitcoin rotation strategy on a fixed
crypto universe at 1D.  Hypothesis: an 'altcoin season' is a period
in which the top quintile of liquid altcoins, ranked by 30-day prior
return, outperforms Bitcoin over the same window.  When the top
quintile's mean prior return exceeds BTC's prior return, allocate
equal-weight across the top quintile (the 'altcoin season' basket);
otherwise, hold BTC as the benchmark asset.  This is a relative
cross-sectional momentum signal used as a market-timing switch
between the alts basket and BTC.

Universe (closed, 11 spot symbols):

    BTC/USDT  -- benchmark / off-season holding.
    Altcoins (10): ETH, SOL, BNB, XRP, ADA, AVAX, DOT, LINK, LTC, UNI.

Algorithm per bar (daily):

  1. On each rebalance bar (every `holding_period` bars):
       a. For every altcoin compute the prior `lookback_period`
          return:
              ret[sym] = close[t] / close[t - lookback_period] - 1
       b. Compute BTC's prior `lookback_period` return analogously.
       c. Rank the altcoins DESCENDING and form the top quintile,
          size = max(1, round(n_alts * top_quintile_pct)).  Default
          top_quintile_pct = 0.20 -> 2 of 10 alts.
       d. If mean(top_quintile_returns) > BTC_return, the regime is
          ALT_SEASON: target = top quintile, equal-weight 1/N.
          Else regime is BTC_BENCHMARK: target = {BTC}, weight 1.0.
  2. Long-only: BUY every target symbol not already held; SELL every
     held symbol that is no longer in target (rotation closes losing-
     regime legs and opens winning-regime legs on the same bar).
  3. Between rebalance bars: HOLD all symbols so existing legs are
     kept for the full holding_period.

Long-only by design: Han, Kang & Ryu (2024) report cryptocurrency
momentum is concentrated in winners; loser portfolios rebound and
inflict losses on shorts.  The off-season fallback to BTC (rather
than cash) is justified by Drogen, Hoffstein & Otte (2023) framing
the long-only altcoin-momentum basket against a BTC buy-and-hold
benchmark -- BTC is the asset whose return must be beaten for the
basket allocation to be worthwhile.

Contract with backtest.engine_multi (mirrors
strategies.crypto_sector_rotation.CryptoSectorRotationStrategy):

  * `symbols`   -- full list of universe pairs, MUST include the
                   benchmark BTC/USDT.
  * `lookback_days` -- engine_multi.min_history_bars =
                       lookback_days + 2; we set this to
                       lookback_period + 1 so the engine waits until
                       at least one well-defined return per symbol
                       exists before invoking the strategy.
  * `position_fraction(df, n_active)` -- equal weight within the
                       current target basket; returns
                       1 / target_basket_size when a target is set,
                       0 during warmup or when no target is active.
  * `generate_signals(prices)` -- dict[symbol, Signal] for the bar.

Citations:
- Drogen, L.; Hoffstein, C.; Otte, K. (2023). "Cross-sectional
  Momentum in Cryptocurrency Markets." SSRN.  Long-only
  best-performing-asset momentum with a 30-day prior-return lookback
  and 7-day holding period delivered excess returns relative to a
  Bitcoin benchmark across 2018-2022.
- Han, C.; Kang, B.; Ryu, J. (2024). "Time-Series and
  Cross-Sectional Momentum in the Cryptocurrency Market: A
  Comprehensive Analysis under Realistic Assumptions." SSRN.
  Momentum is concentrated in past winners; long-only beats
  long-short after costs and liquidation risk.
- Kakushadze, Z.; Yu, W. (2019). "Statistical Arbitrage in
  Cryptocurrency Markets." Bulletin of Applied Economics / SSRN.
  A leading momentum factor in cryptoasset returns drives
  significant alpha in altcoin/BTC mean-reversion arbitrage; the
  same factor underpins the relative-momentum switch between the
  alt basket and BTC used here.
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np
import pandas as pd

from strategies.base import Signal


# CITATION: altcoin-season-rotation-literature
# Default benchmark / off-season holding is BTC/USDT.  Drogen et al.
# (2023) frame the long-only altcoin-momentum basket against BTC
# buy-and-hold; BTC is the asset whose return the alt basket must
# exceed to justify the rotation.
DEFAULT_BENCHMARK_SYMBOL: str = "BTC/USDT"


class AltcoinSeasonRotationStrategy:
    """Long-only altcoin-season vs BTC-benchmark rotation strategy.

    Self-contained: callers supply `dict[symbol, DataFrame]` of OHLCV
    slices ending at the current bar via generate_signals; the
    strategy returns `dict[symbol, Signal]`.  Engine_multi consumes
    `position_fraction` to size BUYs equal-weight within the current
    target basket (top quintile of alts in alt-season, BTC alone in
    benchmark mode).
    """

    def __init__(
        self,
        symbols: list[str],
        timeframe: str = "1d",
        # CITATION: altcoin-season-rotation-literature
        # Drogen et al. (2023) Sec 3 use a 30-day prior-return
        # lookback for the long-only winner-momentum basket on daily
        # crypto bars.  Han et al. (2024) confirm 30-day windows fall
        # within the formation horizon over which winner persistence
        # holds.
        lookback_period: int = 30,
        # CITATION: altcoin-season-rotation-literature
        # Drogen et al. (2023) hold the formed winner portfolio for
        # 7 days before re-ranking.  Holding the full window avoids
        # over-rotation and lets the documented persistence play out
        # before the next ranking.
        holding_period: int = 7,
        # CITATION: altcoin-season-rotation-literature
        # Drogen et al. (2023) report the strongest momentum premium
        # in the top quintile of single-asset returns -- exactly the
        # 'top quintile' wording used in the hypothesis-of-record.
        # 0.20 of a 10-alt universe = top 2 alts.
        top_quintile_pct: float = 0.20,
        benchmark_symbol: str = DEFAULT_BENCHMARK_SYMBOL,
        # CITATION: altcoin-season-rotation-literature
        # Engine default initial_balance for the Phase 4 backtest harness.
        notional_capital: float = 10_000.0,
        name: str = "AltcoinSeasonRotation",
    ):
        if not symbols or len(symbols) < 3:
            raise ValueError(
                "symbols must contain the benchmark plus at least 2 alts"
            )
        if lookback_period < 1:
            raise ValueError("lookback_period must be >= 1")
        if holding_period < 1:
            raise ValueError("holding_period must be >= 1")
        if not (0.0 < top_quintile_pct <= 1.0):
            raise ValueError(
                f"top_quintile_pct must be in (0, 1]; got {top_quintile_pct}"
            )
        if benchmark_symbol not in symbols:
            raise ValueError(
                f"benchmark_symbol={benchmark_symbol!r} must appear in "
                f"symbols={symbols!r}"
            )

        self.name = str(name)
        self.symbols: list[str] = list(symbols)
        self.timeframe = str(timeframe)
        self.lookback_period = int(lookback_period)
        self.holding_period = int(holding_period)
        self.top_quintile_pct = float(top_quintile_pct)
        self.benchmark_symbol = str(benchmark_symbol)
        self.notional_capital = float(notional_capital)

        # Altcoin universe = symbols minus benchmark.  At least 2 alts
        # required so there is always a non-trivial ranking.
        self.alt_symbols: list[str] = [
            s for s in self.symbols if s != self.benchmark_symbol
        ]
        if len(self.alt_symbols) < 2:
            raise ValueError(
                f"At least 2 altcoins required (symbols minus "
                f"benchmark={self.benchmark_symbol!r}); got "
                f"{self.alt_symbols!r}."
            )

        # Top-quintile size: ceil so 0.20 * 10 = 2, 0.20 * 5 = 1.
        n_alts = len(self.alt_symbols)
        self.top_quintile_size: int = max(
            1, int(round(n_alts * self.top_quintile_pct))
        )
        if self.top_quintile_size > n_alts:
            self.top_quintile_size = n_alts

        # engine_multi.min_history_bars = strategy.lookback_days + 2.
        # We need lookback_period + 1 closes per symbol to form one
        # well-defined return; pad via lookback_days so the engine
        # waits for at least one full return per symbol before the
        # first signal call.
        self.lookback_days: int = self.lookback_period + 1

        # Rebalance scheduler -- counter increments per generate_signals
        # call; first call after engine warmup is treated as a rebalance.
        self._held: set[str] = set()
        self._bars_since_rebalance: int = 0
        self._first_signal: bool = True

        # Tracks the current target basket size so position_fraction
        # can size each new long as 1 / target_basket_size.  None
        # during warmup / no-rebalance bars.
        self._current_target_size: Optional[int] = None
        # Optional regime label for diagnostics ('alt_season' /
        # 'btc_benchmark').  Not used for sizing directly.
        self._current_regime: Optional[str] = None

    # -- Engine sizing hook ---------------------------------------------------

    def position_fraction(
        self,
        df: pd.DataFrame,
        n_active: int,
        max_concentration_mult: float = 2.0,
    ) -> float:
        """Equal weight within the current target basket.

        Returns 1 / target_basket_size when a target is active.
        Engine_multi only invokes position_fraction on symbols
        receiving BUY this bar; the strategy only emits BUY for the
        target basket members, so 1/size correctly sizes each new
        leg as one-Nth of equity (full basket allocation when all
        legs fill).  Benchmark mode -> target_basket_size = 1 ->
        BTC sized at 100% of equity.
        """
        if self._current_target_size is None or self._current_target_size <= 0:
            return 0.0
        return float(1.0 / float(self._current_target_size))

    # -- Signal generation ----------------------------------------------------

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        """Single-symbol entry point.

        engine_multi is the canonical caller and uses
        generate_signals (plural).  This method is provided for
        BaseStrategy-interface compatibility checks; for a basket
        strategy a single-symbol view cannot rank altcoins, so it
        always returns HOLD with a not-applicable reason.
        """
        if df is None or len(df) == 0:
            price = 0.0
        else:
            price = float(df["close"].iloc[-1])
        return Signal(
            action="HOLD", strategy=self.name, price=price,
            reason=(
                "single-symbol view not applicable to altcoin-season "
                "rotation; use generate_signals via engine_multi"
            ),
        )

    def generate_signals(
        self,
        prices: dict[str, pd.DataFrame],
    ) -> dict[str, Signal]:
        """Map of symbol -> Signal for the current bar."""
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
            # Mid-holding bar: HOLD every symbol so engine preserves
            # existing legs through the holding period.
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
        # 1. Per-symbol prior return over the lookback window.
        prior_returns: dict[str, float] = {}
        for sym in self.symbols:
            df = prices.get(sym)
            if df is None or len(df) < self.lookback_period + 1:
                continue
            close = df["close"].astype(float).to_numpy()
            window = close[-(self.lookback_period + 1):]
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

        # 2. Need BTC's return AND at least top_quintile_size scored
        # alts to make a regime call.  Otherwise HOLD everything and
        # clear current_target_size.
        btc_ret = prior_returns.get(self.benchmark_symbol)
        scored_alts = {
            s: r for s, r in prior_returns.items() if s in self.alt_symbols
        }
        if btc_ret is None or len(scored_alts) < self.top_quintile_size:
            self._current_target_size = None
            self._current_regime = None
            for sym in self.symbols:
                out_rb[sym] = Signal(
                    action="HOLD", strategy=self.name,
                    price=prices_now.get(sym, 0.0),
                    reason=(
                        f"warmup | scored_alts={len(scored_alts)} < "
                        f"top_quintile_size={self.top_quintile_size} or "
                        f"benchmark return missing"
                    ),
                )
            return out_rb

        # 3. Rank alts DESCENDING and pick the top quintile.
        ranked_alts = sorted(
            scored_alts.items(), key=lambda kv: kv[1], reverse=True,
        )
        top_alts: list[str] = [
            sym for sym, _ in ranked_alts[: self.top_quintile_size]
        ]
        top_alt_mean = float(np.mean([scored_alts[s] for s in top_alts]))

        # 4. Regime decision: ALT_SEASON if top quintile beats BTC,
        # else BTC_BENCHMARK.
        if top_alt_mean > btc_ret:
            regime = "alt_season"
            target_symbols = list(top_alts)
        else:
            regime = "btc_benchmark"
            target_symbols = [self.benchmark_symbol]

        target_set: set[str] = set(target_symbols)
        self._current_target_size = (
            len(target_symbols) if target_symbols else None
        )
        self._current_regime = regime

        regime_label = (
            f"ALT_SEASON | top_alts={'+'.join(top_alts)} "
            f"top_mean={top_alt_mean*100:+.2f}% btc={btc_ret*100:+.2f}%"
            if regime == "alt_season" else
            f"BTC_BENCHMARK | top_mean={top_alt_mean*100:+.2f}% "
            f"btc={btc_ret*100:+.2f}%"
        )

        # 5. Emit signals.
        for sym in self.symbols:
            price = prices_now.get(sym, 0.0)
            sym_score = prior_returns.get(sym)
            score_str = (
                f"prior_ret={sym_score * 100:+.2f}%"
                if sym_score is not None else "prior_ret=NA"
            )

            in_target = sym in target_set
            in_held = sym in self._held

            if in_target and not in_held:
                self._held.add(sym)
                out_rb[sym] = Signal(
                    action="BUY", strategy=self.name, price=price,
                    reason=(
                        f"enter-target | regime={regime} | "
                        f"{regime_label} | {score_str}"
                    ),
                    order_type="market",
                )
            elif (not in_target) and in_held:
                self._held.discard(sym)
                out_rb[sym] = Signal(
                    action="SELL", strategy=self.name, price=price,
                    reason=(
                        f"exit-rotation | regime={regime} | "
                        f"{regime_label} | {score_str}"
                    ),
                    order_type="market",
                )
            else:
                if in_held:
                    reason = (
                        f"holding-target | regime={regime} | "
                        f"{regime_label} | {score_str}"
                    )
                else:
                    reason = (
                        f"flat-non-target | regime={regime} | "
                        f"{regime_label} | {score_str}"
                    )
                out_rb[sym] = Signal(
                    action="HOLD", strategy=self.name, price=price,
                    reason=reason,
                )

        # Annotate the metadata of one signal with the regime details
        # so log readers can reconstruct the rebalance decision.
        # Using the first symbol keeps the field structure identical
        # across symbols and avoids inflating every Signal's payload.
        first_sym = self.symbols[0]
        if first_sym in out_rb:
            out_rb[first_sym].metadata = {
                **(out_rb[first_sym].metadata or {}),
                "regime": regime,
                "top_alts": list(top_alts),
                "top_alt_mean_return": top_alt_mean,
                "btc_return": btc_ret,
                "target_size": self._current_target_size,
            }

        return out_rb

    # -- Helpers --------------------------------------------------------------

    @staticmethod
    def _latest_close(df: Optional[pd.DataFrame]) -> float:
        if df is None or len(df) == 0:
            return 0.0
        return float(df["close"].iloc[-1])
