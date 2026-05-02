"""
backtest/tests/test_cpcv_perp.py — Track H part 2: run_cpcv_perp.

Synthetic two-leg fixtures.  Verifies:
  - run_cpcv_perp routes through engine_perp per block.
  - Result shape matches CPCVResult and is consumable by
    `backtest.dsr.dsr_from_cpcv_result`.
  - CPCVError on insufficient block size (n_blocks too high).
  - CPCVError raised when the entry is not a legs entry (routing
    guard directing caller to run_cpcv).
  - NaN-handling on insufficient-trade blocks parallels run_cpcv.
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

import backtest.holdout as holdout
from backtest import cpcv_perp
from backtest.cpcv_common import CPCVConfig, CPCVError, CPCVResult
from backtest.dsr import dsr_from_cpcv_result
from strategies.base import BaseStrategy, Signal


# ── Fixture data ─────────────────────────────────────────────────────────────

DATA_START = pd.Timestamp("2023-05-03T01:00:00", tz="UTC")
DATA_END = pd.Timestamp("2026-04-29T10:00:00", tz="UTC")
HOLDOUT_START = pd.Timestamp("2025-09-22T22:36:00", tz="UTC")


def _make_ohlcv(start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    idx = pd.date_range(start, end, freq="1h", tz="UTC", inclusive="left")
    n = len(idx)
    rng = np.random.default_rng(7)
    close = 50_000.0 + np.cumsum(rng.normal(0, 5, n))
    return pd.DataFrame(
        {
            "open":   close + rng.normal(0, 1, n),
            "high":   close + np.abs(rng.normal(0, 5, n)),
            "low":    close - np.abs(rng.normal(0, 5, n)),
            "close":  close,
            "volume": np.full(n, 1000.0),
        },
        index=idx,
    )


def _write_manifest(
    path,
    legs_entry: bool = True,
) -> None:
    """Write a manifest with the FundingRateHarvest_BTC entry (legs)
    or a single-symbol VWAP entry (legs_entry=False) used by the
    routing-guard test."""
    if legs_entry:
        manifest = {
            "FundingRateHarvest_BTC": {
                "timeframe": "1h",
                "data_start": DATA_START.isoformat(),
                "data_end": DATA_END.isoformat(),
                "dev_end": HOLDOUT_START.isoformat(),
                "holdout_start": HOLDOUT_START.isoformat(),
                "legs": {"spot": "BTC/USDT", "perp": "BTC/USDT"},
                "funding_cadence_hours": 8,
            },
        }
    else:
        manifest = {
            "VWAP": {
                "timeframe": "1h",
                "data_start": DATA_START.isoformat(),
                "data_end": DATA_END.isoformat(),
                "dev_end": HOLDOUT_START.isoformat(),
                "holdout_start": HOLDOUT_START.isoformat(),
                "symbol": "ETH/USDT",
            },
        }
    path.write_text(json.dumps(manifest), encoding="utf-8")


@pytest.fixture
def patched_paths(tmp_path, monkeypatch):
    """Redirect manifest + cache paths to tmp_path."""
    cache_dir = tmp_path / "cache" / "ohlcv"
    perp_cache_dir = tmp_path / "cache" / "perp"
    cache_dir.mkdir(parents=True, exist_ok=True)
    perp_cache_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = tmp_path / "manifest.json"

    monkeypatch.setattr(holdout, "_MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(holdout, "_CACHE_DIR", cache_dir)
    monkeypatch.setattr(holdout, "_PERP_CACHE_DIR", perp_cache_dir)
    holdout.load_manifest.cache_clear()
    yield {
        "manifest_path": manifest_path,
        "cache_dir": cache_dir,
        "perp_cache_dir": perp_cache_dir,
    }


def _populate_legs_fixture(paths: dict) -> None:
    """Drop spot + perp parquets so load_dev finds legs data."""
    spot = _make_ohlcv(DATA_START, DATA_END)
    perp = _make_ohlcv(DATA_START, DATA_END)
    spot.to_parquet(paths["cache_dir"] / "BTC-USDT_1h_36mo.parquet")
    perp.to_parquet(paths["perp_cache_dir"] / "BTC-USDT-SWAP_1h_36mo.parquet")


# ── Test strategies ──────────────────────────────────────────────────────────

class _PeriodicTwoLegStrategy(BaseStrategy):
    """Issues BUY periodically and SELL after `hold_candles` to
    produce many trades per block — well above _MIN_TRADES_PER_BLOCK
    so blocks aren't NaN."""
    def __init__(
        self,
        symbol: str = "BTC/USDT",
        timeframe: str = "1h",
        trade_period: int = 30,
        hold_candles: int = 5,
    ):
        super().__init__(name="PeriodicTwoLeg", symbol=symbol, timeframe=timeframe)
        self.trade_period = trade_period
        self.hold_candles = hold_candles
        self._counter: int = 0
        self._in_position: bool = False
        self._held: int = 0

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        self._counter += 1
        price = float(df["close"].iloc[-1])
        if self._in_position:
            self._held += 1
            if self._held >= self.hold_candles:
                self._in_position = False
                self._held = 0
                return Signal(
                    action="SELL", strategy=self.name, price=price,
                    reason="periodic_close", order_type="market",
                )
            return Signal(
                action="HOLD", strategy=self.name, price=price,
                reason="hold", order_type="market",
            )
        if self._counter % self.trade_period == 0:
            self._in_position = True
            self._held = 0
            return Signal(
                action="BUY", strategy=self.name, price=price,
                reason="periodic_open", order_type="market",
            )
        return Signal(
            action="HOLD", strategy=self.name, price=price,
            reason="hold", order_type="market",
        )


