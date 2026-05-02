"""data/tests/test_okx_funding.py — unit tests for OKX funding-rate ingestion.

Covers cache hit/miss, paginated funding history, mark-price snap-merge,
the cadence-detection helper that gates the HALT-AND-CONSULT trigger,
and the Path 5 deep-history extension (archive + history-mark-price).

The legacy CCXT-based tests mock `_make_swap_exchange` and never touch
the network.  The Path 5 tests are split: pure logic (hybrid assembly,
dedupe) is mocked end-to-end via `monkeypatch`; smoke tests against
the live archive / deep-history HTTP endpoints are gated on the
`OKX_LIVE_TESTS` env var being truthy so CI can opt out.
"""

from __future__ import annotations

import io
import os
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd
import pytest

from data import okx_funding


_LIVE = os.environ.get("OKX_LIVE_TESTS", "").lower() in ("1", "true", "yes")
needs_live = pytest.mark.skipif(
    not _LIVE,
    reason="set OKX_LIVE_TESTS=1 to run live-network smoke tests",
)


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
        # Bypass the runtime depth probe so it doesn't consume a batch
        # from the side_effect list (the test's call-budget is exact).
        monkeypatch.setattr(
            okx_funding, "_detect_live_api_depth",
            lambda exchange, ccxt_symbol: okx_funding.LIVE_API_DEPTH_DAYS,
        )

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


# ── Path 5: archive month fetcher ────────────────────────────────────────────


def _build_synthetic_archive_zip(
    instid: str = "BTC-USDT-SWAP",
    year: int = 2024,
    month: int = 6,
    n_settlements: int = 90,
    rate_seed: float = 0.0001,
) -> bytes:
    """Build a zip whose CSV mirrors the OKX funding-rate archive shape:
    columns [instrument_name, funding_rate, funding_time]."""
    start_dt = datetime(year, month, 1, tzinfo=timezone.utc)
    rows = []
    for i in range(n_settlements):
        ts_ms = int((start_dt + timedelta(hours=i * 8)).timestamp() * 1000)
        rows.append({
            "instrument_name": instid,
            "funding_rate": rate_seed * (1 + 0.01 * i),
            "funding_time": ts_ms,
        })
    csv_bytes = pd.DataFrame(rows).to_csv(index=False).encode("utf-8")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            f"{instid}-fundingrates-{year:04d}-{month:02d}.csv", csv_bytes,
        )
    return buf.getvalue()


class _MockResponse:
    def __init__(self, status_code: int, content: bytes = b"", text: str = ""):
        self.status_code = status_code
        self.content = content
        self.text = text or content.decode("utf-8", errors="replace")
        self.url = ""

    def json(self):
        import json
        return json.loads(self.text)


