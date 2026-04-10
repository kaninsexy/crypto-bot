"""
portfolio/funding_rate.py — Perpetual futures funding rate provider.

WHY FUNDING RATE MATTERS FOR SPOT DCA
───────────────────────────────────────
Perpetual futures funding rate is one of crypto's highest signal-to-noise
leading indicators. It measures market sentiment and leverage crowding:

  Positive & high rate (e.g. +0.05%/8h):
    Long positions pay short positions every 8 hours.
    → Crowd is heavily long and paying to stay long.
    → Overleveraged → vulnerable to a leveraged flush/cascade.
    → BAD time to open a new DCA cycle (will likely catch the flush).

  Negative rate (e.g. -0.03%/8h):
    Short positions pay long positions every 8 hours.
    → Crowd is net short and paying to hold.
    → Short squeeze risk = upside pressure → fine to enter long.

  Near zero (±0.01%/8h):
    Balanced positioning → no crowding bias.
    → Neutral, no additional edge/risk.

RULE OF THUMB (3Commas / Bitsgap practitioners):
  Block new DCA base orders when funding > +0.05%/8h.
  This single filter has historically reduced DCA drawdowns significantly
  by avoiding the "everyone is long, crash imminent" setup.

DATA SOURCE
───────────
  Binance Futures public API — no authentication required.
  Endpoint: https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT

  Rate is returned as a fraction: 0.0001 = 0.01% per 8-hour period.

  Annualised context: 0.0001 × 3 × 365 ≈ 10.95% p.a. cost for longs.
  High threshold of 0.0005 (0.05%/8h) = ~54% annualised cost for longs —
  clearly unsustainable and signals extreme crowding.

FALLBACK
────────
  If the API call fails (network issue, rate limit), the provider
  returns the last successfully fetched rate and logs a warning.
  If no successful fetch has ever occurred, returns 0.0 (neutral) so
  the DCA filter never blocks trades on a pure connectivity failure.

CACHING
───────
  Funding rates update every 8 hours. The provider caches the last
  fetched value for `cache_seconds` (default: 3600 = 1 hour) to
  avoid hammering the API on every candle.

USAGE
─────
    from portfolio.funding_rate import FundingRateProvider

    provider = FundingRateProvider(symbol="BTCUSDT")

    # In main loop or portfolio manager:
    rate = provider.get_rate()         # e.g. 0.0001 (= 0.01%/8h)
    is_crowded = provider.is_crowded() # True if rate > threshold
"""

import time
from typing import Optional
from loguru import logger

try:
    import requests as _requests
    _REQUESTS_AVAILABLE = True
except ImportError:
    _REQUESTS_AVAILABLE = False


# ── Constants ────────────────────────────────────────────────────────────────

# Binance Futures public endpoint — free, no API key required.
BINANCE_PREMIUM_INDEX_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"

# Default thresholds (expressed as fractions per 8-hour funding period)
DEFAULT_FUNDING_MAX  = 0.0005   # 0.05%/8h → block new DCA longs (extreme crowding)
DEFAULT_FUNDING_MIN  = -0.0003  # -0.03%/8h → anything below is fine (shorts crowded)

# How long to cache a fetched rate before re-querying the API (seconds)
DEFAULT_CACHE_SECONDS = 3600    # 1 hour — funding updates every 8h so hourly is fine


