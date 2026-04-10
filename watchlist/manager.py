"""
watchlist/manager.py — Live Data-Driven Coin Watchlist (3-Tier Rotation)

PURPOSE:
  The watchlist decides WHICH coins the bot is allowed to trade.
  It's not a static list — coins rotate in and out based on live data,
  with strict safeguards to prevent trading garbage or recently-crashed coins.

3-TIER ARCHITECTURE:
  ┌─────────────────────────────────────────────────────────────────┐
  │  TIER 1 — Permanent (never rotates out)                         │
  │    BTC/USDT, ETH/USDT                                           │
  │    Rationale: Most liquidity, lowest manipulation risk,         │
  │    always have reliable signals. These are the anchor.          │
  │                                                                 │
  │  TIER 2 — Quarterly rotation (rechecked every 90 days)          │
  │    SOL, XRP, BNB, DOGE, ADA (top-5 by market cap, ex-BTC/ETH)  │
  │    Rationale: Large-cap alts with established ecosystems.       │
  │    Reviewed quarterly for major ranking changes.                │
  │                                                                 │
  │  TIER 3 — Monthly rotation (rechecked every 30 days)            │
  │    Smaller alts chosen by: 30-day volume, LunarCrush score,    │
  │    price momentum (3-month return), and safety filters.         │
  │    Max 4 slots. Highest-scoring qualifying coins fill slots.    │
  └─────────────────────────────────────────────────────────────────┘

8 SAFEGUARDS (all must pass for a coin to enter Tier 3):
  1. Age filter:        Listed on Binance ≥ 6 months (not a new launch)
  2. Volume floor:      24h trading volume ≥ $50,000 USDT
  3. Crash blacklist:   Has NOT dropped ≥ 35% in the last 30 days
  4. BTC+ETH floor:     BTC+ETH always ≥ 50% of total portfolio
  5. Mid-position lock: Can't swap out a coin we currently hold (forces full exit first)
  6. Stablecoin filter: USDT, USDC, BUSD, DAI, TUSD, FDUSD excluded
  7. Spread check:      Bid-ask spread < 0.15% (ensures liquidity)
  8. Position limit:    Max 6 simultaneous open positions across all strategies

SCORING (for Tier 3 candidates):
  Score = (volume_score × 0.30) + (momentum_score × 0.40) + (sentiment_score × 0.30)
  - volume_score:    Normalized 24h volume (0-100)
  - momentum_score:  30-day price return (capped at -50% to +100%), normalized 0-100
  - sentiment_score: LunarCrush score (0-100), defaults to 50 if unavailable

  Crash penalty: if 30-day return < -35%, coin is blacklisted entirely (not just penalised)

DATA SOURCES:
  - Binance public API (via ccxt): price, volume, spread, listing date
  - LunarCrush (via core/sentiment.py): social sentiment scores
  - Falls back gracefully if either source is unavailable

USAGE:
    wl = WatchlistManager(exchange)
    wl.refresh()                     # Run at startup and on schedule

    symbols = wl.get_active_symbols()  # e.g. ["BTC/USDT", "ETH/USDT", "SOL/USDT", ...]
    if wl.is_tradeable("DOGE/USDT"):
        # Run strategies on DOGE
        ...

    # Check before adding a position
    if wl.can_open_position("SOL/USDT", open_positions):
        # Allowed to enter
        ...
"""

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

from loguru import logger

# ── Constants ─────────────────────────────────────────────────────────────────

TIER_1 = ["BTC/USDT", "ETH/USDT"]

TIER_2_CANDIDATES = [
    "SOL/USDT", "XRP/USDT", "BNB/USDT", "DOGE/USDT", "ADA/USDT",
    "AVAX/USDT", "TRX/USDT", "LINK/USDT", "DOT/USDT", "MATIC/USDT",
]

