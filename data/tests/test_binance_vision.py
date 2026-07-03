"""Unit tests for data/binance_vision.py (synthetic zips, no network)."""

import hashlib
import io
import zipfile

import pandas as pd
import pytest

from data.binance_vision import (
    BinanceVisionError,
    _normalise_epoch,
    _parse_csv,
    download_month,
    load_klines,
    month_range,
)

CSV_ROW_MS = (
    "1609459200000,28923.63,28961.66,28913.12,28961.66,27.457,"
    "1609459259999,794382.04,1292,16.777,485390.82,0\n"
)
CSV_ROW_US = (
    "1735689600000000,4.15,4.16,4.15,4.155,539.23,"
    "1735693199999999,2240.39,13,401.82,1669.98,0\n"
)


def _zip_bytes(csv_text: str, name: str = "x.csv") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, csv_text)
    return buf.getvalue()


class FakeResponse:
    def __init__(self, status_code=200, content=b"", text=""):
        self.status_code = status_code
        self.content = content
        self.text = text


class FakeSession:
    """Maps URL suffixes to FakeResponses; 404 for anything else."""

    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = []

    def get(self, url, timeout=None):
        self.calls.append(url)
        for suffix, resp in self.mapping.items():
            if url.endswith(suffix):
                return resp
        return FakeResponse(404)


# ------------------------------------------------------------- parsing

def test_month_range_inclusive():
    assert month_range("2021-11", "2022-02") == [
        "2021-11", "2021-12", "2022-01", "2022-02"]


def test_month_range_rejects_reversed():
    with pytest.raises(ValueError):
        month_range("2022-02", "2021-11")


def test_epoch_normalisation_ms_and_us():
    s = pd.Series([1609459200000, 1735689600000000])
    out = _normalise_epoch(s)
    assert out.tolist() == [1609459200000, 1735689600000]


def test_parse_csv_ms_timestamps():
    df = _parse_csv(CSV_ROW_MS.encode())
    assert df.index[0] == pd.Timestamp("2021-01-01 00:00:00", tz="UTC")
    assert df["taker_buy_base"].iloc[0] == pytest.approx(16.777)


def test_parse_csv_us_timestamps():
    df = _parse_csv(CSV_ROW_US.encode())
    assert df.index[0] == pd.Timestamp("2025-01-01 00:00:00", tz="UTC")


def test_parse_csv_drops_header_row():
    text = "open_time,open,high,low,close,volume,close_time,quote_volume," \
           "n_trades,taker_buy_base,taker_buy_quote,ignore\n" + CSV_ROW_MS
    df = _parse_csv(text.encode())
    assert len(df) == 1


# ------------------------------------------------------------ download

def test_download_month_404_returns_none():
    session = FakeSession({})
    assert download_month("BTCUSDT", "2019-01", session=session) is None


def test_download_month_checksum_verified():
    payload = _zip_bytes(CSV_ROW_MS)
    good = hashlib.sha256(payload).hexdigest()
    session = FakeSession({
        ".zip": FakeResponse(200, payload),
        ".zip.CHECKSUM": FakeResponse(200, text=f"{good}  x.zip"),
    })
    df = download_month("BTCUSDT", "2021-01", session=session)
    assert len(df) == 1


def test_download_month_checksum_mismatch_raises():
    payload = _zip_bytes(CSV_ROW_MS)
    session = FakeSession({
        ".zip": FakeResponse(200, payload),
        ".zip.CHECKSUM": FakeResponse(200, text="deadbeef  x.zip"),
    })
    with pytest.raises(BinanceVisionError):
        download_month("BTCUSDT", "2021-01", session=session)


def test_download_month_http_error_raises():
    session = FakeSession({".zip": FakeResponse(500)})
    with pytest.raises(BinanceVisionError):
        download_month("BTCUSDT", "2021-01", session=session)


# ------------------------------------------------------------- caching

def test_load_klines_uses_cache_and_negative_caches_404(tmp_path, monkeypatch):
    import data.binance_vision as bv

    payload = _zip_bytes(CSV_ROW_MS)
    session = FakeSession({
        "BTCUSDT-1m-2021-01.zip": FakeResponse(200, payload),
        # CHECKSUM intentionally 404s -> verification skipped gracefully
    })
    monkeypatch.setattr(bv.requests, "Session", lambda: session)

    df = bv.load_klines("BTC/USDT", "2020-12", "2021-01", cache_dir=tmp_path)
    assert len(df) == 1  # 2020-12 was 404 -> contributes nothing
    n_calls_first = len(session.calls)

    # second load: fully served from parquet cache, zero HTTP calls
    df2 = bv.load_klines("BTC/USDT", "2020-12", "2021-01", cache_dir=tmp_path)
    assert len(session.calls) == n_calls_first
    assert df2.equals(df)


def test_load_klines_all_missing_raises(tmp_path, monkeypatch):
    import data.binance_vision as bv
    monkeypatch.setattr(bv.requests, "Session", lambda: FakeSession({}))
    with pytest.raises(BinanceVisionError):
        bv.load_klines("NOPEUSDT", "2021-01", "2021-02", cache_dir=tmp_path)
