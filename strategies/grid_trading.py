"""
strategies/grid_trading.py — Adaptive Grid Trading Strategy (BB + ATR)

UPGRADE FROM v1:
  v1 used a static range (last-50-candle high/low) and fixed step size.
  v2 adapts BOTH the range and step size to live market conditions:

  ┌────────────────────────────────────────────────────────────┐
  │  RANGE → Bollinger Bands (20 period, 2σ)                    │
  │    Upper band = grid top                                    │
  │    Lower band = grid bottom                                 │
  │    Rationale: BB already captures where "too high" and     │
  │    "too low" are relative to recent price behaviour.       │
  │                                                            │
  │  STEP SIZE → ATR-based                                     │
  │    step = ATR × atr_step_mult (default 0.75)               │
  │    Rationale: In volatile markets, wider steps avoid being │
  │    whipsawed. In calm markets, tighter steps = more trades.│
  │                                                            │
  │  AUTO-RECALIBRATION → silent, every N candles              │
  │    If BB or ATR shift significantly, the grid is silently  │
  │    rebuilt (no trades cancelled — just new levels).        │
  └────────────────────────────────────────────────────────────┘

POSITION SIZING:
  Each grid trade uses a fixed USDT amount (usdt_per_trade),
  defaulting to a percentage of initial balance. This ensures the
  grid never deploys all capital on a single level.

  Example (10 levels, $200/trade):
    Max exposure = 10 × $200 = $2,000 (not $10,000)
    Each buy captures one ATR-step of profit on exit.

MARKET REGIME GUARD:
  Grid trading loses in strong trending markets. We check ATR%:
  - If ATR > atr_trend_threshold % of price, the market is
    trending, not ranging → grid returns HOLD and waits.
  - This prevents the grid from accumulating losers in a crash.

BTD MODE (Buy The Dip — base-currency accumulation):
  Inspired by Bitsgap's BTD (Buy The Dip) grid variant.

  In standard grid mode, each cycle buys USDT worth of base and
  sells 100% back to USDT on the next level — net profit is USDT.

  In BTD mode the USDT principal is recouped on each sell, but
  the "profit" portion is kept as base currency (e.g. BTC):

    Buy  0.003000 BTC @ $66,000  → spent $198 (≈$200 after fees)
    Sell at $67,320 (one step up):
      Recoup principal:  $200 / $67,320 = 0.002971 BTC sold
      Profit kept:       0.003000 − 0.002971 = 0.000029 BTC

  Over 100 cycles this silently accumulates real BTC on top of
  the normal USDT grid P&L — identical to Bitsgap LOOP behaviour.

  Implementation note: the simulator still records a full close
  (to keep accounting simple). The profit-in-base is tracked
  virtually in _btd_accumulated_base and reported in signal
  metadata and btd_summary(). The portfolio manager can optionally
  re-buy the equivalent base after each grid cycle exit.

PARAMETERS:
  bb_period          : Bollinger Band SMA period (default: 20)
  bb_std             : Band width in standard deviations (default: 2.0)
  atr_period         : ATR lookback for step sizing (default: 14)
  atr_step_mult      : Step = ATR × this multiplier (default: 0.75)
  atr_trend_threshold: ATR% above this → market is trending → pause grid (default: 2.5%)
  grid_levels        : Number of buy/sell pairs (default: 10)
  usdt_per_trade     : USDT to spend per grid level (default: 200)
  recalibrate_every  : Rebuild grid every N candles (default: 24 = daily on 1h)
  btd_mode           : If True, track profit as accumulated base currency (default: False)
"""

import time
import pandas as pd
import ta
from loguru import logger
from dataclasses import dataclass, field
from typing import Optional

from strategies.base import BaseStrategy, Signal
from portfolio.regime_detector import RegimeDetector, REGIME_RANGE
import config


