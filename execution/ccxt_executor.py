"""
execution/ccxt_executor.py — Live order execution via CCXT.

WHAT THIS DOES
──────────────
CCXTExecutor is a drop-in companion to PaperTrading.  In the
PortfolioManager, both always run in parallel:

  PaperTrading.execute_signal()  → always: tracks equity, drawdown, history
  CCXTExecutor.execute_signal()  → live mode only: fires real exchange orders

This separation means:
  • Kelly sizing and circuit breaker still use paper equity (fast, no API calls).
  • Live fills are best-effort: a transient network error logs an alert but
    never crashes the paper tracking loop.
  • Switching exchanges is one line: change exchange_id="binance" to "okx".

SUPPORTED SIGNAL TYPES
───────────────────────
  BUY  (long, spot)     → market/limit order + optional OCO (SL+TP in one call)
  BUY  (short, futures) → short market order + stop-market SL + TP order
  SELL (close long)     → market/limit sell; cancels any open OCO first
  SELL (close short)    → futures market buy-back; cancels open SL/TP orders

ORDER QUANTITY CALCULATION
──────────────────────────
The signal carries amount_usdt in metadata (set by Kelly sizing in the PM).
We convert that to base-currency qty and round to the exchange's precision:

  qty = amount_usdt / fill_price
  qty = exchange.amount_to_precision(symbol, qty)

For partial SELLs (quantity_pct < 1.0) we take that fraction of the open
position qty that the tracker recorded at entry.

OCO ORDER (spot SL + TP)
────────────────────────
An OCO (One-Cancels-the-Other) order fires EITHER the SL or the TP,
whichever triggers first, and automatically cancels the other leg.
Binance requires both a limit price and a stop-limit price for the SL leg:

  stop_price      = signal.stop_loss
  stop_limit_price = stop_price × (1 − 0.2%)   ← 0.2% slippage allowance

When we SELL, we first cancel any live OCO for that slot before placing
the market exit, to avoid double-selling.

FUTURES SHORTS (BearShort strategy)
────────────────────────────────────
When signal.is_short=True, the executor:
  1. Switches to futures context (exchange.options["defaultType"] = "future")
  2. Places a SHORT market order  (side="sell", reduceOnly=False)
  3. Places a stop-market SL order above entry  (side="buy", stopPrice=SL)
  4. Places a take-profit-market TP order below (side="buy", takeProfitPrice=TP)
  5. Restores spot context

EXCHANGE SUPPORT
────────────────
Any CCXT exchange works — just pass exchange_id="okx", "bybit", "kucoin", etc.
Exchange-specific order type names differ; the executor normalises them:
  Binance futures : stopMarket, takeProfitMarket
  OKX             : stop, conditional
  Bybit           : StopLoss, TakeProfit

We handle the three most popular (Binance/OKX/Bybit). Others fall back to
a plain stop-market if the exchange type isn't recognised.

USAGE
─────
    from execution.ccxt_executor import CCXTExecutor

    executor = CCXTExecutor(
        exchange_id = "binance",      # any CCXT exchange id
        api_key     = "...",
        api_secret  = "...",
        symbol      = "BTC/USDT",
        tracker     = order_tracker,  # optional OrderTracker for persistence
        testnet     = False,          # True to use Binance testnet URLs
    )

    result = executor.execute_signal(signal, current_price, slot_name="DCA")
    if result.success:
        print(f"Filled @ {result.fill_price:.4f} | order_id={result.order_id}")
    else:
        print(f"Execution failed: {result.error}")
"""

import time
from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING
import ccxt
from loguru import logger

from strategies.base import Signal

if TYPE_CHECKING:
    from execution.order_tracker import OrderTracker


# ── Result dataclass ─────────────────────────────────────────────────────────

