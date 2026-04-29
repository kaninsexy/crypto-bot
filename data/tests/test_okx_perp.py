"""data/tests/test_okx_perp.py — unit tests for OKX perp OHLCV ingestion.

Covers symbol translation, cache hit/miss, paginated history fetch.
The download is mocked — tests do not hit the live OKX endpoint.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd
import pytest

from data import okx_perp


# ── Symbol translation ──────────────────────────────────────────────────────

class TestSymbolTranslation:
    def test_manifest_to_okx_instid_btc(self):
        assert okx_perp.manifest_to_okx_instid("BTC/USDT") == "BTC-USDT-SWAP"

    def test_manifest_to_okx_instid_eth(self):
        assert okx_perp.manifest_to_okx_instid("ETH/USDT") == "ETH-USDT-SWAP"

    def test_manifest_to_okx_instid_rejects_no_slash(self):
        with pytest.raises(ValueError, match="BASE/QUOTE"):
            okx_perp.manifest_to_okx_instid("BTCUSDT")

    def test_manifest_to_okx_instid_rejects_empty_base(self):
        with pytest.raises(ValueError):
            okx_perp.manifest_to_okx_instid("/USDT")

    def test_okx_instid_to_ccxt_symbol(self):
        assert okx_perp.okx_instid_to_ccxt_symbol("BTC-USDT-SWAP") == "BTC/USDT:USDT"

    def test_okx_instid_rejects_non_swap(self):
        with pytest.raises(ValueError, match="BASE-QUOTE-SWAP"):
            okx_perp.okx_instid_to_ccxt_symbol("BTC-USDT")

    def test_manifest_to_ccxt_swap_symbol_full_chain(self):
        assert okx_perp.manifest_to_ccxt_swap_symbol("BTC/USDT") == "BTC/USDT:USDT"
        assert okx_perp.manifest_to_ccxt_swap_symbol("ETH/USDT") == "ETH/USDT:USDT"


# ── Paginated fetch (mocked exchange) ───────────────────────────────────────

def _make_ohlcv_batch(start_ms: int, n: int, step_ms: int = 3_600_000) -> list[list]:
    """Synthetic OHLCV batch [ts, open, high, low, close, volume]."""
    return [
        [start_ms + i * step_ms, 100.0 + i, 101.0 + i, 99.0 + i, 100.5 + i, 1000.0]
        for i in range(n)
    ]


class TestFetchPagination:
    def test_single_batch_under_limit(self, monkeypatch):
        """Pagination terminates when a partial batch is returned."""
        ex = mock.MagicMock()
        ex.markets = {"BTC/USDT:USDT": {}}
        ex.fetch_ohlcv.return_value = _make_ohlcv_batch(1_700_000_000_000, 50)

        monkeypatch.setattr(okx_perp, "_make_swap_exchange", lambda: ex)

        df = okx_perp.fetch_perp_ohlcv(
            "BTC/USDT", "1h", months=1, batch_size=300, request_delay_s=0,
        )
        assert len(df) == 49  # last partial candle dropped
        assert ex.fetch_ohlcv.call_count == 1

    def test_multi_batch_pagination(self, monkeypatch):
        """Two full-size batches followed by one partial → 3 calls."""
        full = _make_ohlcv_batch(1_700_000_000_000, 300)
        more = _make_ohlcv_batch(1_700_000_000_000 + 300 * 3_600_000, 300)
        tail = _make_ohlcv_batch(1_700_000_000_000 + 600 * 3_600_000, 50)
        ex = mock.MagicMock()
        ex.markets = {"BTC/USDT:USDT": {}}
        ex.fetch_ohlcv.side_effect = [full, more, tail]

        monkeypatch.setattr(okx_perp, "_make_swap_exchange", lambda: ex)

        df = okx_perp.fetch_perp_ohlcv(
            "BTC/USDT", "1h", months=1, batch_size=300, request_delay_s=0,
        )
        assert ex.fetch_ohlcv.call_count == 3
        # 300 + 300 + 50 = 650 raw rows, minus 1 dropped partial.
        assert len(df) == 649
        assert df.index.is_monotonic_increasing
        assert not df.index.has_duplicates

    def test_passes_ccxt_symbol_not_manifest(self, monkeypatch):
        """fetch_ohlcv must be called with CCXT swap symbol, not manifest form."""
        ex = mock.MagicMock()
        ex.markets = {"BTC/USDT:USDT": {}}
        ex.fetch_ohlcv.return_value = _make_ohlcv_batch(1_700_000_000_000, 5)

        monkeypatch.setattr(okx_perp, "_make_swap_exchange", lambda: ex)

        okx_perp.fetch_perp_ohlcv(
            "BTC/USDT", "1h", months=1, batch_size=300, request_delay_s=0,
        )
        first_call_args = ex.fetch_ohlcv.call_args_list[0]
        assert first_call_args.args[0] == "BTC/USDT:USDT"

    def test_accepts_instid_directly(self, monkeypatch):
        """Caller can pass the OKX instId form too — translation is symmetric."""
        ex = mock.MagicMock()
        ex.markets = {"BTC/USDT:USDT": {}}
        ex.fetch_ohlcv.return_value = _make_ohlcv_batch(1_700_000_000_000, 5)

        monkeypatch.setattr(okx_perp, "_make_swap_exchange", lambda: ex)

        df = okx_perp.fetch_perp_ohlcv(
            "BTC-USDT-SWAP", "1h", months=1, batch_size=300, request_delay_s=0,
        )
        assert ex.fetch_ohlcv.call_args_list[0].args[0] == "BTC/USDT:USDT"
        assert len(df) == 4

    def test_unknown_market_raises(self, monkeypatch):
        ex = mock.MagicMock()
        ex.markets = {}  # No markets loaded for the requested symbol
        monkeypatch.setattr(okx_perp, "_make_swap_exchange", lambda: ex)

        with pytest.raises(ValueError, match="not in"):
            okx_perp.fetch_perp_ohlcv("FOO/USDT", "1h", months=1)


# ── Cache hit/miss (uses backtest.cache.load_or_download_ohlcv) ─────────────

class TestCacheRoundtrip:
    def test_cache_miss_writes_file(self, tmp_path, monkeypatch):
        """First call with empty cache_dir triggers download_fn and writes parquet."""
        cache_dir = tmp_path / "perp"

        ex = mock.MagicMock()
        ex.markets = {"BTC/USDT:USDT": {}}
        ex.fetch_ohlcv.return_value = _make_ohlcv_batch(1_700_000_000_000, 50)

        monkeypatch.setattr(okx_perp, "_make_swap_exchange", lambda: ex)

        df1 = okx_perp.load_or_fetch_perp_ohlcv(
            "BTC/USDT", "1h", months=1,
            cache_dir=cache_dir, ttl_hours=24,
        )
        assert len(df1) == 49
        assert (cache_dir / "BTC-USDT-SWAP_1h_1mo.parquet").exists()

    def test_cache_hit_skips_download(self, tmp_path, monkeypatch):
        """Second call within TTL serves from cache — download_fn not invoked."""
        cache_dir = tmp_path / "perp"
        cache_dir.mkdir(parents=True)

        # Pre-seed parquet
        idx = pd.date_range("2025-01-01", periods=10, freq="1h", tz="UTC")
        idx.name = "timestamp"
        seed = pd.DataFrame({
            "open":   np.arange(10, dtype=float),
            "high":   np.arange(10, dtype=float) + 1,
            "low":    np.arange(10, dtype=float) - 1,
            "close":  np.arange(10, dtype=float) + 0.5,
            "volume": np.full(10, 1000.0),
        }, index=idx)
        seed.to_parquet(cache_dir / "BTC-USDT-SWAP_1h_1mo.parquet")

        ex = mock.MagicMock()
        ex.markets = {"BTC/USDT:USDT": {}}
        # If the cache miss path runs, this would be called — assert it isn't.
        monkeypatch.setattr(okx_perp, "_make_swap_exchange", lambda: ex)

        df = okx_perp.load_or_fetch_perp_ohlcv(
            "BTC/USDT", "1h", months=1,
            cache_dir=cache_dir, ttl_hours=24,
        )
        assert ex.fetch_ohlcv.call_count == 0
        assert len(df) == 10

    def test_cache_filename_uses_instid(self, tmp_path, monkeypatch):
        """Spot/perp key collision is impossible: filename includes -SWAP."""
        cache_dir = tmp_path / "perp"

        ex = mock.MagicMock()
        ex.markets = {"BTC/USDT:USDT": {}}
        ex.fetch_ohlcv.return_value = _make_ohlcv_batch(1_700_000_000_000, 5)

        monkeypatch.setattr(okx_perp, "_make_swap_exchange", lambda: ex)

        okx_perp.load_or_fetch_perp_ohlcv(
            "BTC/USDT", "1h", months=1,
            cache_dir=cache_dir, ttl_hours=24,
        )
        # Filename must contain the SWAP suffix; the spot equivalent
        # (BTC-USDT_1h_1mo.parquet, no -SWAP) must NOT exist.
        assert (cache_dir / "BTC-USDT-SWAP_1h_1mo.parquet").exists()
        assert not (cache_dir / "BTC-USDT_1h_1mo.parquet").exists()
