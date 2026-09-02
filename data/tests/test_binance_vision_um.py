"""Unit tests for data/binance_vision_um.py (fixture CSVs, no network).

Network-touching checks are marked ``network`` and deselected by
default; run them with ``pytest -m network``.
"""

import json
import os

import pandas as pd
import pytest

from data import binance_vision_um as um

_RUN_NETWORK = os.environ.get("BINANCE_UM_NETWORK_TESTS") == "1"


# ────────────────────────────────────────────────────────── fixtures ──

KLINE_ROWS_MS = (
    "1609459200000,28948.19,29668.86,28627.12,29337.16,210716.398,"
    "1609545599999,6157505024.08511,1511793,101247.902,2960175587.62208,0\n"
    "1609545600000,29337.15,33480.00,28958.24,32199.91,545541.080,"
    "1609631999999,17122938614.70610,3514545,273388.463,8578964529.70894,0\n"
)
KLINE_HEADER = (
    "open_time,open,high,low,close,volume,close_time,quote_volume,count,"
    "taker_buy_volume,taker_buy_quote_volume,ignore\n"
)
KLINE_ROWS_US = (
    "1609459200000000,28948.19,29668.86,28627.12,29337.16,210716.398,"
    "1609545599999999,6157505024.08511,1511793,101247.902,2960175587.62208,0\n"
    "1609545600000000,29337.15,33480.00,28958.24,32199.91,545541.080,"
    "1609631999999999,17122938614.70610,3514545,273388.463,8578964529.70894,0\n"
)

FUNDING_CSV = (
    "calc_time,funding_interval_hours,last_funding_rate\n"
    "1609459200002,8,0.00022753\n"
    "1609488000006,8,0.00026336\n"
    "1609516800004,8,-0.00011000\n"
)
FUNDING_CSV_NOHEADER = (
    "1609459200002,8,0.00022753\n"
    "1609488000006,8,0.00026336\n"
)

METRICS_CSV = (
    "create_time,symbol,sum_open_interest,sum_open_interest_value,"
    "count_toptrader_long_short_ratio,sum_toptrader_long_short_ratio,"
    "count_long_short_ratio,sum_taker_long_short_vol_ratio\n"
    "2021-01-05 00:00:00,BTCUSDT,30383.79300000,973250011.32084000,"
    "1.76785426,1.21705153,1.89410548,1.63685596\n"
    "2021-01-05 00:05:00,BTCUSDT,30400.00000000,974000000.00000000,"
    "1.70000000,1.20000000,1.80000000,1.60000000\n"
    "2021-01-05 01:00:00,BTCUSDT,30500.00000000,975000000.00000000,"
    "1.60000000,1.10000000,1.70000000,1.50000000\n"
)


def _month_of(url: str) -> str:
    return um._MONTH_RE.search(url).group(1)


@pytest.fixture
def cache(tmp_path):
    return tmp_path / "binance_um"


# ─────────────────────────────────────────────────── range helpers ──

def test_month_range_inclusive():
    assert um.month_range("2021-11", "2022-01") == ["2021-11", "2021-12", "2022-01"]


def test_month_range_rejects_reversed():
    with pytest.raises(ValueError):
        um.month_range("2022-02", "2021-11")


def test_day_range_inclusive():
    assert um.day_range("2021-01-01", "2021-01-03") == [
        "2021-01-01", "2021-01-02", "2021-01-03"]


def test_normalise_symbol():
    assert um.normalise_symbol("btc-usdt") == "BTCUSDT"


# ────────────────────────────────────────────────────── kline parse ──

def test_parse_klines_ms_no_header():
    df = um.parse_klines_csv(KLINE_ROWS_MS.encode())
    assert len(df) == 2
    assert df.index[0] == pd.Timestamp("2021-01-01", tz="UTC")
    assert df.index[1] == pd.Timestamp("2021-01-02", tz="UTC")
    assert df["close"].iloc[0] == pytest.approx(29337.16)
    assert df["taker_buy_volume"].iloc[0] == pytest.approx(101247.902)
    assert list(df.columns) == um._KLINE_KEEP
    assert all(str(t) == "float64" for t in df.dtypes)


def test_parse_klines_ms_with_header():
    df = um.parse_klines_csv((KLINE_HEADER + KLINE_ROWS_MS).encode())
    assert len(df) == 2
    assert df.index[0] == pd.Timestamp("2021-01-01", tz="UTC")


def test_parse_klines_microsecond_timestamps():
    df = um.parse_klines_csv(KLINE_ROWS_US.encode())
    assert df.index[0] == pd.Timestamp("2021-01-01", tz="UTC")
    assert df.index[1] == pd.Timestamp("2021-01-02", tz="UTC")


