"""
main.py — Entry point for the crypto trading bot.

SINGLE-STRATEGY MODE
──────────────────────
    python main.py                              # Run once with default strategy (DCA)
    python main.py --strategy supertrend        # Run once with Supertrend
    python main.py --strategy supertrend --symbol SOL/USDT
    python main.py --strategy meanrev
    python main.py --strategy grid
    python main.py --strategy dca --loop        # Continuous loop

PORTFOLIO MODE  (Phase D + E — all 6 strategies simultaneously)
──────────────────────────────────────────────────────────────
    python main.py --portfolio                  # Run portfolio once
    python main.py --portfolio --loop           # Continuous portfolio loop

Available strategies (single-strategy mode):
    dca         Dollar Cost Averaging  — price-deviation safety orders, tranche exits
    supertrend  Supertrend             — ATR-based trend with dynamic trailing stop
    trend       Trend Following        — EMA crossover + MACD confirmation
    meanrev     Mean Reversion         — RSI + Bollinger Bands
    grid        Grid Trading           — BB adaptive range + ATR step size
    breakout    Breakout               — volume-confirmed S/R breakout with MTF + sentiment
"""

import sys
import time
import argparse
from loguru import logger

import config
from core.exchange import create_exchange
from core.data_fetcher import fetch_ohlcv, is_new_candle
from paper_trading.simulator import PaperTrading
from strategies.base import BaseStrategy
from strategies.dca import DCAStrategy
from strategies.supertrend import SupertrendStrategy
from strategies.trend_following import TrendFollowingStrategy
from strategies.mean_reversion import MeanReversionStrategy
from strategies.grid_trading import GridTradingStrategy
from strategies.breakout import BreakoutStrategy

# Phase D + E
from portfolio.manager import PortfolioManager
from portfolio.daily_loss_guard import DailyLossGuard
from portfolio.reconciler import reconcile_on_startup

# Notifications (best-effort — bot never crashes if Telegram fails)
from notifications.telegram import get_notifier

# Dashboard state (best-effort — never crash the bot if dashboard writes fail)
try:
    from dashboard.state import write_paper_state, write_bot_status
    from dashboard.trade_logger import log_trade
    _DASHBOARD_ENABLED = True
except ImportError:
    _DASHBOARD_ENABLED = False


# ─── Logging setup ────────────────────────────────────────────────────────────

def setup_logging():
    """Configure loguru to log to both console and a rotating log file."""
    logger.remove()
    logger.add(
        sys.stdout,
        level=config.LOG_LEVEL,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
               "<level>{level: <8}</level> | "
               "<cyan>{name}</cyan>:<cyan>{function}</cyan> | "
               "<level>{message}</level>",
        colorize=True,
    )
    logger.add(
        config.LOG_FILE,
        level="DEBUG",
        rotation="10 MB",
        retention="7 days",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function} | {message}",
    )


# ─── Strategy factory ─────────────────────────────────────────────────────────