# ── End-to-end synthetic run ─────────────────────────────────────────────────

def test_run_cpcv_perp_returns_cpcv_result_shape(patched_paths):
    _write_manifest(patched_paths["manifest_path"], legs_entry=True)
    _populate_legs_fixture(patched_paths)
    holdout.load_manifest.cache_clear()

    config = CPCVConfig(n_blocks=4, k_held_out=1)
    result = cpcv_perp.run_cpcv_perp(
        strategy_id="FundingRateHarvest_BTC",
        params={"warm_up_candles": 50},
        config=config,
        strategy_factory=lambda: _PeriodicTwoLegStrategy(),
    )
    assert isinstance(result, CPCVResult)
    assert result.n_paths == 4
    assert len(result.per_path_sharpes) == 4
    assert len(result.trades_per_path) == 4
    assert len(result.per_block_returns) == 4


def test_run_cpcv_perp_result_shape_compatible_with_dsr_helper(
    patched_paths, monkeypatch,
):
    """The CPCVResult shape returned by run_cpcv_perp is structurally
    compatible with `dsr_from_cpcv_result`: per_block_returns is a
    list of np.ndarray that the helper's
    `np.concatenate([r for r in result.per_block_returns if r.size > 0])`
    consumes successfully.  Whether the helper raises downstream
    (e.g., trials.log absent) is independent of CPCVResult shape."""
    _write_manifest(patched_paths["manifest_path"], legs_entry=True)
    _populate_legs_fixture(patched_paths)
    holdout.load_manifest.cache_clear()

    config = CPCVConfig(n_blocks=4, k_held_out=1)
    result = cpcv_perp.run_cpcv_perp(
        strategy_id="FundingRateHarvest_BTC",
        params={"warm_up_candles": 50},
        config=config,
        strategy_factory=lambda: _PeriodicTwoLegStrategy(),
    )

    # Shape contract: the per_block_returns concatenation is the data
    # the DSR helper consumes.  Mirror its filter+concat exactly.
    valid = [r for r in result.per_block_returns if r.size > 0]
    assert len(valid) > 0
    concat = np.concatenate(valid)
    assert concat.size > 0
    assert concat.dtype == np.float64

    # Patch trials.count_trials_for_dsr to a positive count so the
    # helper's downstream logic gets non-zero n_trials and we can
    # assert it processes a CPCVResult from run_cpcv_perp end-to-end.
    from backtest import trials
    monkeypatch.setattr(trials, "count_trials_for_dsr", lambda sid: 1)
    dsr_result = dsr_from_cpcv_result(
        result=result,
        strategy_id="FundingRateHarvest_BTC",
        sr_candidate=result.sharpe_distribution["mean"],
    )
    assert hasattr(dsr_result, "dsr")
    import math as _math
    assert _math.isfinite(dsr_result.dsr)


