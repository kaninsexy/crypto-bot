"""Phase 4.E manifest entries + substrate-dispatch plumbing tests.

Hermetic: the substrate DISPATCH is tested by monkeypatching the two
loaders, so these tests never touch the (gitignored) Binance Vision 1m
cache.  The manifest tests read the committed manifest directly.
"""

import pandas as pd
import pytest

from backtest import holdout as H
from backtest.holdout import load_manifest


PHASE_4E = [
    ("VolumeProfileAcceptance", "1h"),
    ("LiquiditySweepReversal", "15m"),
    ("LVNTraversal", "15m"),
    ("HVNMeanReversion", "1h"),
    ("DeltaDivergence", "15m"),
    ("VWAPInstitutionalBand", "15m"),
    ("BreakoutDeltaConfirmed", "1h"),
]

DATA_START = "2021-01-01T00:00:00+00:00"
DATA_END = "2026-05-31T00:00:00+00:00"
SPLIT = "2025-05-01T00:00:00+00:00"


# ── Manifest entries ──────────────────────────────────────────────────────────

def test_all_seven_entries_present_and_shaped():
    m = load_manifest()
    for sid, tf in PHASE_4E:
        assert sid in m, f"{sid} missing from manifest"
        e = m[sid]
        assert e["timeframe"] == tf
        assert e["symbol"] == "BTCUSDT"          # Binance concatenated form
        assert e["data_start"] == DATA_START
        assert e["data_end"] == DATA_END
        assert e["dev_end"] == SPLIT
        assert e["holdout_start"] == SPLIT
        assert "Binance spot 1m" in e["notes"]   # provenance disclosure
        assert "execution venue remains OKX" in e["notes"]


def test_entries_use_existing_schema_only():
    # No new schema fields: every key must be one the harness already knows.
    allowed = {
        "timeframe", "data_start", "data_end", "dev_end", "holdout_start",
        "symbol", "symbols", "legs", "notes", "funding_cadence_hours",
        "signal_cadence", "strategy_warmup_candles",
        "min_tradeable_candles_per_block",
    }
    m = load_manifest()
    for sid, _ in PHASE_4E:
        extra = set(m[sid].keys()) - allowed
        assert not extra, f"{sid} introduced new schema field(s): {extra}"


def test_symbols_never_collide_with_okx_entries():
    # The dispatch invariant: Phase 4.E symbols carry no "/", every OKX
    # entry does.  Guarantees cache.py per-symbol enforcement stays disjoint.
    m = load_manifest()
    p4e_syms = {"BTCUSDT"}
    for sid, e in m.items():
        syms = e.get("symbols", [e["symbol"]] if "symbol" in e else [])
        if sid in dict(PHASE_4E):
            assert all("/" not in s for s in syms)
        else:
            # legs entries have no symbol/symbols; skip those
            for s in syms:
                assert s not in p4e_syms


# ── Substrate dispatch ────────────────────────────────────────────────────────

def test_dispatch_slash_symbol_uses_okx_loader(monkeypatch):
    calls = {}
    monkeypatch.setattr(
        H, "_load_symbol_df",
        lambda sym, tf: calls.setdefault("okx", (sym, tf)) or pd.DataFrame(),
    )
    monkeypatch.setattr(
        H, "load_binance_vision_signal_frame",
        lambda sym, tf: calls.setdefault("bv", (sym, tf)) or pd.DataFrame(),
    )
    H._load_substrate_df("BTC/USDT", "1h")
    assert calls.get("okx") == ("BTC/USDT", "1h")
    assert "bv" not in calls


def test_dispatch_concatenated_symbol_uses_binance_vision(monkeypatch):
    calls = {}
    monkeypatch.setattr(
        H, "_load_symbol_df",
        lambda sym, tf: calls.setdefault("okx", (sym, tf)) or pd.DataFrame(),
    )
    monkeypatch.setattr(
        H, "load_binance_vision_signal_frame",
        lambda sym, tf: calls.setdefault("bv", (sym, tf)) or pd.DataFrame(),
    )
    H._load_substrate_df("BTCUSDT", "15m")
    assert calls.get("bv") == ("BTCUSDT", "15m")
    assert "okx" not in calls
