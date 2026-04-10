"""
strategies/dual_momentum.py — Dual Momentum Rotation Strategy

WHAT IS DUAL MOMENTUM?
───────────────────────
Gary Antonacci's Dual Momentum (from his 2014 book of the same name) is one
of the most robust systematic strategies in academic literature.  It combines
two types of momentum to decide both WHAT to buy and WHETHER to buy at all:

  1. RELATIVE MOMENTUM (cross-sectional)
     Rank assets in the universe by their look-back return.  Buy the winner.
     "Which crypto is the strongest right now?"
     → Answer: the asset with the highest 21-candle return.

  2. ABSOLUTE MOMENTUM (time-series)
     Even if an asset is the "best" in the universe, it might still be losing.
     If the top-ranked asset has a NEGATIVE return, go to cash (HOLD) instead.
     "Is the best asset actually going up?"
     → If the winner's return is negative, everything is in downtrend → hold cash.

This two-layer filter dramatically reduces drawdowns vs pure momentum:
  - Relative momentum   → avoids buying weak assets (picks the strongest)
  - Absolute momentum   → avoids buying in bear markets (avoids losers)

IMPLEMENTATION DESIGN:
──────────────────────
  Universe: BTC/USDT, ETH/USDT, BNB/USDT (3 assets to start simple).

  Lookback: 21 candles of whatever timeframe the strategy runs on.
            On 1d candles this = 21 trading days ≈ 1 month of momentum.
            On 1h candles this = 21 hours (shorter-term, more reactive).

  Rebalance cycle: every `rebalance_every` candles (default 5).
                   Between rebalances the strategy holds its position.
                   This prevents whipsawing from candle-to-candle noise.

  Data flow:
    1. Portfolio manager calls `update_universe(dfs)` each candle.
       `dfs` is {symbol: ohlcv_df} for available universe assets.
    2. Every rebalance_every candles, 21-candle returns are recomputed.
    3. `generate_signal(df)` returns BUY / SELL (rotation) / HOLD (cash).

REGIME FILTER:
──────────────
  Not active in CRASH regime (everything collapsing → stay in cash entirely).
  In BEAR regime, the absolute momentum filter usually handles this anyway
  (negative returns trigger HOLD), but the CRASH regime is explicitly blocked.

HOW ROTATION WORKS:
───────────────────
  Current holding: ETH  →  New top: BTC
  → generate_signal() returns SELL first (close ETH)
  → Next candle: no position → returns BUY (open BTC)

  This is handled internally via `_rotation_pending`.

PARAMETERS:
───────────
  universe_symbols : List of symbol strings to rank (default BTC/ETH/BNB USDT).
  lookback         : Number of candles for return calculation (default 21).
  rebalance_every  : Re-rank universe every N candles (default 5).
  regime_filter    : Block all buys in CRASH regime (default True).
"""

import pandas as pd
from typing import Optional
from loguru import logger

from strategies.base import BaseStrategy, Signal
import config


