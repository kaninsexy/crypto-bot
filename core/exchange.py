"""
core/exchange.py — OKX exchange connector.

This module wraps ccxt's OKX class and gives us a clean interface
for the rest of the bot. It handles:
  - Connecting to OKX (paper mode uses public endpoints only)
  - Fetching account balance
  - Placing market buy/sell orders (live mode only)
  - Fetching current market price

Why OKX?
  Binance is geo-blocked in Singapore. OKX is available and has the
  deepest liquidity among the exchanges accessible from SG.

Why ccxt?
  ccxt is a unified library that supports 100+ exchanges with the same API.
  Swapping Binance → OKX is mostly a matter of changing the class name,
  the defaultType value, and adding OKX's required passphrase.

Key OKX quirks vs Binance:
  - defaultType is "swap" (perpetual futures) on OKX, not "future".
  - Authentication requires apiKey + secret + password (passphrase).
  - Perpetual symbols use the format "BTC/USDT:USDT" — the symbol
    normalization is handled in core/data_fetcher.py so strategies
    can keep using plain "BTC/USDT".
"""

import ccxt
from loguru import logger
import config


def create_exchange() -> ccxt.okx:
    """
    Create and return a configured ccxt OKX instance.

    In paper mode:  No API keys needed. We connect anonymously for public
                    market data (prices, candles, order book).
    In live mode:   API key + secret + passphrase are required. We enable
                    rate limiting so we don't accidentally get banned by OKX.
    """
    params = {
        "enableRateLimit": True,   # Automatically respects OKX rate limits
        "options": {
            "defaultType": "swap",  # OKX perpetual futures
        },
    }

    if config.TRADING_MODE == "live":
        params["apiKey"]   = config.OKX_API_KEY
        params["secret"]   = config.OKX_API_SECRET
        params["password"] = config.OKX_PASSPHRASE
        logger.info("Exchange: connected to OKX in LIVE mode")
    else:
        logger.info("Exchange: connected to OKX in PAPER mode (public data only)")

    return ccxt.okx(params)


def get_balance(exchange: ccxt.okx, currency: str = "USDT") -> float:
    """
    Fetch your current balance for a given currency from OKX.

    Args:
        exchange: The ccxt exchange instance.
        currency: The currency to check, e.g. "USDT", "BTC".

    Returns:
        The available (free) balance as a float.

    Note: Only works in live mode. In paper mode, balance is tracked
    by the PaperTrading simulator, not OKX.

    OKX futures balance is reachable through the same fetch_balance call
    because we set defaultType="swap" at exchange creation time.
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
        logger.error("Authentication failed — check your OKX_API_KEY, "
                     "OKX_API_SECRET, and OKX_PASSPHRASE.")
        raise
    except ccxt.NetworkError as e:
        logger.error(f"Network error fetching balance: {e}")
        raise


def get_price(exchange: ccxt.okx, symbol: str = None) -> float:
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
        logger.error(f"Invalid trading pair on OKX: {symbol}")
        raise
    except ccxt.NetworkError as e:
        logger.error(f"Network error fetching price: {e}")
        raise


# ─── Wallet transfers ────────────────────────────────────────────────────────
#
# Only needed for live trading — implement before Phase 6.
#
# OKX uses a single unified trading account by default; funds sometimes need
# to move between the "Funding Account" (spot-style deposits) and the
# "Trading Account" (where the bot actually places orders).
#
# The underlying endpoint is POST /api/v5/asset/transfer, reachable via ccxt
# as `exchange.private_post_asset_transfer(...)`. Required params differ
# from Binance: OKX takes `{type, ccy, amt, from, to}` where `from`/`to`
# are account IDs (6 = Funding, 18 = Trading).
#
# Until then, use the OKX web UI to shuffle USDT between Funding and Trading
# manually before flipping the bot into live mode.


def transfer_spot_to_swap(exchange: ccxt.okx, amount_usdt: float) -> dict:
    """
    Move USDT from OKX Funding Account (spot-style) into the Trading Account
    where perpetual swap orders are placed.

    NOT YET IMPLEMENTED. Use the OKX web UI for now.
    """
    raise NotImplementedError(
        "OKX transfer not yet implemented. "
        "Use OKX web UI to transfer USDT to Trading Account before live trading."
    )


def transfer_swap_to_spot(exchange: ccxt.okx, amount_usdt: float) -> dict:
    """
    Move USDT from the Trading Account back to the Funding Account.

    NOT YET IMPLEMENTED. Use the OKX web UI for now.
    """
    raise NotImplementedError(
        "OKX transfer not yet implemented. "
        "Use OKX web UI to transfer USDT to Funding Account before withdrawing."
    )


def place_market_order(
    exchange: ccxt.okx,
    symbol: str,
    side: str,          # "buy" or "sell"
    amount: float,      # Amount in base currency (e.g. BTC amount, not USDT)
) -> dict:
    """
    Place a market order on OKX.

    A market order executes immediately at the current market price.
    This is the simplest order type — used by most bots for fast execution.

    Args:
        exchange: The ccxt exchange instance.
        symbol:   Trading pair, e.g. "BTC/USDT".
        side:     "buy" or "sell".
        amount:   Amount of base currency to buy/sell (e.g. 0.001 BTC).

    Returns:
        The order dict returned by OKX, containing order ID, status, etc.

    Raises:
        RuntimeError: If called in paper mode (use PaperTrading instead).
    """
    if config.TRADING_MODE == "paper":
        raise RuntimeError("place_market_order() is not available in paper mode. "
                           "Use PaperTrading.execute_signal() instead.")

    if side not in ("buy", "sell"):
        raise ValueError(f"side must be 'buy' or 'sell', got: '{side}'")

    try:
        logger.info(f"Placing LIVE {side.upper()} order on OKX: {amount} {symbol}")
        order = exchange.create_market_order(symbol, side, amount)
        logger.info(f"Order placed: ID={order['id']} | Status={order['status']}")
        return order
    except ccxt.InsufficientFunds:
        logger.error("Insufficient funds on OKX to place order.")
        raise
    except ccxt.InvalidOrder as e:
        logger.error(f"Invalid order parameters on OKX: {e}")
        raise
    except ccxt.NetworkError as e:
        logger.error(f"Network error placing order on OKX: {e}")
        raise