@dataclass
class ExecutionResult:
    """
    Outcome of a single execute_signal() call.

    Attributes:
        success:       True if the primary order was accepted by the exchange.
        action:        The signal action that was executed ("BUY"/"SELL"/"HOLD").
        slot_name:     Which strategy slot triggered this (e.g. "DCA").
        order_id:      Exchange-assigned primary order ID (empty string if failed).
        fill_price:    Average fill price reported by exchange (0.0 if unknown).
        filled_qty:    Base-currency amount actually filled.
        fee_usdt:      Estimated trading fee in USDT.
        sl_order_id:   ID of the stop-loss order placed after entry (if any).
        tp_order_id:   ID of the take-profit order placed after entry (if any).
        oco_order_id:  ID of the OCO order (Binance spot SL+TP combined).
        is_short:      True if this was a futures short execution.
        error:         Error message if success=False.
        raw_response:  Full exchange response dict for debugging.
    """
    success:       bool
    action:        str
    slot_name:     str  = ""
    order_id:      str  = ""
    fill_price:    float = 0.0
    filled_qty:    float = 0.0
    fee_usdt:      float = 0.0
    sl_order_id:   str  = ""
    tp_order_id:   str  = ""
    oco_order_id:  str  = ""
    is_short:      bool = False
    error:         str  = ""
    raw_response:  dict = field(default_factory=dict)

    def __str__(self) -> str:
        if not self.success:
            return f"[{self.slot_name}] FAILED {self.action}: {self.error}"
        parts = [
            f"[{self.slot_name}] {self.action}",
            f"fill={self.fill_price:.4f}",
            f"qty={self.filled_qty:.6f}",
        ]
        if self.fee_usdt:
            parts.append(f"fee=${self.fee_usdt:.4f}")
        if self.sl_order_id or self.oco_order_id:
            sl_id = self.sl_order_id or self.oco_order_id
            parts.append(f"SL_order={sl_id[:8]}...")
        return " | ".join(parts)


# ── Main executor ─────────────────────────────────────────────────────────────