def build_strategy(name: str, symbol: str = None) -> BaseStrategy:
    """
    Create and return a strategy instance by name.

    To tune a strategy's parameters, edit the values below.

    Args:
        name:   Strategy name (dca, supertrend, trend, meanrev, grid).
        symbol: Override trading pair (e.g. "SOL/USDT"). Defaults to config.TRADING_PAIR.
    """
    name = name.lower()
    symbol = symbol or config.TRADING_PAIR

    if name == "dca":
        return DCAStrategy(
            symbol=symbol,
            timeframe=config.TIMEFRAME,
            base_amount=100.0,          # Base order: $100 USDT
            deviation_pct=2.0,          # Safety order triggers at 2% drop from base
            safety_scale=1.5,           # Each safety order 1.5× larger (martingale)
            max_safety_orders=5,        # Up to 5 safety orders
            rsi_threshold=42.0,         # Only add if RSI ≤ 42 (oversold)
            tp1_pct=0.03,               # First tranche exit at +3%
            tp2_pct=0.06,               # Second tranche exit at +6%
            trail_pct=0.025,            # Trailing TP: sell if drops 2.5% from peak
            stop_loss_pct=0.12,         # Hard stop at -12% from avg entry
            panic_protection=True,      # Require 2 consecutive SL closes
            max_hold_candles=336,       # Time exit after 14 days (1h candles)
            compound=True,              # Reinvest 50% of profit
        )

    elif name == "supertrend":
        # BTC filter is auto-enabled for non-BTC pairs
        is_btc = "BTC" in symbol.upper()
        return SupertrendStrategy(
            symbol=symbol,
            timeframe=config.TIMEFRAME,
            atr_period=14,       # Standard ATR period
            multiplier=3.5,      # Wider bands for crypto volatility
            btc_filter=not is_btc,
        )

    elif name == "trend":
        return TrendFollowingStrategy(
            symbol=symbol,
            timeframe=config.TIMEFRAME,
            fast_ema=9,
            slow_ema=21,
            macd_fast=12,
            macd_slow=26,
            macd_signal=9,
        )

    elif name == "meanrev":
        return MeanReversionStrategy(
            symbol=symbol,
            timeframe=config.TIMEFRAME,
            rsi_period=14,
            rsi_oversold=35.0,
            rsi_overbought=65.0,
            bb_period=20,
            bb_std=2.0,
        )

    elif name == "grid":
        return GridTradingStrategy(
            symbol=symbol,
            timeframe=config.TIMEFRAME,
            bb_period=20,               # Bollinger Band period for range detection
            bb_std=2.0,                 # 2σ bands — standard Bollinger setting
            atr_period=14,              # ATR period for step sizing
            atr_step_mult=0.75,         # Step = 0.75× ATR (slightly sub-ATR for more trades)
            atr_trend_threshold=2.5,    # ATR% above this = trending market → grid pauses
            grid_levels=10,             # Up to 10 buy levels
            usdt_per_trade=200.0,       # $200 per grid level (max exposure = $2,000)
            recalibrate_every=24,       # Rebuild grid every 24 candles (daily on 1h)
        )

    elif name == "breakout":
        return BreakoutStrategy(
            symbol=symbol,
            timeframe=config.TIMEFRAME,
            lookback=20,          # Scan last 20 candles for support/resistance
            volume_mult=1.5,      # Volume must be 1.5× the average
            atr_period=14,
            atr_stop_mult=2.0,    # Stop = 2 ATRs from entry
            reward_ratio=2.0,     # Take profit = 2× the stop distance (1:2 RR)
            max_leverage=3,       # Max 3x futures leverage (dynamically reduced)
            mtf_enabled=True,     # Require 4h Supertrend confirmation
            adx_filter=True,      # Only trade in genuine trending markets (ADX > 25)
            adx_period=14,
            adx_threshold=25.0,
        )

    else:
        raise ValueError(
            f"Unknown strategy: '{name}'. "
            f"Choose from: dca, supertrend, trend, meanrev, grid, breakout"
        )


# ─── Market snapshot ──────────────────────────────────────────────────────────

def print_market_snapshot(df, symbol: str = None):
    """Print a summary of the latest candle."""
    latest = df.iloc[-1]
    prev = df.iloc[-2]
    change = latest["close"] - prev["close"]
    change_pct = (change / prev["close"]) * 100
    label = symbol or config.TRADING_PAIR

    print("\n" + "=" * 55)
    print(f"  MARKET SNAPSHOT — {label}  [{config.TIMEFRAME}]")
    print("=" * 55)
    print(f"  Open   : {latest['open']:>12.4f}")
    print(f"  High   : {latest['high']:>12.4f}")
    print(f"  Low    : {latest['low']:>12.4f}")
    print(f"  Close  : {latest['close']:>12.4f}")
    print(f"  Volume : {latest['volume']:>12.2f}")
    print(f"  Change : {change:>+12.4f}  ({change_pct:>+.2f}%)")
    print("=" * 55 + "\n")


# ─── Core run logic ───────────────────────────────────────────────────────────

