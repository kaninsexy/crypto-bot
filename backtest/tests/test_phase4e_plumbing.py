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
    """Phase 4.E symbols carry no "/", every OKX entry does.

    Updated 2026-09-02 (Phase 4.F). This test used to ALSO assert that no
    non-4.E entry may name a 4.E symbol — i.e. that "BTCUSDT" belonged
    exclusively to the Binance Vision spot substrate, so symbol SHAPE alone
    was a sufficient substrate discriminator.

    Phase 4.F breaks that deliberately: the Binance USDT-M perp archive uses
    the same concatenated-ticker form, and its three entries carry
    `symbol: "BTCUSDT"` as a dev/holdout boundary ANCHOR (the manifest schema
    cannot express their dynamic universe). Shape is therefore no longer
    sufficient, and `notes: substrate=<name>` is the explicit discriminator —
    see `holdout._entry_substrate` and `test_binance_um_substrate.py`.

    What survives is the shape half, split out below into the two properties
    the prohibition was standing in for.
    """
    m = load_manifest()
    for sid, tf in PHASE_4E:
        syms = m[sid].get("symbols", [m[sid]["symbol"]])
        assert all("/" not in s for s in syms)
    for sid, e in m.items():
        if sid in dict(PHASE_4E):
            continue
        syms = e.get("symbols", [e["symbol"]] if "symbol" in e else [])
        for s in syms:
            if "/" not in s:
                # A non-4.E entry in the concatenated form MUST declare its
                # substrate, or dispatch would silently send it to the 4.E
                # spot cache.
                assert H._entry_substrate(e) is not None, (
                    f"{sid} uses the concatenated symbol form {s!r} without a "
                    "`substrate=` tag in notes; dispatch would silently load "
                    "the Phase 4.E spot substrate"
                )


def test_shared_symbol_space_does_not_loosen_cache_enforcement():
    """The property the old prohibition was really protecting.

    `cache._earliest_holdout_start(symbol)` takes the MINIMUM holdout_start
    across every entry naming that symbol, and clips the dev window there. So
    a second family sharing a symbol can only ever TIGHTEN that cutoff — never
    loosen it — which is why sharing "BTCUSDT" between Phase 4.E and 4.F is
    safe. Asserting it directly is stronger than banning the sharing, because
    it keeps holding as more families join the symbol space.

    (Today every BTCUSDT entry declares the same 2025-05-01 split, so the
    minimum is unchanged; this test is what would catch a future entry that
    quietly pushed a shared symbol's cutoff later.)
    """
    from backtest import cache as C

    m = load_manifest()
    per_symbol: dict[str, list] = {}
    for e in m.values():
        syms = e.get("symbols", [e["symbol"]] if "symbol" in e else [])
        for s in syms:
            if "holdout_start" in e:
                per_symbol.setdefault(s, []).append(pd.Timestamp(e["holdout_start"]))

    for sym, starts in per_symbol.items():
        enforced = C._earliest_holdout_start(sym)
        assert enforced == min(starts), (
            f"cache enforcement for {sym} is {enforced}, not the minimum "
            f"{min(starts)} across the {len(starts)} entries naming it"
        )
        assert enforced <= min(starts), "enforcement must never loosen"


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
