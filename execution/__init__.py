"""
execution/ — Live exchange execution layer.

This package bridges the gap between paper trading (simulation) and live
trading (real exchange orders).  It is designed so the rest of the bot
never needs to know whether it is running in paper or live mode — the
PortfolioManager just passes signals to execute_signal() as always; the
paper simulator runs unconditionally for equity tracking, and the
CCXTExecutor additionally fires real orders when TRADING_MODE == "live".

Modules
───────
  ccxt_executor.py  — CCXTExecutor: places real orders via any CCXT exchange.
  order_tracker.py  — OrderTracker: persistent JSONL ledger of all live orders.

Quick-start
───────────
    from execution import CCXTExecutor, OrderTracker

    tracker  = OrderTracker(log_path="logs/live_orders.jsonl")
    executor = CCXTExecutor(
        exchange_id = "binance",
        api_key     = "your_key",
        api_secret  = "your_secret",
        symbol      = "BTC/USDT",
        tracker     = tracker,
    )

    # Then pass to PortfolioManager:
    pm = PortfolioManager(total_capital=10_000, live_executor=executor)
"""

from execution.ccxt_executor import CCXTExecutor, ExecutionResult
from execution.order_tracker  import OrderTracker, TrackedOrder

__all__ = [
    "CCXTExecutor",
    "ExecutionResult",
    "OrderTracker",
    "TrackedOrder",
]
