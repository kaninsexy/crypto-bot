"""
core/exchange.py — Binance exchange connector.

This module wraps ccxt's Binance class and gives us a clean interface
for the rest of the bot. It handles:
  - Connecting to Binance (paper mode uses public endpoints only)
  - Fetching account balance
  - Placing market buy/sell orders (live mode only)
  - Fetching current market price

Why ccxt?
  ccxt is a unified library that supports 100+ exchanges with the same API.
  If you ever want to switch from Binance to another exchange, you only need
  to change one line (the exchange name) in this file.
"""

import ccxt
from loguru import logger
import config


def create_exchange() -> ccxt.binance:
    """
    Create and return a configured ccxt Binance instance.

    In paper mode:  No API keys needed. We connect anonymously for public
                    market data (prices, candles, order book).
    In live mode:   API key + secret are required. We enable rate limiting
                    so we don't accidentally get banned by Binance.
    """
    params = {
        "enableRateLimit": True,   # Automatically respects Binance rate limits
        "options": {
            "defaultType": "spot",  # Use spot market (not futures)
        },
    }

    if config.TRADING_MODE == "live":
        params["apiKey"] = config.BINANCE_API_KEY
        params["secret"] = config.BINANCE_API_SECRET
        logger.info("Exchange: connected to Binance in LIVE mode")
    else:
        logger.info("Exchange: connected to Binance in PAPER mode (public data only)")

    exchange = ccxt.binance(params)
    return exchange


def get_balance(exchange: ccxt.binance, currency: str = "USDT") -> float:
    """
    Fetch your current balance for a given currency from Binance.

    Args:
        exchange: The ccxt exchange instance.
        currency: The currency to check, e.g. "USDT", "BTC".

    Returns:
        The available (free) balance as a float.

    Note: Only works in live mode. In paper mode, balance is tracked
    by the PaperTrading simulator, not Binance.
    """
    if config.TRADING_MODE == "paper":
        raise RuntimeError("get_balance() is not available in paper mode. "
                           "Use PaperTrading.get_balance() instead.")
    try:
        balance = exchange.fetch_balance()
        free = balance["free"].get(currency, 0.0)
        logger.debug(f"Balance fetched: {free} {currency}")
        return free
    except ccxt.AuthenticationError:
        logger.error("Authentication failed — check your BINANCE_API_KEY and BINANCE_API_SECRET.")
        raise
    except ccxt.NetworkError as e:
        logger.error(f"Network error fetching balance: {e}")
        raise


def get_price(exchange: ccxt.binance, symbol: str = None) -> float:
    """
    Get the current market price for a trading pair.

    Args:
        exchange: The ccxt exchange instance.
        symbol: Trading pair, e.g. "BTC/USDT". Defaults to config.TRADING_PAIR.

    Returns:
        The last traded price as a float.
    """
    symbol = symbol or config.TRADING_PAIR
    try:
        ticker = exchange.fetch_ticker(symbol)
        price = ticker["last"]
        logger.debug(f"Current price {symbol}: {price}")
        return price
    except ccxt.BadSymbol:
        logger.error(f"Invalid trading pair: {symbol}")
        raise
    except ccxt.NetworkError as e:
        logger.error(f"Network error fetching price: {e}")
        raise


def transfer_spot_to_futures(exchange: ccxt.binance, amount_usdt: float) -> dict:
    """
    Transfer USDT from your Spot wallet to your USD-M Futures wallet.

    Uses Binance's internal Universal Transfer API (POST /sapi/v1/asset/transfer).
    This is an INTERNAL wallet move — no withdrawal occurs and no withdrawal
    permission is needed on your API key. You only need:
      ✓ Enable Reading
      ✓ Enable Futures

    Args:
        exchange: The ccxt exchange instance (must be in live mode with API keys).
        amount_usdt: Amount of USDT to transfer.

    Returns:
        Binance response dict containing 'tranId' on success.

    Raises:
        RuntimeError: If called in paper mode.
        ccxt.InsufficientFunds: If spot balance is too low.
        ccxt.AuthenticationError: If API key lacks required permissions.
    """
    if config.TRADING_MODE == "paper":
        raise RuntimeError(
            "transfer_spot_to_futures() is not available in paper mode. "
            "In paper mode all capital is tracked by the PaperTrading simulators."
        )

    if amount_usdt <= 0:
        raise ValueError(f"Transfer amount must be positive, got: {amount_usdt}")

    try:
        logger.info(f"Transferring ${amount_usdt:.2f} USDT: Spot → Futures")
        # Binance universal transfer: type 1 = SPOT → USDT-M Futures
        result = exchange.sapi_post_asset_transfer({
            "type": "MAIN_UMFUTURE",
            "asset": "USDT",
            "amount": str(amount_usdt),
        })
        logger.info(
            f"Transfer complete: Spot → Futures ${amount_usdt:.2f} USDT | "
            f"tranId={result.get('tranId')}"
        )
        return result
    except ccxt.InsufficientFunds:
        logger.error(f"Insufficient USDT in Spot wallet for transfer of ${amount_usdt:.2f}")
        raise
    except ccxt.AuthenticationError:
        logger.error(
            "Transfer failed: API key missing permission. "
            "Enable 'Enable Futures' in your Binance API settings. "
            "Withdrawal permission is NOT required for internal transfers."
        )
        raise
    except ccxt.NetworkError as e:
        logger.error(f"Network error during Spot→Futures transfer: {e}")
        raise


