"""Phase 4.F: the `substrate=binance_um` loader branch and the access-log
annotation guard (governance port S2, 2026-09-02).

Two behaviours are pinned here, and both are written so they CAN fail:

1. **Substrate dispatch.** A manifest entry whose `notes` carry
   `substrate=binance_um` must load from the Binance USDT-M perp archive, NOT
   from the Phase 4.E Binance Vision SPOT cache. Before this change the
   discriminator was the symbol's SHAPE, and both substrates use the same
   concatenated-ticker form ("BTCUSDT") — so a Phase 4.F entry would silently
   have loaded spot bars. The negative test (`test_untagged_symbol_still_uses_shape_inference`)
   is the one that would catch a regression back to shape-only dispatch.

2. **Non-read annotations.** A `"added": true` row in `holdout_access.log`
   must NOT consume the single-access flag. `_has_prior_access` counts every
   non-`regenerated` event as a read, so without the guard the three Phase 4.F
   manifest-addition rows would have burned each strategy's one holdout access
   before any of them ran.
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from backtest import holdout as h


# ── 1. substrate tag parsing ─────────────────────────────────────────────────

def test_entry_substrate_reads_the_tag_from_notes():
    entry = {"notes": "Phase 4.F perp-structural. substrate=binance_um. "
                      "discovery_end=2023-01-01"}
    assert h._entry_substrate(entry) == "binance_um"


def test_entry_substrate_is_none_without_a_tag():
    assert h._entry_substrate({"notes": "an ordinary note"}) is None
    assert h._entry_substrate({}) is None
    assert h._entry_substrate({"notes": None}) is None


def test_entry_substrate_ignores_a_lookalike_word():
    # `substrate:` and `substrate is` must not parse as a tag — only `=`.
    assert h._entry_substrate({"notes": "the substrate is binance_um"}) is None


# ── 2. dispatch ──────────────────────────────────────────────────────────────

def test_tagged_entry_routes_to_the_um_archive(monkeypatch):
    calls = {}

    def fake_um(symbol, timeframe):
        calls["um"] = (symbol, timeframe)
        return pd.DataFrame({"close": [1.0]})

    def fake_spot(symbol, timeframe):  # pragma: no cover - must not run
        calls["spot"] = (symbol, timeframe)
        return pd.DataFrame({"close": [2.0]})

    monkeypatch.setattr(h, "_load_binance_um_df", fake_um)
    monkeypatch.setattr(h, "load_binance_vision_signal_frame", fake_spot)

    out = h._load_substrate_df("BTCUSDT", "1d", "binance_um")
    assert calls == {"um": ("BTCUSDT", "1d")}, "spot loader must not be called"
    assert float(out["close"].iloc[0]) == 1.0


def test_untagged_symbol_still_uses_shape_inference(monkeypatch):
    """The discriminating case: identical symbol, no tag -> the SPOT cache.

    If dispatch ever regresses to 'concatenated ticker means perp', this test
    goes red. If it regresses to 'ignore the tag', the test above goes red.
    """
    calls = {}
    monkeypatch.setattr(
        h, "_load_binance_um_df",
        lambda s, t: calls.setdefault("um", (s, t)) or pd.DataFrame({"close": [1.0]}))
    monkeypatch.setattr(
        h, "load_binance_vision_signal_frame",
        lambda s, t: calls.setdefault("spot", (s, t)) or pd.DataFrame({"close": [2.0]}))

    h._load_substrate_df("BTCUSDT", "1d", None)
    assert "spot" in calls and "um" not in calls


def test_slash_symbol_still_uses_the_okx_cache(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        h, "_load_symbol_df",
        lambda s, t: seen.setdefault("okx", (s, t)) or pd.DataFrame({"close": [3.0]}))
    h._load_substrate_df("BTC/USDT", "1d", None)
    assert seen["okx"] == ("BTC/USDT", "1d")


def test_unknown_substrate_tag_raises_rather_than_guessing():
    with pytest.raises(h.ManifestSchemaError, match="Unknown substrate tag"):
        h._load_substrate_df("BTCUSDT", "1d", "some_future_venue")


# ── 3. the cache-only reader ─────────────────────────────────────────────────

def test_um_loader_is_cache_only_and_says_how_to_populate(monkeypatch, tmp_path):
    monkeypatch.setattr(h, "_UM_CACHE_DIR", tmp_path)
    with pytest.raises(FileNotFoundError, match="prefetch_binance_um"):
        h._load_binance_um_df("NOPEUSDT", "1d")


def test_um_loader_attaches_funding_when_the_cache_has_it(monkeypatch, tmp_path):
    idx = pd.date_range("2021-01-01", periods=4, freq="D", tz="UTC", name="ts")
    (tmp_path / "klines").mkdir(parents=True)
    pd.DataFrame({"close": [1.0, 2.0, 3.0, 4.0]}, index=idx).to_parquet(
        tmp_path / "klines" / "FOOUSDT_1d.parquet")

    # Settlement stamps with millisecond jitter, as the real archive has.
    f_idx = pd.DatetimeIndex(
        ["2021-01-01T00:00:00.017Z", "2021-01-03T00:00:00.004Z"], name="ts")
    (tmp_path / "funding").mkdir(parents=True)
    pd.DataFrame(
        {"last_funding_rate": [0.0001, 0.0002], "funding_interval_hours": [8, 4]},
        index=f_idx,
    ).to_parquet(tmp_path / "funding" / "FOOUSDT.parquet")

    monkeypatch.setattr(h, "_UM_CACHE_DIR", tmp_path)
    df = h._load_binance_um_df("FOOUSDT", "1d")

    assert list(df.index) == list(idx)
    assert df["last_funding_rate"].iloc[0] == pytest.approx(0.0001)
    assert df["last_funding_rate"].iloc[1] == pytest.approx(0.0001)  # ffilled
    assert df["last_funding_rate"].iloc[2] == pytest.approx(0.0002)
    # BK-0004: the interval is NOT always 8h and must survive as data.
    assert df["funding_interval_hours"].iloc[2] == 4


def test_um_loader_works_without_the_optional_caches(monkeypatch, tmp_path):
    idx = pd.date_range("2021-01-01", periods=2, freq="D", tz="UTC", name="ts")
    (tmp_path / "klines").mkdir(parents=True)
    pd.DataFrame({"close": [1.0, 2.0]}, index=idx).to_parquet(
        tmp_path / "klines" / "BARUSDT_1d.parquet")
    monkeypatch.setattr(h, "_UM_CACHE_DIR", tmp_path)
    df = h._load_binance_um_df("BARUSDT", "1d")
    assert "last_funding_rate" not in df.columns
    assert len(df) == 2


# ── 4. the access-log annotation guard ───────────────────────────────────────

def _write_log(path, events):
    path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")


def test_added_annotation_does_not_consume_the_single_access(monkeypatch, tmp_path):
    log = tmp_path / "holdout_access.log"
    _write_log(log, [{
        "ts": "2026-09-02T00:00:00+00:00",
        "strategy_id": "FundingDispersionCarry",
        "caller": "phase4f.manifest_add",
        "reason": "Phase 4.F perp-structural manifest entry added",
        "added": True,
        "regenerated": False,
    }])
    monkeypatch.setattr(h, "_ACCESS_LOG_PATH", log)
    assert h._has_prior_access("FundingDispersionCarry") is False, (
        "a manifest-addition annotation must not burn the strategy's one "
        "holdout access"
    )


def test_a_real_read_still_consumes_the_single_access(monkeypatch, tmp_path):
    """The discriminating half: the guard must not swallow genuine reads."""
    log = tmp_path / "holdout_access.log"
    _write_log(log, [
        {"ts": "2026-09-02T00:00:00+00:00", "strategy_id": "X",
         "caller": "phase4f.manifest_add", "added": True, "regenerated": False},
        {"ts": "2026-09-03T00:00:00+00:00", "strategy_id": "X",
         "caller": "phase4.X.final_dsr", "n_rows": 480, "regenerated": False},
    ])
    monkeypatch.setattr(h, "_ACCESS_LOG_PATH", log)
    assert h._has_prior_access("X") is True


def test_added_flag_cannot_launder_a_real_read(monkeypatch, tmp_path):
    """`added: true` on an event that CARRIES n_rows is still a read.

    The predicate is a conjunction precisely so a future caller cannot wave a
    genuine holdout read past the guard by tacking on the annotation flag.
    """
    log = tmp_path / "holdout_access.log"
    _write_log(log, [{
        "ts": "2026-09-03T00:00:00+00:00", "strategy_id": "Y",
        "caller": "phase4.Y.final_dsr", "n_rows": 480,
        "added": True, "regenerated": False,
    }])
    monkeypatch.setattr(h, "_ACCESS_LOG_PATH", log)
    assert h._has_prior_access("Y") is True


def test_historical_events_are_unaffected(monkeypatch, tmp_path):
    """No event predating 2026-09-02 carries `added`, so the guard is a no-op
    on the real log — asserted against the shipped file, not a fixture."""
    events = [
        json.loads(line)
        for line in h._ACCESS_LOG_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    pre_existing = [e for e in events if e.get("caller") != "phase4f.manifest_add"]
    assert pre_existing, "expected the real access log to be non-empty"
    assert not any(h._is_non_read_annotation(e) for e in pre_existing)
