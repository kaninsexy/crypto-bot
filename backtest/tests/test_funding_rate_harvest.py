"""
backtest/tests/test_funding_rate_harvest.py — unit tests for
Phase 4.B Variation #1's strategy module.

Pure synthetic fixtures; no network or filesystem.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from strategies.funding_rate_harvest import (
    FundingRateHarvestStrategy,
    make_funding_settlement_counter,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _ohlcv_block(n_bars: int, freq: str = "1h",
                 start: str = "2024-01-01") -> pd.DataFrame:
    idx = pd.date_range(start=start, periods=n_bars, freq=freq, tz="UTC")
    n = len(idx)
    return pd.DataFrame(
        {
            "open": np.full(n, 50000.0),
            "high": np.full(n, 50100.0),
            "low":  np.full(n, 49900.0),
            "close": np.linspace(50000.0, 50000.0 + n, n),
            "volume": np.full(n, 1.0),
        },
        index=idx,
    )


# ── Strategy tests ───────────────────────────────────────────────────────────

class TestFundingRateHarvestStrategy:
    def test_strategy_name_matches(self):
        strategy = FundingRateHarvestStrategy()
        assert strategy.name == "FundingRateHarvest"

    def test_default_symbol_and_timeframe(self):
        strategy = FundingRateHarvestStrategy()
        assert strategy.symbol == "BTC/USDT"
        assert strategy.timeframe == "1h"

    def test_constructor_kwargs_round_trip(self):
        strategy = FundingRateHarvestStrategy(
            symbol="ETH/USDT", timeframe="4h",
        )
        assert strategy.symbol == "ETH/USDT"
        assert strategy.timeframe == "4h"

    def test_emits_buy_every_call(self):
        strategy = FundingRateHarvestStrategy()
        df = _ohlcv_block(100)
        for i in range(50, 100):
            sig = strategy.generate_signal(df.iloc[: i + 1])
            assert sig.action == "BUY", (
                f"call at i={i} returned {sig.action!r}; expected 'BUY'"
            )

    def test_signal_carries_market_order_type_and_full_quantity(self):
        strategy = FundingRateHarvestStrategy()
        df = _ohlcv_block(100)
        sig = strategy.generate_signal(df)
        assert sig.order_type == "market"
        assert sig.quantity_pct == 1.0

    def test_signal_price_tracks_close(self):
        strategy = FundingRateHarvestStrategy()
        df = _ohlcv_block(100)
        sig = strategy.generate_signal(df)
        assert sig.price == pytest.approx(float(df["close"].iloc[-1]))

    def test_signal_strategy_field_matches_name(self):
        strategy = FundingRateHarvestStrategy()
        df = _ohlcv_block(100)
        sig = strategy.generate_signal(df)
        assert sig.strategy == "FundingRateHarvest"


# ── Factory tests ────────────────────────────────────────────────────────────

class TestMakeFundingSettlementCounter:
    def test_factory_returns_callable(self):
        counter = make_funding_settlement_counter(8)
        assert callable(counter)

    def test_counts_settlements_in_block_8h_cadence(self):
        counter = make_funding_settlement_counter(8)
        # Block spans 80 hours at 1h frequency → 80 // 8 = 10 settlements.
        df = _ohlcv_block(81, freq="1h")  # 81 rows → span = 80h
        out = counter(None, {"spot": df})
        assert out == 10

    def test_counts_settlements_in_block_1h_cadence(self):
        counter = make_funding_settlement_counter(1)
        df = _ohlcv_block(50, freq="1h")  # span = 49h → 49 settlements
        assert counter(None, {"spot": df}) == 49

    def test_returns_zero_for_empty_block(self):
        counter = make_funding_settlement_counter(8)
        empty = pd.DataFrame(
            {"close": []},
            index=pd.DatetimeIndex([], tz="UTC", name="timestamp"),
        )
        assert counter(None, {"spot": empty}) == 0

    def test_returns_zero_for_singleton_block(self):
        counter = make_funding_settlement_counter(8)
        idx = pd.date_range("2024-01-01", periods=1, freq="1h", tz="UTC")
        df = pd.DataFrame({"close": [50000.0]}, index=idx)
        assert counter(None, {"spot": df}) == 0

    def test_counts_use_spot_block_when_present(self):
        counter = make_funding_settlement_counter(8)
        spot = _ohlcv_block(81, freq="1h")  # 80h span
        # Different perp length to confirm the spot frame is the one used.
        perp = _ohlcv_block(17, freq="1h")  # 16h span
        out = counter(None, {"spot": spot, "perp": perp})
        assert out == 80 // 8  # = 10 (spot anchor)

    def test_falls_back_to_perp_when_spot_absent(self):
        counter = make_funding_settlement_counter(8)
        perp = _ohlcv_block(33, freq="1h")  # 32h span → 4 settlements
        out = counter(None, {"perp": perp})
        assert out == 4

    def test_factory_parametrizes_over_cadence(self):
        df = _ohlcv_block(81, freq="1h")  # span = 80h
        block = {"spot": df}
        assert make_funding_settlement_counter(8)(None, block) == 10
        assert make_funding_settlement_counter(1)(None, block) == 80
        assert make_funding_settlement_counter(4)(None, block) == 20

    def test_factory_rejects_non_positive_cadence(self):
        with pytest.raises(ValueError):
            make_funding_settlement_counter(0)
        with pytest.raises(ValueError):
            make_funding_settlement_counter(-1)