class TestFetchFundingArchiveMonth:
    def test_fetch_archive_month_smoke(self, monkeypatch, tmp_path):
        """Probe B equivalent: fetch one synthetic archive zip end-to-end."""
        instid = "BTC-USDT-SWAP"
        year, month = 2024, 6
        zip_bytes = _build_synthetic_archive_zip(instid, year, month, n_settlements=93)

        archive_dir = tmp_path / "archive"
        monkeypatch.setattr(okx_funding, "ARCHIVE_CACHE_DIR", archive_dir)

        captured = {}
        def fake_get(url, timeout, headers):
            captured["url"] = url
            return _MockResponse(200, content=zip_bytes)
        monkeypatch.setattr(okx_funding.requests, "get", fake_get)

        df = okx_funding.fetch_funding_archive_month(instid, year, month)
        assert list(df.columns) == ["funding_rate"]
        assert len(df) == 93
        assert isinstance(df.index, pd.DatetimeIndex)
        assert df.index.tz is not None
        assert str(df.index.tz) in ("UTC", "tzutc()", "datetime.timezone.utc")
        # URL contains the instid + year-month format expected by Path 5.
        assert "swaprates/monthly/202406" in captured["url"]
        assert f"{instid}-fundingrates-2024-06.zip" in captured["url"]
        # Cache file written.
        assert (archive_dir / instid / "2024-06.parquet").exists()

    def test_fetch_archive_month_404_raises_filenotfound(
        self, monkeypatch, tmp_path,
    ):
        """Probe-B 404 path: month before the archive's earliest
        publication (March 2022) returns FileNotFoundError."""
        archive_dir = tmp_path / "archive"
        monkeypatch.setattr(okx_funding, "ARCHIVE_CACHE_DIR", archive_dir)

        def fake_get(url, timeout, headers):
            return _MockResponse(404, text='{"code":404,"msg":"Not Found"}')
        monkeypatch.setattr(okx_funding.requests, "get", fake_get)

        with pytest.raises(FileNotFoundError):
            okx_funding.fetch_funding_archive_month(
                "BTC-USDT-SWAP", 2021, 12,
            )

    def test_fetch_archive_month_uses_cache_on_second_call(
        self, monkeypatch, tmp_path,
    ):
        """Second call short-circuits — no HTTP fetch."""
        instid = "BTC-USDT-SWAP"
        year, month = 2024, 6
        zip_bytes = _build_synthetic_archive_zip(instid, year, month, n_settlements=10)
        archive_dir = tmp_path / "archive"
        monkeypatch.setattr(okx_funding, "ARCHIVE_CACHE_DIR", archive_dir)

        call_count = {"n": 0}
        def fake_get(url, timeout, headers):
            call_count["n"] += 1
            return _MockResponse(200, content=zip_bytes)
        monkeypatch.setattr(okx_funding.requests, "get", fake_get)

        # First call: hits the (mock) network and populates parquet.
        df1 = okx_funding.fetch_funding_archive_month(instid, year, month)
        # Second call: short-circuits on the parquet.
        df2 = okx_funding.fetch_funding_archive_month(instid, year, month)
        assert call_count["n"] == 1
        assert len(df1) == len(df2) == 10


# ── Path 5: deep mark-price fetcher ──────────────────────────────────────────


def _make_history_mark_response(
    start_ms: int, n: int, step_ms: int = 3_600_000, price_seed: float = 50000.0,
) -> dict:
    """Mimic OKX history-mark-price-candles response shape:
    {"code":"0","data":[[ts, open, high, low, close, confirm], ...]}.
    OKX returns DESCENDING (newest first); rows generated here are
    intentionally listed newest-first to match.
    """
    data = []
    for i in range(n - 1, -1, -1):
        ts = start_ms + i * step_ms
        op = price_seed + i
        data.append([str(ts), str(op), str(op + 100), str(op - 100), str(op + 50), "1"])
    return {"code": "0", "msg": "", "data": data}


class TestFetchMarkPriceHistoryDeep:
    def test_smoke_assembly_and_schema(self, monkeypatch, tmp_path):
        """Hybrid-assembly Probe C equivalent: mock the HTTP endpoint
        and walk one continuous fetch range, verify schema +
        per-month caching."""
        archive_dir = tmp_path / "mark_price_archive"
        monkeypatch.setattr(
            okx_funding, "MARK_PRICE_ARCHIVE_CACHE_DIR", archive_dir,
        )

        # Window: 7 days at 1H = 168 bars.
        start_dt = datetime(2024, 6, 10, tzinfo=timezone.utc)
        end_dt = start_dt + timedelta(days=7)
        start_ms = int(start_dt.timestamp() * 1000)
        end_ms = int(end_dt.timestamp() * 1000)

        # Synthesize the full 168-row response in one batch via a
        # closure that respects OKX's `after` semantics.  When called
        # by the backward walk, return all bars older than the
        # `after` cursor (one shot, since 168 < 200 limit even though
        # the loop normally chunks by 100).
        def fake_get(url, params, timeout, headers):
            assert url == okx_funding.MARK_PRICE_HISTORY_URL
            after_ms = int(params["after"])
            limit = int(params["limit"])
            # Generate ALL bars in the window then filter to <after.
            full = _make_history_mark_response(
                start_ms=start_ms, n=168, step_ms=3_600_000, price_seed=50000.0,
            )
            filtered = [row for row in full["data"] if int(row[0]) < after_ms]
            return _MockResponse(
                200, text='{"code":"0","msg":"","data":'
                + str([list(map(str, r)) for r in filtered[:limit]]).replace("'", '"')
                + '}',
            )
        monkeypatch.setattr(okx_funding.requests, "get", fake_get)

        df = okx_funding.fetch_mark_price_history_deep(
            instid="BTC-USDT-SWAP", start_ms=start_ms, end_ms=end_ms,
            request_delay_s=0.0,
        )
        assert list(df.columns) == ["open", "close"]
        assert isinstance(df.index, pd.DatetimeIndex)
        assert str(df.index.tz) in ("UTC", "tzutc()", "datetime.timezone.utc")
        assert 160 < len(df) <= 168


