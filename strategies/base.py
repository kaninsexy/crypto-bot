"""
strategies/base.py — Abstract base class for all trading strategies.

Every strategy we build inherits from BaseStrategy and implements generate_signal().

Signal values:
  "BUY"  → Open a long position (buy the asset)
  "SELL" → Close position / go short (sell the asset)
  "HOLD" → Do nothing, wait for the next candle
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional
import pandas as pd
import ta
from loguru import logger


@dataclass
class Signal:
    """
    Output of every strategy's generate_signal() call.

    Carries direction, risk parameters, and execution hints so the
    simulator and live executor know exactly what to do.

    Attributes:
        action:         "BUY", "SELL", or "HOLD"
        strategy:       Name of the strategy that produced this signal.
        price:          Price at signal generation time.
        reason:         Human-readable explanation for logging & debugging.
        stop_loss:      Stop-loss price. None = use config default.
        take_profit:    Primary take-profit price. None = no fixed target.
        trailing_tp:    If True, use trailing take-profit instead of fixed.
        trail_pct:      Trail by this % from peak before closing (e.g. 0.02 = 2%).
        quantity_pct:   Fraction of position to close on SELL (1.0 = 100%).
                        Use <1.0 for tranche/partial exits (e.g. 0.33 = first third).
        order_type:     "limit" or "market". Limit preferred to save fees.
        limit_offset:   For limit orders: place this % inside spread (e.g. 0.0005 = 0.05%).
        is_short:       True if this is a futures short signal.
        leverage:       Suggested leverage for futures trades (1 = spot).
        compound_profit: True if realised profit from this trade should be
                        reinvested into the DCA bucket (set by portfolio manager).
        panic_protection: If True, require 2 consecutive closes below SL before
                          exiting — avoids being stopped out by a wick.
        max_hold_candles: Auto-exit after this many candles if no TP hit (0 = off).
        metadata:       Any extra strategy-specific data.
    """
    action: str                              # "BUY" | "SELL" | "HOLD"
    strategy: str                            # e.g. "DCA", "Supertrend"
    price: float                             # Price when signal was generated
    reason: str = ""

    # Risk parameters
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None

    # Trailing take-profit
    trailing_tp: bool = False
    trail_pct: float = 0.02                  # 2% trail by default

    # Trailing stop-loss (ratchets SL UP as price rises)
    trailing_sl: bool = False
    trail_sl_pct: float = 0.03               # 3% below peak price

    # Tranche / partial exit
    quantity_pct: float = 1.0               # 1.0 = close full position

    # Order execution
    order_type: str = "limit"               # "limit" preferred; fallback to "market"
    limit_offset: float = 0.0005            # 0.05% inside spread for limit orders

    # Futures
    is_short: bool = False
    leverage: int = 1

    # Safety features
    compound_profit: bool = False
    panic_protection: bool = False          # Require 2 closes below SL
    max_hold_candles: int = 0              # 0 = no time-based exit

    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.action not in ("BUY", "SELL", "HOLD"):
            raise ValueError(f"Signal action must be BUY, SELL, or HOLD. Got: '{self.action}'")
        if not (0.0 < self.quantity_pct <= 1.0):
            raise ValueError(f"quantity_pct must be between 0 and 1. Got: {self.quantity_pct}")

    def is_actionable(self) -> bool:
        """True if this signal requires placing an order."""
        return self.action in ("BUY", "SELL")

    def is_partial(self) -> bool:
        """True if this is a partial/tranche exit."""
        return self.action == "SELL" and self.quantity_pct < 1.0

    def __str__(self):
        parts = [
            f"[{self.strategy}] {self.action}",
            f"@ {self.price:.4f}",
        ]
        if self.quantity_pct < 1.0:
            parts.append(f"({self.quantity_pct*100:.0f}% of position)")
        if self.stop_loss:
            parts.append(f"SL={self.stop_loss:.4f}")
        if self.take_profit:
            parts.append(f"TP={self.take_profit:.4f}")
        if self.trailing_tp:
            parts.append(f"TrailTP={self.trail_pct*100:.1f}%")
        if self.trailing_sl:
            parts.append(f"TrailSL={self.trail_sl_pct*100:.1f}%")
        if self.leverage > 1:
            parts.append(f"LEV={self.leverage}x")
        if self.is_short:
            parts.append("SHORT")
        parts.append(f"| {self.reason}")
        return " | ".join(parts[:5]) + " " + parts[-1]


class BaseStrategy(ABC):
    """
    Abstract base class all strategies must inherit from.

    Subclass this, implement generate_signal(), and you're done.
    All helper methods (buy/sell/hold) and validation are provided here.
    """

    def __init__(self, name: str, symbol: str, timeframe: str):
        self.name = name
        self.symbol = symbol
        self.timeframe = timeframe
        # ── Cooldown guard ─────────────────────────────────────────────────────
        # Prevents immediately re-entering after a losing trade.
        # The portfolio manager calls start_cooldown() when it detects a SL exit.
        self._cooldown_remaining: int = 0
        logger.info(f"Strategy initialized: {name} | {symbol} | {timeframe}")

    @abstractmethod
    def generate_signal(self, df: pd.DataFrame) -> Signal:
        """
        Analyse the latest OHLCV data and return a trading signal.

        Args:
            df: DataFrame [open, high, low, close, volume] indexed by timestamp.
                Latest candle is df.iloc[-1].

        Returns:
            Signal with action BUY, SELL, or HOLD.
        """
        pass

    # ── Cooldown helpers ──────────────────────────────────────────────────────

    def start_cooldown(self, candles: int) -> None:
        """
        Block new BUY entries for `candles` candles after a losing trade.

        Call this from the portfolio manager when a position closes at a loss
        (i.e., SL was hit).  Each subsequent generate_signal() call will
        decrement the counter automatically via _tick_cooldown().

        Reasoning: after a stop-loss, the market is clearly moving against
        the strategy's signal.  Waiting a few candles before re-entering
        avoids immediately opening the same losing trade again.
        """
        self._cooldown_remaining = max(0, candles)
        if candles > 0:
            logger.info(
                f"[{self.name}] Cooldown: {candles} candles before re-entry allowed"
            )

    def in_cooldown(self) -> bool:
        """Return True if the strategy is currently in its post-loss cooldown."""
        return self._cooldown_remaining > 0

    def _tick_cooldown(self) -> None:
        """
        Decrement the cooldown counter by 1.
        Call this at the top of every generate_signal() implementation.
        """
        if self._cooldown_remaining > 0:
            self._cooldown_remaining -= 1
            if self._cooldown_remaining == 0:
                logger.info(f"[{self.name}] Cooldown expired — re-entry allowed")

    # ── Volume & volatility helpers ───────────────────────────────────────────

    def volume_is_sufficient(
        self,
        df: pd.DataFrame,
        lookback: int = 20,
        min_ratio: float = 0.8,
    ) -> bool:
        """
        Return True if the latest candle's volume ≥ min_ratio × N-period average.

        Low-volume signals are much more likely to be false: price can be
        pushed around easily when participation is thin.  Filtering them out
        reduces noise across all strategies at essentially zero cost.

        Returns True (permissive) when there isn't enough history to judge.

        Args:
            df:        OHLCV DataFrame with a "volume" column.
            lookback:  Period for average volume (default 20 candles).
            min_ratio: Accept signal if volume ≥ this fraction of average.
                       0.8 = 80% of average → only reject the very thinnest bars.
        """
        if len(df) < lookback + 1:
            return True
        avg_vol = float(df["volume"].iloc[-(lookback + 1):-1].mean())
        curr_vol = float(df["volume"].iloc[-1])
        if avg_vol <= 0:
            return True
        ok = curr_vol >= avg_vol * min_ratio
        if not ok:
            logger.debug(
                f"[{self.name}] Volume filter: {curr_vol:.0f} < "
                f"{avg_vol * min_ratio:.0f} ({min_ratio*100:.0f}% of avg {avg_vol:.0f})"
            )
        return ok

    def adaptive_trail_pct(
        self,
        df: pd.DataFrame,
        atr_period: int = 14,
        atr_mult: float = 1.5,
        floor: float = 0.01,
        ceiling: float = 0.08,
    ) -> float:
        """
        ATR-based trailing percentage that adapts to current market volatility.

        In a volatile market ATR is large → wider trail → avoids noise stop-outs.
        In a calm market ATR is small → tighter trail → locks in more profit.

        All strategies should use this instead of a hardcoded trail_pct when
        they want their trailing stop/TP to survive across different market regimes
        without manual re-tuning.

        Args:
            df:         OHLCV DataFrame (needs at least atr_period + 5 rows).
            atr_period: ATR lookback window (default 14).
            atr_mult:   ATR multiplier → controls how wide the trail is.
                        1.5 means "trail 1.5× one ATR below peak".
            floor:      Minimum trail % (default 1%). Prevents trivially tight stops.
            ceiling:    Maximum trail % (default 8%). Caps the trail in wild markets.

        Returns:
            Float in [floor, ceiling].  Falls back to 0.03 (3%) if ATR unavailable.
        """
        if len(df) < atr_period + 5:
            return 0.03
        try:
            atr_val = ta.volatility.AverageTrueRange(
                df["high"], df["low"], df["close"], window=atr_period
            ).average_true_range().iloc[-1]
            price = float(df["close"].iloc[-1])
            if price > 0 and atr_val > 0:
                trail = (float(atr_val) * atr_mult) / price
                return float(max(floor, min(ceiling, trail)))
        except Exception:
            pass
        return 0.03

    def validate_dataframe(self, df: pd.DataFrame, min_rows: int = 50) -> None:
        """Sanity-check DataFrame before strategy logic runs."""
        required = {"open", "high", "low", "close", "volume"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"DataFrame missing columns: {missing}")
        if len(df) < min_rows:
            raise ValueError(
                f"Not enough candles: need {min_rows}, got {len(df)}. "
                f"Increase CANDLE_LIMIT in .env"
            )
        if df["close"].isnull().any():
            raise ValueError("NaN values found in 'close' column.")

    # ── Signal shortcuts ──────────────────────────────────────────────────────

    def hold(self, price: float, reason: str = "No signal") -> Signal:
        return Signal(action="HOLD", strategy=self.name, price=price, reason=reason)

    def buy(
        self,
        price: float,
        reason: str,
        stop_loss: float = None,
        take_profit: float = None,
        trailing_tp: bool = False,
        trail_pct: float = 0.02,
        trailing_sl: bool = False,
        trail_sl_pct: float = 0.03,
        order_type: str = "limit",
        leverage: int = 1,
        panic_protection: bool = False,
        max_hold_candles: int = 0,
        metadata: dict = None,
        quantity_pct: float = 1.0,
        limit_offset: float = 0.0005,
        is_short: bool = False,
        compound_profit: bool = False,
    ) -> Signal:
        return Signal(
            action="BUY", strategy=self.name, price=price, reason=reason,
            stop_loss=stop_loss, take_profit=take_profit,
            trailing_tp=trailing_tp, trail_pct=trail_pct,
            trailing_sl=trailing_sl, trail_sl_pct=trail_sl_pct,
            order_type=order_type, leverage=leverage,
            panic_protection=panic_protection, max_hold_candles=max_hold_candles,
            metadata=metadata or {},
            quantity_pct=quantity_pct, limit_offset=limit_offset,
            is_short=is_short, compound_profit=compound_profit,
        )

    def sell(
        self,
        price: float,
        reason: str,
        quantity_pct: float = 1.0,
        stop_loss: float = None,
        take_profit: float = None,
        is_short: bool = False,
        leverage: int = 1,
        order_type: str = "limit",
        compound_profit: bool = False,
        metadata: dict = None,
        trailing_tp: bool = False,
        trail_pct: float = 0.02,
        trailing_sl: bool = False,
        trail_sl_pct: float = 0.03,
        limit_offset: float = 0.0005,
        panic_protection: bool = False,
        max_hold_candles: int = 0,
    ) -> Signal:
        return Signal(
            action="SELL", strategy=self.name, price=price, reason=reason,
            quantity_pct=quantity_pct, stop_loss=stop_loss, take_profit=take_profit,
            is_short=is_short, leverage=leverage, order_type=order_type,
            compound_profit=compound_profit,
            metadata=metadata or {},
            trailing_tp=trailing_tp, trail_pct=trail_pct,
            trailing_sl=trailing_sl, trail_sl_pct=trail_sl_pct,
            limit_offset=limit_offset,
            panic_protection=panic_protection, max_hold_candles=max_hold_candles,
        )
