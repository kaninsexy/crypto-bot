"""
backtest/tests/test_generate_manifest.py — Tests for generate_holdout_manifest.py.

All file I/O is redirected to tmp_path.  config.STRATEGY_SYMBOLS is patched
to a minimal 3-strategy dict so tests don't depend on real cache files.
"""

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import config
import backtest.generate_holdout_manifest as gen
import backtest.logs as logs


# ── Shared date landmarks ──────────────────────────────────────────────────────

# Input ranges for fixture DataFrames (weekly candles, inclusive="left").
# The actual idx.max() will be the last weekly candle *before* the end date,
# not the end date itself.  All expected holdout_start values below are derived
# from idx.min()/idx.max() of the fixtures so they match what the generator reads.

# Per-symbol bounds.  BTC / ETH / BNB carry the historical extremes that
# exercise the intersection math (ETH = latest start; BNB = earliest end).
# Any other symbol that ends up in _MULTI_SYMBOL_OVERRIDES gets the
# baseline range, which is wider than both extremes and therefore does
# not perturb max(starts) or min(ends).  This keeps the fixture
# generator-aware: whenever a new symbol is added to the override
# constant the fixture covers it automatically without further edits.

_BASELINE_START = pd.Timestamp("2022-01-01T00:00:00", tz="UTC")
_BASELINE_END   = pd.Timestamp("2024-01-01T00:00:00", tz="UTC")

BTC_START = _BASELINE_START
BTC_END   = _BASELINE_END

ETH_START = pd.Timestamp("2022-03-01T00:00:00", tz="UTC")   # later start → tests max(starts)
ETH_END   = _BASELINE_END

BNB_START = _BASELINE_START
BNB_END   = pd.Timestamp("2023-12-01T00:00:00", tz="UTC")   # earlier end → tests min(ends)

SYMBOL_BOUNDS: dict[str, tuple[pd.Timestamp, pd.Timestamp]] = {
    "BTC/USDT": (BTC_START, BTC_END),
    "ETH/USDT": (ETH_START, ETH_END),
    "BNB/USDT": (BNB_START, BNB_END),
}

# Fixture config: 3 strategies (includes one multi-symbol)
FIXTURE_STRATEGY_SYMBOLS = {
    "VWAP":         "ETH/USDT",
    "BearShort":    "BTC/USDT",
    "DualMomentum": "BTC/USDT",   # primary symbol only; generator overrides for multi-symbol
}


def _expected_holdout_start(data_start: pd.Timestamp, data_end: pd.Timestamp) -> pd.Timestamp:
    """Replicate the generator's 80/20 formula so tests stay in sync."""
    return data_start + (data_end - data_start) * 0.80


