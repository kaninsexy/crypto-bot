"""data/tests/test_okx_funding.py — unit tests for OKX funding-rate ingestion.

Covers cache hit/miss, paginated funding history, mark-price snap-merge,
and the cadence-detection helper that gates the HALT-AND-CONSULT trigger.
The CCXT exchange is mocked — tests do not hit the live OKX endpoint.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd
import pytest

from data import okx_funding


# ── Synthetic data builders ──────────────────────────────────────────────────

def _make_funding_batch(start_ms: int, n: int, cadence_h: int = 8,
                        rate_seed: float = 0.0001) -> list[dict]:
    """Funding-rate-history rows in CCXT shape."""
    step_ms = cadence_h * 3_600_000
    return [
        {
            "timestamp": start_ms + i * step_ms,
            "datetime": "",  # CCXT fills this; we leave blank for tests
            "symbol": "BTC/USDT:USDT",
            "fundingRate": rate_seed * (1 + 0.01 * i),
            "info": {},
        }
        for i in range(n)
    ]


def _make_mark_ohlcv_batch(start_ms: int, n: int, step_ms: int = 3_600_000,
                           price_seed: float = 50000.0) -> list[list]:
    """Mark OHLCV rows: [ts, open, high, low, close, volume]."""
    return [
        [start_ms + i * step_ms, price_seed + i, price_seed + i + 100,
         price_seed + i - 100, price_seed + i + 50, 0.0]
        for i in range(n)
    ]


# ── Cadence detector ────────────────────────────────────────────────────────

class TestCadenceDetector:
    def test_clean_8h_cadence(self):
        idx = pd.date_range("2025-01-01", periods=10, freq="8h", tz="UTC")
        df = pd.DataFrame({"funding_rate": np.arange(10) * 1e-5,
                           "mark_price": np.full(10, 50000.0)}, index=idx)
        cadence = okx_funding.detect_funding_cadence(df)
        assert cadence["is_8h"] is True
        assert cadence["median_seconds"] == 8 * 3600
        assert cadence["n_gaps"] == 9

    def test_4h_cadence_flagged_not_8h(self):
        idx = pd.date_range("2025-01-01", periods=6, freq="4h", tz="UTC")
        df = pd.DataFrame({"funding_rate": np.zeros(6),
                           "mark_price": np.zeros(6)}, index=idx)
        cadence = okx_funding.detect_funding_cadence(df)
        assert cadence["is_8h"] is False
        assert cadence["median_seconds"] == 4 * 3600

    def test_tolerates_60s_jitter_at_8h(self):
        # Settlements drifted by ±30s — still within tolerance.
        ts = [pd.Timestamp("2025-01-01T00:00:00Z"),
              pd.Timestamp("2025-01-01T08:00:30Z"),
              pd.Timestamp("2025-01-01T15:59:45Z"),
              pd.Timestamp("2025-01-02T00:00:10Z")]
        df = pd.DataFrame({"funding_rate": np.zeros(4),
                           "mark_price": np.zeros(4)}, index=pd.DatetimeIndex(ts))
        cadence = okx_funding.detect_funding_cadence(df)
        assert cadence["is_8h"] is True

    def test_empty_or_singleton_returns_zeros(self):
        df_empty = pd.DataFrame({"funding_rate": [], "mark_price": []},
                                index=pd.DatetimeIndex([]))
        cadence = okx_funding.detect_funding_cadence(df_empty)
        assert cadence["n_gaps"] == 0
        assert cadence["is_8h"] is False


# ── Paginated funding fetch + mark-price snap-merge ─────────────────────────

class TestFetchFundingHistory:
    def test_single_funding_batch_with_mark_merge(self, monkeypatch):
        ex = mock.MagicMock()
        ex.markets = {"BTC/USDT:USDT": {}}

        # Funding settles at 00:00 / 08:00 / 16:00.
        # Use a fixed start timestamp aligned to UTC midnight.
        funding_start = int(pd.Timestamp("2025-01-01T00:00:00Z").timestamp() * 1000)
        ex.fetch_funding_rate_history.return_value = _make_funding_batch(
            funding_start, 5, cadence_h=8,
        )

        # Mark candles at 1h cadence covering the funding window
        # (start at funding_start - 1h cushion).
        mark_start = funding_start - 3_600_000
        # Need 5 funding × 8h = 32 hours of mark candles.  Generate 40 to spare.
        ex.fetch_mark_ohlcv.return_value = _make_mark_ohlcv_batch(mark_start, 40)

        monkeypatch.setattr(okx_funding, "_make_swap_exchange", lambda: ex)

        df = okx_funding.fetch_funding_history(
            "BTC/USDT", months=1, batch_size=100, request_delay_s=0,
        )
        assert list(df.columns) == ["funding_rate", "mark_price"]
        assert len(df) == 5
        # Every row has a mark_price filled (snap-merge worked).
        assert df["mark_price"].notna().all()
        # Mark candle indexed at the funding settlement uses its OPEN.
        # Our synthetic generator sets open = price_seed + i; mark_start
        # is 1h before funding_start so the candle at funding_start is
        # index 1 in the mark batch (open = 50000 + 1 = 50001).
        assert df.iloc[0]["mark_price"] == 50001.0

    def test_paginated_funding_history(self, monkeypatch):
        """Two full-size funding batches followed by one partial → 3 calls."""
        funding_start = int(pd.Timestamp("2025-01-01T00:00:00Z").timestamp() * 1000)
        full1 = _make_funding_batch(funding_start, 100)
        full2 = _make_funding_batch(funding_start + 100 * 8 * 3_600_000, 100)
        tail = _make_funding_batch(funding_start + 200 * 8 * 3_600_000, 30)

        ex = mock.MagicMock()
        ex.markets = {"BTC/USDT:USDT": {}}
        ex.fetch_funding_rate_history.side_effect = [full1, full2, tail]
        ex.fetch_mark_ohlcv.return_value = _make_mark_ohlcv_batch(
            funding_start - 3_600_000, 250 * 8 + 5,
        )

        monkeypatch.setattr(okx_funding, "_make_swap_exchange", lambda: ex)

        df = okx_funding.fetch_funding_history(
            "BTC/USDT", months=2, batch_size=100, request_delay_s=0,
        )
        assert ex.fetch_funding_rate_history.call_count == 3
        assert len(df) == 230
        # 8h cadence preserved.
        cadence = okx_funding.detect_funding_cadence(df)
        assert cadence["is_8h"] is True

    def test_calls_with_ccxt_swap_symbol(self, monkeypatch):
        """Both fetch_funding_rate_history and fetch_mark_ohlcv get CCXT symbol."""
        ex = mock.MagicMock()
        ex.markets = {"BTC/USDT:USDT": {}}
        funding_start = int(pd.Timestamp("2025-01-01T00:00:00Z").timestamp() * 1000)
        ex.fetch_funding_rate_history.return_value = _make_funding_batch(funding_start, 3)
        ex.fetch_mark_ohlcv.return_value = _make_mark_ohlcv_batch(
            funding_start - 3_600_000, 30,
        )

        monkeypatch.setattr(okx_funding, "_make_swap_exchange", lambda: ex)

        okx_funding.fetch_funding_history(
            "BTC/USDT", months=1, batch_size=100, request_delay_s=0,
        )
        assert ex.fetch_funding_rate_history.call_args_list[0].args[0] == "BTC/USDT:USDT"
        assert ex.fetch_mark_ohlcv.call_args_list[0].args[0] == "BTC/USDT:USDT"


# ── Cache hit/miss ──────────────────────────────────────────────────────────

class TestFundingCacheRoundtrip:
    def test_cache_miss_writes_parquet(self, tmp_path, monkeypatch):
        cache_dir = tmp_path / "perp_funding"

        funding_start = int(pd.Timestamp("2025-01-01T00:00:00Z").timestamp() * 1000)
        ex = mock.MagicMock()
        ex.markets = {"BTC/USDT:USDT": {}}
        ex.fetch_funding_rate_history.return_value = _make_funding_batch(funding_start, 5)
        ex.fetch_mark_ohlcv.return_value = _make_mark_ohlcv_batch(
            funding_start - 3_600_000, 40,
        )

        monkeypatch.setattr(okx_funding, "_make_swap_exchange", lambda: ex)

        df = okx_funding.load_or_fetch_funding_history(
            "BTC/USDT", months=1, cache_dir=cache_dir, ttl_hours=24,
        )
        assert len(df) == 5
        assert (cache_dir / "BTC-USDT-SWAP_funding_1mo.parquet").exists()

    def test_cache_hit_skips_network(self, tmp_path, monkeypatch):
        cache_dir = tmp_path / "perp_funding"
        cache_dir.mkdir(parents=True)
        idx = pd.date_range("2025-01-01", periods=6, freq="8h", tz="UTC")
        idx.name = "timestamp"
        seed = pd.DataFrame({
            "funding_rate": np.linspace(1e-5, 6e-5, 6),
            "mark_price":   np.full(6, 50000.0),
        }, index=idx)
        seed.to_parquet(cache_dir / "BTC-USDT-SWAP_funding_1mo.parquet")

        ex = mock.MagicMock()
        # If hit-path runs, exchange would be used — we assert it isn't.
        monkeypatch.setattr(okx_funding, "_make_swap_exchange", lambda: ex)

        df = okx_funding.load_or_fetch_funding_history(
            "BTC/USDT", months=1, cache_dir=cache_dir, ttl_hours=24,
        )
        assert ex.fetch_funding_rate_history.call_count == 0
        assert ex.fetch_mark_ohlcv.call_count == 0
        assert len(df) == 6

    def test_until_ts_clipping(self, tmp_path, monkeypatch):
        cache_dir = tmp_path / "perp_funding"
        cache_dir.mkdir(parents=True)
        idx = pd.date_range("2025-01-01", periods=10, freq="8h", tz="UTC")
        idx.name = "timestamp"
        seed = pd.DataFrame({
            "funding_rate": np.linspace(1e-5, 10e-5, 10),
            "mark_price":   np.full(10, 50000.0),
        }, index=idx)
        seed.to_parquet(cache_dir / "BTC-USDT-SWAP_funding_1mo.parquet")

        cutoff = pd.Timestamp("2025-01-02T00:00:00Z")
        df = okx_funding.load_or_fetch_funding_history(
            "BTC/USDT", months=1, cache_dir=cache_dir, ttl_hours=24,
            until_ts=cutoff,
        )
        # Original index has 10 rows over 80h starting Jan 1.  Cutoff at Jan 2
        # 00:00 keeps rows at 00:00, 08:00, 16:00 of Jan 1 only (3 rows).
        assert len(df) == 3
        assert df.index.max() < cutoff