def run_once(exchange, strategy: BaseStrategy, simulator: PaperTrading):
    """Fetch latest data, run strategy, execute signal on simulator."""
    symbol = strategy.symbol
    df = fetch_ohlcv(exchange, symbol=symbol, timeframe=config.TIMEFRAME)
    current_price = float(df["close"].iloc[-1])

    print_market_snapshot(df, symbol=symbol)

    # BTC filter: if strategy is Supertrend on a non-BTC pair,
    # fetch BTC data first and update the filter before generating signal
    if isinstance(strategy, SupertrendStrategy) and strategy.btc_filter:
        logger.debug("BTC filter: fetching BTC/USDT trend...")
        btc_df = fetch_ohlcv(exchange, symbol="BTC/USDT", timeframe=config.TIMEFRAME)
        btc_trend = strategy.update_btc_trend(btc_df)
        logger.info(f"BTC filter status: {btc_trend.upper()}")

    # Breakout: fetch 4h data for MTF confirmation + sentiment from LunarCrush
    if isinstance(strategy, BreakoutStrategy):
        if strategy.mtf_enabled:
            logger.debug("Breakout MTF: fetching 4h candles for higher timeframe check...")
            htf_df = fetch_ohlcv(exchange, symbol=symbol, timeframe="4h", limit=100)
            htf_dir = strategy.update_htf_trend(htf_df)
            logger.info(f"MTF 4h trend: {'↑ UP' if htf_dir == 1 else '↓ DOWN'}")

        # Sentiment (best-effort — skip silently if unavailable)
        try:
            from core.sentiment import get_coin_sentiment
            coin = symbol.split("/")[0]  # e.g. "BTC" from "BTC/USDT"
            score = get_coin_sentiment(coin)
            strategy.update_sentiment(score)
            logger.info(f"Sentiment [{coin}]: {score:.0f}/100")
        except Exception:
            logger.debug("Sentiment unavailable — skipping. Trades will use neutral sizing.")

    # Check stop-loss / take-profit / trailing TP / time exit on open position
    simulator.tick(current_price)

    # DCA: sync internal state in case tick() auto-closed the position externally
    if isinstance(strategy, DCAStrategy):
        strategy.sync_state(simulator_has_position=simulator.position is not None)

    # Generate signal from strategy
    signal = strategy.generate_signal(df)
    logger.info(f"Signal → {signal}")

    # Execute signal on paper trading simulator
    if signal.is_actionable():
        simulator.execute_signal(signal, current_price)

    # Print portfolio summary
    print(simulator.summary(current_price=current_price))


def run_loop(exchange, strategy: BaseStrategy, simulator: PaperTrading, interval_seconds: int = 30):
    """
    Run the bot continuously, acting on each new candle.
    Press Ctrl+C to stop.
    """
    symbol = strategy.symbol   # Use strategy's symbol (may differ from config default)
    logger.info(
        f"Bot running | Strategy: {strategy.name} | "
        f"Pair: {symbol} | TF: {config.TIMEFRAME} | "
        f"Poll: every {interval_seconds}s | Press Ctrl+C to stop"
    )
    last_candle_ts = None
    df = None

    try:
        while True:
            df = fetch_ohlcv(exchange, symbol=symbol, timeframe=config.TIMEFRAME)

            if is_new_candle(df, last_candle_ts):
                last_candle_ts = df.index[-1]
                logger.info(
                    f"▶ New {config.TIMEFRAME} candle: {last_candle_ts} | "
                    f"Close: {df['close'].iloc[-1]:.4f}"
                )
                run_once(exchange, strategy, simulator)
            else:
                candle_close = df.index[-1]
                logger.debug(f"No new candle (latest: {candle_close}) — waiting...")

            time.sleep(interval_seconds)

    except KeyboardInterrupt:
        logger.info("Bot stopped by user (Ctrl+C).")
        if df is not None:
            current_price = float(df["close"].iloc[-1])
            print(simulator.summary(current_price=current_price))

            # Print full trade history if any trades were made
            trade_df = simulator.trade_history_df()
            if not trade_df.empty:
                print("\n── Trade History ──")
                print(trade_df[["entry_price", "exit_price", "pnl", "pnl_pct", "exit_reason"]].to_string())


# ─── Portfolio mode (Phase D + E) ────────────────────────────────────────────