# ── Path 5: hybrid assembly + dedupe (mocked end-to-end) ─────────────────────


class TestFetchFundingHistoryHybrid:
    def _setup_archive_mock(self, monkeypatch, tmp_path):
        """Mock the archive HTTP fetch + cache dir."""
        archive_dir = tmp_path / "archive"
        monkeypatch.setattr(okx_funding, "ARCHIVE_CACHE_DIR", archive_dir)
        mark_archive_dir = tmp_path / "mark_price_archive"
        monkeypatch.setattr(
            okx_funding, "MARK_PRICE_ARCHIVE_CACHE_DIR", mark_archive_dir,
        )
        return archive_dir

    def test_hybrid_assembly_no_overlap_no_nan(self, monkeypatch, tmp_path):
        """months=6 → window crosses cutover_ms → archive + live merge.
        Assert no duplicate index, mark_price end-to-end populated.
        """
        self._setup_archive_mock(monkeypatch, tmp_path)
        instid = "BTC-USDT-SWAP"
        ccxt_symbol = "BTC/USDT:USDT"

        now_dt = datetime(2026, 5, 1, tzinfo=timezone.utc)
        # Pin "now" so the cutover math is reproducible.
        class _FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None): return now_dt if tz else now_dt.replace(tzinfo=None)
        monkeypatch.setattr(okx_funding, "datetime", _FrozenDatetime)

        cutover_dt = now_dt - timedelta(days=okx_funding.LIVE_API_DEPTH_DAYS)
        since_dt = now_dt - timedelta(days=int(6 * 30.44))

        # Archive range: since_dt → cutover_dt.
        # Generate one synthetic zip per month.
        def fake_archive_get(url, timeout, headers):
            # Parse year-month out of the URL.
            import re
            m = re.search(r"-fundingrates-(\d{4})-(\d{2})\.zip", url)
            if not m:
                return _MockResponse(404)
            yr, mo = int(m.group(1)), int(m.group(2))
            return _MockResponse(
                200,
                content=_build_synthetic_archive_zip(
                    instid, yr, mo, n_settlements=90, rate_seed=0.0001,
                ),
            )
        monkeypatch.setattr(okx_funding.requests, "get", fake_archive_get)

        # Live exchange mock: return funding rows from cutover → now
        # at 8h cadence.
        ex = mock.MagicMock()
        ex.markets = {ccxt_symbol: {}}
        live_settlements = []
        cur = cutover_dt.replace(minute=0, second=0, microsecond=0)
        while cur <= now_dt:
            live_settlements.append({
                "timestamp": int(cur.timestamp() * 1000),
                "datetime": "",
                "symbol": ccxt_symbol,
                "fundingRate": 0.00012,
                "info": {},
            })
            cur += timedelta(hours=8)
        # Chunk live into 100-row batches; the live fetcher paginates
        # forward.
        chunks = [
            live_settlements[i:i + 100]
            for i in range(0, len(live_settlements), 100)
        ] or [[]]
        ex.fetch_funding_rate_history.side_effect = chunks + [[]]

        # Live mark mock: 1h candles covering the live segment.
        # Easier: just return enough bars covering the request range.
        mark_rows = []
        bar = cutover_dt.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
        while bar <= now_dt + timedelta(hours=1):
            mark_rows.append([
                int(bar.timestamp() * 1000),
                50000.0, 50100.0, 49900.0, 50050.0, 0.0,
            ])
            bar += timedelta(hours=1)
        ex.fetch_mark_ohlcv.return_value = mark_rows
        monkeypatch.setattr(okx_funding, "_make_swap_exchange", lambda: ex)

        # Path 5 dispatches mark fetch through deep-history HTTP when
        # window crosses cutover.  Mock that too.
        def fake_mark_history(*, instid, start_ms, end_ms, request_delay_s):
            idx = pd.date_range(
                pd.Timestamp(start_ms, unit="ms", tz="UTC"),
                pd.Timestamp(end_ms, unit="ms", tz="UTC"),
                freq="1h",
            )
            return pd.DataFrame(
                {
                    "open": np.full(len(idx), 50000.0),
                    "close": np.full(len(idx), 50050.0),
                },
                index=idx,
            )
        monkeypatch.setattr(
            okx_funding, "fetch_mark_price_history_deep", fake_mark_history,
        )

        df = okx_funding.fetch_funding_history(
            "BTC/USDT", months=6, batch_size=100, request_delay_s=0,
        )
        assert list(df.columns) == ["funding_rate", "mark_price"]
        assert len(df) > 0
        # Earliest ≥ since_dt − a month tolerance (archive months
        # are inclusive at month boundary).
        assert df.index.min() >= since_dt - timedelta(days=31)
        assert df.index.max() <= now_dt
        # No duplicate index.
        assert df.index.is_unique
        # Mark price populated end-to-end.
        assert df["mark_price"].notna().all()

    def test_overlap_dedup_live_wins(self, monkeypatch, tmp_path):
        """Concat archive + live where one settlement appears in both.
        last-wins dedupe → live value retained, archive value
        replaced."""
        self._setup_archive_mock(monkeypatch, tmp_path)

        # Build two frames with one shared timestamp; the shared row's
        # value differs.  Run them through the dedupe helper directly
        # to verify last-wins discipline.
        ts_overlap = pd.Timestamp("2025-01-15T16:00:00Z")
        archive = pd.DataFrame(
            {"funding_rate": [0.0001, 0.0002]},
            index=pd.DatetimeIndex([
                pd.Timestamp("2025-01-15T08:00:00Z"), ts_overlap,
            ]),
        )
        live = pd.DataFrame(
            {"funding_rate": [0.00099, 0.0003]},
            index=pd.DatetimeIndex([
                ts_overlap,  # SAME ts; live should win
                pd.Timestamp("2025-01-16T00:00:00Z"),
            ]),
        )
        merged = okx_funding._concat_dedup_last_wins([archive, live])
        assert merged.loc[ts_overlap, "funding_rate"] == pytest.approx(0.00099)
        assert merged.index.is_unique