def transfer_futures_to_spot(exchange: ccxt.binance, amount_usdt: float) -> dict:
    """
    Transfer USDT from your USD-M Futures wallet back to your Spot wallet.

    Same permission requirements as transfer_spot_to_futures — no withdrawal
    permission needed. Only 'Enable Futures' is required.

    Args:
        exchange: The ccxt exchange instance (must be in live mode with API keys).
        amount_usdt: Amount of USDT to transfer.

    Returns:
        Binance response dict containing 'tranId' on success.

    Raises:
        RuntimeError: If called in paper mode.
        ccxt.InsufficientFunds: If futures balance is too low.
        ccxt.AuthenticationError: If API key lacks required permissions.
    """
    if config.TRADING_MODE == "paper":
        raise RuntimeError(
            "transfer_futures_to_spot() is not available in paper mode."
        )

    if amount_usdt <= 0:
        raise ValueError(f"Transfer amount must be positive, got: {amount_usdt}")

    try:
        logger.info(f"Transferring ${amount_usdt:.2f} USDT: Futures → Spot")
        # Binance universal transfer: type 2 = USDT-M Futures → SPOT
        result = exchange.sapi_post_asset_transfer({
            "type": "UMFUTURE_MAIN",
            "asset": "USDT",
            "amount": str(amount_usdt),
        })
        logger.info(
            f"Transfer complete: Futures → Spot ${amount_usdt:.2f} USDT | "
            f"tranId={result.get('tranId')}"
        )
        return result
    except ccxt.InsufficientFunds:
        logger.error(f"Insufficient USDT in Futures wallet for transfer of ${amount_usdt:.2f}")
        raise
    except ccxt.AuthenticationError:
        logger.error(
            "Transfer failed: API key missing permission. "
            "Enable 'Enable Futures' in your Binance API settings."
        )
        raise
    except ccxt.NetworkError as e:
        logger.error(f"Network error during Futures→Spot transfer: {e}")
        raise


def get_futures_balance(exchange: ccxt.binance, currency: str = "USDT") -> float:
    """
    Fetch your current balance in the USD-M Futures wallet.

    Args:
        exchange: The ccxt exchange instance.
        currency: Currency to check (default: "USDT").

    Returns:
        Available (withdrawable) balance in the futures wallet.
    """
    if config.TRADING_MODE == "paper":
        raise RuntimeError("get_futures_balance() is not available in paper mode.")
    try:
        # Switch to futures context temporarily
        exchange.options["defaultType"] = "future"
        balance = exchange.fetch_balance()
        exchange.options["defaultType"] = "spot"   # Restore spot default
        free = balance["free"].get(currency, 0.0)
        logger.debug(f"Futures balance: {free} {currency}")
        return free
    except Exception:
        exchange.options["defaultType"] = "spot"   # Always restore
        raise


def place_market_order(
    exchange: ccxt.binance,
    symbol: str,
    side: str,          # "buy" or "sell"
    amount: float,      # Amount in base currency (e.g. BTC amount, not USDT)
) -> dict:
    """
    Place a market order on Binance.

    A market order executes immediately at the current market price.
    This is the simplest order type — used by most bots for fast execution.

    Args:
        exchange: The ccxt exchange instance.
        symbol:   Trading pair, e.g. "BTC/USDT".
        side:     "buy" or "sell".
        amount:   Amount of base currency to buy/sell (e.g. 0.001 BTC).

    Returns:
        The order dict returned by Binance, containing order ID, status, etc.

    Raises:
        RuntimeError: If called in paper mode (use PaperTrading instead).
    """
    if config.TRADING_MODE == "paper":
        raise RuntimeError("place_market_order() is not available in paper mode. "
                           "Use PaperTrading.execute_signal() instead.")

    if side not in ("buy", "sell"):
        raise ValueError(f"side must be 'buy' or 'sell', got: '{side}'")

    try:
        logger.info(f"Placing LIVE {side.upper()} order: {amount} {symbol}")
        order = exchange.create_market_order(symbol, side, amount)
        logger.info(f"Order placed: ID={order['id']} | Status={order['status']}")
        return order
    except ccxt.InsufficientFunds:
        logger.error("Insufficient funds to place order.")
        raise
    except ccxt.InvalidOrder as e:
        logger.error(f"Invalid order parameters: {e}")
        raise
    except ccxt.NetworkError as e:
        logger.error(f"Network error placing order: {e}")
        raise
