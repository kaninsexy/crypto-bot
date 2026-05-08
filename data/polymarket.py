"""data/polymarket.py -- Polymarket Gamma + CLOB read-only data layer.

Phase 5 data layer. Mirrors the okx_perp.py / okx_funding.py shape:
parquet cache with TTL, public-read HTTP via `requests`, no-auth path
for the Gamma /markets endpoint.

Public API
----------
    load_or_fetch_markets(active=True, closed=False, limit=200,
                          ttl_hours=1, force_refresh=False)
        -> pd.DataFrame
        High-level cache-wrapped accessor for the Gamma /markets
        endpoint. Returns a DataFrame with one row per market, indexed
        by market id.

    fetch_orderbook(token_id) -> dict
        Live (no cache) CLOB /book fetch for a single outcome token.
        Used by downstream sizers / the scanner agent when it needs
        a current spread snapshot beyond Gamma's reported `spread`.

    filter_scanner_candidates(df, min_liquidity_usd=5000.0,
                              min_time_to_resolution_h=24.0,
                              max_spread_pp=5.0) -> pd.DataFrame
        Apply the scanner agent's default filters
        (.claude/agents/scanner.md step 3) to a markets DataFrame.

No-execute boundary
-------------------
This module ONLY hits Polymarket's READ endpoints:
  - GET https://gamma-api.polymarket.com/markets        (markets metadata)
  - GET https://clob.polymarket.com/book                (orderbook snapshot)

There is NO function in this module that POSTs to any Polymarket
endpoint, places an order, signs an order, or interacts with the
user's wallet. Phase 5's recommend-only execution boundary
(architecture.md D.3) is enforced at this module's surface: there
are no execution functions to call. If you need execution, that's
a separate Phase 5 implementation gate (Polymarket order placement
is the deliberate Phase-5 hook-layer no-execute boundary).

Cache shape
-----------
    Logical: ("polymarket", "markets", filter_key)
    Physical: backtest/cache/polymarket/markets/{filter_key}.parquet

`filter_key` encodes the active/closed flags so different filter sets
cache independently. The default `active=True&closed=False&limit=200`
maps to filter_key="active_open_200". The cache file's mtime is the
TTL anchor (same convention as okx_perp.py).
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import requests
from loguru import logger


# -- Configuration -----------------------------------------------------------

GAMMA_BASE_URL: str = "https://gamma-api.polymarket.com"
CLOB_BASE_URL: str = "https://clob.polymarket.com"

POLYMARKET_CACHE_DIR: Path = Path("backtest/cache/polymarket/markets")
DEFAULT_TTL_HOURS: float = 1.0
DEFAULT_REQUEST_TIMEOUT_S: float = 30.0
DEFAULT_PAGE_SIZE: int = 100  # Gamma /markets page size; smaller = faster first response


# Columns the cache parquet keeps. Anything Gamma returns that's not
# in this list is dropped before save -- avoids parquet schema drift
# when Gamma adds new fields. The scanner agent's filter set drives
# the column choice.
CACHE_COLUMNS: tuple[str, ...] = (
    "id",                      # market id (string)
    "slug",
    "question",
    "description",
    "conditionId",
    "active",
    "closed",
    "archived",
    "acceptingOrders",
    "endDate",                 # resolution datetime (ISO with tz)
    "endDateIso",              # resolution date (YYYY-MM-DD)
    "startDate",
    "createdAt",
    "updatedAt",
    "outcomes",                # JSON-string list, e.g. '["Yes","No"]'
    "outcomePrices",           # JSON-string list, e.g. '["0.55","0.45"]'
    "clobTokenIds",            # JSON-string list of CLOB token ids
    "bestBid",
    "bestAsk",
    "spread",                  # in price units (0.01 = 1 percentage point)
    "lastTradePrice",
    "liquidityNum",
    "volumeNum",
    "volume24hr",
    "volume1wk",
    "volume1mo",
    "volume1yr",
    "oneDayPriceChange",
    "oneWeekPriceChange",
    "oneMonthPriceChange",
    "oneYearPriceChange",
    "competitive",
    "groupItemTitle",
    "negRisk",
)


# -- Direct fetch (no cache) -------------------------------------------------

def _gamma_get(path: str, params: Optional[dict] = None) -> list | dict:
    """GET against the Gamma API; returns parsed JSON. No auth."""
    url = f"{GAMMA_BASE_URL}{path}"
    response = requests.get(
        url, params=params, timeout=DEFAULT_REQUEST_TIMEOUT_S,
    )
    response.raise_for_status()
    return response.json()


def fetch_markets_page(
    active: bool = True,
    closed: bool = False,
    limit: int = DEFAULT_PAGE_SIZE,
    offset: int = 0,
) -> list[dict]:
    """One Gamma /markets page. No cache. Public-read."""
    params: dict = {
        "active": "true" if active else "false",
        "closed": "true" if closed else "false",
        "limit": str(int(limit)),
        "offset": str(int(offset)),
    }
    result = _gamma_get("/markets", params=params)
    if not isinstance(result, list):
        raise RuntimeError(
            f"Gamma /markets returned {type(result).__name__}, "
            f"expected list; first 200 chars: {str(result)[:200]}"
        )
    return result


def fetch_markets_paginated(
    active: bool = True,
    closed: bool = False,
    limit: int = 200,
    page_size: int = DEFAULT_PAGE_SIZE,
    request_delay_s: float = 0.10,
) -> list[dict]:
    """Paginated /markets fetch. Returns up to `limit` markets total.

    Stops early if a page returns fewer than `page_size` rows
    (Gamma signals "no more results" via short page).
    """
    out: list[dict] = []
    offset = 0
    while len(out) < limit:
        page_limit = min(page_size, limit - len(out))
        page = fetch_markets_page(
            active=active, closed=closed,
            limit=page_limit, offset=offset,
        )
        if not page:
            break
        out.extend(page)
        if len(page) < page_limit:
            break
        offset += len(page)
        time.sleep(request_delay_s)
    logger.info(
        "[polymarket] fetched {} markets (active={} closed={} limit={})",
        len(out), active, closed, limit,
    )
    return out


def fetch_market_by_id(market_id: str) -> dict:
    """Single-market fetch via Gamma /markets/{id}. No cache (used by
    the paper-ledger resolution updater, where freshness matters).

    Returns the raw Gamma market dict (same schema as one element of
    `fetch_markets_page`). Resolved markets carry:
      - closed: true
      - outcomePrices: e.g., '["1.0", "0.0"]' for YES resolved,
        '["0.0", "1.0"]' for NO resolved.
    """
    response = requests.get(
        f"{GAMMA_BASE_URL}/markets/{market_id}",
        timeout=DEFAULT_REQUEST_TIMEOUT_S,
    )
    response.raise_for_status()
    return response.json()


def walk_asks_for_buy(
    token_id: str, size_usd: float,
) -> dict:
    """Walk the asks of `token_id`'s orderbook to compute the realistic
    average fill price for a BUY of `size_usd` notional.

    Polymarket asks are SELL-side resting orders -- to BUY a token you
    take liquidity from the asks, lowest price first. Each level
    contributes `price * size_units` USD of cost; we accumulate until
    `size_usd` is filled or the book is exhausted.

    Returns:
        {
            "avg_price":       float (size-weighted average),
            "filled_usd":      float (<= size_usd; equal if filled),
            "filled_units":    float,
            "levels_consumed": int,
            "top_ask":         float,
            "fully_filled":    bool,
            "depth_usd_top5":  float (top-5 ask USD depth, for context)
        }

    Used by the paper-ledger record path: instead of recording the
    top-of-book ask as entry_price, we record the realistic
    book-walked avg fill -- which is what a real BUY order at
    `size_usd` notional would actually pay (modulo timing).
    """
    if size_usd <= 0:
        raise ValueError(f"size_usd must be positive; got {size_usd!r}")
    book = fetch_orderbook(token_id)
    asks = book.get("asks", []) or []
    # Polymarket returns asks in unspecified order; sort ascending price.
    asks_sorted = sorted(
        asks, key=lambda x: float(x.get("price", 1e9))
    )
    if not asks_sorted:
        return {
            "avg_price": float("nan"), "filled_usd": 0.0,
            "filled_units": 0.0, "levels_consumed": 0,
            "top_ask": float("nan"), "fully_filled": False,
            "depth_usd_top5": 0.0,
        }

    top_ask = float(asks_sorted[0].get("price"))
    # Top-5 USD depth for forensics.
    depth_top5 = sum(
        float(a.get("price")) * float(a.get("size"))
        for a in asks_sorted[:5]
    )

    filled_usd = 0.0
    filled_units = 0.0
    cost_usd = 0.0
    levels_consumed = 0
    for level in asks_sorted:
        p = float(level.get("price"))
        s_avail = float(level.get("size"))
        avail_usd = p * s_avail
        if filled_usd + avail_usd <= size_usd:
            # Take whole level.
            filled_usd += avail_usd
            filled_units += s_avail
            cost_usd += avail_usd
            levels_consumed += 1
            if filled_usd >= size_usd:
                break
        else:
            # Partial fill of this level.
            remaining_usd = size_usd - filled_usd
            units_taken = remaining_usd / p
            filled_usd += remaining_usd
            filled_units += units_taken
            cost_usd += remaining_usd
            levels_consumed += 1
            break

    avg_price = cost_usd / filled_units if filled_units > 0 else float("nan")
    fully_filled = filled_usd >= size_usd - 1e-9
    return {
        "avg_price": avg_price,
        "filled_usd": filled_usd,
        "filled_units": filled_units,
        "levels_consumed": levels_consumed,
        "top_ask": top_ask,
        "fully_filled": fully_filled,
        "depth_usd_top5": depth_top5,
    }


def fetch_orderbook(token_id: str) -> dict:
    """CLOB /book for one outcome token. No cache (volatile data).

    Token ids are returned by Gamma /markets in the `clobTokenIds`
    field as a JSON-string list (one per outcome -- typically 2 for
    Yes/No markets). Pass either string element to this function.

    Returns:
        Raw CLOB orderbook dict. Schema (per Polymarket CLOB docs):
            {"market": <condition_id>,
             "asset_id": <token_id>,
             "timestamp": <ms epoch>,
             "bids": [{"price": "<float>", "size": "<float>"}, ...],
             "asks": [{"price": "<float>", "size": "<float>"}, ...]}
    """
    response = requests.get(
        f"{CLOB_BASE_URL}/book",
        params={"token_id": token_id},
        timeout=DEFAULT_REQUEST_TIMEOUT_S,
    )
    response.raise_for_status()
    return response.json()


# -- Cache wrapper -----------------------------------------------------------

def _cache_filename(active: bool, closed: bool, limit: int) -> str:
    """Build a stable cache filename from the filter combination."""
    a = "active" if active else "inactive"
    c = "closed" if closed else "open"
    return f"{a}_{c}_{int(limit)}.parquet"


def _cache_age_hours(path: Path) -> float:
    if not path.exists():
        return float("inf")
    return (time.time() - path.stat().st_mtime) / 3600.0


def _markets_to_df(markets: list[dict]) -> pd.DataFrame:
    """Flatten the Gamma response to a DataFrame keyed by id.

    Drops fields not in CACHE_COLUMNS so the parquet schema is stable
    across Gamma API minor revisions. Preserves the JSON-string
    representation of list-typed fields (outcomes, outcomePrices,
    clobTokenIds) -- callers can json.loads() on demand.
    """
    if not markets:
        return pd.DataFrame(columns=list(CACHE_COLUMNS)).set_index(
            pd.Index([], name="id")
        )

    rows: list[dict] = []
    for m in markets:
        row: dict = {}
        for col in CACHE_COLUMNS:
            v = m.get(col)
            # Coerce list/dict to JSON string for stable parquet.
            if isinstance(v, (list, dict)):
                v = json.dumps(v, ensure_ascii=False)
            row[col] = v
        rows.append(row)

    df = pd.DataFrame(rows)
    # Force string id (Polymarket uses string ids) and set as index.
    df["id"] = df["id"].astype(str)
    df = df.set_index("id")
    return df


def load_or_fetch_markets(
    active: bool = True,
    closed: bool = False,
    limit: int = 200,
    ttl_hours: float = DEFAULT_TTL_HOURS,
    force_refresh: bool = False,
) -> pd.DataFrame:
    """Cache-wrapped Gamma /markets accessor.

    Strategy:
      1. If cache file exists and age < ttl_hours and not force_refresh
         -> load and return cache.
      2. Otherwise, fetch from Gamma (paginated), save to parquet,
         return DataFrame.

    Args:
        active: Filter to active markets (Gamma default true).
        closed: Filter on closed flag (false = open markets).
        limit: Max markets to retrieve total.
        ttl_hours: Cache freshness window. Default 1h -- markets
            change rapidly enough that hour-stale data is the
            useful upper bound for the scanner; force_refresh=True
            for an immediate live read.
        force_refresh: Skip TTL check; always fetch fresh.

    Returns:
        DataFrame indexed by market id (string), one row per market.
        Columns are the CACHE_COLUMNS subset of Gamma's response.
        List-typed fields are stored as JSON strings (use json.loads
        downstream).
    """
    POLYMARKET_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = POLYMARKET_CACHE_DIR / _cache_filename(active, closed, limit)

    age = _cache_age_hours(cache_path)
    if cache_path.exists() and not force_refresh and age < ttl_hours:
        logger.info(
            "[polymarket] cache hit {} (age {:.2f}h < {:.2f}h ttl)",
            cache_path.name, age, ttl_hours,
        )
        return pd.read_parquet(cache_path)

    logger.info(
        "[polymarket] fetching markets (active={} closed={} limit={})",
        active, closed, limit,
    )
    markets = fetch_markets_paginated(
        active=active, closed=closed, limit=limit,
    )
    df = _markets_to_df(markets)
    df.to_parquet(cache_path)
    logger.info(
        "[polymarket] cached {} rows -> {}",
        len(df), cache_path,
    )
    return df


# -- Filtering helpers -------------------------------------------------------

def compute_time_to_resolution_hours(
    df: pd.DataFrame, now: Optional[pd.Timestamp] = None,
) -> pd.Series:
    """Return a Series of hours-until-resolution per market, indexed
    matching `df`. Markets with no `endDate` get NaN.
    """
    if now is None:
        now = pd.Timestamp.now(tz="UTC")
    end = pd.to_datetime(df["endDate"], utc=True, errors="coerce")
    delta = (end - now).dt.total_seconds() / 3600.0
    return delta


def filter_scanner_candidates(
    df: pd.DataFrame,
    min_liquidity_usd: float = 5_000.0,
    min_time_to_resolution_h: float = 24.0,
    max_spread_pp: float = 5.0,
    min_yes_price: float = 0.0,
    max_yes_price: float = 1.0,
    now: Optional[pd.Timestamp] = None,
) -> pd.DataFrame:
    """Apply the scanner agent's default filters to a markets DataFrame.

    Mirrors `.claude/agents/scanner.md` step 3:
      - Drop markets with liquidity < `min_liquidity_usd`
      - Drop markets with time-to-resolution < `min_time_to_resolution_h`
      - Drop markets with spread > `max_spread_pp` percentage points
        (Gamma's `spread` is in price units; 0.01 == 1 pp)
      - (extension 2026-05-08) Drop markets with bestAsk outside
        [min_yes_price, max_yes_price]. Default range is [0, 1] which
        is a no-op (all prices pass). Tighter ranges (e.g., 0.20-0.80)
        exclude long-odds / near-resolution markets where research
        cannot meaningfully shift the implied probability above the
        2pp edge threshold the sizer uses.

    Returns a fresh DataFrame; does not mutate input.
    """
    if df.empty:
        return df.copy()

    work = df.copy()
    # Coerce numerics (Gamma sometimes returns string-typed numbers).
    for col in ("liquidityNum", "spread", "bestAsk", "bestBid"):
        if col in work.columns:
            work[col] = pd.to_numeric(work[col], errors="coerce")

    ttr_h = compute_time_to_resolution_hours(work, now=now)
    work["_ttr_hours"] = ttr_h

    keep = (
        (work["liquidityNum"].fillna(0.0) >= min_liquidity_usd)
        & (work["_ttr_hours"].fillna(0.0) >= min_time_to_resolution_h)
        & ((work["spread"].fillna(1.0) * 100.0) <= max_spread_pp)
        & (work["bestAsk"].fillna(0.0) >= min_yes_price)
        & (work["bestAsk"].fillna(1.0) <= max_yes_price)
    )
    out = work[keep].drop(columns=["_ttr_hours"])
    logger.info(
        "[polymarket] filter_scanner_candidates: {} -> {} markets "
        "(min_liq=${:.0f} min_ttr={}h max_spread={:.2f}pp "
        "yes_price=[{:.2f},{:.2f}])",
        len(df), len(out),
        min_liquidity_usd, min_time_to_resolution_h, max_spread_pp,
        min_yes_price, max_yes_price,
    )
    return out


# -- CLI smoke test (no test framework dependency) ---------------------------

if __name__ == "__main__":
    import sys

    print("=" * 72)
    print("data/polymarket.py smoke test (live Gamma API)")
    print("=" * 72)

    df = load_or_fetch_markets(force_refresh=True, limit=10)
    print(f"\nFetched {len(df)} markets")
    print(f"Columns: {list(df.columns)}")
    print(f"\nTop-3 by liquidity:")
    by_liq = df.copy()
    by_liq["liquidityNum"] = pd.to_numeric(
        by_liq["liquidityNum"], errors="coerce",
    )
    show_cols = [
        "question", "liquidityNum", "spread",
        "bestBid", "bestAsk", "endDateIso",
    ]
    show_cols = [c for c in show_cols if c in df.columns]
    print(by_liq.nlargest(3, "liquidityNum")[show_cols].to_string())

    print("\nFilter to scanner candidates:")
    candidates = filter_scanner_candidates(df)
    print(f"  {len(candidates)} of {len(df)} pass scanner default filters")

    if not candidates.empty:
        # Try one orderbook fetch on the most-liquid candidate.
        top = candidates.nlargest(1, "liquidityNum").iloc[0]
        token_ids_raw = top.get("clobTokenIds")
        try:
            token_ids = json.loads(token_ids_raw)
        except Exception:
            token_ids = []
        if token_ids:
            print(
                f"\nOrderbook smoke for top candidate "
                f"({str(top.get('question',''))[:60]!r}):"
            )
            try:
                book = fetch_orderbook(token_ids[0])
                print(f"  asset_id : {book.get('asset_id', '?')[:16]}...")
                print(f"  bids     : {len(book.get('bids', []))} levels")
                print(f"  asks     : {len(book.get('asks', []))} levels")
                if book.get("bids"):
                    print(
                        f"  top bid  : "
                        f"{book['bids'][0].get('price')} x "
                        f"{book['bids'][0].get('size')}"
                    )
                if book.get("asks"):
                    print(
                        f"  top ask  : "
                        f"{book['asks'][0].get('price')} x "
                        f"{book['asks'][0].get('size')}"
                    )
            except Exception as e:
                print(f"  orderbook fetch failed: {e}")

    print("\n" + "=" * 72)
    print("smoke test complete")
    print("=" * 72)
    sys.exit(0)