# ── Routing guard ────────────────────────────────────────────────────────────

def test_run_cpcv_perp_raises_on_non_legs_entry(patched_paths):
    """A `symbol` entry routed to run_cpcv_perp raises CPCVError
    pointing back to run_cpcv."""
    _write_manifest(patched_paths["manifest_path"], legs_entry=False)
    holdout.load_manifest.cache_clear()

    with pytest.raises(CPCVError) as exc:
        cpcv_perp.run_cpcv_perp(
            strategy_id="VWAP",
            params={},
            config=CPCVConfig(n_blocks=4, k_held_out=1),
            strategy_factory=lambda: _PeriodicTwoLegStrategy(symbol="ETH/USDT"),
        )
    assert "is not a 'legs' entry" in str(exc.value)


# ── Block-size validation ────────────────────────────────────────────────────

def test_run_cpcv_perp_raises_cpcv_error_on_too_small_blocks(patched_paths):
    """Asking for too many blocks on a fixed dev window triggers
    block-size validation."""
    _write_manifest(patched_paths["manifest_path"], legs_entry=True)
    # Small fixture so blocks fall below _MIN_BLOCK_CANDLES (60).
    short_start = pd.Timestamp("2025-09-01", tz="UTC")
    short_end = pd.Timestamp("2025-09-05", tz="UTC")  # 96 hours
    short_holdout = pd.Timestamp("2025-09-04", tz="UTC")
    manifest = {
        "FundingRateHarvest_BTC": {
            "timeframe": "1h",
            "data_start": short_start.isoformat(),
            "data_end": short_end.isoformat(),
            "dev_end": short_holdout.isoformat(),
            "holdout_start": short_holdout.isoformat(),
            "legs": {"spot": "BTC/USDT", "perp": "BTC/USDT"},
            "funding_cadence_hours": 8,
        },
    }
    patched_paths["manifest_path"].write_text(
        json.dumps(manifest), encoding="utf-8",
    )
    spot = _make_ohlcv(short_start, short_end)
    perp = _make_ohlcv(short_start, short_end)
    spot.to_parquet(patched_paths["cache_dir"] / "BTC-USDT_1h_1mo.parquet")
    perp.to_parquet(
        patched_paths["perp_cache_dir"] / "BTC-USDT-SWAP_1h_1mo.parquet",
    )
    holdout.load_manifest.cache_clear()

    with pytest.raises(CPCVError):
        cpcv_perp.run_cpcv_perp(
            strategy_id="FundingRateHarvest_BTC",
            params={"warm_up_candles": 10},
            config=CPCVConfig(n_blocks=10, k_held_out=2),
            strategy_factory=lambda: _PeriodicTwoLegStrategy(),
        )


# ── NaN-handling parallel ────────────────────────────────────────────────────

def test_run_cpcv_perp_raises_when_more_than_50pct_blocks_have_too_few_trades(
    patched_paths,
):
    """A strategy that generates almost no trades trips the > 50 %
    NaN block guard, mirroring run_cpcv's behavior."""
    _write_manifest(patched_paths["manifest_path"], legs_entry=True)
    _populate_legs_fixture(patched_paths)
    holdout.load_manifest.cache_clear()

    class _NoOpStrategy(BaseStrategy):
        def __init__(self):
            super().__init__(
                name="NoOp", symbol="BTC/USDT", timeframe="1h",
            )

        def generate_signal(self, df: pd.DataFrame) -> Signal:
            return Signal(
                action="HOLD", strategy=self.name,
                price=float(df["close"].iloc[-1]),
                reason="never trade", order_type="market",
            )

    with pytest.raises(CPCVError):
        cpcv_perp.run_cpcv_perp(
            strategy_id="FundingRateHarvest_BTC",
            params={"warm_up_candles": 50},
            config=CPCVConfig(n_blocks=4, k_held_out=1),
            strategy_factory=lambda: _NoOpStrategy(),
        )