STABLECOINS = {
    # Fiat-backed USD stablecoins
    "USDT", "USDC", "BUSD", "DAI", "TUSD", "FDUSD", "USDP", "FRAX", "LUSD",
    "USD1",   # World Liberty Financial USD (Trump)
    "XUSD",   # xUSD stablecoin
    "RLUSD",  # Ripple USD stablecoin
    "USDE",   # Ethena USDe
    "PYUSD",  # PayPal USD
    "CRVUSD", # Curve Finance USD
    # EUR stablecoins and fiat FX tokens
    "EURT", "EURC", "EURS", "EUR",   # EUR/USDT is a Forex pair, not crypto
    # Gold-backed (commodity, not crypto)
    "XAUT", "PAXG",
}

MIN_VOLUME_USDT    = 50_000       # 24h volume floor
MIN_AGE_DAYS       = 180          # 6 months minimum listing age
CRASH_THRESHOLD    = -0.35        # -35% in 30 days = blacklisted
MAX_SPREAD_PCT     = 0.0015       # 0.15% bid-ask spread
MAX_POSITIONS      = 6            # Hard cap on simultaneous positions
BTC_ETH_MIN_SHARE  = 0.50         # BTC+ETH ≥ 50% of portfolio value always
TIER_3_MAX_SLOTS   = 4            # Max Tier 3 coins active at once


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class CoinInfo:
    """Live data snapshot for a candidate coin."""
    symbol: str                         # e.g. "SOL/USDT"
    base: str                           # e.g. "SOL"
    price: float = 0.0
    volume_24h_usdt: float = 0.0
    change_30d_pct: float = 0.0         # 30-day price return as decimal (e.g. -0.12 = -12%)
    spread_pct: float = 0.0             # (ask-bid) / mid as decimal
    sentiment_score: float = 50.0       # LunarCrush 0-100
    listing_date: Optional[datetime] = None
    score: float = 0.0                  # Composite ranking score (0-100)

    # Safeguard pass/fail results
    passed_age: bool = False
    passed_volume: bool = False
    passed_crash: bool = False
    passed_spread: bool = False
    passed_stablecoin: bool = True      # Most coins pass this; stables explicitly fail

    @property
    def age_days(self) -> Optional[int]:
        if self.listing_date is None:
            return None
        return (datetime.now(timezone.utc) - self.listing_date).days

    @property
    def passes_all_guards(self) -> bool:
        return (
            self.passed_age
            and self.passed_volume
            and self.passed_crash
            and self.passed_spread
            and self.passed_stablecoin
        )


@dataclass
class WatchlistState:
    tier1: list[str]                = field(default_factory=list)
    tier2: list[str]                = field(default_factory=list)
    tier3: list[str]                = field(default_factory=list)
    blacklisted: list[str]          = field(default_factory=list)
    last_tier2_refresh: Optional[datetime] = None
    last_tier3_refresh: Optional[datetime] = None
    coin_data: dict[str, CoinInfo]  = field(default_factory=dict)


# ── Watchlist Manager ─────────────────────────────────────────────────────────