@dataclass
class GridState:
    """Snapshot of the current grid configuration."""
    lower: float          # BB lower band
    upper: float          # BB upper band
    step: float           # ATR-based step size
    levels: list[float]   # All grid price levels (sorted ascending)
    calibrated_at: int    # Candle index when this grid was built


class GridTradingStrategy(BaseStrategy):
    """
    Adaptive grid trading strategy for ranging markets.
    Range set by Bollinger Bands, step size set by ATR.
    Silently recalibrates every N candles.
    """

    def __init__(
        self,
        symbol: str = None,
        timeframe: str = None,
        bb_period: int = 20,
        bb_std: float = 2.0,
        atr_period: int = 14,
        atr_step_mult: float = 0.75,
        atr_trend_threshold: float = 2.5,
        grid_levels: int = 10,
        usdt_per_trade: float = 200.0,
        recalibrate_every: int = 24,
        btd_mode: bool = False,
        # Trailing grid range (Bitsgap-inspired)
        # When price breaks ABOVE the upper boundary, shift the entire grid
        # range upward by one step instead of pausing.
        # When price breaks BELOW the lower boundary, shift down.
        # Keeps the grid capturing profits through breakouts rather than going dormant.
        trailing_grid: bool = False,
    ):
        """
        Args:
            symbol:               Trading pair, e.g. "BTC/USDT".
            timeframe:            Candle size, e.g. "1h".
            bb_period:            Bollinger Band SMA lookback period.
            bb_std:               BB standard deviation width.
            atr_period:           ATR lookback for volatility-based step sizing.
            atr_step_mult:        Step size = ATR × atr_step_mult.
                                  0.75 = slightly below 1 ATR for more trades per move.
            atr_trend_threshold:  If ATR/price > this %, market is trending → pause.
            grid_levels:          Number of grid lines to place.
            usdt_per_trade:       Fixed USDT per grid level. Does NOT auto-scale to balance.
            recalibrate_every:    Rebuild grid silently every N candles.
            btd_mode:             If True, track profit as accumulated base currency.
                                  Each cycle "holds" the profit portion as base rather
                                  than returning it to USDT. See module docstring.
        """
        super().__init__(
            name="GridTrading",
            symbol=symbol or config.TRADING_PAIR,
            timeframe=timeframe or config.TIMEFRAME,
        )
        self.bb_period = bb_period
        self.bb_std = bb_std
        self.atr_period = atr_period
        self.atr_step_mult = atr_step_mult
        self.atr_trend_threshold = atr_trend_threshold
        self.grid_levels = grid_levels
        self.usdt_per_trade = usdt_per_trade
        self.recalibrate_every = recalibrate_every
        self.btd_mode = btd_mode
        self.trailing_grid = trailing_grid
        self._trailing_shifts: int = 0   # Track how many times the range has shifted

        # Internal state
        self._grid: Optional[GridState] = None
        self._candle_count: int = 0
        self._last_buy_level: Optional[float] = None   # Grid level of current open buy
        self._in_position: bool = False

        # BTD accumulation tracking
        self._btd_accumulated_base: float = 0.0   # Total base currency "kept" as profit
        self._btd_cycles: int = 0                  # Grid cycles completed in BTD mode
        self._btd_total_usdt_profit: float = 0.0  # Running USDT value of base kept

        # SL circuit breaker — prevents re-entry after repeated stop-losses in a
        # short window (e.g. 5 SL hits in one sharp down-move).
        # _sl_consecutive: SL streak, reset to 0 on any profitable exit.
        # _sl_exit_times:  Unix timestamps of recent SL exits (pruned to 6-hour window).
        # _cb_paused_until: Unix timestamp after which new buys are allowed again.
        #                   0.0 means not paused.
        self._sl_consecutive: int = 0
        self._sl_exit_times: list[float] = []
        self._cb_paused_until: float = 0.0

        # ── Regime gate (Phase 4.A variation #1) ──────────────────────────────
        # Chen/Chen/Jang (2025) shows unconditional grid trading has zero EV;
        # conditional firing on the regime that satisfies range AND low-trend
        # AND mid-vol is the structural change.  In the 6-regime detector,
        # REGIME_RANGE is the unique label that matches all three criteria.
        # Detector reads the strategy's own pair df (asset-specific regime),
        # not BTC, per the chat-locked design decision.
        self._regime_detector = RegimeDetector()
        # Detector floor = ema_slow + 10 = 200 + 10 = 210 candles.  Below this
        # the detector returns RANGE/confidence=0 by default (line 225-234 of
        # portfolio/regime_detector.py); we treat warmup as dormant explicitly
        # rather than rely on the default-RANGE fallback firing entries.
        self._regime_min_warmup_candles: int = 210

        logger.info(
            f"GridTrading (adaptive) | BB({bb_period}, {bb_std}σ) | "
            f"ATR({atr_period})×{atr_step_mult} | "
            f"Trend guard: ATR>{atr_trend_threshold}% | "
            f"Levels={grid_levels} | ${usdt_per_trade}/trade | "
            f"Recalibrate every {recalibrate_every} candles | "
            f"BTD={'ON' if btd_mode else 'OFF'} | "
            f"Trailing grid: {'ON' if trailing_grid else 'OFF'}"
        )

    # ── SL circuit breaker helpers ────────────────────────────────────────────

    def _on_sl_exit(self) -> None:
        """
        Called whenever the simulator closes the position via stop-loss.
        Increments the consecutive-SL counter and trips the circuit breaker
        if 3+ SLs have occurred within a 6-hour rolling window.
        """
        now = time.time()
        window_secs = 6 * 3600  # 6-hour look-back

        # Drop exits older than the window
        self._sl_exit_times = [t for t in self._sl_exit_times if now - t < window_secs]
        self._sl_exit_times.append(now)
        self._sl_consecutive += 1

        if self._sl_consecutive >= 3 and len(self._sl_exit_times) >= 3:
            self._cb_paused_until = now + 12 * 3600  # block re-entry for 12 hours
            logger.warning(
                f"[GridTrading] Circuit breaker: paused re-entry after "
                f"{self._sl_consecutive} consecutive SLs "
                f"({len(self._sl_exit_times)} SLs in last 6h) — "
                f"resuming at {time.strftime('%H:%M:%S', time.localtime(self._cb_paused_until))}"
            )

    def _on_tp_exit(self) -> None:
        """
        Called whenever the position closes at a profit (grid target or upper-band break).
        Resets the consecutive-SL counter so a single win clears the streak.
        """
        if self._sl_consecutive > 0:
            logger.debug(
                f"[GridTrading] Circuit breaker streak reset "
                f"(was {self._sl_consecutive}) after profitable exit."
            )
        self._sl_consecutive = 0

    # ── Grid construction ─────────────────────────────────────────────────────

    def _build_grid(self, df: pd.DataFrame) -> GridState:
        """
        Compute Bollinger Bands and ATR, then construct grid levels.
        Grid lines are spaced `step` apart, starting from BB lower,
        up to BB upper (or as many as grid_levels allows).
        """
        close = df["close"]
        high = df["high"]
        low = df["low"]

        # Bollinger Bands → range
        bb = ta.volatility.BollingerBands(close, window=self.bb_period, window_dev=self.bb_std)
        bb_lower = float(bb.bollinger_lband().iloc[-1])
        bb_upper = float(bb.bollinger_hband().iloc[-1])

        # ATR → step size
        atr = ta.volatility.AverageTrueRange(
            high=high, low=low, close=close, window=self.atr_period
        ).average_true_range()
        current_atr = float(atr.iloc[-1])
        step = current_atr * self.atr_step_mult

        # Safety: step must be at least 0.1% of price and < (range / 2)
        current_price = float(close.iloc[-1])
        min_step = current_price * 0.001
        max_step = (bb_upper - bb_lower) / max(2, self.grid_levels - 1)
        step = max(min_step, min(step, max_step))

        # Build grid levels from lower band upward, spaced by step
        levels = []
        level = bb_lower
        while level <= bb_upper and len(levels) < self.grid_levels + 1:
            levels.append(round(level, 2))
            level += step

        # Ensure at least 2 levels
        if len(levels) < 2:
            levels = [round(bb_lower, 2), round(bb_upper, 2)]

        return GridState(
            lower=bb_lower,
            upper=bb_upper,
            step=step,
            levels=levels,
            calibrated_at=self._candle_count,
        )

    def _should_recalibrate(self) -> bool:
        """True if grid hasn't been built yet or recalibration interval has elapsed."""
        if self._grid is None:
            return True
        candles_since = self._candle_count - self._grid.calibrated_at
        return candles_since >= self.recalibrate_every

    # ── Grid level helpers ────────────────────────────────────────────────────

    def _nearest_level_at_or_below(self, price: float) -> Optional[float]:
        """Highest grid level ≤ current price."""
        candidates = [lv for lv in self._grid.levels if lv <= price]
        return max(candidates) if candidates else None

    def _next_level_above(self, price: float) -> Optional[float]:
        """Lowest grid level > current price."""
        candidates = [lv for lv in self._grid.levels if lv > price]
        return min(candidates) if candidates else None

    # ── Signal generation ─────────────────────────────────────────────────────

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        """
        Check whether price has crossed a grid level and return BUY/SELL/HOLD.
        Silently recalibrates the grid every recalibrate_every candles.

        Args:
            df: OHLCV DataFrame.

        Returns:
            Signal with action BUY, SELL, or HOLD.
        """
        min_rows = max(self.bb_period, self.atr_period) + 10
        self.validate_dataframe(df, min_rows=min_rows)

        self._candle_count += 1
        current_price = float(df["close"].iloc[-1])
        close = df["close"]
        high = df["high"]
        low = df["low"]

        # ── Regime gate (Phase 4.A variation #1) ──────────────────────────────
        # Block new ENTRIES outside REGIME_RANGE.  Existing positions still
        # exit normally through the SELL / out-of-range / SL/TP branches
        # below — the gate is entry-only.
        if len(df) < self._regime_min_warmup_candles:
            regime_blocks_entry = True
            regime_label = (
                f"warmup ({len(df)}/{self._regime_min_warmup_candles})"
            )
        else:
            self._regime_detector.detect(df)
            regime_label = self._regime_detector.current_regime
            regime_blocks_entry = regime_label != REGIME_RANGE

        # ── ATR trend guard ────────────────────────────────────────────────────
        atr = ta.volatility.AverageTrueRange(
            high=high, low=low, close=close, window=self.atr_period
        ).average_true_range()
        current_atr = float(atr.iloc[-1])
        atr_pct = (current_atr / current_price) * 100

        if atr_pct > self.atr_trend_threshold:
            logger.debug(
                f"Grid trend guard: ATR={atr_pct:.2f}% > {self.atr_trend_threshold}% — "
                f"market is trending, not ranging. Grid paused."
            )
            if self._in_position:
                # Emergency exit if we're in a position and market trends hard
                return self.hold(
                    current_price,
                    reason=(
                        f"Trend guard: ATR={atr_pct:.2f}% > {self.atr_trend_threshold}% threshold | "
                        f"Market trending — holding open position, won't add new levels"
                    )
                )
            return self.hold(
                current_price,
                reason=f"Trend guard: ATR={atr_pct:.2f}% > {self.atr_trend_threshold}% — market trending, grid paused"
            )

        # ── Recalibrate grid (silent) ──────────────────────────────────────────
        if self._should_recalibrate():
            prev_step = self._grid.step if self._grid else None
            self._grid = self._build_grid(df)
            if prev_step is None:
                logger.info(
                    f"Grid initialized: {len(self._grid.levels)} levels | "
                    f"Range: [{self._grid.lower:.2f} – {self._grid.upper:.2f}] | "
                    f"Step: ${self._grid.step:.2f} (ATR×{self.atr_step_mult}) | "
                    f"ATR={current_atr:.2f} ({atr_pct:.2f}%)"
                )
            else:
                change_pct = abs(self._grid.step - prev_step) / prev_step * 100
                logger.info(
                    f"Grid recalibrated (candle {self._candle_count}) | "
                    f"Range: [{self._grid.lower:.2f} – {self._grid.upper:.2f}] | "
                    f"Step: ${self._grid.step:.2f} (was ${prev_step:.2f}, Δ{change_pct:.1f}%) | "
                    f"ATR={current_atr:.2f} ({atr_pct:.2f}%)"
                )

        # ── Out-of-range handling ──────────────────────────────────────────────
        # Standard mode: pause when price leaves the BB-defined range.
        # Trailing grid mode: shift the entire range to follow price so the
        # grid stays active through breakouts and doesn't go dormant.

        if current_price < self._grid.lower:
            if self.trailing_grid and not self._in_position:
                # Shift range DOWN by one step to chase price
                shift = self._grid.step
                new_lower = self._grid.lower - shift
                new_upper = self._grid.upper - shift
                new_levels = [round(lv - shift, 2) for lv in self._grid.levels]
                self._grid = GridState(
                    lower=new_lower,
                    upper=new_upper,
                    step=self._grid.step,
                    levels=new_levels,
                    calibrated_at=self._candle_count,
                )
                self._trailing_shifts += 1
                logger.info(
                    f"Grid trailing DOWN: range shifted to "
                    f"[{new_lower:.2f}–{new_upper:.2f}] "
                    f"(shift #{self._trailing_shifts})"
                )
                # Don't return — fall through to normal BUY logic below
            else:
                return self.hold(
                    current_price,
                    reason=(
                        f"Price ${current_price:.2f} below grid lower ${self._grid.lower:.2f} | "
                        f"{'Trailing grid: in position, holding' if self.trailing_grid and self._in_position else 'Trending down — grid paused'}"
                    )
                )

        if current_price > self._grid.upper:
            if self._in_position:
                # Price broke above grid top — sell regardless of trailing mode
                self._in_position = False
                sell_level = self._last_buy_level
                self._last_buy_level = None
                buy_info = f"${sell_level:.2f}" if sell_level is not None else "entry"
                self._on_tp_exit()
                return self.sell(
                    price=current_price,
                    reason=(
                        f"Price ${current_price:.2f} broke above grid upper ${self._grid.upper:.2f} | "
                        f"Selling at grid top (bought at {buy_info})"
                    ),
                )

            if self.trailing_grid:
                # Shift range UP by one step to follow the breakout
                shift = self._grid.step
                new_lower = self._grid.lower + shift
                new_upper = self._grid.upper + shift
                new_levels = [round(lv + shift, 2) for lv in self._grid.levels]
                self._grid = GridState(
                    lower=new_lower,
                    upper=new_upper,
                    step=self._grid.step,
                    levels=new_levels,
                    calibrated_at=self._candle_count,
                )
                self._trailing_shifts += 1
                logger.info(
                    f"Grid trailing UP: range shifted to "
                    f"[{new_lower:.2f}–{new_upper:.2f}] "
                    f"(shift #{self._trailing_shifts})"
                )
                # Fall through to normal logic in the new range
            else:
                return self.hold(
                    current_price,
                    reason=f"Price ${current_price:.2f} above grid upper ${self._grid.upper:.2f} | Waiting for pullback"
                )

        nearest_below = self._nearest_level_at_or_below(current_price)
        next_above = self._next_level_above(current_price)

        # ── SELL: price reached target level above buy ─────────────────────────
        if self._in_position and self._last_buy_level is not None:
            sell_target = self._last_buy_level + self._grid.step
            if current_price >= sell_target:
                profit_pct = (current_price - self._last_buy_level) / self._last_buy_level * 100
                bought_at = self._last_buy_level

                # ── BTD mode: compute base currency accumulated this cycle ────
                # Profit portion = units bought minus units needed to recoup principal.
                # buy_qty  = usdt_per_trade / buy_level
                # sell_qty = usdt_per_trade / sell_price  ← just enough to get principal back
                # base_kept = buy_qty - sell_qty           ← "free" base accumulation
                btd_metadata = {}
                if self.btd_mode:
                    buy_qty  = self.usdt_per_trade / bought_at
                    recoup_qty = self.usdt_per_trade / current_price
                    base_kept = max(0.0, buy_qty - recoup_qty)
                    self._btd_accumulated_base += base_kept
                    self._btd_cycles += 1
                    self._btd_total_usdt_profit += base_kept * current_price
                    btd_metadata = {
                        "btd_profit_base":      round(base_kept, 8),
                        "btd_accumulated_base": round(self._btd_accumulated_base, 8),
                        "btd_cycles":           self._btd_cycles,
                        "btd_usdt_equiv":       round(base_kept * current_price, 4),
                    }
                    logger.info(
                        f"[BTD] Cycle {self._btd_cycles}: kept {base_kept:.8f} base "
                        f"(≈${base_kept * current_price:.2f}) | "
                        f"Total accumulated: {self._btd_accumulated_base:.6f} base "
                        f"(≈${self._btd_total_usdt_profit:.2f})"
                    )

                self._in_position = False
                self._last_buy_level = None
                self._on_tp_exit()
                sell_reason = (
                    f"Grid sell: ${current_price:.2f} ≥ target ${sell_target:.2f} | "
                    f"Bought at ${bought_at:.2f} | "
                    f"Grid profit: +{profit_pct:.2f}% | "
                    f"ATR step: ${self._grid.step:.2f}"
                )
                if self.btd_mode:
                    sell_reason += (
                        f" | BTD: accumulated {self._btd_accumulated_base:.6f} base "
                        f"over {self._btd_cycles} cycles"
                    )
                return self.sell(
                    price=current_price,
                    reason=sell_reason,
                    metadata=btd_metadata,
                )

        # ── BUY: price dropped to a new (lower) grid level ────────────────────
        if not self._in_position and nearest_below is not None:
            # Regime gate: dormant outside RANGE.  Existing positions still
            # exit through SELL / out-of-range / SL/TP branches above.
            if regime_blocks_entry:
                return self.hold(
                    current_price,
                    reason=(
                        f"Regime gate: dormant (regime={regime_label}, "
                        f"need RANGE) | nearest level=${nearest_below:.2f}"
                    ),
                )

            # Circuit breaker: skip re-entry if tripped by consecutive SLs
            now = time.time()
            if self._cb_paused_until > now:
                remaining_h = (self._cb_paused_until - now) / 3600
                logger.debug(
                    f"[GridTrading] Circuit breaker active — "
                    f"skipping buy at ${nearest_below:.2f} | "
                    f"{remaining_h:.1f}h remaining"
                )
                return self.hold(
                    current_price,
                    reason=(
                        f"[GridTrading] Circuit breaker: paused re-entry after "
                        f"3 consecutive SLs | {remaining_h:.1f}h remaining"
                    ),
                )

            # Only buy if this level is new (lower than previous) or first buy
            if self._last_buy_level is None or nearest_below < self._last_buy_level:
                self._in_position = True
                self._last_buy_level = nearest_below
                sell_target = nearest_below + self._grid.step

                return self.buy(
                    price=current_price,
                    reason=(
                        f"Grid buy at level ${nearest_below:.2f} | "
                        f"Target: ${sell_target:.2f} (+{self._grid.step/nearest_below*100:.2f}%) | "
                        f"ATR step: ${self._grid.step:.2f} | ATR%: {atr_pct:.2f}%"
                    ),
                    take_profit=sell_target,
                    stop_loss=self._grid.lower,
                    metadata={"amount_usdt": self.usdt_per_trade},
                )

        # ── HOLD ──────────────────────────────────────────────────────────────
        status = "IN POSITION" if self._in_position else "WAITING"
        sell_info = ""
        if self._in_position and self._last_buy_level:
            sell_info = f" | Sell target: ${self._last_buy_level + self._grid.step:.2f}"

        return self.hold(
            current_price,
            reason=(
                f"{status} | "
                f"BB range: [${self._grid.lower:.2f} – ${self._grid.upper:.2f}] | "
                f"Step: ${self._grid.step:.2f} | "
                f"ATR: {atr_pct:.2f}% | "
                f"Nearest level: ${nearest_below if nearest_below is not None else 0:.2f}"
                f"{sell_info}"
            )
        )

    def sync_state(self, simulator_has_position: bool) -> None:
        """
        Sync Grid's internal flag with the simulator's actual position state.

        Called from PortfolioManager.run_candle() after every tick, the same
        way DCAStrategy.sync_state() is called.

        Two cases this handles:

        1. Simulator CLOSED position externally (SL/TP via tick) but Grid still
           thinks it's in position → reset Grid so it can re-enter next signal.

        2. Simulator has an OPEN position on restart but Grid thinks it's flat
           (because _in_position is always False at __init__) → set _in_position=True
           to prevent an immediate double-buy on the first candle after restart.
           _last_buy_level is left as None because the original buy level is not
           persisted; the simulator's SL is the safety net for the restored position.
        """
        if self._in_position and not simulator_has_position:
            # Tick closed the position behind Grid's back — reset so we can re-enter.
            # We treat this as a stop-loss (the simulator hits SL via tick, not via
            # generate_signal; any TP hit inside generate_signal is handled there).
            logger.info(
                "Grid: simulator closed position externally (SL/TP via tick). "
                "Resetting _in_position to allow re-entry."
            )
            self._in_position = False
            self._last_buy_level = None
            self._on_sl_exit()

        elif not self._in_position and simulator_has_position:
            # Restart recovery: simulator has a position Grid doesn't know about
            logger.info(
                "Grid: simulator has restored position but _in_position=False "
                "(restart). Setting _in_position=True to prevent double-buy. "
                "_last_buy_level is unknown — SL from checkpoint is the safety net."
            )
            self._in_position = True
            # _last_buy_level intentionally left as None — the sell target won't
            # fire until the position exits via SL (handled by simulator) or a
            # fresh grid buy occurs. This is conservative but safe.

    def reset_grid(self):
        """Force a full grid rebuild on the next candle. Call after major moves."""
        self._grid = None
        self._last_buy_level = None
        self._in_position = False
        logger.info("Grid reset — will recalibrate on next candle.")

    # ── BTD reporting ──────────────────────────────────────────────────────────

    def btd_summary(self, current_price: Optional[float] = None) -> str:
        """
        Return a formatted summary of BTD (base-currency) accumulation.

        Args:
            current_price: If provided, shows current USDT value of accumulated base.

        Returns:
            Multi-line string report.
        """
        if not self.btd_mode:
            return "BTD mode is OFF — no base accumulation tracked."

        lines = [
            "─" * 50,
            f"  BTD (Buy The Dip) Accumulation Report",
            "─" * 50,
            f"  Completed cycles  : {self._btd_cycles}",
            f"  Accumulated base  : {self._btd_accumulated_base:.8f}",
            f"  Cumulative profit : ${self._btd_total_usdt_profit:.2f} (at exit prices)",
        ]
        if current_price and current_price > 0:
            current_val = self._btd_accumulated_base * current_price
            lines.append(f"  Current value     : ${current_val:.2f} @ ${current_price:,.2f}")
        lines.append("─" * 50)
        return "\n".join(lines)