def run_portfolio_once(
    exchange,
    pm: PortfolioManager,
    symbol: str,
    candle_count: int = 0,
    daily_guard: "DailyLossGuard | None" = None,
    notifier=None,
) -> None:
    """
    Fetch latest data and run one candle across all portfolio strategies.

    Each strategy fetches its own assigned symbol from config.STRATEGY_SYMBOLS.
    BTC/USDT is always fetched for regime detection regardless of strategy symbols.
    """
    tf = config.TIMEFRAME

    # Fetch BTC for regime detection (always needed — BTC is the regime reference)
    btc_df = fetch_ohlcv(exchange, symbol="BTC/USDT", timeframe=tf)

    # ── Per-strategy symbol fetching ─────────────────────────────────────────
    # Each strategy can run on a different coin. We cache fetched dataframes so
    # that two strategies sharing the same symbol only trigger one API call.
    _symbol_cache: dict[str, object] = {}
    strategy_dfs: dict[str, object] = {}

    for sname in PortfolioManager.STRATEGY_KEYS:
        sym = config.STRATEGY_SYMBOLS.get(sname, symbol)
        if sym not in _symbol_cache:
            try:
                _symbol_cache[sym] = fetch_ohlcv(exchange, symbol=sym, timeframe=tf)
            except Exception as e:
                logger.warning(f"[Portfolio] Could not fetch {sym} for {sname}: {e}")
                _symbol_cache[sym] = None
        strategy_dfs[sname] = _symbol_cache[sym]

    # Fetch 4h candles for Breakout's multi-timeframe (MTF) filter.
    # Stored under the reserved key "_htf_4h" so run_candle() can pick it up.
    # Best-effort: a failure here skips MTF confirmation but won't crash the bot.
    try:
        htf_df = fetch_ohlcv(exchange, symbol=symbol, timeframe="4h", limit=100)
        strategy_dfs["_htf_4h"] = htf_df
    except Exception as _htf_err:
        logger.debug(f"4h HTF fetch skipped (non-fatal): {_htf_err}")

    # Attach guard to pm so per-slot checks work inside run_candle()
    if daily_guard is not None:
        pm._daily_guard = daily_guard

    # On first run: initialize, then try to resume previous session
    if candle_count == 0:
        pm.initialize(initial_btc_df=btc_df)

        # ── Crash / disconnect recovery ───────────────────────────────────────
        # 1. Restore balances, open positions, circuit breaker peak from disk.
        # 2. Replay any candles that closed while the bot was offline so that
        #    SL/TP fire at the correct price, not at the restart price.
        # Both steps are best-effort — a failure here starts fresh safely.
        _checkpoint_time = None
        try:
            import json as _json
            _cp_path = pm._CHECKPOINT_FILE
            if _cp_path.exists():
                _checkpoint_time = _json.loads(_cp_path.read_text()).get("saved_at")
        except Exception:
            pass

        try:
            resumed = pm.load_checkpoint(strategy_dfs=strategy_dfs)
            if resumed:
                logger.info(
                    "[Portfolio] 🔄 Resumed from previous session — "
                    "balances and open positions restored."
                )
        except Exception as _e:
            logger.debug(f"Checkpoint load skipped (non-fatal): {_e}")

        # ── Exchange reconciliation (live mode only; no-op in paper mode) ─────
        # Compares internal checkpoint positions against actual Binance Futures
        # positions. Must run AFTER load_checkpoint() and BEFORE the first candle
        # so that any ghost/zombie discrepancies are resolved before orders fire.
        # A network failure here is non-fatal: startup continues with a warning.
        try:
            reconcile_on_startup(pm, exchange)
        except Exception as _recon_err:
            logger.warning(
                f"[Reconciler] Unexpected error during reconciliation "
                f"({type(_recon_err).__name__}: {_recon_err}) — "
                f"continuing startup. Check positions manually."
            )

        # Replay missed candles AFTER checkpoint is loaded (positions restored).
        # NOTE: replay is crash recovery, NOT a dashboard feature. It must run
        # even when _DASHBOARD_ENABLED is False (e.g. fresh server, missing deps).
        # Only the trade *logging* step inside is gated on _DASHBOARD_ENABLED.
        if _checkpoint_time:
            try:
                n_replayed = pm.replay_missed_candles(strategy_dfs, _checkpoint_time)
                if n_replayed:
                    logger.info(
                        f"[Portfolio] ✅ Replayed {n_replayed} missed candle(s) — "
                        f"SL/TP levels applied at correct historical prices."
                    )
                    # Log any trades that fired during replay (dashboard optional)
                    if _DASHBOARD_ENABLED:
                        try:
                            for trade in pm.flush_new_trades():
                                log_trade(trade)
                                logger.info(
                                    f"[TradeLog] 📝 {trade['strategy']} closed during replay | "
                                    f"P&L: ${trade['pnl']:+.2f} | Reason: {trade['exit_reason']}"
                                )
                        except Exception:
                            pass
            except Exception as _e:
                logger.debug(f"Missed candle replay skipped (non-fatal): {_e}")

    print_market_snapshot(btc_df, symbol="BTC/USDT")

    # ── Daily loss guard check ───────────────────────────────────────────────
    # update() handles midnight UTC reset and returns the current tier.
    # Graduated tiers: NORMAL → WARNING (75%) → CAUTIOUS (50%) →
    #                  HALT (block) → REDUCE (close-only) → EMERGENCY (close all)
    # run_candle() reads _daily_guard.size_multiplier() and tier internally.
    current_total_equity = pm._compute_total_equity(strategy_dfs)
    if daily_guard is not None:
        daily_tier = daily_guard.update(current_total_equity)
        loss_pct   = daily_guard.current_loss_pct(current_total_equity)

        if not daily_guard.allows_new_buys():
            # HALT / REDUCE / EMERGENCY — block all new buys
            logger.warning(
                f"[DailyLossGuard] Tier={daily_tier.value} | "
                f"Loss: -{loss_pct:.2f}% today — new buys blocked."
            )
            if notifier and not daily_guard._notified_today:
                notifier.daily_loss_limit_hit(loss_pct, current_total_equity)
                daily_guard._notified_today = True
            pm._daily_loss_block = True
        elif daily_tier.value != "NORMAL":
            # WARNING / CAUTIOUS — sizes reduced, buys still allowed
            logger.info(
                f"[DailyLossGuard] Tier={daily_tier.value} | "
                f"Loss: -{loss_pct:.2f}% today | "
                f"Size mult: {daily_guard.size_multiplier():.0%}"
            )
            pm._daily_loss_block = False
            daily_guard._notified_today = False
        else:
            pm._daily_loss_block = False
            daily_guard._notified_today = False

    # Run all strategies for this candle
    actions = pm.run_candle(btc_df=btc_df, strategy_dfs=strategy_dfs)

    # Log actions
    action_str = " | ".join(f"{k}:{v}" for k, v in actions.items())
    logger.info(f"[Portfolio] Actions → {action_str}")

    # Notify on trades
    if notifier:
        for sname, action in actions.items():
            if action in ("BUY", "SELL"):
                sym = config.STRATEGY_SYMBOLS.get(sname, symbol)
                df  = strategy_dfs.get(sname)
                price = float(df["close"].iloc[-1]) if df is not None and not df.empty else 0.0
                if action == "BUY":
                    slot = pm._slots.get(sname)
                    size = slot.simulator.position.total_cost if (slot and slot.simulator.position) else 0.0
                    notifier.trade_opened(sname, sym, price, size)
                elif action == "SELL":
                    slot = pm._slots.get(sname)
                    if slot and slot.simulator.trade_history:
                        t = slot.simulator.trade_history[-1]
                        notifier.trade_closed(
                            sname, sym, t.entry_price, t.exit_price,
                            t.pnl, t.pnl_pct, t.exit_reason,
                        )

    # Print dashboard
    print(pm.summary(strategy_dfs))

    # Circuit breaker state
    print(pm.circuit_breaker.summary(pm._compute_total_equity(strategy_dfs)))

    # Write state for the web dashboard (best-effort)
    if _DASHBOARD_ENABLED:
        try:
            write_paper_state(pm.export_state(strategy_dfs))
        except Exception as _e:
            logger.debug(f"Dashboard state write failed (non-fatal): {_e}")

        # Persist any newly closed trades to the SQLite trade log
        try:
            for trade in pm.flush_new_trades():
                log_trade(trade)
                logger.info(
                    f"[TradeLog] 📝 {trade['strategy']} {trade['side'].upper()} "
                    f"closed | P&L: ${trade['pnl']:+.2f} ({trade['pnl_pct']:+.2f}%) "
                    f"| Reason: {trade['exit_reason']}"
                )
        except Exception as _e:
            logger.debug(f"Trade log write failed (non-fatal): {_e}")

        # Save full portfolio state for crash recovery.
        # Written atomically so a mid-write crash never corrupts the file.
        try:
            pm.save_checkpoint()
        except Exception as _e:
            logger.debug(f"Checkpoint save failed (non-fatal): {_e}")