class WatchlistManager:
    """
    Live-data coin watchlist with 3-tier rotation and 8 safeguards.
    Designed to run every hour in the bot's main loop.
    """

    def __init__(
        self,
        exchange,
        tier2_refresh_days: int = 90,
        tier3_refresh_days: int = 30,
        tier3_slots: int = TIER_3_MAX_SLOTS,
    ):
        """
        Args:
            exchange:              ccxt exchange instance (paper or live).
            tier2_refresh_days:    How often Tier 2 is reviewed (days).
            tier3_refresh_days:    How often Tier 3 rotates (days).
            tier3_slots:           Max Tier 3 coins at once.
        """
        self.exchange = exchange
        self.tier2_refresh_days = tier2_refresh_days
        self.tier3_refresh_days = tier3_refresh_days
        self.tier3_slots = tier3_slots

        self._state = WatchlistState(tier1=TIER_1.copy())
        self._open_positions: set[str] = set()   # Symbols currently held

        logger.info(
            f"WatchlistManager initialized | "
            f"Tier1: {TIER_1} | "
            f"Tier2 refresh: {tier2_refresh_days}d | "
            f"Tier3 refresh: {tier3_refresh_days}d | "
            f"Tier3 slots: {tier3_slots}"
        )

    # ── Public API ────────────────────────────────────────────────────────────

    def get_active_symbols(self) -> list[str]:
        """Return all currently tradeable symbols (Tier 1 + 2 + 3)."""
        return (
            self._state.tier1
            + self._state.tier2
            + self._state.tier3
        )

    def is_tradeable(self, symbol: str) -> bool:
        """True if this symbol is on the active watchlist."""
        return symbol in self.get_active_symbols()

    def can_open_position(
        self,
        symbol: str,
        open_positions: dict,     # {symbol: position_value_usdt}
        portfolio_value_usdt: float = 0.0,
    ) -> tuple[bool, str]:
        """
        Full pre-trade check: watchlist membership + position cap + BTC/ETH floor.

        Args:
            symbol:               The coin we want to trade.
            open_positions:       Dict of currently open positions {symbol: value_usdt}.
            portfolio_value_usdt: Total portfolio value for BTC/ETH floor check.

        Returns:
            (allowed: bool, reason: str)
        """
        if not self.is_tradeable(symbol):
            return False, f"{symbol} not on active watchlist"

        if symbol in open_positions:
            return True, f"{symbol} already in portfolio — adding to existing position OK"

        if len(open_positions) >= MAX_POSITIONS:
            return False, f"Max positions reached ({MAX_POSITIONS}). Close one before opening {symbol}."

        # BTC+ETH floor check
        if portfolio_value_usdt > 0:
            btc_eth_value = sum(
                v for s, v in open_positions.items()
                if s in ("BTC/USDT", "ETH/USDT")
            )
            btc_eth_share = btc_eth_value / portfolio_value_usdt
            if btc_eth_share < BTC_ETH_MIN_SHARE and symbol not in ("BTC/USDT", "ETH/USDT"):
                return (
                    False,
                    f"BTC+ETH floor: {btc_eth_share*100:.1f}% < {BTC_ETH_MIN_SHARE*100:.0f}% required. "
                    f"Add BTC or ETH first."
                )

        return True, "All checks passed"

    def notify_position_opened(self, symbol: str) -> None:
        """Call when a position is opened. Locks the coin during Tier 3 rotation."""
        self._open_positions.add(symbol)

    def notify_position_closed(self, symbol: str) -> None:
        """Call when a position is closed. Frees the coin for rotation."""
        self._open_positions.discard(symbol)

    # ── Refresh logic ─────────────────────────────────────────────────────────

    def refresh(self, force: bool = False) -> None:
        """
        Check if Tier 2 or Tier 3 needs updating and run the refresh if so.
        Safe to call every candle — only does work when the interval has elapsed.

        Args:
            force: If True, always refresh both tiers regardless of schedule.
        """
        now = datetime.now(timezone.utc)

        tier2_due = (
            force
            or self._state.last_tier2_refresh is None
            or (now - self._state.last_tier2_refresh).days >= self.tier2_refresh_days
        )

        tier3_due = (
            force
            or self._state.last_tier3_refresh is None
            or (now - self._state.last_tier3_refresh).days >= self.tier3_refresh_days
        )

        if tier2_due:
            self._refresh_tier2()
            self._state.last_tier2_refresh = now

        if tier3_due:
            self._refresh_tier3()
            self._state.last_tier3_refresh = now

    def _refresh_tier2(self) -> None:
        """Update Tier 2 from the candidate list, applying safety filters."""
        logger.info("Refreshing Tier 2 watchlist...")
        approved = []
        for symbol in TIER_2_CANDIDATES:
            if symbol in TIER_1:
                continue
            info = self._fetch_coin_info(symbol)
            if info is None:
                continue
            self._apply_safeguards(info)
            if info.passed_volume and info.passed_crash and info.passed_stablecoin:
                approved.append(symbol)
                logger.info(f"  ✓ Tier 2: {symbol} | vol=${info.volume_24h_usdt:,.0f} | 30d={info.change_30d_pct*100:+.1f}%")
            else:
                logger.warning(f"  ✗ Tier 2 REJECTED: {symbol} — vol_ok={info.passed_volume} crash_ok={info.passed_crash}")

        self._state.tier2 = approved
        logger.info(f"Tier 2 updated: {approved}")

    def _refresh_tier3(self) -> None:
        """Score and rank candidates for Tier 3 slots."""
        logger.info("Refreshing Tier 3 watchlist...")

        # Get top-200 coins by volume from Binance
        candidates = self._get_volume_candidates(top_n=80)

        scored = []
        for symbol in candidates:
            base = symbol.replace("/USDT", "")

            # Skip Tier 1 and Tier 2 (already covered)
            if symbol in TIER_1 or symbol in self._state.tier2:
                continue

            # Skip stablecoins
            if base in STABLECOINS:
                continue

            info = self._fetch_coin_info(symbol)
            if info is None:
                continue

            self._apply_safeguards(info)

            if not info.passes_all_guards:
                reasons = []
                if not info.passed_age:
                    age_str = f"{info.age_days}d" if info.age_days is not None else f"vol=${info.volume_24h_usdt/1e6:.1f}M<$5M"
                    reasons.append(f"age_fail({age_str})")
                if not info.passed_volume:
                    reasons.append(f"vol=${info.volume_24h_usdt:,.0f}<${MIN_VOLUME_USDT:,.0f}")
                if not info.passed_crash:
                    reasons.append(f"30d={info.change_30d_pct*100:.1f}%<{CRASH_THRESHOLD*100:.0f}%")
                if not info.passed_spread:
                    reasons.append(f"spread={info.spread_pct*100:.3f}%>{MAX_SPREAD_PCT*100:.3f}%")
                logger.debug(f"  ✗ {symbol} rejected: {', '.join(reasons)}")

                if not info.passed_crash:
                    if symbol not in self._state.blacklisted:
                        self._state.blacklisted.append(symbol)
                        logger.warning(f"  🚫 {symbol} BLACKLISTED: 30d crash {info.change_30d_pct*100:.1f}%")
                continue

            # Score the coin
            info.score = self._compute_score(info)
            scored.append(info)
            logger.debug(
                f"  {symbol}: score={info.score:.1f} | "
                f"vol=${info.volume_24h_usdt/1e6:.1f}M | "
                f"30d={info.change_30d_pct*100:+.1f}% | "
                f"sent={info.sentiment_score:.0f}"
            )

        # Sort by score descending
        scored.sort(key=lambda x: x.score, reverse=True)

        # Apply mid-position lock: can't remove coins we currently hold
        locked = [s for s in self._state.tier3 if s in self._open_positions]

        # Fill remaining slots with top-scored coins not already locked in
        new_tier3 = locked.copy()
        for info in scored:
            if len(new_tier3) >= self.tier3_slots:
                break
            if info.symbol not in new_tier3:
                new_tier3.append(info.symbol)
                logger.info(
                    f"  ✓ Tier 3: {info.symbol} | score={info.score:.1f} | "
                    f"vol=${info.volume_24h_usdt/1e6:.1f}M | "
                    f"30d={info.change_30d_pct*100:+.1f}% | "
                    f"sent={info.sentiment_score:.0f}"
                )

        # Log rotation
        removed = [s for s in self._state.tier3 if s not in new_tier3]
        added   = [s for s in new_tier3 if s not in self._state.tier3]
        if removed:
            logger.info(f"Tier 3 OUT: {removed}")
        if added:
            logger.info(f"Tier 3 IN:  {added}")

        self._state.tier3 = new_tier3
        self._state.coin_data.update({i.symbol: i for i in scored})
        logger.info(f"Tier 3 updated: {new_tier3}")

    # ── Data fetching ─────────────────────────────────────────────────────────

    def _get_volume_candidates(self, top_n: int = 80) -> list[str]:
        """
        Fetch top N USDT-quoted symbols from Binance by 24h volume.
        Returns list of symbols like ["BTC/USDT", "ETH/USDT", ...].
        """
        try:
            tickers = self.exchange.fetch_tickers()
            usdt_tickers = {
                k: v for k, v in tickers.items()
                if k.endswith("/USDT") and v.get("quoteVolume")
            }
            # Sort by quoteVolume (USDT volume) descending
            sorted_symbols = sorted(
                usdt_tickers.keys(),
                key=lambda s: usdt_tickers[s].get("quoteVolume", 0),
                reverse=True
            )
            result = sorted_symbols[:top_n]
            logger.info(f"Fetched {len(result)} USDT pairs from Binance (sorted by volume)")
            return result
        except Exception as e:
            logger.warning(f"Could not fetch volume candidates: {e}. Using fallback list.")
            return TIER_2_CANDIDATES  # Fallback to known list

    def _fetch_coin_info(self, symbol: str) -> Optional[CoinInfo]:
        """
        Fetch live data for a single symbol from the exchange.
        Returns None if data is unavailable or the symbol doesn't exist.
        """
        base = symbol.replace("/USDT", "")
        try:
            ticker = self.exchange.fetch_ticker(symbol)
            if ticker is None:
                return None

            price  = float(ticker.get("last") or ticker.get("close") or 0)
            vol_24 = float(ticker.get("quoteVolume") or 0)
            bid    = float(ticker.get("bid") or price * 0.9999)
            ask    = float(ticker.get("ask") or price * 1.0001)
            mid    = (bid + ask) / 2
            spread = (ask - bid) / mid if mid > 0 else 1.0

            # 30-day return: use percentage change if available from ticker,
            # otherwise approximate from OHLCV
            change_30d = self._fetch_30d_return(symbol, price)

            # Listing date: try exchange.markets metadata
            listing_date = self._fetch_listing_date(symbol)

            # Sentiment: try LunarCrush, default to neutral
            sentiment = self._fetch_sentiment(base)

            return CoinInfo(
                symbol=symbol,
                base=base,
                price=price,
                volume_24h_usdt=vol_24,
                change_30d_pct=change_30d,
                spread_pct=spread,
                sentiment_score=sentiment,
                listing_date=listing_date,
            )
        except Exception as e:
            logger.debug(f"Could not fetch info for {symbol}: {e}")
            return None

    def _fetch_30d_return(self, symbol: str, current_price: float) -> float:
        """
        Compute 30-day price return. Uses daily OHLCV (30 candles).
        Returns 0.0 on failure (treated as neutral, not crashed).
        """
        try:
            ohlcv = self.exchange.fetch_ohlcv(symbol, "1d", limit=31)
            if not ohlcv or len(ohlcv) < 2:
                return 0.0
            price_30d_ago = float(ohlcv[0][4])  # close of oldest candle
            if price_30d_ago <= 0:
                return 0.0
            return (current_price - price_30d_ago) / price_30d_ago
        except Exception:
            return 0.0

    def _fetch_listing_date(self, symbol: str) -> Optional[datetime]:
        """
        Attempt to retrieve the listing date from exchange.markets metadata.
        Falls back to None if not available (treated as unknown age).
        """
        try:
            markets = self.exchange.load_markets()
            market = markets.get(symbol, {})
            info = market.get("info", {})

            # Binance onboardDate is in milliseconds
            onboard_ms = info.get("onboardDate") or info.get("listTime")
            if onboard_ms:
                return datetime.fromtimestamp(int(onboard_ms) / 1000, tz=timezone.utc)

            # Some exchanges provide 'created' as ISO string
            created = market.get("created")
            if created:
                return datetime.fromisoformat(str(created).replace("Z", "+00:00"))

            return None
        except Exception:
            return None

    def _fetch_sentiment(self, coin: str) -> float:
        """Fetch LunarCrush sentiment. Returns 50 (neutral) on failure."""
        try:
            from core.sentiment import get_coin_sentiment
            return get_coin_sentiment(coin)
        except Exception:
            return 50.0

    # ── Safeguards ────────────────────────────────────────────────────────────

    def _apply_safeguards(self, info: CoinInfo) -> None:
        """Run all 8 safeguard checks and set pass/fail flags on the CoinInfo."""

        # 1. Stablecoin exclusion
        info.passed_stablecoin = info.base not in STABLECOINS

        # 2. Volume floor
        info.passed_volume = info.volume_24h_usdt >= MIN_VOLUME_USDT

        # 3. Crash blacklist (35% drop in 30 days)
        info.passed_crash = info.change_30d_pct > CRASH_THRESHOLD

        # 4. Spread check
        info.passed_spread = info.spread_pct < MAX_SPREAD_PCT

        # 5. Age filter
        # Binance's ccxt API rarely exposes onboardDate, so we use volume as a proxy:
        # Any coin with >$5M USDT/day almost certainly has been live ≥6 months.
        # If listing_date IS known, use it directly.
        VOLUME_AGE_BYPASS = 5_000_000   # $5M/day → assume established
        if info.listing_date is not None:
            info.passed_age = (info.age_days or 0) >= MIN_AGE_DAYS
        elif info.volume_24h_usdt >= VOLUME_AGE_BYPASS:
            info.passed_age = True       # High-volume proxy: almost certainly old enough
        else:
            # Low volume + no listing date → too risky (new/obscure token)
            info.passed_age = False

        # Safeguards 6-8 are enforced at the portfolio level (not per-coin):
        # 6. BTC+ETH floor — checked in can_open_position()
        # 7. Mid-position lock — enforced in _refresh_tier3() via _open_positions
        # 8. Max 6 positions — checked in can_open_position()

    # ── Scoring ───────────────────────────────────────────────────────────────

    def _compute_score(self, info: CoinInfo) -> float:
        """
        Composite score for Tier 3 ranking (0-100).
        Weights: volume 30%, momentum 40%, sentiment 30%.
        """
        # Volume score: log-normalized (prevents BTC from dominating)
        import math
        vol_score = min(100, math.log10(max(1, info.volume_24h_usdt)) / math.log10(1e9) * 100)

        # Momentum score: 30d return capped to [-50%, +100%], mapped to 0-100
        return_pct = max(-0.50, min(1.00, info.change_30d_pct))
        momentum_score = ((return_pct + 0.50) / 1.50) * 100  # -50%→0, 0%→33, +100%→100

        # Sentiment score: already 0-100 from LunarCrush
        sentiment_score = max(0, min(100, info.sentiment_score))

        return (vol_score * 0.30) + (momentum_score * 0.40) + (sentiment_score * 0.30)

    # ── Reporting ─────────────────────────────────────────────────────────────

    def summary(self) -> str:
        active = self.get_active_symbols()
        lines = [
            "=" * 54,
            "         WATCHLIST SUMMARY",
            "=" * 54,
            f"  Tier 1 (permanent)  : {', '.join(self._state.tier1)}",
            f"  Tier 2 (quarterly)  : {', '.join(self._state.tier2) or 'none yet'}",
            f"  Tier 3 (monthly)    : {', '.join(self._state.tier3) or 'none yet'}",
            f"  Total active        : {len(active)} symbols",
            f"  Blacklisted (crash) : {len(self._state.blacklisted)} coins",
            f"  Open positions      : {len(self._open_positions)} / {MAX_POSITIONS}",
        ]
        if self._state.last_tier2_refresh:
            days_ago = (datetime.now(timezone.utc) - self._state.last_tier2_refresh).days
            lines.append(f"  Tier 2 last refresh : {days_ago}d ago")
        if self._state.last_tier3_refresh:
            days_ago = (datetime.now(timezone.utc) - self._state.last_tier3_refresh).days
            lines.append(f"  Tier 3 last refresh : {days_ago}d ago")
        lines.append("=" * 54)
        return "\n".join(lines)

    def get_blacklist(self) -> list[str]:
        return self._state.blacklisted.copy()

    def get_coin_data(self, symbol: str) -> Optional[CoinInfo]:
        return self._state.coin_data.get(symbol)