def test_parse_klines_us_matches_ms():
    a = um.parse_klines_csv(KLINE_ROWS_MS.encode())
    b = um.parse_klines_csv(KLINE_ROWS_US.encode())
    pd.testing.assert_frame_equal(a, b)


# ──────────────────────────────────────────────────── funding parse ──

def test_parse_funding_with_header():
    df = um.parse_funding_csv(FUNDING_CSV.encode())
    assert len(df) == 3
    assert df.index[0] == pd.Timestamp("2021-01-01 00:00:00.002", tz="UTC")
    assert df["last_funding_rate"].iloc[2] == pytest.approx(-0.00011)
    assert df["funding_interval_hours"].iloc[0] == pytest.approx(8.0)


def test_parse_funding_without_header():
    df = um.parse_funding_csv(FUNDING_CSV_NOHEADER.encode())
    assert len(df) == 2
    assert list(df.columns) == um._FUNDING_KEEP


# ──────────────────────────────────────────────────── metrics parse ──

def test_parse_metrics():
    df = um.parse_metrics_csv(METRICS_CSV.encode())
    assert len(df) == 3
    assert df.index[0] == pd.Timestamp("2021-01-05 00:00:00", tz="UTC")
    assert df["sum_open_interest"].iloc[0] == pytest.approx(30383.793)
    assert "symbol" not in df.columns


def test_resample_metrics_hourly_takes_last():
    df = um.parse_metrics_csv(METRICS_CSV.encode())
    h = um.resample_metrics(df, "1h")
    assert len(h) == 2
    # 00:00 bin -> last 5-min row inside it (00:05)
    assert h["sum_open_interest"].iloc[0] == pytest.approx(30400.0)
    assert h["sum_open_interest"].iloc[1] == pytest.approx(30500.0)


def test_resample_metrics_daily_takes_last():
    df = um.parse_metrics_csv(METRICS_CSV.encode())
    d = um.resample_metrics(df, "1d")
    assert len(d) == 1
    assert d["sum_taker_long_short_vol_ratio"].iloc[0] == pytest.approx(1.5)


# ───────────────────────────────────────────────────────── universe ──

FAKE_KEYS = {
    "BTCUSDT": ["2020-01", "2020-02", "2026-07", "2026-08"],
    "LUNAUSDT": ["2021-01", "2022-04", "2022-05"],
    "EMPTYUSDT": [],
}


def test_universe_table_delisted_logic(monkeypatch, cache):
    monkeypatch.setattr(um, "list_symbols", lambda session=None: sorted(FAKE_KEYS))
    monkeypatch.setattr(um, "_LIST_SLEEP", 0.0)

    def fake_list_keys(prefix, session=None):
        sym = prefix[len(um.KLINES_PREFIX):].split("/")[0]
        return [
            f"{prefix}{sym}-1d-{m}.zip" for m in FAKE_KEYS[sym]
        ] + [f"{prefix}{sym}-1d-{m}.zip.CHECKSUM" for m in FAKE_KEYS[sym]]

    monkeypatch.setattr(um, "list_keys", fake_list_keys)
    monkeypatch.setattr(um, "_current_month", lambda: pd.Period("2026-09", freq="M"))

    df = um.universe_table(force=True, cache_dir=cache)
    assert list(df["symbol"]) == ["BTCUSDT", "LUNAUSDT"]  # EMPTYUSDT dropped
    row = df.set_index("symbol")
    assert row.loc["BTCUSDT", "first_month"] == "2020-01"
    assert row.loc["BTCUSDT", "last_month"] == "2026-08"
    assert bool(row.loc["BTCUSDT", "delisted"]) is False   # 2026-08 == cutoff
    assert row.loc["LUNAUSDT", "last_month"] == "2022-05"
    assert bool(row.loc["LUNAUSDT", "delisted"]) is True
    assert (cache / "universe.parquet").exists()


def test_universe_table_uses_cache(monkeypatch, cache):
    monkeypatch.setattr(um, "list_symbols", lambda session=None: sorted(FAKE_KEYS))
    monkeypatch.setattr(um, "_LIST_SLEEP", 0.0)
    monkeypatch.setattr(um, "_current_month", lambda: pd.Period("2026-09", freq="M"))

    calls = {"n": 0}

    def fake_list_keys(prefix, session=None):
        calls["n"] += 1
        sym = prefix[len(um.KLINES_PREFIX):].split("/")[0]
        return [f"{prefix}{sym}-1d-{m}.zip" for m in FAKE_KEYS[sym]]

    monkeypatch.setattr(um, "list_keys", fake_list_keys)
    um.universe_table(force=True, cache_dir=cache)
    first = calls["n"]
    um.universe_table(force=False, cache_dir=cache)
    assert calls["n"] == first  # second call served from parquet


# ────────────────────────────────────────────────────────── klines ──

