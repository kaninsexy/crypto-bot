"""
core/data_fetcher.py — OHLCV (candlestick) data fetcher.

OHLCV stands for: Open, High, Low, Close, Volume.
These are the building blocks for every technical indicator.

This module fetches candle data from Binance via ccxt and returns
it as a pandas DataFrame — the format all our strategies expect.

Example output (1h candles for BTC/USDT):
  ┌─────────────────────┬─────────┬─────────┬─────────┬─────────┬─────────┐
  │ timestamp           │  open   │  high   │   low   │  close  │ volume  │
  ├─────────────────────┼─────────┼─────────┼─────────┼─────────┼─────────┤
  │ 2024-01-01 00:00:00 │ 42000.0 │ 42500.0 │ 41800.0 │ 42300.0 │  1250.3 │
  │ 2024-01-01 01:00:00 │ 42300.0 │ 42800.0 │ 42100.0 │ 42600.0 │  980.1  │
  └─────────────────────┴─────────┴─────────┴─────────┴─────────┴─────────┘
"""

import time
import ccxt
import pandas as pd
from loguru import logger
import config

# ── Retry config ───────────────────────────────────────────────────────────────
# How many times to retry on transient errors before giving up.
# Backoff: 5s → 10s → 20s → 40s (doubles each attempt, capped at 120s).
_MAX_RETRIES   = 5
_BACKOFF_BASE  = 5    # seconds
_BACKOFF_MAX   = 120  # seconds cap

# Errors that are worth retrying (temporary, not logic errors)
_RETRYABLE = (
    ccxt.NetworkError,
    ccxt.RequestTimeout,
    ccxt.ExchangeNotAvailable,
    ccxt.DDoSProtection,
    ccxt.RateLimitExceeded,
)


def fetch_ohlcv(
    exchange: ccxt.binance,
    symbol: str = None,
    timeframe: str = None,
    limit: int = None,
) -> pd.DataFrame:
    """
    Fetch OHLCV candle data from Binance and return as a DataFrame.

    Retries automatically on transient errors (network glitch, rate limit,
    Binance maintenance) with exponential backoff — up to 5 attempts.
    A temporary internet drop will be survived without crashing the bot.

    Args:
        exchange:  The ccxt exchange instance (from core/exchange.py).
        symbol:    Trading pair, e.g. "BTC/USDT". Defaults to config.TRADING_PAIR.
        timeframe: Candle size, e.g. "1h", "15m", "4h". Defaults to config.TIMEFRAME.
        limit:     How many candles to fetch. Defaults to config.CANDLE_LIMIT.

    Returns:
        DataFrame with columns [open, high, low, close, volume], indexed by UTC timestamp.

    Raises:
        ccxt.BadSymbol:    Trading pair doesn't exist (not retried).
        ccxt.NetworkError: If all retry attempts are exhausted.
    """
    symbol    = symbol    or config.TRADING_PAIR
    timeframe = timeframe or config.TIMEFRAME
    limit     = limit     or config.CANDLE_LIMIT

    logger.debug(f"Fetching {limit} × {timeframe} candles for {symbol}…")

    last_error: Exception = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            raw = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
            break   # success — exit retry loop

        except ccxt.BadSymbol:
            logger.error(f"Symbol '{symbol}' not found on Binance.")
            raise   # not retryable — wrong config

        except _RETRYABLE as e:
            last_error = e
            wait = min(_BACKOFF_BASE * (2 ** (attempt - 1)), _BACKOFF_MAX)
            if attempt < _MAX_RETRIES:
                logger.warning(
                    f"Binance API error (attempt {attempt}/{_MAX_RETRIES}): {e} — "
                    f"retrying in {wait}s…"
                )
                time.sleep(wait)
            else:
                logger.error(
                    f"Binance API failed after {_MAX_RETRIES} attempts: {e}"
                )
                raise

    if not raw:
        raise ValueError(f"No candle data returned for {symbol} {timeframe}")

    df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df.set_index("timestamp", inplace=True)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)

    logger.debug(f"Fetched {len(df)} candles. Latest close: {df['close'].iloc[-1]:.4f}")
    return df


def fetch_latest_price(exchange: ccxt.binance, symbol: str = None) -> float:
    """
    Convenience function: fetch just the latest closing price.

    This is faster than fetching full OHLCV when you only need the current price.

    Args:
        exchange: The ccxt exchange instance.
        symbol:   Trading pair. Defaults to config.TRADING_PAIR.

    Returns:
        The latest closing price as a float.
    """
    symbol = symbol or config.TRADING_PAIR
    df = fetch_ohlcv(exchange, symbol=symbol, timeframe="1m", limit=1)
    return float(df["close"].iloc[-1])


def is_new_candle(df: pd.DataFrame, last_timestamp: pd.Timestamp) -> bool:
    """
    Check whether a new candle has appeared since the last check.

    Use this in your main loop to avoid re-processing the same candle twice.

    Args:
        df:              The latest OHLCV DataFrame.
        last_timestamp:  The timestamp of the last candle we processed.

    Returns:
        True if the latest candle in df is newer than last_timestamp.

    Example:
        last_ts = None
        while True:
            df = fetch_ohlcv(exchange)
            if is_new_candle(df, last_ts):
                last_ts = df.index[-1]
                signal = strategy.generate_signal(df)
                ...
            time.sleep(30)
    """
    latest = df.index[-1]
    if last_timestamp is None:
        return True
    return latest > last_timestamp
