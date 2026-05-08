"""strategies/cross_sectional_residual_reversal.py -- Phase 4 sq-028.

Cross-sectional residual reversal on a 9-alt crypto basket at 1D, with
BTC/USDT as the market-factor regressor (BTC is in `symbols` but never
traded). Hypothesis: cryptocurrencies with the most-negative recent
RESIDUAL returns (returns orthogonal to the BTC market factor)
outperform on the subsequent day; long the bottom quintile of
residual-ranked alts captures positive risk-adjusted returns by
isolating idiosyncratic, liquidity-driven price dislocations from the
common BTC-driven market move (Blitz et al. 2013; Brogaard et al. 2024).

Algorithm per bar (daily):

  1. For BTC and each alt in `symbols`, compute log-returns over the
     full available history.
  2. For each alt at bar t, fit a rolling beta_window-bar OLS beta vs
     BTC log-returns:
         beta[t] = cov(alt_ret[t-W+1:t+1], btc_ret[t-W+1:t+1]) /
                   var(btc_ret[t-W+1:t+1])
  3. residual_ret[t] = alt_ret[t] - beta[t] * btc_ret[t]
  4. Rank alts by residual_ret ASCENDING (most-negative first); pick
     bottom-N as the target loser portfolio (N = top_n).
  5. BUY for symbols entering the loser portfolio with no position.
  6. SELL for symbols leaving the loser portfolio (rotation).
  7. Otherwise HOLD.  BTC always HOLDs (reference leg, never traded).

Long-only adaptation rationale (mirrors sq-013 / sq-026 precedent):

  The original Blitz et al. (2013) and Brogaard et al. (2024)
  specification calls for a long-short residual-reversal portfolio
  (long bottom quintile, short top quintile). This trial tests only
  the LONG leg because (a) `backtest.engine_multi` is structurally
  long-only (no short execution path); (b) Han et al. (2024) document
  that crypto loser-shorts get punished by rebound moves -- the same
  precedent applied to sq-013 (CrossSectionalReversal) and sq-026
  (DailyCrossSectionalReversal); and (c) Zaremba (2021) reports that
  the cross-sectional reversal premium concentrates on the long-loser
  leg, which residual orthogonalisation is expected to amplify per
  Blitz et al. (2013).

Contract with backtest.engine_multi:

  * `symbols` constructor arg is the FULL list including BTC; the
    engine reads `len(strategy.symbols)` for n_active.
  * BTC/USDT is the reference leg only -- `generate_signals` always
    emits HOLD for it; `position_fraction` returns the alt sizing,
    which the engine will not invoke for BTC because BTC never
    receives a BUY.
  * `lookback_days` exposes the warmup floor used by engine_multi's
    `min_history_bars = strategy.lookback_days + 2` check, set to
    `beta_window + 1` so the engine waits until the rolling beta
    window plus one defined return is available before signal
    generation.

Citations:
- Zaremba, A.; Bilgin, M. H.; Long, H.; Mercik, A.; Szczygielski, J. J.
  (2021). "Up or down? Short-term reversal, momentum, and liquidity
  effects in cryptocurrency markets." International Review of
  Financial Analysis. Crypto with low last-day returns outperforms
  high last-day returns; effect attributed to illiquidity.
- Blitz, D.; Huij, J.; Lansdorp, S.; Verbeek, M. (2013). "Short-term
  residual reversal." Journal of Financial Markets. A reversal
  strategy on residual returns (orthogonal to market factors) earns
  risk-adjusted returns roughly twice as large as a conventional
  reversal strategy.
- Brogaard, J.; Han, J.; Kim, H. (2024). "Intraday Residual Reversal
  in the U.S. Stock Market." SSRN. Buying stocks with negative
  residual returns and selling those with positive residual returns
  captures the returns to liquidity provision on transitory price
  movements.
- Han, C.; Kang, B.; Ryu, J. (2024). "Time-Series and Cross-Sectional
  Momentum in the Cryptocurrency Market: A Comprehensive Analysis
  under Realistic Assumptions." SSRN. Loser-shorts in crypto are
  punished by rebound moves -- justifies long-only adaptation.
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np
import pandas as pd

from strategies.base import Signal


class _InsufficientHistory(Exception):
    """Raised when the BTC reference series is too short for a beta."""


class CrossSectionalResidualReversalStrategy:
    """Long-only bottom-N residual-reversal basket (BTC = regressor only).

    Variant of CrossSectionalReversalStrategy that ranks the cross-
    section on RESIDUAL returns (alt_ret minus rolling-beta * btc_ret)
    rather than raw 1-day returns. Engine-multi compatible
    (generate_signals + position_fraction).
    """

    def __init__(
        self,
        symbols: list[str],
        btc_symbol: str = "BTC/USDT",
        timeframe: str = "1d",
        # CITATION: cross-sectional-residual-reversal-literature
        # 30-day rolling OLS beta window for the BTC market factor.
        # Blitz et al. (2013) use 36-month estimation for monthly
        # equity residuals; the daily-crypto analogue scales to ~30
        # days, matching the lookback used by Fil & Kristoufek (2020)
        # for crypto residuals at higher frequency.
        beta_window: int = 30,
        # CITATION: cross-sectional-residual-reversal-literature
        # 1-day residual lookback for the cross-sectional ranking,
        # matching the sq-028 implementation note ("rebalance daily,
        # hold one day") and the Zaremba (2021) 1-day reversal horizon.
        residual_lookback: int = 1,
        # CITATION: cross-sectional-residual-reversal-literature
        # Bottom quintile of a 9-alt traded universe = 2 holdings,
        # matching the sq-013 / sq-026 sibling specifications and the
        # Zaremba (2021) bottom-tail concentration.
        top_n: int = 2,
        # CITATION: cross-sectional-residual-reversal-literature
        # Engine default initial_balance for the Phase 4 backtest harness.
        notional_capital: float = 10_000.0,
        name: str = "CrossSectionalResidualReversal",
    ):
        if not symbols or len(symbols) < 2:
            raise ValueError("symbols must contain at least 2 entries")
        if btc_symbol not in symbols:
            raise ValueError(
                f"btc_symbol={btc_symbol!r} must appear in "
                f"symbols={symbols!r}"
            )
        if beta_window < 2:
            raise ValueError("beta_window must be >= 2")
        if residual_lookback < 1:
            raise ValueError("residual_lookback must be >= 1")

        self.name = str(name)
        self.symbols: list[str] = list(symbols)
        self.btc_symbol = btc_symbol
        self.alt_symbols: list[str] = [
            s for s in self.symbols if s != btc_symbol
        ]
        if not self.alt_symbols:
            raise ValueError(
                "symbols must include at least one alt besides BTC"
            )
        if top_n < 1 or top_n > len(self.alt_symbols):
            raise ValueError(
                "top_n must satisfy 1 <= top_n <= len(alt_symbols)="
                f"{len(self.alt_symbols)}; got {top_n}"
            )

        self.timeframe = str(timeframe)
        self.beta_window = int(beta_window)
        self.residual_lookback = int(residual_lookback)
        self.top_n = int(top_n)
        self.notional_capital = float(notional_capital)

        # engine_multi.min_history_bars = strategy.lookback_days + 2.
        # Set to beta_window + residual_lookback so the engine waits
        # until both the rolling-beta window and the residual lookback
        # are full before invoking the strategy.
        self.lookback_days: int = self.beta_window + self.residual_lookback

        # Held set drives BUY / SELL / HOLD emission on rotation.
        self._held: set[str] = set()

    # -- Engine sizing hook ---------------------------------------------------

    def position_fraction(
        self,
        df: pd.DataFrame,
        n_active: int,
        max_concentration_mult: float = 2.0,
    ) -> float:
        """Equal weight across the top_n held alts -- 1 / top_n.

        engine_multi only invokes position_fraction on symbols that
        receive a BUY this bar; BTC never receives a BUY (HOLD always)
        so this fraction applies only to the alt loser legs.
        """
        if self.top_n <= 0:
            return 0.0
        return float(1.0 / float(self.top_n))

    # -- Signal generation ----------------------------------------------------

    def generate_signals(
        self,
        prices: dict[str, pd.DataFrame],
    ) -> dict[str, Signal]:
        """Map of symbol -> Signal for the current bar."""
        out: dict[str, Signal] = {}

        # BTC always HOLD -- reference leg, never traded.
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
        min_history = self.beta_window + self.residual_lookback + 1
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

        # Compute residual return at the latest bar for each alt.
        try:
            residual_at_t = self._compute_recent_residual(prices, btc_df)
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

        scored = {
            sym: r for sym, r in residual_at_t.items()
            if r is not None and math.isfinite(r)
        }

        prices_now: dict[str, float] = {}
        for sym in self.alt_symbols:
            df = prices.get(sym)
            prices_now[sym] = (
                float(df["close"].iloc[-1])
                if df is not None and len(df) > 0 else 0.0
            )

        # If we cannot rank yet, HOLD all alts.
        if len(scored) < self.top_n:
            for sym in self.alt_symbols:
                out[sym] = Signal(
                    action="HOLD", strategy=self.name,
                    price=prices_now.get(sym, 0.0),
                    reason=(
                        f"warmup | scored={len(scored)} < top_n={self.top_n}"
                    ),
                )
            return out

        # Rank ASCENDING (most-negative residual first); bottom-N losers.
        ranked = sorted(scored.items(), key=lambda kv: kv[1])
        target_set: set[str] = {sym for sym, _ in ranked[: self.top_n]}

        for sym in self.alt_symbols:
            price = prices_now.get(sym, 0.0)
            score = residual_at_t.get(sym)
            score_str = (
                f"resid={score * 100:+.4f}%"
                if score is not None and math.isfinite(score)
                else "resid=NA"
            )

            in_target = sym in target_set
            in_held = sym in self._held

            if in_target and not in_held:
                self._held.add(sym)
                out[sym] = Signal(
                    action="BUY", strategy=self.name, price=price,
                    reason=(
                        f"enter-residual-loser-bottom-{self.top_n} | "
                        f"{score_str}"
                    ),
                    order_type="market",
                )
            elif (not in_target) and in_held:
                self._held.discard(sym)
                out[sym] = Signal(
                    action="SELL", strategy=self.name, price=price,
                    reason=f"exit-rotation | {score_str}",
                    order_type="market",
                )
            else:
                if in_held:
                    reason = f"holding | {score_str}"
                else:
                    reason = f"flat | {score_str}"
                out[sym] = Signal(
                    action="HOLD", strategy=self.name, price=price,
                    reason=reason,
                )

        return out

    # -- Helpers --------------------------------------------------------------

    def _compute_recent_residual(
        self,
        prices: dict[str, pd.DataFrame],
        btc_df: pd.DataFrame,
    ) -> dict[str, Optional[float]]:
        """Compute the most-recent residual return per alt.

        For each alt at bar t (= last available bar):

            beta_t   = cov(alt_ret[t-W+1:t+1], btc_ret[t-W+1:t+1]) /
                       var(btc_ret[t-W+1:t+1])
            resid_t  = alt_ret[t] - beta_t * btc_ret[t]

        When residual_lookback > 1, returns the cumulative sum of the
        last `residual_lookback` residuals (each computed against its
        own backward-looking beta).
        """
        btc_close = btc_df["close"].astype(float).sort_index()
        if len(btc_close) < self.beta_window + self.residual_lookback + 1:
            raise _InsufficientHistory(
                f"BTC history {len(btc_close)} < required "
                f"{self.beta_window + self.residual_lookback + 1}"
            )
        btc_logret = np.log(btc_close / btc_close.shift(1)).dropna()

        out: dict[str, Optional[float]] = {}
        W = self.beta_window
        L = self.residual_lookback

        for sym in self.alt_symbols:
            df = prices.get(sym)
            if df is None or len(df) < W + L + 1:
                out[sym] = None
                continue
            alt_close = df["close"].astype(float).sort_index()
            alt_logret = np.log(alt_close / alt_close.shift(1)).dropna()
            common = alt_logret.index.intersection(btc_logret.index)
            if len(common) < W + L:
                out[sym] = None
                continue
            alt_ret = alt_logret.loc[common].to_numpy(dtype=np.float64)
            btc_ret = btc_logret.loc[common].to_numpy(dtype=np.float64)
            n = len(alt_ret)

            resid_per_bar = np.empty(L, dtype=np.float64)
            ok = True
            for k, j in enumerate(range(n - L, n)):
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