class TestArchiveVsLiveSignMatch:
    def test_archive_csv_to_in_memory_dataframe_signs_match_input(
        self, monkeypatch, tmp_path,
    ):
        """Path 5 contract: the archive CSV is consumed without sign
        flipping or unit scaling.  Build a synthetic zip with a row
        whose funding_rate has known magnitude and sign, fetch it,
        and verify the produced DataFrame's float matches exactly."""
        archive_dir = tmp_path / "archive"
        monkeypatch.setattr(okx_funding, "ARCHIVE_CACHE_DIR", archive_dir)

        instid = "BTC-USDT-SWAP"
        year, month = 2024, 6
        # Build a single-row CSV with a precise funding_rate value.
        ts_ms = int(
            datetime(year, month, 1, tzinfo=timezone.utc).timestamp() * 1000,
        )
        expected_fr = -3.69797398106e-05  # value from probe day 2026-05-02
        csv_bytes = (
            f"instrument_name,funding_rate,funding_time\n"
            f"{instid},{expected_fr},{ts_ms}\n"
        ).encode("utf-8")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(f"{instid}-fundingrates-{year:04d}-{month:02d}.csv",
                        csv_bytes)
        zip_bytes = buf.getvalue()

        def fake_get(url, timeout, headers):
            return _MockResponse(200, content=zip_bytes)
        monkeypatch.setattr(okx_funding.requests, "get", fake_get)

        df = okx_funding.fetch_funding_archive_month(instid, year, month)
        assert len(df) == 1
        # Exact float64 round-trip — same sign, same magnitude.
        assert df["funding_rate"].iloc[0] == pytest.approx(
            expected_fr, rel=1e-12, abs=1e-15,
        )