class FundingRateProvider:
    """
    Fetches and caches the current perpetual futures funding rate.

    Designed to be instantiated once at startup and reused every candle.
    Thread-safe for single-threaded event loops (no locks needed).

    Args:
        symbol:          Binance futures symbol, e.g. "BTCUSDT", "ETHUSDT".
        cache_seconds:   How long to reuse a cached value before re-fetching.
        timeout:         HTTP request timeout in seconds.
        high_threshold:  Rate above this → market is overcrowded long.
        low_threshold:   Rate below this → market is overcrowded short.
    """

    def __init__(
        self,
        symbol: str = "BTCUSDT",
        cache_seconds: int = DEFAULT_CACHE_SECONDS,
        timeout: int = 5,
        high_threshold: float = DEFAULT_FUNDING_MAX,
        low_threshold: float = DEFAULT_FUNDING_MIN,
    ):
        self.symbol        = symbol.upper()
        self.cache_seconds = cache_seconds
        self.timeout       = timeout
        self.high_threshold = high_threshold
        self.low_threshold  = low_threshold

        # Internal state
        self._cached_rate: float = 0.0       # Last successfully fetched rate
        self._last_fetch_ts: float = 0.0     # Unix timestamp of last fetch
        self._fetch_count: int = 0           # Total successful fetches
        self._error_count: int = 0           # Consecutive fetch errors

        logger.info(
            f"FundingRateProvider initialized | "
            f"Symbol: {self.symbol} | "
            f"Cache: {cache_seconds}s | "
            f"Thresholds: >{high_threshold*100:.3f}% (block) / "
            f"<{low_threshold*100:.3f}% (fine)"
        )

    # ── Public API ────────────────────────────────────────────────────────────

    def get_rate(self, force_refresh: bool = False) -> float:
        """
        Return the current funding rate (fraction per 8-hour period).

        Uses cached value if within cache_seconds of last fetch.
        On failure, returns last known rate (or 0.0 if never fetched).

        Args:
            force_refresh: If True, bypass cache and fetch immediately.

        Returns:
            float — e.g. 0.0001 means 0.01%/8h.
                    Positive = longs pay shorts.
                    Negative = shorts pay longs.
        """
        now = time.time()
        cache_stale = (now - self._last_fetch_ts) >= self.cache_seconds

        if force_refresh or cache_stale:
            self._fetch()

        return self._cached_rate

    def is_crowded_long(self, rate: Optional[float] = None) -> bool:
        """
        True if the market is overcrowded to the long side.

        This is the primary DCA entry gate: when True, opening a new
        long DCA cycle is high-risk (leveraged flush likely).

        Args:
            rate: Pre-fetched rate to check. If None, calls get_rate().
        """
        r = rate if rate is not None else self.get_rate()
        return r > self.high_threshold

    def is_crowded_short(self, rate: Optional[float] = None) -> bool:
        """
        True if the market is overcrowded short (rare in bull markets).
        Actually a mild tailwind for new long DCA entries.

        Args:
            rate: Pre-fetched rate to check. If None, calls get_rate().
        """
        r = rate if rate is not None else self.get_rate()
        return r < self.low_threshold

    def sentiment_label(self, rate: Optional[float] = None) -> str:
        """Human-readable label for the current funding regime."""
        r = rate if rate is not None else self.get_rate()
        pct = r * 100
        if r > self.high_threshold:
            return f"CROWDED LONG ({pct:+.4f}%/8h) — longs at risk"
        elif r < self.low_threshold:
            return f"CROWDED SHORT ({pct:+.4f}%/8h) — short squeeze risk"
        else:
            return f"NEUTRAL ({pct:+.4f}%/8h)"

    def summary(self) -> str:
        """Return a one-line diagnostic string."""
        return (
            f"FundingRate({self.symbol}): {self._cached_rate*100:+.4f}%/8h | "
            f"{self.sentiment_label(self._cached_rate)} | "
            f"Fetches: {self._fetch_count} ok / {self._error_count} err"
        )

    # ── Private ───────────────────────────────────────────────────────────────

    def _fetch(self) -> None:
        """
        Fetch the current funding rate from Binance Futures public API.
        Updates _cached_rate on success; leaves it unchanged on failure.
        """
        if not _REQUESTS_AVAILABLE:
            logger.warning(
                "FundingRateProvider: 'requests' library not installed. "
                "pip install requests. Using rate=0.0 (neutral)."
            )
            return

        url = BINANCE_PREMIUM_INDEX_URL
        params = {"symbol": self.symbol}

        try:
            resp = _requests.get(url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()

            raw_rate = data.get("lastFundingRate")
            if raw_rate is None:
                raise ValueError(f"'lastFundingRate' not in response: {data}")

            rate = float(raw_rate)
            self._cached_rate = rate
            self._last_fetch_ts = time.time()
            self._fetch_count += 1
            self._error_count = 0   # Reset consecutive error count on success

            logger.info(
                f"[FundingRate] {self.symbol}: {rate*100:+.4f}%/8h "
                f"({self.sentiment_label(rate)}) | "
                f"Next funding: {data.get('nextFundingTime', 'N/A')}"
            )

        except Exception as exc:
            self._error_count += 1
            logger.warning(
                f"[FundingRate] Fetch failed ({exc}) — "
                f"using cached rate {self._cached_rate*100:+.4f}%/8h | "
                f"Consecutive errors: {self._error_count}"
            )
