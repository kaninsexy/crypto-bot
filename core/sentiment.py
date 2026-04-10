"""
core/sentiment.py — LunarCrush sentiment fetcher.

Fetches real-time social sentiment for a coin from the LunarCrush API.
This is used by the Breakout strategy to scale position size:
  - High sentiment (>60) → full size
  - Neutral (40-60)      → 75% size
  - Low sentiment (<40)  → 50% size

LunarCrush provides a 'galaxy_score' (0-100) and 'sentiment' (0-100).
We use sentiment directly as it maps cleanly to our 0-100 scale.

Note: Requires LunarCrush MCP to be connected. If unavailable, caller
should catch the exception and fall back to neutral (50).
"""

import requests
from loguru import logger

# LunarCrush public API endpoint (free tier, no key required for basic data)
_LUNARCRUSH_BASE = "https://lunarcrush.com/api4/public/coins"

# Simple in-memory cache: {symbol: (score, timestamp)}
_cache: dict = {}
_CACHE_TTL_SECONDS = 300  # Re-fetch every 5 minutes


def get_coin_sentiment(coin: str) -> float:
    """
    Fetch the current sentiment score for a coin.

    Args:
        coin: Coin ticker, e.g. "BTC", "ETH", "SOL".

    Returns:
        Sentiment score 0-100 (50 = neutral).
        Returns 50 if fetch fails (neutral — no position sizing penalty).

    Raises:
        Exception: If the API call fails and caller wants to handle it.
    """
    import time

    coin = coin.upper()

    # Check cache
    if coin in _cache:
        cached_score, cached_time = _cache[coin]
        if time.time() - cached_time < _CACHE_TTL_SECONDS:
            logger.debug(f"Sentiment [{coin}]: {cached_score:.0f}/100 (cached)")
            return cached_score

    # Fetch from LunarCrush
    try:
        url = f"{_LUNARCRUSH_BASE}/{coin.lower()}/v1"
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        data = response.json()

        # LunarCrush returns sentiment as a value between 1-5
        # We normalize it to 0-100 for consistency
        raw = data.get("data", {}).get("sentiment", None)
        if raw is None:
            raise ValueError("No sentiment field in response")

        # sentiment: 1 (very bearish) to 5 (very bullish) → map to 0-100
        score = ((float(raw) - 1) / 4) * 100
        score = max(0.0, min(100.0, score))

        _cache[coin] = (score, time.time())
        logger.debug(f"Sentiment [{coin}]: {score:.0f}/100 (raw={raw})")
        return score

    except Exception as e:
        logger.debug(f"Sentiment fetch failed for {coin}: {e} — defaulting to neutral (50)")
        return 50.0