def _kline_downloader(available, counter=None):
    def fake(url, session=None):
        month = _month_of(url)
        if counter is not None:
            counter.append(month)
        if month not in available:
            return None
        return available[month].encode()
    return fake


def _kline_month(month: str, n: int = 2) -> str:
    base = pd.Timestamp(month + "-01", tz="UTC")
    lines = []
    for i in range(n):
        ms = int((base + pd.Timedelta(days=i)).timestamp() * 1000)
        lines.append(
            f"{ms},1,2,0.5,1.5,10,{ms + 86399999},15,7,4,6,0"
        )
    return "\n".join(lines) + "\n"


def test_fetch_klines_skips_missing_months(monkeypatch, cache):
    available = {"2021-01": _kline_month("2021-01"), "2021-03": _kline_month("2021-03")}
    monkeypatch.setattr(um, "_download_zip", _kline_downloader(available))
    df = um.fetch_klines("BTCUSDT", "1d", "2021-01", "2021-03", cache_dir=cache)
    assert len(df) == 4
    assert df.index[0] == pd.Timestamp("2021-01-01", tz="UTC")
    assert df.index[-1] == pd.Timestamp("2021-03-02", tz="UTC")


def test_fetch_klines_until_truncation(monkeypatch, cache):
    available = {"2021-01": _kline_month("2021-01", n=5)}
    monkeypatch.setattr(um, "_download_zip", _kline_downloader(available))
    df = um.fetch_klines("BTCUSDT", "1d", "2021-01", "2021-01",
                         until="2021-01-03", cache_dir=cache)
    assert len(df) == 2
    assert df.index[-1] == pd.Timestamp("2021-01-02", tz="UTC")


def test_fetch_klines_cache_is_idempotent(monkeypatch, cache):
    available = {"2021-01": _kline_month("2021-01")}
    seen = []
    monkeypatch.setattr(um, "_download_zip", _kline_downloader(available, seen))

    a = um.fetch_klines("BTCUSDT", "1d", "2021-01", "2021-01", cache_dir=cache)
    n_first = len(seen)
    b = um.fetch_klines("BTCUSDT", "1d", "2021-01", "2021-01", cache_dir=cache)
    pd.testing.assert_frame_equal(a, b)
    assert len(a) == 2                     # no duplicated rows on re-read
    assert len(seen) == n_first            # second call hit the parquet cache


def test_fetch_klines_missing_month_negative_cached(monkeypatch, cache):
    seen = []
    monkeypatch.setattr(um, "_download_zip", _kline_downloader({"2021-01": _kline_month("2021-01")}, seen))
    um.fetch_klines("BTCUSDT", "1d", "2021-01", "2021-02", cache_dir=cache)
    assert "2021-02" in seen
    miss = json.loads((cache / "klines" / "BTCUSDT_1d.missing.json").read_text())
    assert miss == ["2021-02"]
    seen.clear()
    um.fetch_klines("BTCUSDT", "1d", "2021-01", "2021-02", cache_dir=cache)
    assert seen == []


def test_fetch_klines_no_data_returns_empty(monkeypatch, cache):
    monkeypatch.setattr(um, "_download_zip", _kline_downloader({}))
    df = um.fetch_klines("NOPEUSDT", "1d", "2021-01", "2021-02", cache_dir=cache)
    assert len(df) == 0
    assert list(df.columns) == um._KLINE_KEEP


def test_fetch_klines_rejects_unsupported_interval(cache):
    with pytest.raises(ValueError):
        um.fetch_klines("BTCUSDT", "5m", "2021-01", "2021-01", cache_dir=cache)


# ───────────────────────────────────────────────────────── funding ──

def test_fetch_funding_parses_and_caches(monkeypatch, cache):
    seen = []

    def fake(url, session=None):
        month = _month_of(url)
        seen.append(month)
        return FUNDING_CSV.encode() if month == "2021-01" else None

    monkeypatch.setattr(um, "_download_zip", fake)
    df = um.fetch_funding("BTCUSDT", "2021-01", "2021-01", cache_dir=cache)
    assert len(df) == 3
    assert (cache / "funding" / "BTCUSDT.parquet").exists()
    seen.clear()
    again = um.fetch_funding("BTCUSDT", "2021-01", "2021-01", cache_dir=cache)
    assert seen == []
    pd.testing.assert_frame_equal(df, again)


def test_fetch_funding_until(monkeypatch, cache):
    monkeypatch.setattr(
        um, "_download_zip",
        lambda url, session=None: FUNDING_CSV.encode() if _month_of(url) == "2021-01" else None,
    )
    df = um.fetch_funding("BTCUSDT", "2021-01", "2021-01",
                          until="2021-01-01 08:00", cache_dir=cache)
    assert len(df) == 1


# ───────────────────────────────────────────────────────── metrics ──