class DualMomentumStrategy(BaseStrategy):
    """
    Cross-sectional + absolute momentum rotation across BTC, ETH, BNB.

    Ranks assets by 21-candle return (relative momentum).
    Only buys if the winner has a positive return (absolute momentum).
    Rebalances every 5 candles to reduce noise.
    """

    # Default universe — 3 assets keeps it simple and well-diversified
    DEFAULT_UNIVERSE = ["BTC/USDT", "ETH/USDT", "BNB/USDT"]

    def __init__(
        self,
        symbol: str = None,
        timeframe: str = None,
        universe_symbols: list = None,
        lookback: int = 21,
        rebalance_every: int = 5,
        regime_filter: bool = True,
    ):
        """
        Args:
            symbol:          Primary symbol (used for BaseStrategy init).
                             DualMomentum can actually trade any asset in the universe.
            timeframe:       Candle size passed to BaseStrategy.
            universe_symbols: List of symbol strings to rank each rebalance.
                             Default: ["BTC/USDT", "ETH/USDT", "BNB/USDT"].
            lookback:        Candles back for momentum return (default 21).
                             On 1h candles: 21h.  On 1d candles: 21 trading days.
            rebalance_every: Re-rank and possibly rotate every N candles (default 5).
                             Lower = more responsive, higher = fewer trades/fees.
            regime_filter:   If True, block all BUYs in CRASH regime (default True).
        """
        super().__init__(
            name="DualMomentum",
            symbol=symbol or "BTC/USDT",
            timeframe=timeframe or config.TIMEFRAME,
        )
        self.universe_symbols = universe_symbols or self.DEFAULT_UNIVERSE
        self.lookback         = lookback
        self.rebalance_every  = rebalance_every
        self.regime_filter    = regime_filter

        # Internal state
        self._current_regime:    str                        = "BULL"
        self._universe_returns:  dict[str, float]           = {}   # {symbol: 21c return}
        self._top_symbol:        Optional[str]              = None  # Highest-ranked asset
        self._top_return:        float                      = 0.0
        self._candle_count:      int                        = 0
        self._in_position:       bool                       = False
        self._current_holding:   Optional[str]              = None  # Symbol we hold
        self._rotation_pending:  bool                       = False # Need to sell before buying

        logger.info(
            f"DualMomentum | Universe: {self.universe_symbols} | "
            f"Lookback: {lookback} candles | Rebalance every {rebalance_every} | "
            f"Crash filter: {'ON' if regime_filter else 'OFF'}"
        )

    # ── External state updaters ───────────────────────────────────────────────

    def update_regime(self, regime: str) -> None:
        """
        Set current market regime.  CRASH blocks all new BUYs.

        Call before generate_signal() each candle from PortfolioManager.
        """
        self._current_regime = regime

    def update_universe(self, dfs: dict) -> None:
        """
        Provide OHLCV DataFrames for universe assets and recompute 21-candle returns.

        Called by PortfolioManager BEFORE generate_signal() each candle.
        Only re-ranks every `rebalance_every` candles to reduce noise.

        Args:
            dfs: {symbol_string: ohlcv_df} for available universe symbols.
                 Symbols not present in `dfs` are skipped for this cycle.
        """
        self._candle_count += 1

        # Only re-rank on rebalance cycles (or if we have no data yet)
        if (self._candle_count % self.rebalance_every != 0
                and self._universe_returns):
            return

        returns = {}
        for sym in self.universe_symbols:
            df = dfs.get(sym)
            if df is None or len(df) < self.lookback + 1:
                # Skip symbols without enough data; log once to diagnose setup issues
                logger.debug(
                    f"DualMomentum: No data for {sym} "
                    f"(need {self.lookback + 1} rows, got {len(df) if df is not None else 0})"
                )
                continue

            old_close = float(df["close"].iloc[-(self.lookback + 1)])
            new_close = float(df["close"].iloc[-1])
            if old_close > 0:
                returns[sym] = (new_close - old_close) / old_close
            else:
                returns[sym] = 0.0

        if not returns:
            return   # No universe data available yet

        self._universe_returns = returns

        # Rank by return (highest first) — this is the "relative momentum" step
        self._top_symbol = max(returns, key=returns.get)
        self._top_return = returns[self._top_symbol]

        # Log the full ranking for transparency
        ranking = sorted(returns.items(), key=lambda x: x[1], reverse=True)
        ranking_str = " | ".join(
            f"{sym}={ret*100:+.2f}%" for sym, ret in ranking
        )
        logger.debug(
            f"DualMomentum rebalance #{self._candle_count // self.rebalance_every} | "
            f"Rankings: {ranking_str} | Top: {self._top_symbol} ({self._top_return*100:+.2f}%)"
        )

    # ── Signal generation ─────────────────────────────────────────────────────

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        """
        Return BUY (open/rotate), SELL (close/rotate), or HOLD (cash).

        Logic:
          1. CRASH regime → exit any position, go to cash.
          2. No universe data yet → HOLD (warmup).
          3. Absolute momentum negative → HOLD (cash better than losers).
          4. If holding wrong asset (different from top-ranked) → SELL to rotate.
          5. If not holding and top asset has positive return → BUY.
          6. Already holding the top asset → HOLD (maintain).
        """
        current_price = float(df["close"].iloc[-1])

        # ── 1. CRASH regime: exit everything ─────────────────────────────────
        if self.regime_filter and self._current_regime == "CRASH":
            if self._in_position:
                logger.info(
                    f"DualMomentum: CRASH regime — closing {self._current_holding}"
                )
                self._in_position    = False
                self._rotation_pending = False
                holding              = self._current_holding
                self._current_holding = None
                return self.sell(
                    current_price,
                    reason=f"DualMomentum: CRASH regime — closing {holding}, going to cash",
                )
            return self.hold(
                current_price,
                reason="DualMomentum: CRASH regime — no new positions until regime improves",
            )

        # ── 2. No universe data (warmup) ──────────────────────────────────────
        if not self._universe_returns:
            return self.hold(
                current_price,
                reason=(
                    "DualMomentum: Waiting for universe data. "
                    "Call update_universe() before generate_signal()."
                ),
            )

        # ── 3. Absolute momentum filter ───────────────────────────────────────
        # If the BEST asset has a negative 21-candle return, the whole market
        # is in downtrend.  Hold cash instead of buying a losing asset.
        if self._top_return <= 0.0:
            if self._in_position:
                logger.info(
                    f"DualMomentum: Absolute momentum negative "
                    f"({self._top_symbol}={self._top_return*100:.2f}%) — closing position"
                )
                self._in_position    = False
                holding              = self._current_holding
                self._current_holding = None
                return self.sell(
                    current_price,
                    reason=(
                        f"DualMomentum: Absolute momentum negative "
                        f"(top={self._top_symbol} at {self._top_return*100:.2f}%) — "
                        f"closing {holding}, going to cash"
                    ),
                )
            return self.hold(
                current_price,
                reason=(
                    f"DualMomentum: Best asset ({self._top_symbol}) "
                    f"has {self._top_return*100:.2f}% return — "
                    f"absolute momentum negative → hold cash"
                ),
            )

        # ── 4. Rotation: holding wrong asset ──────────────────────────────────
        # A new winner has emerged.  Sell current holding first; buy next candle.
        if self._in_position and self._current_holding != self._top_symbol:
            logger.info(
                f"DualMomentum: Rotating {self._current_holding} → {self._top_symbol} | "
                f"Returns: {self._universe_returns}"
            )
            old_holding          = self._current_holding
            self._in_position    = False
            self._current_holding = None
            self._rotation_pending = True   # Signal BUY on the next candle
            return self.sell(
                current_price,
                reason=(
                    f"DualMomentum: Rotate out of {old_holding} → {self._top_symbol} | "
                    f"New top return: {self._top_return*100:+.2f}%"
                ),
            )

        # ── 5. Pending rotation: BUY the new top asset ───────────────────────
        if self._rotation_pending and not self._in_position:
            self._rotation_pending = False
            self._in_position      = True
            self._current_holding  = self._top_symbol
            return self.buy(
                current_price,
                reason=(
                    f"DualMomentum: Rotation BUY {self._top_symbol} | "
                    f"21c return={self._top_return*100:+.2f}% | "
                    f"All returns: "
                    + ", ".join(
                        f"{s}={r*100:+.2f}%"
                        for s, r in sorted(
                            self._universe_returns.items(),
                            key=lambda x: x[1], reverse=True
                        )
                    )
                ),
            )

        # ── 6. New position: not holding, conditions met → BUY ───────────────
        if not self._in_position:
            self._in_position     = True
            self._current_holding = self._top_symbol
            return self.buy(
                current_price,
                reason=(
                    f"DualMomentum: BUY {self._top_symbol} | "
                    f"Top 21c return={self._top_return*100:+.2f}% | "
                    f"Regime={self._current_regime} | "
                    "Absolute + relative momentum both positive"
                ),
            )

        # ── 7. Already holding the winner → HOLD ─────────────────────────────
        return self.hold(
            current_price,
            reason=(
                f"DualMomentum: Holding {self._current_holding} | "
                f"Return={self._universe_returns.get(self._current_holding, 0)*100:+.2f}% | "
                f"Still top-ranked | Next rebalance in "
                f"{self.rebalance_every - (self._candle_count % self.rebalance_every)} candles"
            ),
        )

    def sync_state(self, simulator_has_position: bool) -> None:
        """
        Sync internal flag with simulator after external SL/TP closes.
        Called by PortfolioManager after each tick.
        """
        if self._in_position and not simulator_has_position:
            logger.info(
                f"DualMomentum: Simulator closed {self._current_holding} externally "
                f"(SL/TP hit). Resetting to allow re-entry."
            )
            self._in_position     = False
            self._current_holding = None
