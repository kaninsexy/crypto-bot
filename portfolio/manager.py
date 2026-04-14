"""
portfolio/manager.py — Portfolio Manager (Phase D + E combined)

The Portfolio Manager is the top-level brain that coordinates all strategies.
It replaces the single-strategy main.py loop with a multi-strategy portfolio
that adapts to market conditions, sizes positions with Kelly Criterion, and
protects capital with a circuit breaker.

ARCHITECTURE
────────────
  PortfolioManager
    ├── RegimeDetector        — detects market regime from BTC/USDT data
    ├── KellyCalculator       — computes per-trade sizes from Phase C backtest
    ├── CircuitBreaker        — 30% drawdown hard stop
    └── StrategySlot × 6     — each strategy + its own PaperTrading simulator

  Note: DepositManager (portfolio/deposit_manager.py) is a standalone utility
  for manual monthly THB→USDT deposit flows. It is NOT used internally here —
  PortfolioManager distributes capital directly via REGIME_ALLOCATIONS.

  On every candle:
    1. Detect regime → update target allocations
    2. Update circuit breaker → block buys if tripped
    3. For each strategy:
       a. Tick simulator (SL/TP/trail checks)
       b. Generate signal
       c. If BUY: check circuit breaker + compute Kelly size
       d. Execute signal on strategy's simulator
    4. Log portfolio summary

KEY DESIGN DECISIONS
────────────────────
  - Each strategy gets its own isolated PaperTrading simulator with a
    capital slice proportional to its regime allocation weight.
    (e.g. if DCA = 25% of $10k → DCA simulator starts with $2,500)
  - Kelly sizing is applied WITHIN each strategy's simulator.
    The Kelly fraction × strategy capital = per-trade USDT spend.
  - Rebalancing happens on deposits only. Mid-period: over-allocated strategies
    receive no new capital until they're back in line with target weight.
  - Circuit breaker monitors TOTAL equity across all simulators.

USAGE
─────
    pm = PortfolioManager(total_capital=10_000)
    pm.initialize(initial_regime_df=btc_df)

    # Each candle:
    pm.run_candle(
        btc_df=btc_df,          # For regime detection
        strategy_dfs=dfs,       # {strategy_name: its_ohlcv_df}
    )

    # Monthly deposit:
    pm.deposit(2_850.0)

    print(pm.summary())
"""

from __future__ import annotations
import sys
import json
from dataclasses import dataclass, field
from typing import Optional
from pathlib import Path
import pandas as pd
from loguru import logger

from paper_trading.simulator import PaperTrading
from strategies.base import BaseStrategy
from strategies.dca import DCAStrategy
from strategies.supertrend import SupertrendStrategy
from strategies.trend_following import TrendFollowingStrategy
from strategies.mean_reversion import MeanReversionStrategy
from strategies.grid_trading import GridTradingStrategy
from strategies.breakout import BreakoutStrategy
from strategies.bear_short import BearShortStrategy
from strategies.vwap import VWAPStrategy
from strategies.volatility_breakout import VolatilityBreakoutStrategy
from strategies.dual_momentum import DualMomentumStrategy

from portfolio.regime_detector import RegimeDetector, RegimeReading, REGIME_ALLOCATIONS, REGIME_CASH_RESERVE, REGIME_RANGE
from portfolio.kelly import KellyCalculator, KellyProfile, PHASE_C_PROFILES
from portfolio.circuit_breaker import CircuitBreaker, BreakerState
from portfolio.funding_rate import FundingRateProvider
from portfolio.leverage_guard import LeverageGuard

# Live execution layer — optional; only imported when TRADING_MODE == "live".
# Using TYPE_CHECKING avoids a hard import dependency in paper mode.
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from execution.ccxt_executor import CCXTExecutor

import config


# ── Strategy slot ─────────────────────────────────────────────────────────────

@dataclass
class StrategySlot:
    """
    One "slot" in the portfolio: a strategy + its isolated paper trading simulator.
    """
    name:       str                   # e.g. "DCA", "Supertrend"
    strategy:   BaseStrategy
    simulator:  PaperTrading
    bucket_key: str                   # Deposit manager bucket name (lowercase)
    capital:    float                 # Allocated capital at init
    active:     bool = True           # False = suspended by regime or circuit breaker

    @property
    def equity(self) -> float:
        """Current equity: cash + open position MTM (at last known price)."""
        return self.simulator.balance

    def equity_at(self, price: float) -> float:
        return self.simulator.get_equity(price)


# ── Portfolio Manager ─────────────────────────────────────────────────────────