def _bounds_for(symbol: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Return the fixture bounds for `symbol`, falling back to the baseline range
    so any future addition to `_MULTI_SYMBOL_OVERRIDES` is covered automatically.
    The baseline is wider than both ETH's start and BNB's end, so unknown
    symbols never perturb the intersection extremes."""
    return SYMBOL_BOUNDS.get(symbol, (_BASELINE_START, _BASELINE_END))


def _all_fixture_symbols() -> list[str]:
    """Union of single-symbol strategies' primary symbols and every symbol
    that appears in `_MULTI_SYMBOL_OVERRIDES` on the module under test.
    Reading the override constant at call-time keeps the fixture in sync
    when production baskets change."""
    syms: set[str] = set(FIXTURE_STRATEGY_SYMBOLS.values())
    for basket in gen._MULTI_SYMBOL_OVERRIDES.values():
        syms.update(basket)
    return sorted(syms)


def _expected_multi_symbol_intersection(
    symbols: list[str],
) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Replicate the generator's data_start = max(starts), data_end = min(ends)
    over `symbols`, using the same fixture DataFrames the generator reads."""
    starts: list[pd.Timestamp] = []
    ends:   list[pd.Timestamp] = []
    for sym in symbols:
        s, e = _bounds_for(sym)
        df = make_ohlcv(s, e)
        starts.append(df.index.min())
        ends.append(df.index.max())
    return max(starts), min(ends)


# ── Helpers ────────────────────────────────────────────────────────────────────

def make_ohlcv(start: pd.Timestamp, end: pd.Timestamp, freq: str = "7D") -> pd.DataFrame:
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


def write_fixture_cache(cache_dir: Path) -> None:
    """Write parquet fixtures for every symbol referenced by FIXTURE_STRATEGY_SYMBOLS
    or by `_MULTI_SYMBOL_OVERRIDES`.  Reading the override constant at fixture
    build time means new basket symbols are picked up automatically."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    for sym in _all_fixture_symbols():
        s, e = _bounds_for(sym)
        filename = f"{sym.replace('/', '-')}_1h_36mo.parquet"
        make_ohlcv(s, e).to_parquet(cache_dir / filename)


# ── Autouse fixture: redirect paths and patch config ─────────────────────────

@pytest.fixture(autouse=True)
def patch_gen_paths(tmp_path, monkeypatch):
    manifest_path  = tmp_path / "manifest.json"
    access_log     = tmp_path / "holdout_access.log"
    cache_dir      = tmp_path / "cache" / "ohlcv"

    write_fixture_cache(cache_dir)

    monkeypatch.setattr(gen, "_MANIFEST_PATH",  manifest_path)
    monkeypatch.setattr(gen, "_ACCESS_LOG_PATH", access_log)
    monkeypatch.setattr(gen, "_CACHE_DIR",       cache_dir)
    monkeypatch.setattr(config, "STRATEGY_SYMBOLS", FIXTURE_STRATEGY_SYMBOLS)


# ── Test 1: generate_initial produces entries for all strategies ───────────────

def test_generate_initial_produces_all_strategies():
    """generate_initial writes a manifest entry for every key in STRATEGY_SYMBOLS."""
    gen.generate_initial()

    manifest = json.loads(gen._MANIFEST_PATH.read_text())
    assert set(manifest.keys()) == set(FIXTURE_STRATEGY_SYMBOLS.keys())


# ── Test 2: generate_initial raises ManifestAlreadyExists if file present ─────

def test_generate_initial_raises_if_manifest_exists():
    """generate_initial raises ManifestAlreadyExists when the manifest already exists."""
    gen._MANIFEST_PATH.write_text("{}", encoding="utf-8")

    with pytest.raises(gen.ManifestAlreadyExists):
        gen.generate_initial()


# ── Test 3: multi-symbol intersection math ─────────────────────────────────────

def test_multi_symbol_intersection_math():
    """DualMomentum holdout_start uses max(starts) / min(ends) across whatever
    basket the generator's `_MULTI_SYMBOL_OVERRIDES` currently declares."""
    expected_basket = gen._MULTI_SYMBOL_OVERRIDES["DualMomentum"]
    exp_data_start, exp_data_end = _expected_multi_symbol_intersection(expected_basket)
    exp_holdout_start = _expected_holdout_start(exp_data_start, exp_data_end)

    gen.generate_initial()
    manifest = json.loads(gen._MANIFEST_PATH.read_text())
    dm = manifest["DualMomentum"]

    assert dm["symbols"] == expected_basket
    assert pd.Timestamp(dm["data_start"]) == exp_data_start
    assert pd.Timestamp(dm["data_end"])   == exp_data_end
    assert abs(pd.Timestamp(dm["holdout_start"]) - exp_holdout_start) < pd.Timedelta(seconds=1)


def test_single_symbol_bounds_use_full_range():
    """Single-symbol strategies use the symbol's full cache range, not an intersection."""
    df_eth = make_ohlcv(ETH_START, ETH_END)
    exp_hs = _expected_holdout_start(df_eth.index.min(), df_eth.index.max())

    gen.generate_initial()
    manifest = json.loads(gen._MANIFEST_PATH.read_text())
    actual_hs = pd.Timestamp(manifest["VWAP"]["holdout_start"])

    assert abs(actual_hs - exp_hs) < pd.Timedelta(seconds=1), (
        f"VWAP holdout_start: expected≈{exp_hs}, got {actual_hs}"
    )


# ── Test 4: regenerate_manifest appends events only for changed strategies ─────

def test_regenerate_appends_events_for_changed_strategies_only():
    """regenerate_manifest writes a regenerated=true event only for strategies
    whose holdout_start changed from what the old manifest recorded."""
    # First generate a correct manifest.
    gen.generate_initial()

    # Corrupt VWAP's holdout_start in the manifest to force a "change" on regen.
    manifest = json.loads(gen._MANIFEST_PATH.read_text())
    manifest["VWAP"]["holdout_start"] = "2020-01-01T00:00:00+00:00"
    gen._MANIFEST_PATH.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    gen.regenerate_manifest(caller="manual.testharness.manifest_regen", reason="unit test")

    events = list(logs.read_jsonl(gen._ACCESS_LOG_PATH))
    regen_events = [e for e in events if e.get("regenerated") is True]

    # Only VWAP changed; BearShort and DualMomentum holdout_starts are identical.
    regen_ids = {e["strategy_id"] for e in regen_events}
    assert "VWAP" in regen_ids, "Expected a regenerated event for VWAP"
    assert "BearShort" not in regen_ids, "BearShort should NOT have a regen event"
    assert "DualMomentum" not in regen_ids, "DualMomentum should NOT have a regen event"


def test_regenerate_event_contains_old_and_new_holdout_start():
    """The regenerated=true event records both old_holdout_start and new_holdout_start."""
    gen.generate_initial()

    old_hs_str = "2020-01-01T00:00:00+00:00"
    manifest = json.loads(gen._MANIFEST_PATH.read_text())
    manifest["VWAP"]["holdout_start"] = old_hs_str
    gen._MANIFEST_PATH.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    gen.regenerate_manifest(caller="manual.testharness.manifest_regen", reason="unit test")

    events = list(logs.read_jsonl(gen._ACCESS_LOG_PATH))
    regen_event = next(
        e for e in events
        if e.get("regenerated") is True and e.get("strategy_id") == "VWAP"
    )

    df_eth = make_ohlcv(ETH_START, ETH_END)
    exp_hs = _expected_holdout_start(df_eth.index.min(), df_eth.index.max())

    assert regen_event["old_holdout_start"] == old_hs_str
    new_hs = pd.Timestamp(regen_event["new_holdout_start"])
    assert abs(new_hs - exp_hs) < pd.Timedelta(seconds=1)


# ── Test 5: warning lists exactly the changed strategies ──────────────────────

def test_regenerate_warning_lists_only_changed_strategies(capsys):
    """The STALE DSR warning on stderr names changed strategies and only them."""
    gen.generate_initial()

    # Corrupt both VWAP and DualMomentum; leave BearShort intact.
    manifest = json.loads(gen._MANIFEST_PATH.read_text())
    manifest["VWAP"]["holdout_start"] = "2020-01-01T00:00:00+00:00"
    manifest["DualMomentum"]["holdout_start"] = "2020-01-01T00:00:00+00:00"
    gen._MANIFEST_PATH.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    gen.regenerate_manifest(caller="manual.testharness.manifest_regen", reason="unit test")

    stderr = capsys.readouterr().err
    assert "STALE DSR" in stderr
    assert "VWAP" in stderr
    assert "DualMomentum" in stderr
    assert "BearShort" not in stderr


def test_regenerate_no_warning_when_nothing_changed(capsys):
    """No STALE DSR message on stderr when all holdout_starts are already current."""
    gen.generate_initial()

    gen.regenerate_manifest(caller="manual.testharness.manifest_regen", reason="unit test")

    stderr = capsys.readouterr().err
    assert "STALE DSR" not in stderr


# ── Additional: manifest contains required holdout.py schema fields ────────────

def test_generated_manifest_passes_holdout_schema():
    """Entries written by generate_initial are accepted by holdout.load_manifest."""
    import backtest.holdout as holdout

    gen.generate_initial()

    # Point holdout at the same generated manifest.
    holdout._MANIFEST_PATH = gen._MANIFEST_PATH
    holdout.load_manifest.cache_clear()

    try:
        m = holdout.load_manifest()
        assert set(m.keys()) == set(FIXTURE_STRATEGY_SYMBOLS.keys())
    finally:
        # Restore to avoid polluting other test runs.
        holdout._MANIFEST_PATH = Path("backtest/holdout_manifest.json")
        holdout.load_manifest.cache_clear()


# ── Additional: generate_initial creates empty access log ─────────────────────

def test_generate_initial_creates_empty_access_log():
    """generate_initial touches holdout_access.log if it does not exist."""
    assert not gen._ACCESS_LOG_PATH.exists()
    gen.generate_initial()
    assert gen._ACCESS_LOG_PATH.exists()
    assert gen._ACCESS_LOG_PATH.stat().st_size == 0


# ── Additional: regenerate_manifest on subset only touches listed strategies ──

def test_regenerate_subset_only_updates_listed_strategies():
    """Passing a strategy list regenerates only those entries."""
    gen.generate_initial()
    manifest_before = json.loads(gen._MANIFEST_PATH.read_text())

    # Corrupt BearShort in manifest so it would change on full regen.
    manifest_before["BearShort"]["holdout_start"] = "2020-01-01T00:00:00+00:00"
    gen._MANIFEST_PATH.write_text(json.dumps(manifest_before, indent=2))

    # Regenerate only DualMomentum — BearShort should remain corrupted in the file.
    gen.regenerate_manifest(strategies=["DualMomentum"], caller="manual.testharness.manifest_regen", reason="unit test")

    manifest_after = json.loads(gen._MANIFEST_PATH.read_text())
    assert manifest_after["BearShort"]["holdout_start"] == "2020-01-01T00:00:00+00:00", (
        "BearShort was not in the target list and should not have been updated"
    )
    # DualMomentum should now reflect the correct computed value over whatever
    # basket the override constant currently declares.
    expected_basket = gen._MULTI_SYMBOL_OVERRIDES["DualMomentum"]
    exp_dm_start, exp_dm_end = _expected_multi_symbol_intersection(expected_basket)
    exp_dm_hs = _expected_holdout_start(exp_dm_start, exp_dm_end)
    dm_hs = pd.Timestamp(manifest_after["DualMomentum"]["holdout_start"])
    assert abs(dm_hs - exp_dm_hs) < pd.Timedelta(seconds=1)


# ── 2026-06-11 (work-order item 7d): regen attribution ───────────────────────

def test_regenerate_requires_valid_caller():
    """Unattributed or malformed callers are rejected before any
    manifest write or access-log append."""
    gen.generate_initial()
    with pytest.raises(gen.InvalidRegenCaller, match="manifest_regen"):
        gen.regenerate_manifest(caller="", reason="x")
    with pytest.raises(gen.InvalidRegenCaller, match="manifest_regen"):
        gen.regenerate_manifest(caller="kanin", reason="x")
    with pytest.raises(gen.InvalidRegenCaller, match="manifest_regen"):
        gen.regenerate_manifest(
            caller="phase99.kanin.manifest_regen", reason="x",
        )
    with pytest.raises(gen.InvalidRegenCaller, match="reason"):
        gen.regenerate_manifest(
            caller="manual.kanin.manifest_regen", reason="  ",
        )
    # Nothing was appended to the access log by the rejected calls.
    assert not gen._ACCESS_LOG_PATH.exists() or not [
        e for e in logs.read_jsonl(gen._ACCESS_LOG_PATH)
        if e.get("regenerated") is True
    ]


def test_regenerate_records_caller_and_reason():
    """Every regenerated=true event carries the attribution fields."""
    gen.generate_initial()
    # Corrupt VWAP's holdout_start to force a "change" on regen (same
    # pattern as test 4 above).
    manifest = json.loads(gen._MANIFEST_PATH.read_text())
    manifest["VWAP"]["holdout_start"] = "2020-01-01T00:00:00+00:00"
    gen._MANIFEST_PATH.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    gen.regenerate_manifest(
        caller="manual.testharness.manifest_regen",
        reason="bounds moved in unit test",
    )
    events = list(logs.read_jsonl(gen._ACCESS_LOG_PATH))
    regen_events = [e for e in events if e.get("regenerated") is True]
    assert regen_events, "expected at least one regenerated=true event"
    for e in regen_events:
        assert e["caller"] == "manual.testharness.manifest_regen"
        assert e["reason"] == "bounds moved in unit test"
        assert "git_commit" in e