# ── Path 5 live-network smoke tests (opt-in) ─────────────────────────────────

@needs_live
def test_live_archive_month_smoke():
    """Hits the real OKX archive CDN.  Confirms a known-good month
    (2026-03 was confirmed reachable in probe 2026-05-02)."""
    df = okx_funding.fetch_funding_archive_month("BTC-USDT-SWAP", 2026, 3)
    assert list(df.columns) == ["funding_rate"]
    assert len(df) > 80  # ~93 rows expected
    assert isinstance(df.index, pd.DatetimeIndex)


@needs_live
def test_live_archive_month_404_pre_march_2022():
    """Hits the real OKX archive CDN.  Confirms 2021-12 returns 404
    (archive earliest is March 2022 per probe 2026-05-02)."""
    with pytest.raises(FileNotFoundError):
        okx_funding.fetch_funding_archive_month("BTC-USDT-SWAP", 2021, 12)


@needs_live
def test_live_history_mark_price_deep_smoke():
    """Hits the real OKX history-mark-price-candles HTTP endpoint.
    Fetches 7 days at cutover − 100 days; expects ~7*24 rows."""
    now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    end_ms = now_ms - 100 * 86400 * 1000
    start_ms = end_ms - 7 * 86400 * 1000
    df = okx_funding.fetch_mark_price_history_deep(
        instid="BTC-USDT-SWAP", start_ms=start_ms, end_ms=end_ms,
        request_delay_s=0.1,
    )
    assert list(df.columns) == ["open", "close"]
    assert 150 < len(df) < 200  # 7 days × 24 = 168


@needs_live
def test_live_api_depth_probe_returns_sensible_value():
    """Auto-calibration sanity: the runtime probe returns a
    positive integer in a reasonable range (1 to LIVE_API_DEPTH_DAYS
    plus a small slack for measurement variance)."""
    import ccxt
    ex = ccxt.okx({
        "enableRateLimit": True,
        "options": {"defaultType": "swap"},
    })
    ex.load_markets()
    depth = okx_funding._detect_live_api_depth(ex, "BTC/USDT:USDT")
    assert 1 <= depth <= okx_funding.LIVE_API_DEPTH_DAYS + 30, (
        f"depth probe returned implausible value: {depth}"
    )


def test_detect_live_api_depth_falls_back_on_exception(monkeypatch):
    """If the probe raises, fall back to the constant."""
    ex = mock.MagicMock()
    ex.fetch_funding_rate_history.side_effect = RuntimeError("boom")
    depth = okx_funding._detect_live_api_depth(ex, "BTC/USDT:USDT")
    assert depth == okx_funding.LIVE_API_DEPTH_DAYS


@needs_live
def test_live_archive_vs_live_v5_sign_match():
    """One settlement timestamp present in both archive and V5
    funding-rate-history must produce identical funding_rate values
    (probe 2026-05-02 measured exact float64 match)."""
    import requests as _req
    # Use a recent month that overlaps the live API depth (last ~3
    # months).  The first day of last month is reliably in both.
    now = datetime.now(tz=timezone.utc)
    ref = (now.replace(day=1) - timedelta(days=1)).replace(day=15)
    yr, mo = ref.year, ref.month
    archive_df = okx_funding.fetch_funding_archive_month(
        "BTC-USDT-SWAP", yr, mo,
    )
    sample = archive_df.iloc[5]
    sample_ts_ms = int(sample.name.timestamp() * 1000)
    sample_fr = float(sample["funding_rate"])

    live_url = "https://www.okx.com/api/v5/public/funding-rate-history"
    r = _req.get(live_url, params={
        "instId": "BTC-USDT-SWAP", "limit": "10",
        "after": str(sample_ts_ms + 1),
    }, timeout=15, headers={"User-Agent": "okx_funding-test"})
    body = r.json()
    matched = next(
        (d for d in (body.get("data") or [])
         if int(d["fundingTime"]) == sample_ts_ms),
        None,
    )
    assert matched is not None, (
        f"sample ts {sample_ts_ms} not found in live API window"
    )
    assert float(matched["fundingRate"]) == pytest.approx(
        sample_fr, abs=1e-9,
    )