class PortfolioManager:
    """
    Multi-strategy portfolio manager with regime detection, Kelly sizing,
    and circuit breaker protection.

    Args:
        total_capital:      Total USDT to split across strategies at init.
        initial_regime:     Override starting regime (auto-detected if None).
        trip_pct:           Circuit breaker trip threshold (% drawdown from peak).
        warn_pct:           Circuit breaker warning threshold.
        reset_pct:          Circuit breaker reset threshold.
        kelly_fraction:     0.5 = half Kelly (default). 0.25 = quarter Kelly.
        symbol:             Primary trading pair (for strategy init).
        timeframe:          Candle timeframe for strategies.
    """

    # Map portfolio manager strategy names → build_strategy() names
    STRATEGY_KEYS = ["DCA", "Supertrend", "MeanReversion", "GridTrading", "Breakout", "TrendFollowing", "BearShort", "VWAP", "VolatilityBreakout", "DualMomentum"]
    BUCKET_KEYS   = ["dca", "supertrend", "meanrev",       "grid",        "breakout", "trend",          "bearshort", "vwap", "volbreakout",        "dual_momentum"]

    def __init__(
        self,
        total_capital:        float = 10_000.0,
        initial_regime:       Optional[str] = None,
        trip_pct:             float = 30.0,
        warn_pct:             float = 15.0,
        reset_pct:            float = 15.0,
        kelly_fraction:       float = 0.5,
        symbol:               str   = None,
        timeframe:            str   = None,
        funding_filter:       bool  = False,
        funding_symbol:       str   = "BTCUSDT",
        funding_rate_max:     float = 0.0005,
        funding_update_every: int   = 60,       # Candles between funding rate refreshes
        # Cross-strategy correlation cap.
        # If two or more strategies share the same symbol (e.g. DCA and
        # TrendFollowing both trading BTC/USDT), total exposure to that symbol
        # is capped at this % of portfolio equity.
        # Example: 40% cap → if BTC positions across all slots already consume
        # 40% of total equity, no additional BTC buys are allowed until some
        # positions close.  Set to 0 to disable.
        max_symbol_exposure_pct: float = 40.0,
        # Live execution layer — pass a CCXTExecutor to trade real money.
        # Paper simulator always runs regardless; live_executor is an ADD-ON.
        # If None (default) the bot runs in paper-only mode.
        live_executor:        Optional["CCXTExecutor"] = None,
    ):
        self.total_capital = total_capital
        self.symbol        = symbol  or config.TRADING_PAIR
        self.timeframe     = timeframe or config.TIMEFRAME

        # ── Sub-components ─────────────────────────────────────────────────
        self.regime_detector = RegimeDetector()
        self.kelly_calc      = KellyCalculator(kelly_fraction=kelly_fraction)
        self.kelly_profiles  = self.kelly_calc.build_profiles(PHASE_C_PROFILES)
        self.circuit_breaker = CircuitBreaker(
            initial_equity=total_capital,
            trip_pct=trip_pct,
            warn_pct=warn_pct,
            reset_pct=reset_pct,
        )

        # ── Leverage guard ─────────────────────────────────────────────────
        # Runs every 4 candles: liquidation proximity check, regime leverage
        # caps, and correlated multi-strategy exposure warnings.
        self._leverage_guard = LeverageGuard(max_portfolio_loss_pct=0.30)

        # ── Funding rate provider (optional) ───────────────────────────────
        self.funding_filter       = funding_filter
        self.funding_update_every = funding_update_every
        self._funding_candle_counter: int = 0
        if funding_filter:
            self.funding_provider = FundingRateProvider(
                symbol=funding_symbol,
                high_threshold=funding_rate_max,
            )
            logger.info(
                f"PortfolioManager: Funding rate filter ENABLED | "
                f"Symbol: {funding_symbol} | "
                f"Max: {funding_rate_max*100:.3f}%/8h | "
                f"Update every {funding_update_every} candles"
            )
        else:
            self.funding_provider = None

        # ── Correlation cap ────────────────────────────────────────────────
        self.max_symbol_exposure_pct = max_symbol_exposure_pct

        # ── State ─────────────────────────────────────────────────────────
        self._current_regime: str             = initial_regime or REGIME_RANGE
        self._current_reading: Optional[RegimeReading] = None
        self._slots: dict[str, StrategySlot] = {}
        self._candle_count: int = 0
        self._last_prices:  dict[str, float] = {}
        self._regime_change_count: int = 0
        self._daily_loss_block: bool = False    # Set by DailyLossGuard in main.py
        self._last_daily_tier:  str  = "NORMAL" # Tracks tier between candles for transition detection
        # Track how many trades per slot have already been flushed to the
        # persistent logger so we only log each trade once.
        self._logged_trade_counts: dict[str, int] = {}

        # ── Live executor (optional) ───────────────────────────────────────────
        # When set, every BUY/SELL signal is ALSO sent to the real exchange
        # via this executor.  Paper sim still runs independently for equity
        # tracking, drawdown monitoring, and Kelly sizing.
        self.live_executor = live_executor
        if live_executor is not None:
            logger.info(
                f"PortfolioManager: Live execution ENABLED via "
                f"{live_executor.exchange_id.upper()} | "
                f"Paper sim runs in parallel for equity tracking."
            )
        else:
            logger.info("PortfolioManager: PAPER mode (no live executor attached).")

        corr_label = (
            f"{max_symbol_exposure_pct:.0f}% per-symbol cap"
            if max_symbol_exposure_pct > 0 else "disabled"
        )
        logger.info(
            f"PortfolioManager initialized | "
            f"Capital: ${total_capital:,.0f} | "
            f"Pair: {self.symbol} | TF: {self.timeframe} | "
            f"Kelly: {kelly_fraction}× | "
            f"CircuitBreaker: trip={trip_pct}% / warn={warn_pct}% | "
            f"CorrCap: {corr_label}"
        )

    # ── Initialization ──────────────────────────────────────────────────────

    def initialize(self, initial_btc_df: pd.DataFrame) -> None:
        """
        Initialize all strategy slots with capital sliced by regime allocation.

        Must be called once before the first candle is processed.

        Args:
            initial_btc_df: BTC/USDT OHLCV for regime detection.
                           Needs at least 210 rows.
        """
        # Detect initial regime
        reading = self.regime_detector.detect(initial_btc_df)
        self._current_regime  = reading.regime
        self._current_reading = reading

        logger.info(f"Initial regime: {reading.regime} (confidence {reading.confidence:.0%})")
        logger.info(f"\n{self.regime_detector.summary(reading)}")

        # Compute Kelly sizes per strategy
        allocs   = REGIME_ALLOCATIONS[reading.regime]
        kelly_sz = self.kelly_calc.portfolio_kelly_sizes(
            self.kelly_profiles, allocs, self.total_capital
        )
        logger.info(f"\n{self.kelly_calc.summary(self.kelly_profiles)}")

        # Create strategy slots with capital proportional to regime weights
        for sname, bucket_key in zip(self.STRATEGY_KEYS, self.BUCKET_KEYS):
            weight   = allocs.get(bucket_key, 0.0)
            capital  = round(self.total_capital * weight, 2)
            # Use per-strategy symbol from config if available
            slot_symbol = config.STRATEGY_SYMBOLS.get(sname, self.symbol)
            strategy = self._build_strategy(sname, slot_symbol)
            sim      = PaperTrading(initial_balance=capital, symbol=slot_symbol)

            self._slots[sname] = StrategySlot(
                name=sname, strategy=strategy,
                simulator=sim, bucket_key=bucket_key,
                capital=capital, active=(capital > 0),
            )
            logger.info(
                f"  Slot: {sname:<15} | "
                f"Capital: ${capital:>8,.0f} ({weight*100:.0f}%) | "
                f"Active: {capital > 0}"
            )

        logger.info("PortfolioManager ready. Call run_candle() on each new candle.")

    # ── Main candle loop ────────────────────────────────────────────────────

    def run_candle(
        self,
        btc_df: pd.DataFrame,
        strategy_dfs: dict[str, pd.DataFrame],
    ) -> dict[str, str]:
        """
        Process one candle across all strategies.

        Args:
            btc_df:         BTC/USDT OHLCV (for regime detection). Same or
                            different from strategy symbol.
            strategy_dfs:   Dict {strategy_name: ohlcv_df} for each slot.
                            Keys must match STRATEGY_KEYS.
                            If a key is missing, that strategy is skipped.

        Returns:
            Dict {strategy_name: "BUY"/"SELL"/"HOLD"/"BLOCKED"} of actions taken.
        """
        self._candle_count += 1
        actions = {}

        # 1. Detect regime
        reading = self.regime_detector.detect(btc_df)
        if reading.regime != self._current_regime:
            self._regime_change_count += 1
            self._current_regime  = reading.regime
            self._current_reading = reading
            logger.info(f"[Portfolio] Regime → {reading.regime} | Rebalancing target allocations.")

        # 2. Compute current total equity + update circuit breaker
        total_equity = self._compute_total_equity(strategy_dfs)
        cb_state     = self.circuit_breaker.update(total_equity)
        size_mult    = self.circuit_breaker.size_multiplier()

        # ── Graduated daily loss guard ───────────────────────────────────────
        # Runs independently of the circuit breaker (which tracks peak drawdown).
        # _daily_guard is injected from main.py; update() was already called there
        # before run_candle(), so _daily_guard.tier is current.
        _dg = getattr(self, '_daily_guard', None)
        if _dg is not None:
            _daily_tier = _dg.tier.value  # "NORMAL"/"WARNING"/"CAUTIOUS"/"HALT"/"REDUCE"/"EMERGENCY"

            # One-shot: close ALL positions when EMERGENCY tier first triggers
            if _dg.needs_emergency_close():
                self._emergency_close_all(strategy_dfs)

            # One-shot: tighten stops when entering REDUCE tier
            if _daily_tier == "REDUCE" and self._last_daily_tier != "REDUCE":
                self._tighten_all_stops(strategy_dfs)

            self._last_daily_tier = _daily_tier

            # Combine: CircuitBreaker × DailyLossGuard (e.g. CB=0.5 × DLG=0.75 → 0.375)
            size_mult = size_mult * _dg.size_multiplier()

        if cb_state == BreakerState.TRIPPED:
            logger.warning(
                f"[Portfolio] 🚨 Circuit breaker TRIPPED | "
                f"Equity: ${total_equity:,.0f} | "
                f"Drawdown: {self.circuit_breaker.current_drawdown_pct(total_equity):.1f}% | "
                f"All new buys blocked."
            )

        # 3. Refresh funding rate (if filter enabled, every N candles)
        # Funding updates every 8 hours on-chain; hourly fetches are more than
        # sufficient. We skip most candles to avoid hammering the API.
        if self.funding_provider is not None:
            self._funding_candle_counter += 1
            if self._funding_candle_counter >= self.funding_update_every:
                self._funding_candle_counter = 0
                current_rate = self.funding_provider.get_rate()
                # Push the fresh rate into every DCA slot that has the filter on
                for slot in self._slots.values():
                    if (
                        isinstance(slot.strategy, DCAStrategy)
                        and slot.strategy.funding_filter_enabled
                    ):
                        slot.strategy.update_funding_rate(current_rate)

        # 4. Leverage & risk guard — pure math, no API calls.
        # Runs every 4 candles (not every candle) to avoid redundant recalculation;
        # risk levels don't change meaningfully candle-to-candle.
        if self._candle_count % 4 == 0:
            _lv_positions = self._get_open_positions_summary()
            # Build {symbol: price} — multiple strategies can share a symbol;
            # last one wins which is fine since they see the same market price.
            _lv_prices: dict[str, float] = {}
            for _sn, _slot in self._slots.items():
                _df = strategy_dfs.get(_sn)
                if _df is not None and not _df.empty:
                    _lv_prices[_slot.strategy.symbol] = float(_df["close"].iloc[-1])

            # Liquidation safety check
            liq_warnings = self._leverage_guard.check_liquidation_safety(
                _lv_positions, _lv_prices, self.total_capital
            )
            for w in liq_warnings:
                logger.critical(f"[LeverageGuard] LIQUIDATION RISK: {w}")

            # Correlated exposure check
            corr = self._leverage_guard.check_correlated_risk(
                _lv_positions, _lv_prices, self.total_capital
            )
            for symbol, data in corr.items():
                if data["warning"]:
                    logger.warning(
                        f"[LeverageGuard] CORRELATED RISK {symbol}: "
                        f"{len(data['strategies'])} strategies exposed | "
                        f"notional={data['exposure_pct']:.1f}% of capital | "
                        f"max combined loss={data['max_combined_loss_pct']:.1f}%"
                    )

        # 5. Process each strategy
        allocs = REGIME_ALLOCATIONS[self._current_regime]

        for sname, slot in self._slots.items():
            df = strategy_dfs.get(sname)
            if df is None or df.empty:
                actions[sname] = "SKIP"
                continue

            current_price = float(df["close"].iloc[-1])
            self._last_prices[sname] = current_price
            bucket_key = slot.bucket_key

            # ── Tick: SL/TP/trail checks ─────────────────────────────
            # Use full OHLCV tick so SL fires at candle LOW (not just close).
            # A 5% wick below SL that closes above it should still exit —
            # tick(close_only) would miss that entirely.
            slot.simulator.tick_ohlcv_candle(
                high=float(df["high"].iloc[-1]),
                low=float(df["low"].iloc[-1]),
                close=current_price,
            )

            # Strategy state sync — must happen AFTER tick so that if tick()
            # just closed a position via SL/TP, the strategy sees the updated
            # simulator state and resets its own _in_position flag correctly.
            had_position = slot.simulator.position is not None
            # We need pre-tick state: check if trade history grew (= SL/TP exit happened)
            pre_trade_count = self._logged_trade_counts.get(sname, 0)
            has_position = slot.simulator.position is not None

            if isinstance(slot.strategy, DCAStrategy):
                slot.strategy.sync_state(simulator_has_position=has_position)
            elif isinstance(slot.strategy, GridTradingStrategy):
                slot.strategy.sync_state(simulator_has_position=has_position)
            elif isinstance(slot.strategy, BearShortStrategy):
                slot.strategy.sync_state(simulator_has_position=has_position)
            elif isinstance(slot.strategy, (MeanReversionStrategy, VWAPStrategy)):
                slot.strategy.sync_state(simulator_has_position=has_position)
            elif isinstance(slot.strategy, (VolatilityBreakoutStrategy, DualMomentumStrategy)):
                slot.strategy.sync_state(simulator_has_position=has_position)

            # ── Cooldown trigger on SL exit ───────────────────────────────────
            # If the simulator has more closed trades than we last counted AND
            # the most recent trade was a loss (SL hit), start the strategy's
            # cooldown so it doesn't immediately re-enter the same bad setup.
            current_trade_count = len(slot.simulator.trade_history)
            if current_trade_count > pre_trade_count:
                last_trade = slot.simulator.trade_history[-1]
                if last_trade.pnl < 0 and hasattr(slot.strategy, 'start_cooldown'):
                    # Different strategies have different default cooldown lengths
                    cooldown_c = getattr(slot.strategy, 'cooldown_candles', 3)
                    slot.strategy.start_cooldown(cooldown_c)
                    logger.debug(
                        f"[Portfolio] {sname} SL exit detected "
                        f"(pnl={last_trade.pnl:.2f}) — "
                        f"cooldown {cooldown_c} candles started"
                    )
                # Update our count
                self._logged_trade_counts[sname] = current_trade_count

            # ── Supertrend BTC filter + 4H direction filter ───────────
            if isinstance(slot.strategy, SupertrendStrategy):
                if slot.strategy.btc_filter:
                    slot.strategy.update_btc_trend(btc_df)
                if slot.strategy.htf_filter:
                    htf_df = strategy_dfs.get("_htf_4h")
                    if htf_df is not None:
                        slot.strategy.update_htf_trend(htf_df)

            # ── Breakout 4h MTF filter ────────────────────────────────
            # Without this, _htf_trend stays 0 forever in portfolio mode
            # and Breakout can never fire a signal (mtf_bullish=False always).
            if isinstance(slot.strategy, BreakoutStrategy) and slot.strategy.mtf_enabled:
                htf_df = strategy_dfs.get("_htf_4h")
                if htf_df is not None and not htf_df.empty:
                    slot.strategy.update_htf_trend(htf_df)

            # ── DCA 4h MTF entry filter ───────────────────────────────
            # Feed 4h data to DCA so it can gate base-order entry on
            # 4h RSI range (avoid opening new cycles in crash/overbought).
            if isinstance(slot.strategy, DCAStrategy) and slot.strategy.mtf_filter_enabled:
                htf_df = strategy_dfs.get("_htf_4h")
                if htf_df is not None and not htf_df.empty:
                    slot.strategy.update_htf_data(htf_df)

            # ── Regime update for Breakout + VolatilityBreakout ──────
            # Both strategies have an internal regime filter that blocks
            # signals in non-bull regimes.  Push the current regime each
            # candle so they can make that decision internally.
            if isinstance(slot.strategy, (BreakoutStrategy, VolatilityBreakoutStrategy)):
                if hasattr(slot.strategy, 'update_regime'):
                    slot.strategy.update_regime(self._current_regime)

            # ── DualMomentum universe data update ─────────────────────
            # Build a symbol→df map from whatever data the portfolio has
            # already fetched this candle, then hand it to the strategy
            # so it can re-rank its universe on the rebalance cycle.
            if isinstance(slot.strategy, DualMomentumStrategy):
                symbol_df_map: dict = {}
                for sn, s in self._slots.items():
                    sym = s.strategy.symbol
                    candidate_df = strategy_dfs.get(sn)
                    if candidate_df is not None and sym not in symbol_df_map:
                        symbol_df_map[sym] = candidate_df
                slot.strategy.update_universe(symbol_df_map)
                slot.strategy.update_regime(self._current_regime)

            # ── Generate signal ───────────────────────────────────────
            try:
                signal = slot.strategy.generate_signal(df)
            except Exception as e:
                logger.debug(f"[Portfolio] {sname} signal error (warm-up?): {e}")
                actions[sname] = "WARM_UP"
                continue

            # ── BUY logic ─────────────────────────────────────────────
            if signal.action == "BUY":
                # Portfolio-level daily loss block (set by main.py via DailyLossGuard)
                # Covers HALT / REDUCE / EMERGENCY tiers; WARNING/CAUTIOUS only reduce size.
                if self._daily_loss_block:
                    tier_label = self._last_daily_tier
                    actions[sname] = "DAILY_LOSS_BLOCK"
                    logger.debug(
                        f"[Portfolio] {sname} BUY blocked by daily loss guard "
                        f"(tier={tier_label})."
                    )
                    continue

                # Per-strategy daily loss block
                if getattr(self, '_daily_guard', None) is not None:
                    slot_eq = slot.simulator.get_equity(current_price)
                    self._daily_guard.update_slot(sname, slot_eq)
                    if not self._daily_guard.allows_slot_buy(sname, slot_eq):
                        loss_pct = self._daily_guard.slot_loss_pct(sname, slot_eq)
                        actions[sname] = "SLOT_DAILY_LOSS_BLOCK"
                        logger.warning(
                            f"[Portfolio] {sname} BUY blocked: slot daily loss "
                            f"{loss_pct:.1f}% exceeded limit."
                        )
                        continue

                # Cross-strategy symbol exposure cap.
                # Prevents two strategies from piling into the same coin
                # simultaneously (e.g. DCA + TrendFollowing both long BTC).
                if self.max_symbol_exposure_pct > 0:
                    slot_symbol   = slot.strategy.symbol
                    exposure_pct  = self._symbol_exposure_pct(slot_symbol)
                    if exposure_pct >= self.max_symbol_exposure_pct:
                        actions[sname] = "CORR_BLOCK"
                        logger.debug(
                            f"[Portfolio] {sname} BUY blocked: {slot_symbol} "
                            f"already {exposure_pct:.1f}% of portfolio equity "
                            f"(cap={self.max_symbol_exposure_pct:.0f}%)"
                        )
                        continue

                if not self.circuit_breaker.allows_new_buys():
                    actions[sname] = "BLOCKED"
                    logger.debug(f"[Portfolio] {sname} BUY blocked by circuit breaker.")
                    continue

                # Regime allocation weight for this strategy
                regime_weight = allocs.get(bucket_key, 0.0)
                if regime_weight == 0.0:
                    actions[sname] = "SUSPENDED"
                    logger.debug(f"[Portfolio] {sname} suspended (0% allocation in {self._current_regime})")
                    continue

                # Kelly size for this trade
                kelly_profile = self.kelly_profiles.get(sname)
                if kelly_profile and kelly_profile.recommended_kelly > 0:
                    # Kelly fraction of strategy's available capital
                    strategy_equity = slot.simulator.get_equity(current_price)

                    # Volatility-scaled size multiplier:
                    # If current short-term volatility > historical baseline,
                    # scale down position size proportionally.
                    # e.g. if vol doubled → Kelly halved. Floor at 25% to avoid
                    # going fully flat on a momentary vol spike.
                    vol_mult = self._vol_size_mult(df)

                    kelly_usdt = (
                        strategy_equity
                        * kelly_profile.recommended_kelly
                        * size_mult
                        * vol_mult
                    )

                    # Respect existing metadata if strategy set amount_usdt
                    if "amount_usdt" not in signal.metadata and kelly_usdt > 0:
                        signal.metadata["amount_usdt"] = round(kelly_usdt, 2)
                        logger.debug(
                            f"[Portfolio] {sname} Kelly sizing: ${kelly_usdt:.2f} "
                            f"(½K={kelly_profile.half_kelly:.3f} × "
                            f"equity=${strategy_equity:.0f} × "
                            f"CB={size_mult:.2f} × vol={vol_mult:.2f})"
                        )

                # Apply regime-based leverage cap before handing the signal
                # to the simulator.  Only relevant for strategies that request
                # leverage > 1 (currently Breakout with ATR-dynamic leverage).
                if signal.leverage > 1:
                    signal.leverage = self._leverage_guard.apply_leverage_cap(
                        requested_leverage=signal.leverage,
                        regime=self._current_regime,
                        strategy_name=sname,
                    )

                slot.simulator.execute_signal(signal, current_price)
                actions[sname] = "BUY"

                # ── Live execution (BUY) ────────────────────────────────────
                # Fire the real exchange order AFTER the paper sim has
                # confirmed it accepts the signal.  Any live failure is logged
                # but does NOT roll back the paper sim — we keep simulating so
                # Kelly / circuit-breaker tracking never stalls on a network blip.
                if self.live_executor is not None:
                    try:
                        live_result = self.live_executor.execute_signal(
                            signal, current_price, slot_name=sname
                        )
                        if not live_result.success:
                            logger.error(
                                f"[Portfolio] LIVE BUY FAILED for {sname}: "
                                f"{live_result.error} | "
                                f"Paper sim advanced; MANUAL RECONCILIATION may be needed."
                            )
                    except Exception as live_exc:
                        logger.error(
                            f"[Portfolio] Live executor exception on BUY for {sname}: "
                            f"{live_exc}"
                        )

            elif signal.action == "SELL":
                slot.simulator.execute_signal(signal, current_price)
                actions[sname] = "SELL"

                # ── Live execution (SELL) ───────────────────────────────────
                if self.live_executor is not None:
                    try:
                        live_result = self.live_executor.execute_signal(
                            signal, current_price, slot_name=sname
                        )
                        if not live_result.success:
                            logger.error(
                                f"[Portfolio] LIVE SELL FAILED for {sname}: "
                                f"{live_result.error} | "
                                f"Paper sim advanced; MANUAL RECONCILIATION may be needed."
                            )
                    except Exception as live_exc:
                        logger.error(
                            f"[Portfolio] Live executor exception on SELL for {sname}: "
                            f"{live_exc}"
                        )

                # ── DCA profit compounding ──────────────────────────────────
                # When the trailing TP (3rd tranche) closes — marked by
                # compound_profit=True — reinvest compound_rate × profit
                # back into the strategy's capital pool AND grow base_amount
                # so the next cycle deploys slightly more.
                if (
                    isinstance(slot.strategy, DCAStrategy)
                    and slot.strategy.compound
                    and signal.compound_profit
                    and slot.simulator.trade_history
                ):
                    last_trade = slot.simulator.trade_history[-1]
                    if last_trade.pnl > 0:
                        reinvested = slot.strategy.apply_compound(last_trade.pnl)
                        if reinvested > 0:
                            # Deposit reinvested amount back into strategy's cash so
                            # the larger base_amount can actually be spent next cycle.
                            slot.simulator.deposit(reinvested)
                            slot.capital += reinvested

                # ── Grid BTD re-buy ─────────────────────────────────────────
                # When btd_mode=True and a grid cycle exits, the strategy
                # computes how much base currency "should" be kept as profit.
                # We honour that by immediately re-buying that quantity at
                # market price, so the base truly accumulates in the sim.
                if (
                    isinstance(slot.strategy, GridTradingStrategy)
                    and slot.strategy.btd_mode
                    and signal.metadata.get("btd_profit_base", 0) > 0
                ):
                    btd_base = signal.metadata["btd_profit_base"]
                    btd_usdt = btd_base * current_price
                    # Only rebuy if simulator has enough cash to cover it
                    if slot.simulator.balance >= btd_usdt:
                        btd_signal = slot.strategy.buy(
                            price=current_price,
                            reason=(
                                f"BTD rebuy: holding {btd_base:.8f} base "
                                f"(≈${btd_usdt:.2f}) as accumulated profit | "
                                f"cycle {signal.metadata.get('btd_cycles', '?')}"
                            ),
                            order_type="market",
                            metadata={"amount_usdt": round(btd_usdt, 4), "btd_rebuy": True},
                        )
                        slot.simulator.execute_signal(btd_signal, current_price)
                        logger.info(
                            f"[Portfolio] Grid BTD rebuy: {btd_base:.8f} base "
                            f"≈ ${btd_usdt:.2f} @ ${current_price:,.2f}"
                        )

            else:
                actions[sname] = "HOLD"

        return actions

    # ── Deposits ────────────────────────────────────────────────────────────

    def deposit(
        self,
        amount_usdt: float,
        note: str = "",
    ) -> dict[str, float]:
        """
        Add new capital and distribute across strategy simulators by regime weight.

        Distribution respects the current regime's cash-reserve % — if the regime
        says hold 15% cash (BEAR/CRASH), only 85% of the deposit is deployed.
        The undeploped remainder stays as a tracked reserve (not sent to any sim).

        Args:
            amount_usdt: Capital to add (USDT).
            note:        Optional label for logging.

        Returns:
            Dict of {strategy_name: amount_deployed}.
        """
        logger.info(f"[Portfolio] 💰 Deposit: ${amount_usdt:,.2f} | {note or 'monthly'}")
        self.total_capital += amount_usdt

        allocs     = REGIME_ALLOCATIONS[self._current_regime]
        cash_hold  = REGIME_CASH_RESERVE.get(self._current_regime, 0.0)
        deployable = amount_usdt * (1.0 - cash_hold)

        if cash_hold > 0:
            logger.info(
                f"[Portfolio] Regime={self._current_regime}: holding "
                f"{cash_hold:.0%} (${amount_usdt * cash_hold:.2f}) as cash reserve."
            )

        deployed: dict[str, float] = {}
        for sname, slot in self._slots.items():
            regime_weight = allocs.get(slot.bucket_key, 0.0)
            share = round(deployable * regime_weight, 2)
            if share <= 0:
                continue
            slot.simulator.deposit(share)
            slot.capital += share
            deployed[sname] = share
            logger.info(f"[Portfolio]   → {sname}: +${share:.2f}")

        return deployed

    def deposit_thb(
        self,
        amount_thb: float,
        actual_usdt: Optional[float] = None,
        note: str = "",
    ) -> dict[str, float]:
        """
        Convenience wrapper: deposit Thai Baht monthly contribution.

        Always pass actual_usdt= from your Binance statement so the
        bot uses the real received amount, not an estimated rate.
        """
        if actual_usdt is not None:
            usdt = round(actual_usdt, 2)
            rate = usdt / amount_thb
            logger.info(
                f"[Portfolio] THB deposit: {amount_thb:,.0f} → ${usdt:,.2f} USDT "
                f"(actual rate: {rate:.5f})"
            )
        else:
            usdt = round(amount_thb * 0.028, 2)
            logger.warning(
                f"[Portfolio] THB deposit estimated @ 0.028: {amount_thb:,.0f} → ${usdt:,.2f}. "
                f"Pass actual_usdt= for live accuracy."
            )
        return self.deposit(usdt, note=note or f"{amount_thb:,.0f} THB")

    # ── Portfolio rebalancing ───────────────────────────────────────────────

    def rebalance(
        self,
        current_dfs: Optional[dict[str, pd.DataFrame]] = None,
        drift_threshold_pct: float = 10.0,
        min_transfer: float = 20.0,
    ) -> dict[str, float]:
        """
        Soft cash rebalance: redistribute idle cash from over-allocated strategy
        simulators to under-allocated ones without closing any open positions.

        "Soft" means only undeployed cash (not position value) is moved.
        Positions are never forced closed — the bot waits for natural exits.
        This is a standard cash-sweep rebalance used by most multi-strategy
        portfolio managers.

        When to call:
          • After each monthly deposit (auto-called by deposit() if desired)
          • Manually after a large regime change
          • On a weekly schedule in live trading

        Logic:
          1. Compute each strategy's current equity vs its target
             (total_equity × regime_weight).
          2. Strategies over target by > drift_threshold_pct AND with idle
             cash become donors.
          3. Strategies under target by > drift_threshold_pct are receivers.
          4. Transfer the smaller of (needed deficit) or (50% of donor's
             idle cash) per pair — never empties a donor's cash completely.

        Args:
            current_dfs:          Optional OHLCV dict to price open positions.
            drift_threshold_pct:  Min % drift before a strategy is rebalanced.
                                  Default 10% — smaller values rebalance more
                                  aggressively.
            min_transfer:         Ignore transfers below this USDT amount.

        Returns:
            Dict of {"DonorName→ReceiverName": usdt_transferred}.
            Empty dict if nothing was rebalanced.
        """
        total_equity = self._compute_total_equity(current_dfs)
        allocs = REGIME_ALLOCATIONS[self._current_regime]

        # Build current state for each slot
        donors:    list[tuple] = []   # (sname, slot, free_cash, over_pct)
        receivers: list[tuple] = []   # (sname, slot, needed_usdt, under_pct)

        for sname, slot in self._slots.items():
            weight = allocs.get(slot.bucket_key, 0.0)
            if weight <= 0:
                continue  # 0%-weight strategy — skip

            target  = total_equity * weight
            price   = self._last_prices.get(sname)
            current = slot.equity_at(price) if price else slot.equity
            free    = slot.simulator.balance   # Cash only, not position value

            if target == 0:
                continue

            drift_pct = (current - target) / target * 100

            if drift_pct > drift_threshold_pct and free > min_transfer:
                # Over-allocated AND has idle cash → potential donor
                donors.append((sname, slot, free, drift_pct))
            elif drift_pct < -drift_threshold_pct:
                # Under-allocated → wants more capital
                needed = target - current
                receivers.append((sname, slot, needed, abs(drift_pct)))

        if not donors or not receivers:
            if donors or receivers:
                logger.info(
                    f"[Rebalance] Drift detected but no valid transfer pairs found "
                    f"(donors={len(donors)}, receivers={len(receivers)}). "
                    f"Waiting for positions to close."
                )
            return {}

        # Sort: biggest over-allocation donates first, biggest gap receives first
        donors.sort(key=lambda x: x[3], reverse=True)
        receivers.sort(key=lambda x: x[3], reverse=True)

        moved: dict[str, float] = {}
        donor_cash = {d[0]: d[2] for d in donors}  # Mutable local cash tracker

        for rec_name, rec_slot, needed, under_pct in receivers:
            remaining = needed
            for don_name, don_slot, _, _ in donors:
                available = donor_cash.get(don_name, 0)
                if available < min_transfer:
                    continue

                # Transfer at most half of donor's idle cash per receiver
                transfer = min(remaining, available * 0.5)
                if transfer < min_transfer:
                    continue

                transfer = round(transfer, 2)
                don_slot.simulator.balance -= transfer
                rec_slot.simulator.balance += transfer
                rec_slot.capital           += transfer
                donor_cash[don_name]       -= transfer
                remaining                  -= transfer

                key = f"{don_name}→{rec_name}"
                moved[key] = moved.get(key, 0) + transfer

                logger.info(
                    f"[Rebalance] ${transfer:,.2f}: {don_name} "
                    f"(+{under_pct:.0f}% over) → {rec_name} "
                    f"(-{under_pct:.0f}% under)"
                )

                if remaining < min_transfer:
                    break

        if moved:
            total_moved = sum(moved.values())
            logger.info(
                f"[Rebalance] Complete: ${total_moved:,.2f} redistributed "
                f"across {len(moved)} transfer(s) | "
                f"Regime: {self._current_regime}"
            )
        return moved

    # ── Daily loss guard helpers ────────────────────────────────────────────

    def _emergency_close_all(
        self,
        strategy_dfs: Optional[dict[str, pd.DataFrame]] = None,
    ) -> None:
        """
        Close all open positions at market price.

        Called exactly once by run_candle() when DailyLossGuard.needs_emergency_close()
        returns True (EMERGENCY tier, ≥ 5% daily loss).  The one-shot flag on the
        guard itself prevents repeat calls on subsequent candles.
        """
        from strategies.base import Signal  # local import to avoid circular
        logger.critical(
            "[Portfolio] 🚨 EMERGENCY CLOSE-ALL — daily loss ≥ 5%. "
            "Closing every open position at market price."
        )
        closed = 0
        for sname, slot in self._slots.items():
            if slot.simulator.position is None:
                continue
            price = None
            if strategy_dfs and sname in strategy_dfs:
                df = strategy_dfs[sname]
                if df is not None and not df.empty:
                    price = float(df["close"].iloc[-1])
            if price is None:
                price = self._last_prices.get(sname)
            if price is None:
                logger.warning(
                    f"[Portfolio] Emergency close {sname}: no price available — skipping."
                )
                continue
            signal = Signal(
                action="SELL",
                price=price,
                reason="EMERGENCY_DAILY_LOSS_5PCT",
                order_type="market",
            )
            try:
                slot.simulator.execute_signal(signal, price)
                logger.critical(
                    f"[Portfolio]   ✓ Emergency close {sname} @ ${price:,.2f}"
                )
                closed += 1
            except Exception as exc:
                logger.error(f"[Portfolio] Emergency close {sname} failed: {exc}")
        logger.critical(
            f"[Portfolio] Emergency close complete: {closed} position(s) closed."
        )

    def _tighten_all_stops(
        self,
        strategy_dfs: Optional[dict[str, pd.DataFrame]] = None,
        trail_pct: float = 0.02,
    ) -> None:
        """
        Tighten stop-losses on all open positions to trail_pct below current price.

        Called once when DailyLossGuard transitions into REDUCE tier (4% daily loss).
        Only ever moves stops UP (never loosens them) — if the existing stop is
        already tighter than trail_pct below current price, it is left untouched.

        Args:
            strategy_dfs: Latest OHLCV data to read current price from.
            trail_pct:    Fraction below current price for the tightened stop (default 2%).
        """
        logger.warning(
            f"[Portfolio] 🔴 REDUCE tier — tightening all open-position stops "
            f"to {trail_pct*100:.0f}% below current price."
        )
        tightened = 0
        for sname, slot in self._slots.items():
            pos = slot.simulator.position
            if pos is None:
                continue
            price = None
            if strategy_dfs and sname in strategy_dfs:
                df = strategy_dfs[sname]
                if df is not None and not df.empty:
                    price = float(df["close"].iloc[-1])
            if price is None:
                price = self._last_prices.get(sname)
            if price is None:
                continue
            new_sl  = round(price * (1.0 - trail_pct), 8)
            old_sl  = pos.stop_loss
            # Only tighten — never move stop further away
            if old_sl is None or new_sl > old_sl:
                pos.stop_loss = new_sl
                tightened += 1
                old_str = f"${old_sl:,.4f}" if old_sl is not None else "none"
                logger.warning(
                    f"[Portfolio]   {sname}: stop {old_str} → ${new_sl:,.4f} "
                    f"({trail_pct*100:.0f}% below ${price:,.2f})"
                )
        if tightened == 0:
            logger.info("[Portfolio] REDUCE: no open-position stops needed tightening.")

    # ── Equity & reporting ──────────────────────────────────────────────────

    def _vol_size_mult(
        self,
        df: Optional[pd.DataFrame],
        fast: int = 10,
        slow: int = 50,
    ) -> float:
        """
        Compute a [0.25, 1.0] position-size multiplier based on recent
        volatility relative to the historical baseline.

        Logic:
          current_vol  = std of last `fast` candle returns (short-term burst)
          baseline_vol = std of last `slow` candle returns (recent normal)
          multiplier   = baseline_vol / current_vol

          If the market is in a volatility spike (current > baseline),
          the fraction falls below 1.0 and Kelly sizing is scaled down
          proportionally. If vol is calm or below baseline, returns 1.0
          (never scales UP beyond full Kelly).

          Floor at 0.25 so the bot never drops below ¼ size even in
          extreme vol — it keeps participating rather than going fully flat.

        Called once per strategy per BUY signal from run_candle().

        Args:
            df:   The strategy's OHLCV DataFrame for the current candle.
            fast: Short lookback for "current" volatility (default: 10 candles).
            slow: Long lookback for baseline volatility (default: 50 candles).

        Returns:
            Float in [0.25, 1.0]. 1.0 means no scaling.
        """
        if df is None or len(df) < slow + fast:
            return 1.0
        try:
            returns = df["close"].pct_change().dropna()
            if len(returns) < slow:
                return 1.0
            current_vol  = float(returns.tail(fast).std())
            baseline_vol = float(returns.tail(slow).std())
            if baseline_vol <= 0 or current_vol <= 0:
                return 1.0
            mult = baseline_vol / current_vol
            return float(max(0.25, min(1.0, mult)))
        except Exception:
            return 1.0

    def _compute_total_equity(
        self, strategy_dfs: Optional[dict[str, pd.DataFrame]] = None
    ) -> float:
        """Sum equity across all strategy simulators."""
        total = 0.0
        for sname, slot in self._slots.items():
            price = None
            if strategy_dfs and sname in strategy_dfs:
                df = strategy_dfs[sname]
                if df is not None and not df.empty:
                    price = float(df["close"].iloc[-1])
            if price is not None:
                total += slot.equity_at(price)
            else:
                total += slot.equity
        return total

    def summary(self, strategy_dfs: Optional[dict[str, pd.DataFrame]] = None) -> str:
        """Print a full portfolio status dashboard."""
        total_equity = self._compute_total_equity(strategy_dfs)
        total_return = (total_equity - self.total_capital) / self.total_capital * 100
        allocs       = REGIME_ALLOCATIONS[self._current_regime]

        # Circuit breaker
        cb_dd = self.circuit_breaker.current_drawdown_pct(total_equity)

        # Regime info
        reading = self._current_reading

        live_tag = (
            f" [LIVE → {self.live_executor.exchange_id.upper()}]"
            if self.live_executor is not None
            else " [PAPER]"
        )

        lines = [
            "",
            "═" * 62,
            f"  PORTFOLIO MANAGER DASHBOARD{live_tag}",
            "═" * 62,
            f"  Total Capital   : ${self.total_capital:>10,.2f}",
            f"  Total Equity    : ${total_equity:>10,.2f}",
            f"  Portfolio Return: {total_return:>+9.2f}%",
            "",
            f"  Regime          : {self._current_regime}",
        ]

        if reading:
            lines.extend([
                f"    EMA50={reading.ema50:,.0f} | EMA200={reading.ema200:,.0f} | "
                f"RSI={reading.rsi:.1f} | ATR%={reading.atr_pct:.2f}% | "
                f"Conf={reading.confidence:.0%}",
            ])

        # Daily loss guard info (if attached)
        _dg = getattr(self, '_daily_guard', None)
        dlg_lines: list[str] = []
        if _dg is not None:
            dlg_lines = [
                f"  Daily Loss Guard: {_dg.tier.value}  "
                f"(loss today: -{_dg.current_loss_pct(total_equity):.2f}%  "
                f"size mult: {_dg.size_multiplier():.0%})",
            ]

        lines.extend([
            "",
            f"  Circuit Breaker : {self.circuit_breaker.state.value}  "
            f"(DD from peak: -{cb_dd:.1f}%)",
            f"  Size Multiplier : {self.circuit_breaker.size_multiplier():.0%}",
            *dlg_lines,
            "",
            f"  {'Strategy':<16} {'Allocated':>10} {'Equity':>10} {'Return%':>9} "
            f"{'Weight':>7} {'Active':>7}",
            "  " + "─" * 60,
        ])

        for sname, slot in self._slots.items():
            price = self._last_prices.get(sname)
            eq    = slot.equity_at(price) if price else slot.equity
            ret   = (eq - slot.capital) / slot.capital * 100 if slot.capital > 0 else 0
            w     = allocs.get(slot.bucket_key, 0.0) * 100
            trades = len(slot.simulator.trade_history)
            lines.append(
                f"  {sname:<16} ${slot.capital:>9,.0f} ${eq:>9,.0f} "
                f"{ret:>+8.2f}%  {w:>5.0f}%  {'✓' if slot.active else '—':>7}  "
                f"({trades} trades)"
            )

        # ── Leverage & Risk Guard section ──────────────────────────────────
        lines.extend([
            "",
            "─" * 62,
            f"  LEVERAGE & RISK GUARD",
            "─" * 62,
        ])

        try:
            # Build a {symbol: price} dict.
            # Primary source: self._last_prices (populated by run_candle every
            # candle).  Fallback: position.avg_entry_price so the section still
            # renders meaningful data if summary() is called before the first
            # candle (e.g. at startup after checkpoint restore).
            _lv_prices: dict[str, float] = {}
            for sn, sl in self._slots.items():
                px = self._last_prices.get(sn)
                if px is not None:
                    _lv_prices[sl.strategy.symbol] = px

            # If _last_prices is still empty (pre-first-candle), fall back to
            # each open position's entry price as a proxy.  Not perfectly
            # accurate but gives the section something to display.
            if not _lv_prices:
                for sn, sl in self._slots.items():
                    pos = sl.simulator.position
                    if pos is not None and pos.avg_entry_price > 0:
                        _lv_prices[sl.strategy.symbol] = pos.avg_entry_price

            _lv_positions = self._get_open_positions_summary()
            _regime_cap   = self._leverage_guard.get_regime_leverage_cap(self._current_regime)
            _corr         = self._leverage_guard.check_correlated_risk(
                _lv_positions, _lv_prices, self.total_capital
            )
            _liq_warns    = self._leverage_guard.check_liquidation_safety(
                _lv_positions, _lv_prices, self.total_capital
            )

            # Liquidation label: three distinct states so the reason is explicit.
            _has_leveraged = any(p["leverage"] > 1 for p in _lv_positions)
            if not _has_leveraged:
                _liq_label = "NONE (all positions ≤ 1x leverage)"
            elif not _liq_warns:
                _liq_label = "NONE"
            else:
                worst = min(_liq_warns, key=lambda w: w["distance_pct"])
                _liq_label = (
                    f"⚠ {worst['severity']} — "
                    f"{worst['strategy']} {worst['symbol']} "
                    f"{worst['distance_pct']:.1f}% from liquidation"
                )

            lines.append(f"  Regime leverage cap  : {_regime_cap}x ({self._current_regime})")
            lines.append(f"  Liquidation risk     : {_liq_label}")
            lines.append(f"  ── Correlated Exposure ──")

            if _corr:
                for sym, data in _corr.items():
                    n         = len(data["strategies"])
                    warn_flag = "  ⚠ EXCEEDS 15% THRESHOLD" if data["warning"] else ""
                    lines.append(
                        f"  {sym:<20}: {n} {'strategy' if n == 1 else 'strategies'} | "
                        f"{data['exposure_pct']:.1f}% of capital | "
                        f"max loss {data['max_combined_loss_pct']:.1f}%{warn_flag}"
                    )
            else:
                lines.append("  No open positions.")

        except Exception as _lv_err:
            # Never let a leverage-guard failure crash the whole summary.
            # Log the error but still render the separator so the section is
            # visually present and the operator knows to investigate.
            logger.warning(f"[Portfolio] LeverageGuard summary error: {_lv_err}")
            lines.append(f"  [error computing leverage guard data — see logs]")

        lines.extend([
            "─" * 62,
            "═" * 62,
        ])
        return "\n".join(lines)

    # ── State export (for dashboard) ────────────────────────────────────────

    def export_state(self, strategy_dfs: Optional[dict] = None) -> dict:
        """
        Return a JSON-serialisable snapshot of the full portfolio state.
        Called by main.py after each candle to feed the dashboard.
        """
        from datetime import datetime, timezone

        total_equity = self._compute_total_equity(strategy_dfs)
        allocs       = REGIME_ALLOCATIONS[self._current_regime]
        reading      = self._current_reading

        strategies: dict = {}
        all_trades:  list = []

        for sname, slot in self._slots.items():
            price  = self._last_prices.get(sname)
            eq     = slot.equity_at(price) if price else slot.equity
            ret    = (eq - slot.capital) / slot.capital * 100 if slot.capital > 0 else 0
            w      = allocs.get(slot.bucket_key, 0.0) * 100

            # Open position snapshot
            pos_data = None
            if slot.simulator.position:
                p      = slot.simulator.position
                upnl   = p.unrealized_pnl(price)   if price else 0.0
                upnl_p = p.unrealized_pnl_pct(price) if price else 0.0
                pos_data = {
                    "side":            p.side,
                    "quantity":        round(p.quantity, 8),
                    "avg_entry_price": round(p.avg_entry_price, 4),
                    "total_cost":      round(p.total_cost, 2),
                    "entry_time":      p.entry_time.isoformat(),
                    "stop_loss":       round(p.stop_loss, 4)    if p.stop_loss    else None,
                    "take_profit":     round(p.take_profit, 4)  if p.take_profit  else None,
                    "unrealized_pnl":  round(upnl, 2),
                    "unrealized_pnl_pct": round(upnl_p, 2),
                    "current_price":   round(price, 4) if price else None,
                }

            trades   = slot.simulator.trade_history
            wins     = [t for t in trades if t.pnl > 0]
            win_rate = len(wins) / len(trades) * 100 if trades else 0.0
            total_pnl = sum(t.pnl for t in trades)

            strategies[sname] = {
                "capital":    round(slot.capital, 2),
                "equity":     round(eq, 2),
                "return_pct": round(ret, 2),
                "active":     slot.active,
                "weight_pct": round(w, 1),
                "trades":     len(trades),
                "win_rate":   round(win_rate, 1),
                "total_pnl":  round(total_pnl, 2),
                "position":   pos_data,
                "current_price": round(price, 4) if price else None,
            }

            for t in trades:
                all_trades.append({
                    "strategy":    sname,
                    "exit_time":   t.exit_time.isoformat(),
                    "symbol":      t.symbol,
                    "side":        t.side,
                    "entry_price": round(t.entry_price, 4),
                    "exit_price":  round(t.exit_price, 4),
                    "pnl":         round(t.pnl, 2),
                    "pnl_pct":     round(t.pnl_pct, 2),
                    "fees_paid":   round(t.fees_paid, 4),
                    "exit_reason": t.exit_reason,
                    "is_partial":  t.is_partial,
                })

        all_trades.sort(key=lambda x: x["exit_time"], reverse=True)

        total_return_pct = (
            (total_equity - self.total_capital) / self.total_capital * 100
            if self.total_capital > 0 else 0.0
        )

        return {
            "updated_at":      datetime.now(timezone.utc).isoformat(),
            "mode":            config.TRADING_MODE,
            "live_executor":   self.live_executor.exchange_id if self.live_executor else None,
            "symbol":          self.symbol,
            "timeframe":       self.timeframe,
            "candle_count":    self._candle_count,
            "regime":          self._current_regime,
            "regime_confidence": round(reading.confidence * 100, 1) if reading else 0,
            "regime_ema50":    round(reading.ema50,    2) if reading else None,
            "regime_ema200":   round(reading.ema200,   2) if reading else None,
            "regime_rsi":      round(reading.rsi,      1) if reading else None,
            "regime_atr_pct":  round(reading.atr_pct,  3) if reading else None,
            "circuit_breaker": self.circuit_breaker.state.value,
            "circuit_breaker_drawdown_pct": round(
                self.circuit_breaker.current_drawdown_pct(total_equity), 2
            ),
            "size_multiplier": round(self.circuit_breaker.size_multiplier(), 2),
            "total_capital":   round(self.total_capital, 2),
            "total_equity":    round(total_equity, 2),
            "total_return_pct": round(total_return_pct, 2),
            "strategies":      strategies,
            "recent_trades":   all_trades[:30],
        }

    # ── Persistent trade flusher ─────────────────────────────────────────────

    def flush_new_trades(self) -> list[dict]:
        """
        Return any trades that closed since the last call to this method.

        Each returned dict is ready to pass to dashboard.trade_logger.log_trade().
        Internally tracks a per-slot count so every trade is only returned once,
        no matter how many times this method is called.

        Usage in main.py:
            new_trades = pm.flush_new_trades()
            for t in new_trades:
                log_trade(t)
        """
        new: list[dict] = []
        for sname, slot in self._slots.items():
            history    = slot.simulator.trade_history
            prev_count = self._logged_trade_counts.get(sname, 0)
            if len(history) > prev_count:
                for trade in history[prev_count:]:
                    new.append({
                        "strategy":    sname,
                        "symbol":      trade.symbol,
                        "side":        trade.side,
                        "entry_price": round(trade.entry_price, 4),
                        "exit_price":  round(trade.exit_price,  4),
                        "quantity":    round(trade.quantity,     8),
                        "pnl":         round(trade.pnl,          2),
                        "pnl_pct":     round(trade.pnl_pct,      4),
                        "fees_paid":   round(trade.fees_paid,    4),
                        "exit_reason": trade.exit_reason,
                        "entry_time":  trade.entry_time.isoformat(),
                        "exit_time":   trade.exit_time.isoformat(),
                        "is_partial":  trade.is_partial,
                    })
                self._logged_trade_counts[sname] = len(history)
        return new

    # ── Crash recovery checkpoint ────────────────────────────────────────────

    # Checkpoint lives next to the other dashboard data files
    _CHECKPOINT_FILE = (
        Path(__file__).parent.parent / "dashboard" / "data" / "portfolio_checkpoint.json"
    )

    def save_checkpoint(self) -> None:
        """
        Save the full portfolio state to disk after every candle.

        What is saved:
          - total_capital (cumulative deposits)
          - candle_count, current_regime
          - circuit breaker peak equity (so drawdown calculation survives restart)
          - per-strategy: balance, open position (entry price, qty, SL/TP, etc.)

        What is NOT saved (intentionally):
          - trade_history  — already in SQLite via flush_new_trades()
          - strategy warm-up indicators — recalculated naturally on next candle
        """
        from datetime import datetime, timezone
        data = {
            "saved_at":              datetime.now(timezone.utc).isoformat(),
            "total_capital":         self.total_capital,
            "candle_count":          self._candle_count,
            "current_regime":        self._current_regime,
            "circuit_breaker_peak":  self.circuit_breaker._peak_equity,
            "slots": {
                sname: slot.simulator.get_checkpoint()
                for sname, slot in self._slots.items()
            },
        }
        # Persist start-of-day equity so restarts within the same UTC day
        # continue from the correct daily loss baseline (not today's open equity).
        _dg = getattr(self, '_daily_guard', None)
        if _dg is not None:
            data["daily_loss_guard"] = _dg.save_state()
        path = self._CHECKPOINT_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(path)   # atomic write — no half-written file on crash

    def _inject_atr_stop_if_missing(
        self,
        sname: str,
        slot,
        strategy_dfs: dict,
    ) -> None:
        """
        Post-restore guard: if a position has no stop loss and the strategy
        uses ATR trailing stops, compute ATR on the current df and inject
        a sensible floor stop.

        This handles the case where a position was opened before ATR stops
        were implemented — the checkpoint has no SL, but the current code
        expects one.  Strategies without ATR config (DCA, GridTrading, VWAP,
        MeanReversion) are silently skipped — they manage stops differently.

        Only injected when:
          - position.stop_loss is None (already has a SL → untouched)
          - strategy has _atr_trail_mult or _ATR_TRAIL_MULT attribute
          - a valid OHLCV df with enough rows is available
        """
        pos = slot.simulator.position
        if pos is None or pos.stop_loss is not None:
            return  # Nothing to do — no position, or SL already set

        strategy = slot.strategy

        # Duck-type for ATR config: instance attrs (DualMomentum/Supertrend/Breakout)
        # or class-level constants (TrendFollowing uses uppercase names).
        mult = (
            getattr(strategy, "_atr_trail_mult",  None)
            or getattr(strategy, "_ATR_TRAIL_MULT", None)
        )
        if mult is None:
            return  # Strategy doesn't use ATR stops → skip (DCA, Grid, VWAP, etc.)

        period = int(
            getattr(strategy, "_atr_trail_period",  None)
            or getattr(strategy, "_ATR_TRAIL_PERIOD", 14)
        )

        df = strategy_dfs.get(sname)
        min_rows = period + 5
        if df is None or len(df) < min_rows:
            logger.warning(
                f"[{sname}] Cannot inject ATR SL on restore — "
                f"no df or insufficient rows "
                f"(need {min_rows}, got {len(df) if df is not None else 0})"
            )
            return

        import ta as _ta
        try:
            atr_series = _ta.volatility.AverageTrueRange(
                high=df["high"], low=df["low"], close=df["close"],
                window=period,
            ).average_true_range()
            atr = float(atr_series.iloc[-1])
        except Exception as exc:
            logger.warning(f"[{sname}] ATR calculation failed on restore: {exc}")
            return

        if pd.isna(atr) or atr <= 0:
            logger.warning(
                f"[{sname}] ATR SL inject skipped — "
                f"insufficient data (ATR={atr}, rows={len(df)})"
            )
            return

        entry = pos.avg_entry_price
        if pos.side == "long":
            pos.stop_loss = entry - (mult * atr)
        else:
            pos.stop_loss = entry + (mult * atr)

        # Arm trailing stop if the old checkpoint didn't have it set.
        # (Positions opened before trailing-stop code shipped may lack these.)
        if not pos.trailing_sl:
            pos.trailing_sl  = True
            pos.trail_sl_pct = (mult * atr) / entry if entry > 0 else 0.03

        logger.warning(
            f"[{sname}] ATR SL injected on restore | "
            f"Entry={entry:.2f} | ATR({period})={atr:.2f} | "
            f"Mult={mult}× | SL={pos.stop_loss:.2f}"
        )

    def load_checkpoint(self, strategy_dfs: dict | None = None) -> bool:
        """
        Try to restore state from the last saved checkpoint.

        Must be called AFTER initialize() so that all StrategySlots exist.
        Returns True if a checkpoint was found and loaded, False if starting fresh.

        Args:
            strategy_dfs: Optional {strategy_name: ohlcv_df} dict — the same
                dict passed to run_candle().  When provided, any restored open
                position that has no stop loss will have an ATR-based SL
                injected automatically (covers positions opened before ATR
                trailing stops were implemented).

        On success:
          - Each simulator's balance and open position are restored
          - Circuit breaker peak is restored (drawdown tracking continues)
          - total_capital reflects real accumulated capital (not config default)
          - DCA/strategy sync works automatically because position flag is restored
        """
        path = self._CHECKPOINT_FILE
        if not path.exists():
            logger.info("[Portfolio] No checkpoint found — starting fresh.")
            return False

        try:
            data = json.loads(path.read_text())

            self.total_capital   = data.get("total_capital",  self.total_capital)
            self._candle_count   = data.get("candle_count",   0)
            self._current_regime = data.get("current_regime", self._current_regime)

            # Restore circuit breaker peak so drawdown % is correct after restart
            peak = data.get("circuit_breaker_peak")
            if peak:
                self.circuit_breaker._peak_equity = peak

            # Restore daily loss guard SOD equity (same-day restart only)
            _dg = getattr(self, '_daily_guard', None)
            if _dg is not None:
                dlg_data = data.get("daily_loss_guard")
                if dlg_data:
                    _dg.restore_state(dlg_data)

            # Restore each strategy simulator + sync strategy internal state
            slot_data = data.get("slots", {})
            restored_positions = 0
            for sname, slot in self._slots.items():
                if sname in slot_data:
                    slot.simulator.restore_checkpoint(slot_data[sname])

                    # ── Re-enabled strategy balance repair ────────────────────
                    # Scenario: a strategy was previously allocated 0% (saved
                    # checkpoint with balance=0, no position, no trades). The
                    # user then changes the regime allocation to give it capital
                    # (e.g. MeanReversion goes from 0% → 5%), and on restart
                    # initialize() creates the simulator with the new capital but
                    # restore_checkpoint() immediately overwrites balance=0 from
                    # the stale checkpoint.
                    #
                    # Detection: balance exactly 0, flat (no position), slot has
                    # positive capital from initialize(), and no fees were ever
                    # paid (meaning the strategy was genuinely never active rather
                    # than having traded its capital down to zero).
                    #
                    # Fix: reset balance to the capital initialize() allocated.
                    # This is safe — a strategy that truly lost all its capital
                    # through trading would have non-zero total_fees_paid.
                    if (
                        slot.simulator.balance == 0.0
                        and slot.simulator.position is None
                        and slot.simulator.total_fees_paid == 0.0
                        and slot.capital > 0.0
                    ):
                        slot.simulator.balance = slot.capital
                        logger.info(
                            f"[Portfolio]   ↩ {sname}: was 0% allocation in checkpoint — "
                            f"balance restored to allocated capital ${slot.capital:,.2f}"
                        )

                    pos = slot.simulator.position

                    if pos:
                        restored_positions += 1

                        # ── ATR SL injection ──────────────────────────────────
                        # If the checkpoint has no stop loss (position opened
                        # before ATR trailing stops were implemented), inject one
                        # now using the current candle's ATR.  Non-ATR strategies
                        # (DCA, Grid, VWAP) are silently skipped by the helper.
                        if pos.stop_loss is None and strategy_dfs:
                            self._inject_atr_stop_if_missing(sname, slot, strategy_dfs)

                        sl_str = f"SL ${pos.stop_loss:,.2f}" if pos.stop_loss else "no SL"
                        logger.info(
                            f"[Portfolio]   ↩ {sname}: restored OPEN {pos.side} | "
                            f"Entry ${pos.avg_entry_price:,.2f} | Qty {pos.quantity:.6f} | {sl_str}"
                        )
                        # ── Fix DCA double-entry bug ──────────────────────────
                        # DCA's _entries list is empty on restart so it thinks
                        # it's flat. Without this it would place a new base
                        # order on top of the restored position immediately.
                        if isinstance(slot.strategy, DCAStrategy):
                            slot.strategy.restore_position_from_checkpoint(pos)
                    else:
                        logger.info(
                            f"[Portfolio]   ↩ {sname}: restored | "
                            f"Balance ${slot.simulator.balance:,.2f} | flat"
                        )

            logger.info(
                f"[Portfolio] ✅ Checkpoint restored from {data.get('saved_at', '?')} | "
                f"Capital ${self.total_capital:,.0f} | "
                f"Regime {self._current_regime} | "
                f"Open positions: {restored_positions}"
            )
            return True

        except Exception as e:
            logger.warning(
                f"[Portfolio] ⚠️ Checkpoint load failed ({e}) — starting fresh. "
                f"Previous session data is safe in SQLite."
            )
            return False

    def replay_missed_candles(
        self,
        strategy_dfs: dict[str, pd.DataFrame],
        checkpoint_time_iso: str,
    ) -> int:
        """
        Process candles that occurred while the bot was offline.

        For each missed candle (newer than checkpoint_time), runs
        tick_ohlcv_candle(high, low, close) on every simulator that has an
        open position.  This means:
          - A stop-loss that should have fired while offline fires at the
            correct SL price, not at whatever price exists on restart.
          - A take-profit hit mid-downtime is correctly captured.
          - Trailing TP peak is updated from the candle HIGHs.

        Returns the number of missed candles processed.
        """
        from datetime import datetime, timezone

        try:
            cutoff = datetime.fromisoformat(checkpoint_time_iso)
            if cutoff.tzinfo is None:
                cutoff = cutoff.replace(tzinfo=timezone.utc)
        except Exception:
            return 0

        # Use the first available df to find missed candle timestamps
        sample_df = next(
            (df for df in strategy_dfs.values() if df is not None and not df.empty),
            None,
        )
        if sample_df is None:
            return 0

        missed = sample_df[sample_df.index > cutoff]
        if missed.empty:
            return 0

        # Exclude the very last candle — that's the "current" candle and will
        # be processed normally by run_candle() right after this call.
        missed = missed.iloc[:-1]
        if missed.empty:
            return 0

        n = len(missed)
        logger.info(
            f"[Portfolio] ⏩ Replaying {n} missed candle(s) "
            f"(from {missed.index[0]} to {missed.index[-1]})"
        )

        for ts, row in missed.iterrows():
            high  = float(row["high"])
            low   = float(row["low"])
            close = float(row["close"])
            for sname, slot in self._slots.items():
                if slot.simulator.position is not None:
                    slot.simulator.tick_ohlcv_candle(high, low, close)

            # Flush any trades that closed during replay to SQLite
            # (caller should do this, but safe to note here)

        # Log what's left after replay
        for sname, slot in self._slots.items():
            pos = slot.simulator.position
            if pos:
                logger.info(
                    f"[Portfolio]   After replay — {sname} still OPEN | "
                    f"Balance ${slot.simulator.balance:,.2f}"
                )

        return n

    # ── Correlation helper ──────────────────────────────────────────────────

    def _get_open_positions_summary(self) -> list[dict]:
        """
        Return a snapshot of every currently open position across all strategy slots.

        Used by the LeverageGuard to check liquidation proximity and correlated
        symbol exposure without needing direct access to the simulator internals.

        Returns:
            List of dicts, one per open position:
            {
                strategy:  str    — slot name (e.g. "Breakout")
                symbol:    str    — trading pair (e.g. "BTC/USDT")
                entry:     float  — average entry price (VWAP across DCA safety orders)
                qty:       float  — total quantity held
                leverage:  int    — leverage the position was opened with
                side:      str    — "long" or "short"
                stop_loss: float|None — current stop-loss price (None if not set)
            }
            Empty list if no positions are currently open.
        """
        result = []
        for slot_name, slot in self._slots.items():
            pos = slot.simulator.position
            if pos is None:
                continue
            result.append({
                "strategy":  slot_name,
                "symbol":    slot.strategy.symbol,
                "entry":     pos.avg_entry_price,
                "qty":       pos.quantity,
                # Use position.leverage (set at open time) rather than
                # slot.strategy.leverage which may be a default/class attribute.
                "leverage":  pos.leverage,
                "side":      "short" if pos.is_short else "long",
                "stop_loss": pos.stop_loss,
            })
        return result

    def _symbol_exposure_pct(self, symbol: str) -> float:
        """
        What % of total portfolio equity is currently held in open positions
        for the given symbol, across ALL strategy slots.

        Used to prevent two strategies from simultaneously opening large long
        positions in the same coin (e.g. DCA + TrendFollowing both trading
        BTC/USDT would double the effective BTC exposure).

        Logic:
          - Sum unrealised position values (quantity × last_price) for every slot
            whose strategy.symbol matches the requested symbol.
          - Divide by total portfolio equity (sum of all slot get_equity() calls).
          - Returns 0.0 if no prices are known yet (warm-up) or no positions open.

        Example:
          DCA holds 0.3 BTC worth $9,000 out of $20,000 portfolio → 45%.
          If cap is 40%, TrendFollowing's BTC BUY would be blocked.
        """
        total_equity   = 0.0
        sym_pos_value  = 0.0

        for sname, slot in self._slots.items():
            last_px = self._last_prices.get(sname, 0.0)
            if last_px <= 0:
                continue
            total_equity += slot.simulator.get_equity(last_px)
            if slot.strategy.symbol == symbol and slot.simulator.position is not None:
                sym_pos_value += slot.simulator.position.quantity * last_px

        if total_equity <= 0:
            return 0.0
        return (sym_pos_value / total_equity) * 100.0

    # ── Strategy builder ────────────────────────────────────────────────────

    @staticmethod
    def _build_strategy(name: str, symbol: str) -> BaseStrategy:
        """Build a strategy instance with production-grade defaults."""
        tf = config.TIMEFRAME

        # ── DCA instance architecture — READ THIS if you see duplicate "DCA ADD" logs ──
        #
        # INSTANCE COUNT: exactly ONE DCAStrategy instance and ONE PaperTrading simulator.
        #
        # How this is guaranteed:
        #   - STRATEGY_KEYS contains "DCA" only once (line ~137).
        #   - _slots is a plain dict[str, StrategySlot] keyed by strategy name.
        #     Dict keys are unique — "DCA" can only map to one slot.
        #   - The slot-creation loop (line ~271) does one pass: one DCAStrategy
        #     instance, one PaperTrading(initial_balance=capital) per name.
        #
        # CAPITAL ALLOCATION: one DCA simulator receives total_capital × regime_weight.
        #   Regime weights (from REGIME_ALLOCATIONS, post May-2025 tuning):
        #     STRONG_BULL 15% | BULL 15% | RANGE 20% | VOLATILE 45% | BEAR 56% | CRASH 79%
        #   On $100k total capital, that is $15k–$79k depending on regime.
        #   The DCA strategy then sizes each base/safety order from this slice
        #   (base_amount=200, safety_scale=1.5 × 5 safety orders → max $3,050 per full cycle).
        #
        # WHY YOU SEE TWO "DCA ADD" LOG LINES ON THE SAME CANDLE (NOT a bug):
        #   simulator.py's _handle_buy() logs "DCA ADD" whenever it ADDS TO AN EXISTING
        #   POSITION (self.position is not None + BUY signal).  This code path is shared
        #   by ALL strategies — the label "DCA ADD" is the simulator's generic term for
        #   "accumulating into an open position", not a DCA-strategy-specific term.
        #
        #   Two simultaneous "DCA ADD" entries at the same price with different balances
        #   (e.g. $4,030 and $9,503) means TWO DIFFERENT STRATEGY SIMULATORS both had
        #   open positions and both fired a BUY on that candle — for example:
        #     • DCA simulator    (larger balance)  → safety order triggered by price drop
        #     • VWAP/Grid/etc.   (smaller balance) → re-entry signal in its own cycle
        #   Each simulator is fully isolated; their balances are independent.
        #
        # COMBINED MAX DCA EXPOSURE (single DCA cycle, all safety orders filled):
        #   base_amount=200 + 5 safety orders × martingale scale 1.5:
        #     SO1=300, SO2=450, SO3=675, SO4=1,012, SO5=1,518 → total ≈ $4,155 per cycle
        #   Compounding grows base_amount over time but does not change the structure.
        #
        # CONCLUSION: no double-instance bug exists. If log confusion persists, the fix
        #   is cosmetic: add the strategy name to the "DCA ADD" log in simulator.py
        #   (e.g. "[PAPER] ➕ DCA ADD [{self.position.strategy}] …") so each simulator's
        #   accumulation line is clearly attributable to its owning strategy.
        # ─────────────────────────────────────────────────────────────────────────────
        if name == "DCA":
            return DCAStrategy(
                symbol=symbol, timeframe=tf,
                base_amount=200.0, deviation_pct=2.0,
                safety_scale=1.5, max_safety_orders=5,
                rsi_threshold=42.0, tp1_pct=0.03, tp2_pct=0.06,
                trail_pct=0.025, stop_loss_pct=0.12,
                panic_protection=True, max_hold_candles=336,
                compound=True,
                macd_filter_enabled=True,  # Blocks new cycles when MACD histogram is negative (bearish momentum)
            )
        elif name == "Supertrend":
            is_btc = "BTC" in symbol.upper()
            return SupertrendStrategy(
                symbol=symbol, timeframe=tf,
                # P5: updated params — faster (10) and tighter bands (3.0)
                atr_period=10, multiplier=3.0,
                btc_filter=not is_btc,
            )
        elif name == "MeanReversion":
            return MeanReversionStrategy(
                symbol=symbol, timeframe=tf,
                # v2: StochRSI + BB %B replaces plain RSI + band-touch
                stochrsi_period=14, stochrsi_smooth_k=3, stochrsi_smooth_d=3,
                # P4: crypto-specific thresholds (more extreme = higher quality signals)
                stochrsi_oversold=10.0, stochrsi_overbought=90.0,
                rsi_period=14, rsi_overbought=80.0,
                bb_period=20, bb_std=2.0,
                bb_pct_b_entry=0.05, bb_pct_b_exit=0.95,
                divergence_confirm=False,  # Enable after paper trading validates it
                ema_filter=True, ema_fast=50, ema_slow=200,
                stop_loss_pct=0.04, cooldown_candles=5,
            )
        elif name == "GridTrading":
            return GridTradingStrategy(
                symbol=symbol, timeframe=tf,
                bb_period=20, bb_std=2.0, atr_period=14,
                atr_step_mult=0.75, atr_trend_threshold=2.5,
                grid_levels=10, usdt_per_trade=200.0,
                recalibrate_every=24,
                trailing_grid=True,  # Shift range on breakout instead of going dormant
            )
        elif name == "VWAP":
            return VWAPStrategy(
                symbol=symbol, timeframe=tf,
                vwap_period=24,      # 24h rolling VWAP (matches 1h candles)
                entry_dev_pct=1.5,   # Buy when 1.5% below VWAP
                exit_dev_pct=0.5,    # Exit when 0.5% above VWAP
                rsi_period=14, rsi_entry=50.0, rsi_exit=65.0,
                stop_loss_pct=0.03, atr_period=14,
                volume_filter=True, cooldown_candles=6,
            )
        elif name == "Breakout":
            return BreakoutStrategy(
                symbol=symbol, timeframe=tf,
                lookback=20,
                # P1: raised to 2.0× (research: 1.5× let through too many fake-outs)
                volume_mult=2.0,
                atr_period=14, atr_stop_mult=2.0,
                reward_ratio=2.0, max_leverage=3, mtf_enabled=False,
                # ADX filter: ADX > 25 AND rising (trend strengthening)
                adx_filter=True, adx_period=14, adx_threshold=25.0,
                # Regime filter: only fire in STRONG_BULL / BULL
                regime_filter=True,
            )
        elif name == "TrendFollowing":
            return TrendFollowingStrategy(
                symbol=symbol, timeframe=tf,
                fast_ema=9, slow_ema=21,
                macd_fast=12, macd_slow=26, macd_signal=9,
            )
        elif name == "BearShort":
            return BearShortStrategy(
                symbol=symbol, timeframe=tf,
                supertrend_period=10, supertrend_mult=3.0,
                ema_fast=20, ema_slow=50,
                rsi_period=14, rsi_entry_max=55.0, rsi_exit=70.0,
                atr_period=14, atr_stop_mult=1.5, atr_tp_mult=3.0,
                leverage=1,   # Spot-equivalent — no extra leverage
                # MACD histogram must be negative to confirm bearish momentum
                macd_confirm=True, macd_fast=12, macd_slow=26, macd_signal=9,
            )
        elif name == "VolatilityBreakout":
            return VolatilityBreakoutStrategy(
                symbol=symbol, timeframe=tf,
                k=0.5,              # Classic Larry Williams K factor
                ema_period=50,      # 50-candle EMA trend filter
                atr_period=14,
                min_atr_pct=0.005,  # Minimum 0.5% expected move (covers fees)
                stop_loss_pct=0.03, # 3% hard stop
                regime_filter=True, # Only BULL / STRONG_BULL / RANGE
            )
        elif name == "DualMomentum":
            return DualMomentumStrategy(
                symbol=symbol, timeframe=tf,
                universe_symbols=["BTC/USDT", "ETH/USDT", "BNB/USDT"],
                lookback=21,         # 21-candle momentum lookback
                rebalance_every=5,   # Re-rank every 5 candles to reduce churn
                regime_filter=True,  # Block buys in CRASH regime
            )
        else:
            raise ValueError(f"Unknown strategy: {name}")