def _send_daily_summary(pm: "PortfolioManager", notifier, current_equity: float) -> None:
    """Compile yesterday's trade stats and send to Telegram."""
    try:
        from datetime import date, timedelta
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        all_trades = []
        for slot in pm._slots.values():
            all_trades.extend(slot.simulator.trade_history)

        # Tally trades closed yesterday
        day_trades = [
            t for t in all_trades
            if hasattr(t, 'exit_time') and str(t.exit_time)[:10] == yesterday
        ]
        if not day_trades:
            # Fallback: report on all trades from any recent day
            day_trades = all_trades[-20:] if all_trades else []

        total_pnl  = sum(t.pnl for t in day_trades)
        n_trades   = len(day_trades)
        wins       = sum(1 for t in day_trades if t.pnl > 0)
        win_rate   = wins / n_trades * 100 if n_trades else 0.0

        notifier.daily_summary(
            date=yesterday,
            pnl=total_pnl,
            trades=n_trades,
            win_rate=win_rate,
            equity=current_equity,
        )
    except Exception:
        pass   # Never crash the main loop over a summary


def run_portfolio_loop(
    exchange,
    symbol: str,
    interval_seconds: int = 30,
) -> None:
    """
    Run the full multi-strategy portfolio continuously.
    Press Ctrl+C to stop and print final summary.
    """
    notifier       = get_notifier()
    daily_guard    = DailyLossGuard(portfolio_max_pct=config.DAILY_LOSS_LIMIT_PCT)
    _last_summary_date = None          # Track which UTC date we last sent a summary

    pm = PortfolioManager(
        total_capital=config.PAPER_BALANCE,
        symbol=symbol,
        timeframe=config.TIMEFRAME,
    )

    notifier.bot_started(
        mode=config.TRADING_MODE,
        symbol=symbol,
        balance=config.PAPER_BALANCE,
    )

    logger.info(
        f"Portfolio bot running | "
        f"Pairs: {list(set(config.STRATEGY_SYMBOLS.values()))} | "
        f"TF: {config.TIMEFRAME} | "
        f"Balance: ${config.PAPER_BALANCE:,.0f} | "
        f"Daily loss limit: {config.DAILY_LOSS_LIMIT_PCT:.1f}% | "
        f"Poll: every {interval_seconds}s | Press Ctrl+C to stop"
    )

    last_candle_ts = None
    candle_count   = 0

    try:
        while True:
            df = fetch_ohlcv(exchange, symbol=symbol, timeframe=config.TIMEFRAME)

            if is_new_candle(df, last_candle_ts):
                last_candle_ts = df.index[-1]
                logger.info(
                    f"▶ New {config.TIMEFRAME} candle: {last_candle_ts} | "
                    f"Close: {df['close'].iloc[-1]:.4f}"
                )
                run_portfolio_once(
                    exchange, pm, symbol, candle_count,
                    daily_guard=daily_guard, notifier=notifier,
                )
                candle_count += 1
            else:
                logger.debug(f"No new candle (latest: {df.index[-1]}) — waiting...")

            # Heartbeat + daily P&L summary via Telegram
            try:
                from datetime import date as _date
                eq = pm._compute_total_equity(strategy_dfs if 'strategy_dfs' in dir() else {})
                open_pos = sum(
                    1 for s in pm._slots.values()
                    if s.simulator.position is not None
                )
                notifier.heartbeat(
                    equity=eq, open_positions=open_pos,
                    candle_count=candle_count,
                    regime=pm._current_regime,
                )
                # Daily P&L summary — send once per UTC day (first candle of new day)
                today = _date.today()
                if _last_summary_date is not None and _last_summary_date != today:
                    _send_daily_summary(pm, notifier, eq)
                _last_summary_date = today
            except Exception:
                pass

            # Heartbeat for dashboard
            if _DASHBOARD_ENABLED:
                try:
                    from datetime import datetime, timezone
                    write_bot_status({
                        "updated_at":     datetime.now(timezone.utc).isoformat(),
                        "running":        True,
                        "mode":           "portfolio",
                        "symbol":         symbol,
                        "timeframe":      config.TIMEFRAME,
                        "candle_count":   candle_count,
                        "last_candle_ts": str(last_candle_ts) if last_candle_ts else None,
                    })
                except Exception:
                    pass

            time.sleep(interval_seconds)

    except KeyboardInterrupt:
        logger.info("Portfolio bot stopped by user (Ctrl+C).")
        strategy_dfs = {name: df for name in PortfolioManager.STRATEGY_KEYS}
        try:
            eq = pm._compute_total_equity(strategy_dfs)
            notifier.bot_stopped(reason="Ctrl+C", equity=eq)
        except Exception:
            pass
        print(pm.summary(strategy_dfs))
        print(pm.circuit_breaker.summary(pm._compute_total_equity(strategy_dfs)))

        # Explicit final checkpoint so restart picks up cleanly from this moment
        if _DASHBOARD_ENABLED:
            try:
                pm.save_checkpoint()
                logger.info("[Portfolio] 💾 Final checkpoint saved — safe to restart.")
            except Exception:
                pass