class CCXTExecutor:
    """
    Live order executor that translates Signal objects into real exchange orders.

    Designed as a companion to PaperTrading — both run in parallel so that
    equity tracking never depends on the live exchange being reachable.

    Args:
        exchange_id:    CCXT exchange identifier, e.g. "binance", "okx", "bybit".
        api_key:        Your exchange API key.
        api_secret:     Your exchange API secret.
        symbol:         Primary trading pair, e.g. "BTC/USDT".
        tracker:        Optional OrderTracker for persistent order logging.
        testnet:        If True, connect to the exchange's sandbox/testnet.
        sandbox_urls:   Override dict for testnet URLs if exchange needs custom setup.
        max_retries:    Number of retry attempts on transient network errors.
        retry_delay_s:  Seconds to wait between retries.
        oco_sl_slip:    Fraction below stop-price for OCO stop-limit leg.
                        e.g. 0.002 = place stop-limit 0.2% below stop-price.
                        Required by Binance; ignored for exchanges that use
                        stop-market orders instead.
    """

    # Exchange families for order type naming
    _BINANCE_FAMILY = {"binance", "binanceus", "binanceusdm", "binancecoinm"}
    _OKX_FAMILY     = {"okx", "okex"}
    _BYBIT_FAMILY   = {"bybit"}

    def __init__(
        self,
        exchange_id:     str,
        api_key:         str,
        api_secret:      str,
        symbol:          str,
        tracker:         Optional["OrderTracker"] = None,
        testnet:         bool  = False,
        max_retries:     int   = 3,
        retry_delay_s:   float = 1.5,
        oco_sl_slip:     float = 0.002,     # 0.2% — Binance OCO stop-limit offset
        passphrase:      str   = "",        # Required by OKX
    ):
        self.exchange_id   = exchange_id.lower()
        self.symbol        = symbol
        self.tracker       = tracker
        self.max_retries   = max_retries
        self.retry_delay_s = retry_delay_s
        self.oco_sl_slip   = oco_sl_slip

        # ── Track per-slot open SL/TP/OCO orders so we can cancel before exit
        # Key: slot_name → {"oco_id": str, "sl_id": str, "tp_id": str, "qty": float}
        self._open_protective_orders: dict[str, dict] = {}

        # ── Build the CCXT exchange instance ──────────────────────────────────
        exchange_class = getattr(ccxt, self.exchange_id, None)
        if exchange_class is None:
            raise ValueError(
                f"Unknown CCXT exchange: '{exchange_id}'. "
                f"Check https://docs.ccxt.com for valid exchange ids."
            )

        params: dict = {
            "apiKey":          api_key,
            "secret":          api_secret,
            "enableRateLimit": True,    # Respect exchange rate limits automatically
            "options":         {"defaultType": "spot"},
        }
        if passphrase:
            params["password"] = passphrase   # OKX requires this

        self.exchange: ccxt.Exchange = exchange_class(params)

        if testnet:
            # Most exchanges expose a set_sandbox_mode() helper
            if hasattr(self.exchange, "set_sandbox_mode"):
                self.exchange.set_sandbox_mode(True)
                logger.info(f"[Executor] {exchange_id} TESTNET mode enabled")
            else:
                logger.warning(
                    f"[Executor] {exchange_id} has no set_sandbox_mode() — "
                    f"testnet flag ignored. Check CCXT docs for manual URL override."
                )

        logger.info(
            f"[Executor] Initialised | Exchange: {exchange_id} | "
            f"Symbol: {symbol} | Testnet: {testnet} | "
            f"Retries: {max_retries}"
        )

    # ── Public API ────────────────────────────────────────────────────────────

    def execute_signal(
        self,
        signal:        Signal,
        current_price: float,
        slot_name:     str = "",
    ) -> ExecutionResult:
        """
        Execute a Signal on the live exchange.

        The paper simulator should ALWAYS be called first so that equity
        tracking is never blocked by a live execution error.

        Args:
            signal:        Signal from a strategy's generate_signal() call.
            current_price: Latest close price (used as fallback fill reference).
            slot_name:     Identifies which portfolio slot triggered this.

        Returns:
            ExecutionResult with order IDs and fill details.
            result.success=False means the order was NOT placed — log and alert.
        """
        if signal.action == "HOLD":
            return ExecutionResult(success=True, action="HOLD", slot_name=slot_name)

        logger.info(
            f"[Executor] {slot_name} → {signal.action} | "
            f"price~{current_price:.4f} | "
            f"{'SHORT' if signal.is_short else 'LONG'} | "
            f"{signal.order_type.upper()} | "
            f"{signal.reason[:80]}"
        )

        if signal.is_short:
            # Route futures/short signals through the futures path
            if signal.action == "BUY":
                result = self._place_futures_short_entry(signal, current_price, slot_name)
            else:
                result = self._place_futures_short_exit(signal, current_price, slot_name)
        else:
            # Spot path
            if signal.action == "BUY":
                result = self._place_spot_buy(signal, current_price, slot_name)
            else:
                result = self._place_spot_sell(signal, current_price, slot_name)

        # ── Log to persistent tracker ─────────────────────────────────────────
        if self.tracker is not None:
            try:
                self.tracker.record_execution(result, signal)
            except Exception as te:
                logger.warning(f"[Executor] OrderTracker.record_execution failed: {te}")

        if result.success:
            logger.info(f"[Executor] {result}")
        else:
            logger.error(f"[Executor] Order FAILED for {slot_name}: {result.error}")

        return result

    def cancel_protective_orders(self, slot_name: str) -> None:
        """
        Cancel any open SL/TP/OCO orders associated with a slot.

        Call this before placing a manual exit so we don't accidentally
        double-sell. Errors are swallowed and logged — if a cancel fails,
        the exit order still goes through.
        """
        info = self._open_protective_orders.get(slot_name, {})
        for key in ("oco_id", "sl_id", "tp_id"):
            order_id = info.get(key, "")
            if order_id:
                try:
                    self.exchange.cancel_order(order_id, self.symbol)
                    logger.info(
                        f"[Executor] Cancelled {key.upper()} order "
                        f"{order_id[:8]}... for slot={slot_name}"
                    )
                except ccxt.OrderNotFound:
                    logger.debug(
                        f"[Executor] {key} order {order_id[:8]}... already gone (filled/cancelled)"
                    )
                except Exception as e:
                    logger.warning(
                        f"[Executor] Failed to cancel {key} for {slot_name}: {e}"
                    )
        # Clear tracked orders for this slot
        self._open_protective_orders.pop(slot_name, None)

    def get_open_position(self, slot_name: str) -> Optional[dict]:
        """
        Return the current tracked protective-order info for a slot.
        Returns None if no open position is tracked for that slot.
        """
        return self._open_protective_orders.get(slot_name)

    def sync_balance(self, currency: str = "USDT") -> float:
        """
        Fetch and return the free balance for a currency from the exchange.
        Useful for reconciling paper equity against real wallet after deposits.
        """
        try:
            bal = self.exchange.fetch_balance()
            free = float(bal.get("free", {}).get(currency, 0.0))
            logger.info(f"[Executor] Real {currency} balance: {free:.4f}")
            return free
        except Exception as e:
            logger.error(f"[Executor] sync_balance failed: {e}")
            return 0.0

    # ── Spot execution ────────────────────────────────────────────────────────

    def _place_spot_buy(
        self,
        signal: Signal,
        current_price: float,
        slot_name: str,
    ) -> ExecutionResult:
        """
        Place a spot BUY order, then attach SL/TP via OCO if both are set.

        Flow:
          1. Calculate qty from signal.metadata["amount_usdt"]
          2. Place market or limit buy
          3. If signal.stop_loss and signal.take_profit → place OCO sell
             If only signal.stop_loss → place stop-limit sell
             If only signal.take_profit → place limit sell
        """
        amount_usdt = signal.metadata.get("amount_usdt", 0.0)
        if amount_usdt <= 0:
            return ExecutionResult(
                success=False, action="BUY", slot_name=slot_name,
                error="amount_usdt not set in signal.metadata — Kelly sizing may not have run."
            )

        qty = self._qty_from_usdt(amount_usdt, current_price)
        if qty <= 0:
            return ExecutionResult(
                success=False, action="BUY", slot_name=slot_name,
                error=f"Computed qty={qty} from amount_usdt={amount_usdt:.2f} @ {current_price:.4f}"
            )

        # ── Primary BUY order ─────────────────────────────────────────────────
        try:
            if signal.order_type == "market":
                raw = self._retry(
                    lambda: self.exchange.create_market_buy_order(
                        self.symbol, qty,
                        params={"quoteOrderQty": amount_usdt}
                        # quoteOrderQty: spend exactly this much USDT rather than
                        # a fixed base amount — handles BTC precision cleanly.
                        # Supported by Binance; gracefully ignored by others.
                    )
                )
            else:
                # Limit order: place slightly below current price
                limit_px = current_price * (1 - signal.limit_offset)
                limit_px = float(self.exchange.price_to_precision(self.symbol, limit_px))
                raw = self._retry(
                    lambda: self.exchange.create_limit_buy_order(
                        self.symbol, qty, limit_px
                    )
                )
        except Exception as e:
            return ExecutionResult(
                success=False, action="BUY", slot_name=slot_name, error=str(e)
            )

        fill_price = self._extract_fill_price(raw) or current_price
        filled_qty = float(raw.get("filled", qty))
        fee_usdt   = self._estimate_fee(fill_price * filled_qty)
        order_id   = str(raw.get("id", ""))

        result = ExecutionResult(
            success     = True,
            action      = "BUY",
            slot_name   = slot_name,
            order_id    = order_id,
            fill_price  = fill_price,
            filled_qty  = filled_qty,
            fee_usdt    = fee_usdt,
            raw_response= raw,
        )

        # ── Protective orders (OCO / SL / TP) ────────────────────────────────
        protective_info: dict = {"qty": filled_qty}

        if signal.stop_loss and signal.take_profit:
            # Full OCO: SL + TP in one call (Binance and Bybit support this)
            oco_result = self._place_oco_sell(
                filled_qty, signal.stop_loss, signal.take_profit, slot_name
            )
            protective_info["oco_id"] = oco_result
            result.oco_order_id = oco_result

        elif signal.stop_loss:
            # Stop-limit sell only
            sl_id = self._place_stop_limit_sell(
                filled_qty, signal.stop_loss, slot_name
            )
            protective_info["sl_id"] = sl_id
            result.sl_order_id = sl_id

        elif signal.take_profit:
            # Limit TP sell only
            tp_id = self._place_limit_tp_sell(
                filled_qty, signal.take_profit, slot_name
            )
            protective_info["tp_id"] = tp_id
            result.tp_order_id = tp_id

        self._open_protective_orders[slot_name] = protective_info
        return result

    def _place_spot_sell(
        self,
        signal: Signal,
        current_price: float,
        slot_name: str,
    ) -> ExecutionResult:
        """
        Close a spot LONG position.

        Steps:
          1. Cancel any open OCO / SL / TP orders for this slot first.
          2. Determine sell qty from open position (from tracker) or use
             all available balance as fallback.
          3. Place market or limit sell.
        """
        # Cancel open protective orders so we don't double-sell
        self.cancel_protective_orders(slot_name)

        # Work out how many units to sell
        tracked = self._open_protective_orders.get(slot_name, {})
        held_qty = tracked.get("qty", 0.0)

        if held_qty <= 0:
            # Fall back: sell all available base currency
            try:
                bal = self.exchange.fetch_balance()
                base = self.symbol.split("/")[0]
                held_qty = float(bal.get("free", {}).get(base, 0.0))
            except Exception:
                pass

        sell_qty = held_qty * signal.quantity_pct
        sell_qty = self._round_qty(sell_qty)

        if sell_qty <= 0:
            return ExecutionResult(
                success=False, action="SELL", slot_name=slot_name,
                error=f"Cannot determine sell qty for {slot_name} (no tracked position)."
            )

        try:
            if signal.order_type == "market":
                raw = self._retry(
                    lambda: self.exchange.create_market_sell_order(self.symbol, sell_qty)
                )
            else:
                limit_px = current_price * (1 + signal.limit_offset)
                limit_px = float(self.exchange.price_to_precision(self.symbol, limit_px))
                raw = self._retry(
                    lambda: self.exchange.create_limit_sell_order(
                        self.symbol, sell_qty, limit_px
                    )
                )
        except Exception as e:
            return ExecutionResult(
                success=False, action="SELL", slot_name=slot_name, error=str(e)
            )

        fill_price = self._extract_fill_price(raw) or current_price
        filled_qty = float(raw.get("filled", sell_qty))
        fee_usdt   = self._estimate_fee(fill_price * filled_qty)

        # If full sell, clear tracked position; otherwise reduce qty
        if signal.quantity_pct >= 1.0:
            self._open_protective_orders.pop(slot_name, None)
        else:
            remaining = held_qty - filled_qty
            if slot_name in self._open_protective_orders:
                self._open_protective_orders[slot_name]["qty"] = max(0.0, remaining)

        return ExecutionResult(
            success     = True,
            action      = "SELL",
            slot_name   = slot_name,
            order_id    = str(raw.get("id", "")),
            fill_price  = fill_price,
            filled_qty  = filled_qty,
            fee_usdt    = fee_usdt,
            raw_response= raw,
        )

    # ── Futures execution (BearShort) ─────────────────────────────────────────

    def _place_futures_short_entry(
        self,
        signal: Signal,
        current_price: float,
        slot_name: str,
    ) -> ExecutionResult:
        """
        Open a SHORT position on futures.

        For a short: we SELL the contract (borrow + sell). The BUY signal
        from BearShortStrategy uses is_short=True, so we map it here.

        Steps:
          1. Switch to futures context.
          2. Set leverage.
          3. Place market SHORT (side="sell", reduceOnly=False).
          4. Place stop-market SL order above entry (side="buy").
          5. Place take-profit-market TP order below entry (side="buy").
          6. Restore spot context.
        """
        amount_usdt = signal.metadata.get("amount_usdt", 0.0)
        if amount_usdt <= 0:
            return ExecutionResult(
                success=False, action="BUY", slot_name=slot_name, is_short=True,
                error="amount_usdt not set — Kelly sizing may not have run."
            )

        qty = self._qty_from_usdt(amount_usdt, current_price)
        if qty <= 0:
            return ExecutionResult(
                success=False, action="BUY", slot_name=slot_name, is_short=True,
                error=f"Computed qty={qty} from amount_usdt={amount_usdt:.2f}"
            )

        self._switch_to_futures()
        try:
            # Set leverage
            try:
                self.exchange.set_leverage(signal.leverage, self.symbol)
            except Exception as lev_err:
                logger.warning(
                    f"[Executor] set_leverage({signal.leverage}x) failed "
                    f"(may already be set): {lev_err}"
                )

            # Main short order: SELL into futures
            raw = self._retry(
                lambda: self.exchange.create_market_sell_order(
                    self.symbol, qty,
                    params={"reduceOnly": False}
                )
            )
            fill_price = self._extract_fill_price(raw) or current_price
            filled_qty = float(raw.get("filled", qty))
            order_id   = str(raw.get("id", ""))

            # Place SL stop-market order above entry (reduces position when price rises)
            sl_id, tp_id = "", ""
            if signal.stop_loss:
                sl_id = self._place_futures_sl(
                    filled_qty, signal.stop_loss, slot_name, is_short=True
                )
            if signal.take_profit:
                tp_id = self._place_futures_tp(
                    filled_qty, signal.take_profit, slot_name, is_short=True
                )

            self._open_protective_orders[slot_name] = {
                "qty": filled_qty,
                "sl_id": sl_id,
                "tp_id": tp_id,
                "is_short": True,
            }

            return ExecutionResult(
                success     = True,
                action      = "BUY",   # Signal action was BUY (open short)
                slot_name   = slot_name,
                order_id    = order_id,
                fill_price  = fill_price,
                filled_qty  = filled_qty,
                fee_usdt    = self._estimate_fee(fill_price * filled_qty),
                sl_order_id = sl_id,
                tp_order_id = tp_id,
                is_short    = True,
                raw_response= raw,
            )

        except Exception as e:
            return ExecutionResult(
                success=False, action="BUY", slot_name=slot_name, is_short=True,
                error=str(e)
            )
        finally:
            self._switch_to_spot()

    def _place_futures_short_exit(
        self,
        signal: Signal,
        current_price: float,
        slot_name: str,
    ) -> ExecutionResult:
        """
        Close a SHORT futures position (buy back to cover).

        Steps:
          1. Cancel open SL/TP orders for this slot.
          2. Place market BUY with reduceOnly=True (closes the short, no new long).
          3. Restore spot context.
        """
        tracked = self._open_protective_orders.get(slot_name, {})
        held_qty = tracked.get("qty", 0.0)

        # Cancel protective orders first
        self._switch_to_futures()
        try:
            for key in ("sl_id", "tp_id"):
                oid = tracked.get(key, "")
                if oid:
                    try:
                        self.exchange.cancel_order(oid, self.symbol)
                    except ccxt.OrderNotFound:
                        pass
                    except Exception as ce:
                        logger.warning(f"[Executor] Cancel futures {key} failed: {ce}")

            sell_qty = held_qty * signal.quantity_pct
            sell_qty = self._round_qty(sell_qty)

            if sell_qty <= 0:
                return ExecutionResult(
                    success=False, action="SELL", slot_name=slot_name, is_short=True,
                    error=f"Cannot determine close qty for short slot={slot_name}"
                )

            raw = self._retry(
                lambda: self.exchange.create_market_buy_order(
                    self.symbol, sell_qty,
                    params={"reduceOnly": True}
                )
            )
            fill_price = self._extract_fill_price(raw) or current_price
            filled_qty = float(raw.get("filled", sell_qty))

            if signal.quantity_pct >= 1.0:
                self._open_protective_orders.pop(slot_name, None)
            else:
                remaining = held_qty - filled_qty
                self._open_protective_orders[slot_name]["qty"] = max(0.0, remaining)

            return ExecutionResult(
                success     = True,
                action      = "SELL",
                slot_name   = slot_name,
                order_id    = str(raw.get("id", "")),
                fill_price  = fill_price,
                filled_qty  = filled_qty,
                fee_usdt    = self._estimate_fee(fill_price * filled_qty),
                is_short    = True,
                raw_response= raw,
            )

        except Exception as e:
            return ExecutionResult(
                success=False, action="SELL", slot_name=slot_name, is_short=True,
                error=str(e)
            )
        finally:
            self._switch_to_spot()

    # ── Protective order helpers ──────────────────────────────────────────────

    def _place_oco_sell(
        self,
        qty: float,
        stop_loss: float,
        take_profit: float,
        slot_name: str,
    ) -> str:
        """
        Place an OCO (One-Cancels-the-Other) sell order on Binance spot.

        An OCO combines a limit TP sell and a stop-limit SL sell in one call.
        Whichever triggers first automatically cancels the other leg.

        Returns the OCO order list ID, or "" if not supported / failed.
        """
        stop_limit_px = stop_loss * (1 - self.oco_sl_slip)

        qty_str          = self.exchange.amount_to_precision(self.symbol, qty)
        tp_px_str        = self.exchange.price_to_precision(self.symbol, take_profit)
        sl_px_str        = self.exchange.price_to_precision(self.symbol, stop_loss)
        sl_limit_px_str  = self.exchange.price_to_precision(self.symbol, stop_limit_px)

        try:
            raw = self._retry(
                lambda: self.exchange.create_order(
                    symbol    = self.symbol,
                    type      = "oco",
                    side      = "sell",
                    amount    = float(qty_str),
                    price     = float(tp_px_str),      # TP limit price
                    params    = {
                        "stopPrice":           float(sl_px_str),       # Trigger price
                        "stopLimitPrice":      float(sl_limit_px_str), # Execution price
                        "stopLimitTimeInForce": "GTC",
                    },
                )
            )
            # Binance returns {"orderListId": ..., "orders": [...]}
            oco_id = str(raw.get("orderListId", raw.get("id", "")))
            logger.info(
                f"[Executor] OCO placed for {slot_name} | "
                f"TP={take_profit:.4f} | SL={stop_loss:.4f} | id={oco_id[:8]}..."
            )
            return oco_id

        except ccxt.NotSupported:
            logger.warning(
                f"[Executor] OCO not supported on {self.exchange_id}. "
                f"Falling back to stop-limit only for {slot_name}."
            )
            return self._place_stop_limit_sell(qty, stop_loss, slot_name)

        except Exception as e:
            logger.error(f"[Executor] OCO placement failed for {slot_name}: {e}")
            # Fall back to stop-limit only
            return self._place_stop_limit_sell(qty, stop_loss, slot_name)

    def _place_stop_limit_sell(
        self, qty: float, stop_loss: float, slot_name: str
    ) -> str:
        """Place a stop-limit sell order as standalone SL for spot longs."""
        stop_limit_px = stop_loss * (1 - self.oco_sl_slip)
        qty_str = self.exchange.amount_to_precision(self.symbol, qty)
        sl_px   = float(self.exchange.price_to_precision(self.symbol, stop_loss))
        sl_lmt  = float(self.exchange.price_to_precision(self.symbol, stop_limit_px))
        try:
            raw = self._retry(
                lambda: self.exchange.create_order(
                    symbol = self.symbol,
                    type   = "stopLimit",
                    side   = "sell",
                    amount = float(qty_str),
                    price  = sl_lmt,
                    params = {"stopPrice": sl_px, "timeInForce": "GTC"},
                )
            )
            return str(raw.get("id", ""))
        except Exception as e:
            logger.error(f"[Executor] Stop-limit sell failed for {slot_name}: {e}")
            return ""

    def _place_limit_tp_sell(
        self, qty: float, take_profit: float, slot_name: str
    ) -> str:
        """Place a limit sell order as standalone TP for spot longs."""
        qty_str = self.exchange.amount_to_precision(self.symbol, qty)
        tp_px   = float(self.exchange.price_to_precision(self.symbol, take_profit))
        try:
            raw = self._retry(
                lambda: self.exchange.create_limit_sell_order(
                    self.symbol, float(qty_str), tp_px
                )
            )
            return str(raw.get("id", ""))
        except Exception as e:
            logger.error(f"[Executor] Limit TP sell failed for {slot_name}: {e}")
            return ""

    def _place_futures_sl(
        self,
        qty: float,
        stop_price: float,
        slot_name: str,
        is_short: bool = True,
    ) -> str:
        """
        Place a stop-market order on futures to close a position at SL.

        For a short, the SL is ABOVE entry (buy back if price rises).
        side="buy" with reduceOnly=True closes the short without opening a new long.
        """
        qty_str = self.exchange.amount_to_precision(self.symbol, qty)
        sl_px   = float(self.exchange.price_to_precision(self.symbol, stop_price))
        side    = "buy" if is_short else "sell"

        order_params = {
            "stopPrice":  sl_px,
            "reduceOnly": True,
            "workingType": "MARK_PRICE",   # Binance: use mark price to trigger
        }

        # Exchange-specific order type for stop-market
        stop_type = self._futures_stop_type()

        try:
            raw = self._retry(
                lambda: self.exchange.create_order(
                    symbol = self.symbol,
                    type   = stop_type,
                    side   = side,
                    amount = float(qty_str),
                    price  = None,        # market fill when triggered
                    params = order_params,
                )
            )
            sl_id = str(raw.get("id", ""))
            logger.info(
                f"[Executor] Futures SL placed for {slot_name} | "
                f"stopPrice={stop_price:.4f} | id={sl_id[:8]}..."
            )
            return sl_id
        except Exception as e:
            logger.error(f"[Executor] Futures SL placement failed for {slot_name}: {e}")
            return ""

    def _place_futures_tp(
        self,
        qty: float,
        take_profit: float,
        slot_name: str,
        is_short: bool = True,
    ) -> str:
        """
        Place a take-profit-market order on futures.

        For a short, TP is BELOW entry (buy back if price falls enough).
        """
        qty_str = self.exchange.amount_to_precision(self.symbol, qty)
        tp_px   = float(self.exchange.price_to_precision(self.symbol, take_profit))
        side    = "buy" if is_short else "sell"

        tp_type = self._futures_tp_type()

        try:
            raw = self._retry(
                lambda: self.exchange.create_order(
                    symbol = self.symbol,
                    type   = tp_type,
                    side   = side,
                    amount = float(qty_str),
                    price  = None,
                    params = {
                        "stopPrice":   tp_px,
                        "reduceOnly":  True,
                        "workingType": "MARK_PRICE",
                    },
                )
            )
            tp_id = str(raw.get("id", ""))
            logger.info(
                f"[Executor] Futures TP placed for {slot_name} | "
                f"triggerPrice={take_profit:.4f} | id={tp_id[:8]}..."
            )
            return tp_id
        except Exception as e:
            logger.error(f"[Executor] Futures TP placement failed for {slot_name}: {e}")
            return ""

    # ── Context switching ─────────────────────────────────────────────────────

    def _switch_to_futures(self) -> None:
        """Switch the exchange instance to its USD-M futures endpoint."""
        self.exchange.options["defaultType"] = "future"

    def _switch_to_spot(self) -> None:
        """Restore the exchange instance to its spot endpoint."""
        self.exchange.options["defaultType"] = "spot"

    # ── Retry wrapper ─────────────────────────────────────────────────────────

    def _retry(self, fn, *args, **kwargs):
        """
        Call fn() up to max_retries times, retrying on transient network errors.

        Raises the last exception if all retries are exhausted.
        """
        last_exc = None
        for attempt in range(1, self.max_retries + 1):
            try:
                return fn(*args, **kwargs)
            except (ccxt.NetworkError, ccxt.RequestTimeout) as e:
                last_exc = e
                if attempt < self.max_retries:
                    logger.warning(
                        f"[Executor] Network error (attempt {attempt}/{self.max_retries}): "
                        f"{e} — retrying in {self.retry_delay_s}s"
                    )
                    time.sleep(self.retry_delay_s)
            except (ccxt.AuthenticationError, ccxt.InvalidOrder,
                    ccxt.InsufficientFunds, ccxt.BadSymbol) as e:
                # Non-retryable errors — raise immediately
                raise
        raise last_exc  # type: ignore[misc]

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _qty_from_usdt(self, amount_usdt: float, price: float) -> float:
        """
        Convert a USDT spend amount to base-currency quantity, rounded to
        the exchange's minimum step size (amount precision).
        """
        if price <= 0:
            return 0.0
        raw_qty = amount_usdt / price
        try:
            return float(self.exchange.amount_to_precision(self.symbol, raw_qty))
        except Exception:
            return raw_qty  # Fallback: return unrounded

    def _round_qty(self, qty: float) -> float:
        """Round a quantity to exchange precision."""
        try:
            return float(self.exchange.amount_to_precision(self.symbol, qty))
        except Exception:
            return qty

    def _extract_fill_price(self, raw_order: dict) -> Optional[float]:
        """
        Extract average fill price from a CCXT order response.
        Returns None if the fill isn't known yet (e.g. partially filled limit).
        """
        # CCXT normalises the field to "average" for market orders
        avg = raw_order.get("average") or raw_order.get("price")
        if avg:
            return float(avg)
        # Some exchanges put it under info.avgPrice
        info = raw_order.get("info", {})
        avg_info = info.get("avgPrice") or info.get("avgFillPrice")
        if avg_info:
            try:
                return float(avg_info)
            except (TypeError, ValueError):
                pass
        return None

    def _estimate_fee(self, notional_usdt: float) -> float:
        """
        Estimate trading fee in USDT.
        Binance default: 0.1% for spot, 0.04% for futures taker.
        Override this if you have BNB fee discount or VIP tier.
        """
        is_futures = self.exchange.options.get("defaultType") == "future"
        fee_rate = 0.0004 if is_futures else 0.001
        return round(notional_usdt * fee_rate, 6)

    def _futures_stop_type(self) -> str:
        """Return the correct 'stop-market' order type string for this exchange."""
        if self.exchange_id in self._BINANCE_FAMILY:
            return "STOP_MARKET"
        if self.exchange_id in self._OKX_FAMILY:
            return "conditional"
        if self.exchange_id in self._BYBIT_FAMILY:
            return "StopLoss"
        return "stopMarket"   # Generic CCXT fallback

    def _futures_tp_type(self) -> str:
        """Return the correct 'take-profit-market' order type string for this exchange."""
        if self.exchange_id in self._BINANCE_FAMILY:
            return "TAKE_PROFIT_MARKET"
        if self.exchange_id in self._OKX_FAMILY:
            return "conditional"
        if self.exchange_id in self._BYBIT_FAMILY:
            return "TakeProfit"
        return "takeProfitMarket"   # Generic CCXT fallback