def _metrics_day(day: str) -> str:
    return (
        um._METRICS_COLUMNS and
        f"create_time,symbol,sum_open_interest,sum_open_interest_value,"
        f"count_toptrader_long_short_ratio,sum_toptrader_long_short_ratio,"
        f"count_long_short_ratio,sum_taker_long_short_vol_ratio\n"
        f"{day} 00:00:00,BTCUSDT,1,2,3,4,5,6\n"
        f"{day} 00:05:00,BTCUSDT,10,20,30,40,50,60\n"
    )


def test_fetch_metrics_max_days_guard(monkeypatch, cache):
    seen = []

    def fake(url, session=None):
        day = url.rsplit("-metrics-", 1)[-1].replace(".zip", "")
        seen.append(day)
        return _metrics_day(day).encode()

    monkeypatch.setattr(um, "_download_zip", fake)
    df = um.fetch_metrics("BTCUSDT", "2021-01-01", "2021-01-10",
                          max_days=3, cache_dir=cache)
    assert seen == ["2021-01-01", "2021-01-02", "2021-01-03"]
    assert len(df) == 6
    assert (cache / "metrics_5m" / "BTCUSDT.parquet").exists()


def test_fetch_metrics_resumes_without_refetch(monkeypatch, cache):
    seen = []

    def fake(url, session=None):
        day = url.rsplit("-metrics-", 1)[-1].replace(".zip", "")
        seen.append(day)
        return _metrics_day(day).encode()

    monkeypatch.setattr(um, "_download_zip", fake)
    um.fetch_metrics("BTCUSDT", "2021-01-01", "2021-01-05", max_days=2, cache_dir=cache)
    seen.clear()
    df = um.fetch_metrics("BTCUSDT", "2021-01-01", "2021-01-05", max_days=2, cache_dir=cache)
    assert seen == ["2021-01-03", "2021-01-04"]
    assert len(df) == 8  # 4 days x 2 rows, no duplicates


def test_fetch_metrics_until(monkeypatch, cache):
    monkeypatch.setattr(
        um, "_download_zip",
        lambda url, session=None: _metrics_day(
            url.rsplit("-metrics-", 1)[-1].replace(".zip", "")).encode(),
    )
    df = um.fetch_metrics("BTCUSDT", "2021-01-01", "2021-01-03",
                          until="2021-01-02", max_days=10, cache_dir=cache)
    assert len(df) == 2
    assert df.index[-1] == pd.Timestamp("2021-01-01 00:05:00", tz="UTC")


def test_fetch_metrics_missing_day_is_warning_not_error(monkeypatch, cache):
    monkeypatch.setattr(um, "_download_zip", lambda url, session=None: None)
    df = um.fetch_metrics("BTCUSDT", "2021-01-01", "2021-01-02",
                          max_days=10, cache_dir=cache)
    assert len(df) == 0


# ─────────────────────────────────────────────────── S3 XML parsing ──

_XML = """<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
<Name>data.binance.vision</Name><Prefix>data/futures/um/monthly/klines/</Prefix>
<Marker></Marker><MaxKeys>1000</MaxKeys><Delimiter>/</Delimiter>
<IsTruncated>false</IsTruncated>
<CommonPrefixes><Prefix>data/futures/um/monthly/klines/BTCUSDT/</Prefix></CommonPrefixes>
<CommonPrefixes><Prefix>data/futures/um/monthly/klines/ETHUSDT/</Prefix></CommonPrefixes>
<Contents><Key>data/futures/um/monthly/klines/x.zip</Key></Contents>
</ListBucketResult>"""


class _Resp:
    def __init__(self, content):
        self.status_code = 200
        self.content = content


def test_list_page_ignores_top_level_prefix_element(monkeypatch):
    monkeypatch.setattr(um, "_get", lambda url, session=None: _Resp(_XML.encode()))
    prefixes, keys, trunc, marker = um._list_page(um.KLINES_PREFIX)
    assert prefixes == [
        "data/futures/um/monthly/klines/BTCUSDT/",
        "data/futures/um/monthly/klines/ETHUSDT/",
    ]
    assert keys == ["data/futures/um/monthly/klines/x.zip"]
    assert trunc is False


def test_list_symbols_from_prefixes(monkeypatch):
    monkeypatch.setattr(um, "_get", lambda url, session=None: _Resp(_XML.encode()))
    assert um.list_symbols() == ["BTCUSDT", "ETHUSDT"]


# ───────────────────────────────────────────────────────── network ──

@pytest.mark.network
@pytest.mark.skipif(
    not _RUN_NETWORK,
    reason="network test; set BINANCE_UM_NETWORK_TESTS=1 to run",
)
def test_network_luna_delisted_month():
    keys = um.list_keys(f"{um.KLINES_PREFIX}LUNAUSDT/1d/")
    months = um._months_from_keys(keys)
    assert months[0] == "2021-01"
    assert months[-1] == "2022-05"