# ─── Entry point ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Crypto Trading Bot — Paper Trading Mode",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py                            Run DCA once (default)
  python main.py --strategy supertrend      Run Supertrend once
  python main.py --strategy dca --loop      Run DCA continuously
  python main.py --strategy grid --loop --interval 60
  python main.py --portfolio                Run all 6 strategies once (Phase D+E)
  python main.py --portfolio --loop         Run all 6 strategies continuously
        """
    )
    parser.add_argument(
        "--strategy",
        type=str,
        default="dca",
        choices=["dca", "supertrend", "trend", "meanrev", "grid", "breakout"],
        help="Which strategy to run in single-strategy mode (default: dca)"
    )
    parser.add_argument(
        "--symbol",
        type=str,
        default=None,
        help="Trading pair override, e.g. SOL/USDT (default: from .env TRADING_PAIR)"
    )
    parser.add_argument(
        "--portfolio",
        action="store_true",
        help="Run all 6 strategies simultaneously with regime detection + Kelly sizing (Phase D+E)"
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Run continuously, acting on each new candle"
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=30,
        help="Polling interval in seconds when using --loop (default: 30)"
    )
    args = parser.parse_args()

    setup_logging()

    # Validate config
    try:
        config.validate()
    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        sys.exit(1)

    # Connect to exchange
    logger.info("Connecting to Binance...")
    exchange = create_exchange()

    symbol = args.symbol or config.TRADING_PAIR

    # ── Portfolio mode (Phase D + E) ────────────────────────────────────────
    if args.portfolio:
        if args.loop:
            run_portfolio_loop(exchange, symbol, interval_seconds=args.interval)
        else:
            pm = PortfolioManager(
                total_capital=config.PAPER_BALANCE,
                symbol=symbol,
                timeframe=config.TIMEFRAME,
            )
            run_portfolio_once(exchange, pm, symbol, candle_count=0)
        return

    # ── Single-strategy mode ─────────────────────────────────────────────────
    logger.info(f"Loading strategy: {args.strategy} | Symbol: {symbol}")
    strategy  = build_strategy(args.strategy, symbol=symbol)
    simulator = PaperTrading(initial_balance=config.PAPER_BALANCE)

    if args.loop:
        run_loop(exchange, strategy, simulator, interval_seconds=args.interval)
    else:
        run_once(exchange, strategy, simulator)


if __name__ == "__main__":
    main()
