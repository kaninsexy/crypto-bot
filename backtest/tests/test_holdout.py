"""
backtest/tests/test_holdout.py — Invariant tests for the holdout accessor.

All file I/O goes to tmp_path fixtures — never to the real
backtest/holdout_access.log or backtest/holdout_manifest.json.

The patch_holdout_paths fixture (autouse) redirects all module-level
path globals before each test and clears the load_manifest lru_cache.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import backtest.holdout as holdout
import backtest.logs as logs


# ── Fixture helpers ────────────────────────────────────────────────────────────

# Date landmarks used across tests
DATA_START = pd.Timestamp("2022-01-01T00:00:00", tz="UTC")
HOLDOUT_START = pd.Timestamp("2023-07-02T00:00:00", tz="UTC")
DATA_END = pd.Timestamp("2024-01-01T00:00:00", tz="UTC")

# DualMomentum has a slightly different start to test multi-symbol intersection
DM_BTC_START = pd.Timestamp("2022-01-01T00:00:00", tz="UTC")
DM_ETH_START = pd.Timestamp("2022-03-01T00:00:00", tz="UTC")  # later start
DM_BNB_START = pd.Timestamp("2022-01-01T00:00:00", tz="UTC")
DM_DATA_END = pd.Timestamp("2024-01-01T00:00:00", tz="UTC")
# intersection: max(starts) = 2022-03-01 → holdout at 80% through 2022-03-01..2024-01-01
DM_MANIFEST_START = pd.Timestamp("2022-03-01T00:00:00", tz="UTC")
DM_HOLDOUT_START = pd.Timestamp("2023-07-17T00:00:00", tz="UTC")  # ~80% of intersection


def make_ohlcv(start: pd.Timestamp, end: pd.Timestamp, freq: str = "7D") -> pd.DataFrame:
    """Create a small OHLCV DataFrame with a UTC DatetimeIndex named 'timestamp'."""
    idx = pd.date_range(start, end, freq=freq, tz="UTC", inclusive="left")
    idx.name = "timestamp"
    n = len(idx)
    return pd.DataFrame(
        {
            "open":   np.full(n, 100.0),
            "high":   np.full(n, 101.0),
            "low":    np.full(n, 99.0),
            "close":  np.full(n, 100.5),
            "volume": np.full(n, 1000.0),
        },
        index=idx,
    )


def write_manifest(path: Path, extra: dict | None = None) -> None:
    manifest = {
        "VWAP": {
            "symbol": "ETH/USDT",
            "timeframe": "1h",
            "data_start": DATA_START.isoformat(),
            "data_end": DATA_END.isoformat(),
            "dev_end": HOLDOUT_START.isoformat(),
            "holdout_start": HOLDOUT_START.isoformat(),
        },
        "DualMomentum": {
            "symbols": ["BTC/USDT", "ETH/USDT", "BNB/USDT"],
            "timeframe": "1h",
            "data_start": DM_MANIFEST_START.isoformat(),
            "data_end": DM_DATA_END.isoformat(),
            "dev_end": DM_HOLDOUT_START.isoformat(),
            "holdout_start": DM_HOLDOUT_START.isoformat(),
        },
    }
    if extra is not None:
        manifest.update(extra)
    path.write_text(json.dumps(manifest), encoding="utf-8")


def write_parquet_fixtures(cache_dir: Path) -> None:
    """Write minimal parquet files for each symbol used by fixture strategies."""
    df_eth = make_ohlcv(DATA_START, DATA_END)
    df_btc = make_ohlcv(DM_BTC_START, DM_DATA_END)
    df_bnb = make_ohlcv(DM_BNB_START, DM_DATA_END)

    df_eth.to_parquet(cache_dir / "ETH-USDT_1h_36mo.parquet")
    df_btc.to_parquet(cache_dir / "BTC-USDT_1h_36mo.parquet")
    df_bnb.to_parquet(cache_dir / "BNB-USDT_1h_36mo.parquet")


# ── Primary autouse fixture ────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def patch_holdout_paths(tmp_path, monkeypatch):
    """Redirect holdout module paths to tmp_path for every test."""
    manifest_path = tmp_path / "manifest.json"
    access_log_path = tmp_path / "holdout_access.log"
    cache_dir = tmp_path / "cache" / "ohlcv"
    cache_dir.mkdir(parents=True)

    write_manifest(manifest_path)
    write_parquet_fixtures(cache_dir)

    monkeypatch.setattr(holdout, "_MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(holdout, "_ACCESS_LOG_PATH", access_log_path)
    monkeypatch.setattr(holdout, "_CACHE_DIR", cache_dir)

    holdout.load_manifest.cache_clear()


# ── Invariant 1: load_dev returns only dev rows ───────────────────────────────

def test_load_dev_only_before_holdout_start():
    """Invariant 1: load_dev returns rows with timestamp < holdout_start."""
    df = holdout.load_dev("VWAP")
    assert len(df) > 0, "Expected non-empty dev set"
    assert (df.index < HOLDOUT_START).all(), (
        f"load_dev returned rows at or after holdout_start: {df.index[df.index >= HOLDOUT_START]}"
    )


# ── Invariant 2: load_holdout returns only holdout rows ───────────────────────

def test_load_holdout_only_at_or_after_holdout_start():
    """Invariant 2: load_holdout returns rows with timestamp >= holdout_start."""
    df = holdout.load_holdout(
        "VWAP", caller="phase3c.VWAP.final_dsr", reason="test"
    )
    assert len(df) > 0, "Expected non-empty holdout set"
    assert (df.index >= HOLDOUT_START).all(), (
        f"load_holdout returned rows before holdout_start: {df.index[df.index < HOLDOUT_START]}"
    )


# ── Invariant 3: first load_holdout succeeds ─────────────────────────────────

def test_first_load_holdout_succeeds():
    """Invariant 3: First load_holdout with a valid caller succeeds."""
    df = holdout.load_holdout(
        "VWAP", caller="phase3c.VWAP.final_dsr", reason="test inv 3"
    )
    assert isinstance(df, pd.DataFrame)
    assert len(df) > 0


# ── Invariant 4: second load_holdout raises HoldoutAlreadyAccessed ───────────

def test_second_load_holdout_raises(tmp_path):
    """Invariant 4: Second load_holdout raises HoldoutAlreadyAccessed."""
    # Pre-populate the log with one access event for VWAP.
    prior = {
        "ts": "2026-01-01T00:00:00+00:00",
        "strategy_id": "VWAP",
        "caller": "phase3c.VWAP.final_dsr",
        "reason": "prior run",
        "git_commit": "abc1234",
        "n_rows": 10,
        "regenerated": False,
    }
    logs.append_jsonl(holdout._ACCESS_LOG_PATH, prior)

    with pytest.raises(holdout.HoldoutAlreadyAccessed):
        holdout.load_holdout("VWAP", caller="phase3c.VWAP.final_dsr", reason="second attempt")


# ── Invariant 5: every successful load_holdout appends exactly one line ───────

def test_load_holdout_appends_exactly_one_event():
    """Invariant 5: successful load_holdout appends one event with required fields."""
    holdout.load_holdout(
        "VWAP", caller="phase3c.VWAP.final_dsr", reason="inv 5"
    )

    events = list(logs.read_jsonl(holdout._ACCESS_LOG_PATH))
    assert len(events) == 1

    ev = events[0]
    assert ev["strategy_id"] == "VWAP"
    assert ev["caller"] == "phase3c.VWAP.final_dsr"
    assert ev["reason"] == "inv 5"
    assert "ts" in ev
    assert "git_commit" in ev
    assert isinstance(ev["n_rows"], int) and ev["n_rows"] > 0


# ── Invariant 6: load_holdout without caller/reason raises TypeError ──────────

def test_load_holdout_keyword_only():
    """Invariant 6: caller and reason are keyword-only; positional raises TypeError."""
    with pytest.raises(TypeError):
        holdout.load_holdout("VWAP", "phase3c.VWAP.final_dsr", "test")  # type: ignore[call-arg]

    with pytest.raises(TypeError):
        holdout.load_holdout("VWAP")  # type: ignore[call-arg]


# ── Invariant 7: ManifestNotFound when manifest absent ───────────────────────

def test_load_manifest_raises_manifest_not_found(monkeypatch, tmp_path):
    """Invariant 7: load_manifest raises ManifestNotFound if file absent."""
    monkeypatch.setattr(holdout, "_MANIFEST_PATH", tmp_path / "nonexistent.json")
    holdout.load_manifest.cache_clear()

    with pytest.raises(holdout.ManifestNotFound):
        holdout.load_manifest()


# ── Invariant 8: ManifestSchemaError on malformed manifest ───────────────────

def test_load_manifest_raises_on_bad_json(monkeypatch, tmp_path):
    """Invariant 8a: ManifestSchemaError on malformed JSON."""
    bad = tmp_path / "bad.json"
    bad.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr(holdout, "_MANIFEST_PATH", bad)
    holdout.load_manifest.cache_clear()

    with pytest.raises(holdout.ManifestSchemaError):
        holdout.load_manifest()


def test_load_manifest_raises_on_missing_fields(monkeypatch, tmp_path):
    """Invariant 8b: ManifestSchemaError when required fields are missing."""
    bad = tmp_path / "missing_fields.json"
    # Missing holdout_start and data_end
    bad.write_text(json.dumps({
        "VWAP": {
            "symbol": "ETH/USDT",
            "timeframe": "1h",
            "data_start": "2022-01-01T00:00:00+00:00",
        }
    }), encoding="utf-8")
    monkeypatch.setattr(holdout, "_MANIFEST_PATH", bad)
    holdout.load_manifest.cache_clear()

    with pytest.raises(holdout.ManifestSchemaError):
        holdout.load_manifest()


def test_load_manifest_raises_on_missing_symbol_field(monkeypatch, tmp_path):
    """Invariant 8c: ManifestSchemaError when neither symbol nor symbols present."""
    bad = tmp_path / "no_symbol.json"
    bad.write_text(json.dumps({
        "VWAP": {
            "timeframe": "1h",
            "data_start": "2022-01-01T00:00:00+00:00",
            "data_end": "2024-01-01T00:00:00+00:00",
            "dev_end": "2023-07-02T00:00:00+00:00",
            "holdout_start": "2023-07-02T00:00:00+00:00",
        }
    }), encoding="utf-8")
    monkeypatch.setattr(holdout, "_MANIFEST_PATH", bad)
    holdout.load_manifest.cache_clear()

    with pytest.raises(holdout.ManifestSchemaError):
        holdout.load_manifest()


# ── Invariant 9: multi-symbol returns concatenated df with symbol column ──────

def test_multi_symbol_load_dev_returns_symbol_column():
    """Invariant 9a: load_dev on multi-symbol entry returns a 'symbol' column."""
    df = holdout.load_dev("DualMomentum")
    assert "symbol" in df.columns
    present = set(df["symbol"].unique())
    assert present == {"BTC/USDT", "ETH/USDT", "BNB/USDT"}


def test_multi_symbol_load_holdout_returns_symbol_column():
    """Invariant 9b: load_holdout on multi-symbol entry returns 'symbol' column."""
    df = holdout.load_holdout(
        "DualMomentum",
        caller="phase3c.DualMomentum.final_dsr",
        reason="multi-symbol test",
    )
    assert "symbol" in df.columns
    assert set(df["symbol"].unique()) == {"BTC/USDT", "ETH/USDT", "BNB/USDT"}


def test_single_symbol_load_dev_returns_symbol_column():
    """Invariant 9c: load_dev on single-symbol entry also returns 'symbol' column."""
    df = holdout.load_dev("VWAP")
    assert "symbol" in df.columns
    assert (df["symbol"] == "ETH/USDT").all()


# ── Invariant 10: post-regeneration reset allows a new access ─────────────────

def test_load_holdout_succeeds_after_regeneration_event():
    """Invariant 10: access succeeds when a regenerated=true event follows prior access."""
    # Simulate: one prior access, then a regeneration resets the window.
    prior_access = {
        "ts": "2026-01-01T00:00:00+00:00",
        "strategy_id": "VWAP",
        "caller": "phase3c.VWAP.final_dsr",
        "reason": "first run",
        "git_commit": "abc",
        "n_rows": 5,
        "regenerated": False,
    }
    regen_event = {
        "ts": "2026-02-01T00:00:00+00:00",
        "strategy_id": "VWAP",
        "regenerated": True,
        "old_holdout_start": "2023-07-02T00:00:00+00:00",
        "new_holdout_start": "2023-08-01T00:00:00+00:00",
    }
    logs.append_jsonl(holdout._ACCESS_LOG_PATH, prior_access)
    logs.append_jsonl(holdout._ACCESS_LOG_PATH, regen_event)

    # Should succeed — regen cleared the prior access
    df = holdout.load_holdout(
        "VWAP", caller="phase3c.VWAP.final_dsr", reason="post-regen access"
    )
    assert isinstance(df, pd.DataFrame)


# ── Invariants 11–13: InvalidCallerFormat for malformed caller strings ─────────

def test_caller_missing_phase_raises():
    """Invariant 11: caller without phase segment raises InvalidCallerFormat."""
    with pytest.raises(holdout.InvalidCallerFormat):
        holdout.load_holdout(
            "VWAP",
            caller="VWAP.final_dsr",
            reason="missing phase",
        )


def test_caller_unknown_phase_raises():
    """Invariant 12: caller with unknown phase raises InvalidCallerFormat."""
    with pytest.raises(holdout.InvalidCallerFormat):
        holdout.load_holdout(
            "VWAP",
            caller="phase99.VWAP.final_dsr",
            reason="unknown phase",
        )


def test_caller_unknown_purpose_raises():
    """Invariant 13: caller with unknown purpose raises InvalidCallerFormat."""
    with pytest.raises(holdout.InvalidCallerFormat):
        holdout.load_holdout(
            "VWAP",
            caller="phase3c.VWAP.random_poke",
            reason="unknown purpose",
        )


# ── Invariant 14: StrategyNotInManifest for well-formed but unknown strategy ──

def test_well_formed_caller_with_unknown_strategy_id_raises():
    """Invariant 14: valid caller format but strategy_id not in manifest raises
    StrategyNotInManifest, not InvalidCallerFormat."""
    with pytest.raises(holdout.StrategyNotInManifest):
        holdout.load_holdout(
            "GhostStrategy",
            caller="phase3c.GhostStrategy.final_dsr",
            reason="strategy not in manifest",
        )


# ── Invariant 15: valid caller passes regex ───────────────────────────────────

def test_valid_caller_string_passes_regex():
    """Invariant 15: 'phase3c.VWAP.final_dsr' passes the caller regex."""
    assert holdout._CALLER_RE.match("phase3c.VWAP.final_dsr") is not None
    assert holdout._CALLER_RE.match("phase4.BearShort.regression_check") is not None
    assert holdout._CALLER_RE.match("manual.GridTrading.manual_inspection") is not None


# ── Invariant 16: InvalidCallerFormat appends no event ───────────────────────

def test_invalid_caller_appends_no_event():
    """Invariant 16: when InvalidCallerFormat is raised no event is appended."""
    with pytest.raises(holdout.InvalidCallerFormat):
        holdout.load_holdout(
            "VWAP",
            caller="VWAP.final_dsr",  # missing phase
            reason="should not log",
        )

    # Log should be absent or empty
    events = list(logs.read_jsonl(holdout._ACCESS_LOG_PATH))
    assert len(events) == 0, f"Expected 0 events but got {len(events)}: {events}"


# ── Additional: StrategyNotInManifest from load_dev ──────────────────────────

def test_load_dev_raises_strategy_not_in_manifest():
    """Additional: load_dev raises StrategyNotInManifest for unknown strategy."""
    with pytest.raises(holdout.StrategyNotInManifest):
        holdout.load_dev("NoSuchStrategy")


# ── Additional: dev and holdout windows are disjoint and together cover full range

def test_dev_and_holdout_windows_are_disjoint():
    """Additional: dev and holdout rows do not overlap on the same symbol."""
    dev = holdout.load_dev("VWAP")
    hot = holdout.load_holdout(
        "VWAP", caller="phase3c.VWAP.final_dsr", reason="disjoint check"
    )
    dev_set = set(dev.index)
    hot_set = set(hot.index)
    assert dev_set.isdisjoint(hot_set), "dev and holdout windows overlap"


# ── Additional: logs.py round-trip ───────────────────────────────────────────

def test_logs_append_and_read_roundtrip(tmp_path):
    """Additional: append_jsonl + read_jsonl roundtrip preserves event."""
    path = tmp_path / "test.jsonl"
    ev = {"a": 1, "b": "hello", "c": None}
    logs.append_jsonl(path, ev)
    result = list(logs.read_jsonl(path))
    assert result == [ev]


def test_read_jsonl_empty_when_file_absent(tmp_path):
    """Additional: read_jsonl on absent file yields empty iterator."""
    result = list(logs.read_jsonl(tmp_path / "nonexistent.jsonl"))
    assert result == []


def test_iter_jsonl_filtered(tmp_path):
    """Additional: iter_jsonl_filtered only yields matching events."""
    path = tmp_path / "mixed.jsonl"
    logs.append_jsonl(path, {"strategy_id": "A", "val": 1})
    logs.append_jsonl(path, {"strategy_id": "B", "val": 2})
    logs.append_jsonl(path, {"strategy_id": "A", "val": 3})

    result = list(logs.iter_jsonl_filtered(
        path, lambda e: e.get("strategy_id") == "A"
    ))
    assert len(result) == 2
    assert all(e["strategy_id"] == "A" for e in result)


# ── Additional: _load_symbol_df picks highest month count when multiple files ──

# ── Phase 4.B Track I: legs schema validation + dispatch ──────────────────────

def _make_legs_manifest(extra_funding_cadence: bool = True) -> dict:
    """Build a legs-typed manifest entry for Phase 4.B testing."""
    entry = {
        "timeframe": "1h",
        "data_start": DATA_START.isoformat(),
        "data_end": DATA_END.isoformat(),
        "dev_end": HOLDOUT_START.isoformat(),
        "holdout_start": HOLDOUT_START.isoformat(),
        "legs": {"spot": "BTC/USDT", "perp": "BTC/USDT"},
    }
    if extra_funding_cadence:
        entry["funding_cadence_hours"] = 8
    return {"FundingRateHarvest_BTC": entry}


def test_manifest_accepts_legs_entry(monkeypatch, tmp_path):
    """Track I: manifest validator accepts a `legs: {spot, perp}` entry."""
    bad = tmp_path / "legs.json"
    bad.write_text(json.dumps(_make_legs_manifest()), encoding="utf-8")
    monkeypatch.setattr(holdout, "_MANIFEST_PATH", bad)
    holdout.load_manifest.cache_clear()

    m = holdout.load_manifest()
    assert "FundingRateHarvest_BTC" in m
    assert m["FundingRateHarvest_BTC"]["legs"] == {
        "spot": "BTC/USDT", "perp": "BTC/USDT",
    }
    assert m["FundingRateHarvest_BTC"]["funding_cadence_hours"] == 8


def test_manifest_rejects_symbol_and_legs_both_present(monkeypatch, tmp_path):
    """Track I: exactly one of {symbol, symbols, legs} is allowed."""
    bad = tmp_path / "conflict.json"
    entry = _make_legs_manifest()["FundingRateHarvest_BTC"]
    entry["symbol"] = "BTC/USDT"
    bad.write_text(json.dumps({"FundingRateHarvest_BTC": entry}), encoding="utf-8")
    monkeypatch.setattr(holdout, "_MANIFEST_PATH", bad)
    holdout.load_manifest.cache_clear()

    with pytest.raises(holdout.ManifestSchemaError):
        holdout.load_manifest()


def test_manifest_rejects_symbols_and_legs_both_present(monkeypatch, tmp_path):
    """Track I: symbols + legs both present is rejected."""
    bad = tmp_path / "conflict2.json"
    entry = _make_legs_manifest()["FundingRateHarvest_BTC"]
    entry["symbols"] = ["BTC/USDT", "ETH/USDT"]
    bad.write_text(json.dumps({"FundingRateHarvest_BTC": entry}), encoding="utf-8")
    monkeypatch.setattr(holdout, "_MANIFEST_PATH", bad)
    holdout.load_manifest.cache_clear()

    with pytest.raises(holdout.ManifestSchemaError):
        holdout.load_manifest()


def test_manifest_rejects_legs_with_extra_keys(monkeypatch, tmp_path):
    """Track I: legs must have exactly {spot, perp} — no extra keys."""
    bad = tmp_path / "extra_leg.json"
    entry = _make_legs_manifest()["FundingRateHarvest_BTC"]
    entry["legs"] = {"spot": "BTC/USDT", "perp": "BTC/USDT", "future": "BTC/USDT"}
    bad.write_text(json.dumps({"FundingRateHarvest_BTC": entry}), encoding="utf-8")
    monkeypatch.setattr(holdout, "_MANIFEST_PATH", bad)
    holdout.load_manifest.cache_clear()

    with pytest.raises(holdout.ManifestSchemaError):
        holdout.load_manifest()


def test_manifest_rejects_legs_missing_perp(monkeypatch, tmp_path):
    """Track I: legs without 'perp' key is rejected."""
    bad = tmp_path / "missing_perp.json"
    entry = _make_legs_manifest()["FundingRateHarvest_BTC"]
    entry["legs"] = {"spot": "BTC/USDT"}
    bad.write_text(json.dumps({"FundingRateHarvest_BTC": entry}), encoding="utf-8")
    monkeypatch.setattr(holdout, "_MANIFEST_PATH", bad)
    holdout.load_manifest.cache_clear()

    with pytest.raises(holdout.ManifestSchemaError):
        holdout.load_manifest()


def test_manifest_rejects_legs_with_non_string_symbol(monkeypatch, tmp_path):
    """Track I: leg values must be 'BASE/QUOTE' strings."""
    bad = tmp_path / "bad_leg.json"
    entry = _make_legs_manifest()["FundingRateHarvest_BTC"]
    entry["legs"] = {"spot": 123, "perp": "BTC/USDT"}
    bad.write_text(json.dumps({"FundingRateHarvest_BTC": entry}), encoding="utf-8")
    monkeypatch.setattr(holdout, "_MANIFEST_PATH", bad)
    holdout.load_manifest.cache_clear()

    with pytest.raises(holdout.ManifestSchemaError):
        holdout.load_manifest()


def test_manifest_rejects_funding_cadence_non_int(monkeypatch, tmp_path):
    """Track I: funding_cadence_hours must be an int (not float, not bool)."""
    bad = tmp_path / "bad_cadence.json"
    entry = _make_legs_manifest()["FundingRateHarvest_BTC"]
    entry["funding_cadence_hours"] = 8.5
    bad.write_text(json.dumps({"FundingRateHarvest_BTC": entry}), encoding="utf-8")
    monkeypatch.setattr(holdout, "_MANIFEST_PATH", bad)
    holdout.load_manifest.cache_clear()

    with pytest.raises(holdout.ManifestSchemaError):
        holdout.load_manifest()


def test_manifest_rejects_funding_cadence_bool(monkeypatch, tmp_path):
    """Track I: funding_cadence_hours rejects bool (subclass of int trap)."""
    bad = tmp_path / "bad_cadence_bool.json"
    entry = _make_legs_manifest()["FundingRateHarvest_BTC"]
    entry["funding_cadence_hours"] = True
    bad.write_text(json.dumps({"FundingRateHarvest_BTC": entry}), encoding="utf-8")
    monkeypatch.setattr(holdout, "_MANIFEST_PATH", bad)
    holdout.load_manifest.cache_clear()

    with pytest.raises(holdout.ManifestSchemaError):
        holdout.load_manifest()


def test_manifest_accepts_legs_without_funding_cadence(monkeypatch, tmp_path):
    """Track I: funding_cadence_hours is optional."""
    bad = tmp_path / "no_cadence.json"
    bad.write_text(
        json.dumps(_make_legs_manifest(extra_funding_cadence=False)),
        encoding="utf-8",
    )
    monkeypatch.setattr(holdout, "_MANIFEST_PATH", bad)
    holdout.load_manifest.cache_clear()

    m = holdout.load_manifest()
    assert "FundingRateHarvest_BTC" in m
    assert "funding_cadence_hours" not in m["FundingRateHarvest_BTC"]


def test_manifest_accepts_tbd_timeframe_for_legs_entry(monkeypatch, tmp_path):
    """Track I: <TBD> placeholder timeframe is accepted at schema level
    (the trial-time data load fails on the placeholder; gate fires at
    trial run, not at schema add)."""
    bad = tmp_path / "tbd.json"
    entry = _make_legs_manifest()["FundingRateHarvest_BTC"]
    entry["timeframe"] = "<TBD>"
    bad.write_text(json.dumps({"FundingRateHarvest_BTC": entry}), encoding="utf-8")
    monkeypatch.setattr(holdout, "_MANIFEST_PATH", bad)
    holdout.load_manifest.cache_clear()

    m = holdout.load_manifest()
    assert m["FundingRateHarvest_BTC"]["timeframe"] == "<TBD>"


def test_load_dev_returns_legs_dict_for_legs_entry(monkeypatch, tmp_path):
    """Track I: load_dev on a legs entry returns
    {'spot': DataFrame, 'perp': DataFrame}."""
    cache_dir = tmp_path / "cache" / "ohlcv"
    perp_cache_dir = tmp_path / "cache" / "perp"
    cache_dir.mkdir(parents=True, exist_ok=True)
    perp_cache_dir.mkdir(parents=True, exist_ok=True)

    spot_df = make_ohlcv(DATA_START, DATA_END)
    perp_df = make_ohlcv(DATA_START, DATA_END)
    spot_df.to_parquet(cache_dir / "BTC-USDT_1h_36mo.parquet")
    perp_df.to_parquet(perp_cache_dir / "BTC-USDT-SWAP_1h_36mo.parquet")

    manifest_path = tmp_path / "legs_manifest.json"
    manifest_path.write_text(
        json.dumps(_make_legs_manifest()), encoding="utf-8",
    )
    monkeypatch.setattr(holdout, "_MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(holdout, "_CACHE_DIR", cache_dir)
    monkeypatch.setattr(holdout, "_PERP_CACHE_DIR", perp_cache_dir)
    holdout.load_manifest.cache_clear()

    dev = holdout.load_dev("FundingRateHarvest_BTC")
    assert isinstance(dev, dict)
    assert set(dev.keys()) == {"spot", "perp"}
    assert (dev["spot"].index < HOLDOUT_START).all()
    assert (dev["perp"].index < HOLDOUT_START).all()


def test_load_holdout_returns_legs_dict_for_legs_entry(monkeypatch, tmp_path):
    """Track I: load_holdout on a legs entry returns
    {'spot': DataFrame, 'perp': DataFrame}."""
    cache_dir = tmp_path / "cache" / "ohlcv"
    perp_cache_dir = tmp_path / "cache" / "perp"
    cache_dir.mkdir(parents=True, exist_ok=True)
    perp_cache_dir.mkdir(parents=True, exist_ok=True)

    spot_df = make_ohlcv(DATA_START, DATA_END)
    perp_df = make_ohlcv(DATA_START, DATA_END)
    spot_df.to_parquet(cache_dir / "BTC-USDT_1h_36mo.parquet")
    perp_df.to_parquet(perp_cache_dir / "BTC-USDT-SWAP_1h_36mo.parquet")

    manifest_path = tmp_path / "legs_manifest.json"
    manifest_path.write_text(
        json.dumps(_make_legs_manifest()), encoding="utf-8",
    )
    access_log = tmp_path / "holdout_access.log"
    monkeypatch.setattr(holdout, "_MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(holdout, "_CACHE_DIR", cache_dir)
    monkeypatch.setattr(holdout, "_PERP_CACHE_DIR", perp_cache_dir)
    monkeypatch.setattr(holdout, "_ACCESS_LOG_PATH", access_log)
    holdout.load_manifest.cache_clear()

    hot = holdout.load_holdout(
        "FundingRateHarvest_BTC",
        caller="phase4.FundingRateHarvest_BTC.regression_check",
        reason="legs dispatch test",
    )
    assert isinstance(hot, dict)
    assert set(hot.keys()) == {"spot", "perp"}
    assert (hot["spot"].index >= HOLDOUT_START).all()
    assert (hot["perp"].index >= HOLDOUT_START).all()
    # Access event was appended.
    events = list(logs.read_jsonl(access_log))
    assert len(events) == 1
    assert events[0]["strategy_id"] == "FundingRateHarvest_BTC"


def test_load_symbol_df_picks_highest_month_count(tmp_path, monkeypatch):
    """Additional: when two parquet files exist for the same symbol/timeframe,
    _load_symbol_df selects the one with the higher {N}mo suffix, not the larger
    byte size."""
    cache_dir = tmp_path / "cache" / "ohlcv"
    cache_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(holdout, "_CACHE_DIR", cache_dir)

    # The autouse fixture already wrote ETH-USDT_1h_36mo.parquet into this
    # cache_dir. Remove it so this test controls both files explicitly.
    existing = list(cache_dir.glob("ETH-USDT_1h_*.parquet"))
    for f in existing:
        f.unlink()

    # 12-month fixture: fewer rows, but make it slightly larger in bytes to
    # confirm byte-size is NOT the selection criterion.
    df_12 = make_ohlcv(DATA_START, DATA_START + pd.Timedelta(days=365), freq="1D")
    df_12["extra"] = "padding_to_inflate_file_size_xxxxxxxxxxxxxxxxxxxxxxxxxxx"

    # 36-month fixture: more months, fewer padding bytes.
    df_36 = make_ohlcv(DATA_START, DATA_END, freq="7D")

    path_12 = cache_dir / "ETH-USDT_1h_12mo.parquet"
    path_36 = cache_dir / "ETH-USDT_1h_36mo.parquet"
    df_12.to_parquet(path_12)
    df_36.to_parquet(path_36)

    # Confirm 12mo file is actually larger on disk so the test is meaningful.
    assert path_12.stat().st_size > path_36.stat().st_size, (
        "Test setup invalid: 12mo file should be larger in bytes than 36mo file"
    )

    loaded = holdout._load_symbol_df("ETH/USDT", "1h")

    # The 36mo DataFrame spans a wider date range; use that as the discriminator.
    assert loaded.index.min() <= df_36.index.min() + pd.Timedelta(days=7), (
        "Expected the 36mo (higher month count) file to be selected"
    )
    assert loaded.index.max() >= df_12.index.max(), (
        "Expected the 36mo file, which covers a longer period, to be selected"
    )


# ── 2026-06-11 (work-order item 7d): unattributed regen no longer resets ─────

def test_regen_resets_access_grandfathers_pre_cutoff_events():
    """Historical (pre-2026-06-11) regenerated=true events without a
    caller keep resetting the flag — they were legitimate, just
    unattributed (e.g. the 2026-05-08 AttentionMomentum 429-retry
    reset)."""
    assert holdout._regen_resets_access({
        "ts": "2026-05-08T03:12:05.743402+00:00", "regenerated": True,
    }) is True


def test_regen_resets_access_requires_caller_post_cutoff():
    assert holdout._regen_resets_access({
        "ts": "2026-07-01T00:00:00+00:00", "regenerated": True,
    }) is False
    assert holdout._regen_resets_access({
        "ts": "2026-07-01T00:00:00+00:00", "regenerated": True,
        "caller": "   ",
    }) is False
    assert holdout._regen_resets_access({
        "ts": "2026-07-01T00:00:00+00:00", "regenerated": True,
        "caller": "manual.kanin.manifest_regen",
    }) is True


def test_has_prior_access_ignores_unattributed_post_cutoff_regen(
    tmp_path, monkeypatch,
):
    """An unattributed post-cutoff regenerated=true event must NOT
    re-open the holdout; an attributed one must."""
    import json as _json
    log = tmp_path / "access.log"
    monkeypatch.setattr(holdout, "_ACCESS_LOG_PATH", log)

    def _append(ev):
        with log.open("a", encoding="utf-8") as f:
            f.write(_json.dumps(ev) + "\n")

    _append({
        "ts": "2026-06-20T00:00:00+00:00", "strategy_id": "X",
        "caller": "phase4.X.final_dsr", "reason": "r",
        "git_commit": "abc", "n_rows": 1, "regenerated": False,
    })
    assert holdout._has_prior_access("X") is True

    # Unattributed reset attempt: flag stays set.
    _append({
        "ts": "2026-06-21T00:00:00+00:00", "strategy_id": "X",
        "regenerated": True,
        "old_holdout_start": "s", "new_holdout_start": "s",
    })
    assert holdout._has_prior_access("X") is True

    # Attributed regeneration: flag clears.
    _append({
        "ts": "2026-06-22T00:00:00+00:00", "strategy_id": "X",
        "regenerated": True, "caller": "manual.kanin.manifest_regen",
        "reason": "redraw", "old_holdout_start": "s",
        "new_holdout_start": "t",
    })
    assert holdout._has_prior_access("X") is False
