"""
backtest/tests/test_cpcv.py — Tests for backtest.cpcv (block-Sharpe mode).

Covers:
  - CPCVConfig.validate boundary conditions
  - summarize() shape and quantile correctness
  - block construction (single + multi-symbol, last absorbs remainder,
    minimum-size enforcement, lockstep timestamps)
  - Sharpe-from-returns helper and candle-duration inference
  - purge / embargo trim
  - run_cpcv end-to-end on a synthetic dev window
  - CPCVError raised on too-small blocks and on > 50 % NaN blocks
  - Block-Sharpe variance (the methodology produces real distribution
    variance, not the chunk-6 path-CPCV degeneracy)
  - Purge / embargo actually changes per-block Sharpe outputs
"""

import math

import numpy as np
import pandas as pd
import pytest

import backtest.holdout as holdout
from backtest.cpcv import (
    CPCVConfig,
    CPCVError,
    CPCVResult,
    _apply_purge_embargo,
    _infer_candle_hours,
    _run_engine_per_block,
    _sharpe_from_returns,
    _split_blocks,
    _split_blocks_multi,
    run_cpcv,
    summarize,
)
from strategies.base import BaseStrategy


# ── CPCVConfig.validate ──────────────────────────────────────────────────────

def test_cpcv_config_validate_accepts_valid_config():
    CPCVConfig(
        n_blocks=10, k_held_out=2, purge_periods=24, embargo_periods=6,
    ).validate()


def test_cpcv_config_validate_accepts_minimum_n_blocks():
    CPCVConfig(
        n_blocks=4, k_held_out=1, purge_periods=0, embargo_periods=0,
    ).validate()


def test_cpcv_config_validate_rejects_n_blocks_below_four():
    with pytest.raises(ValueError):
        CPCVConfig(
            n_blocks=3, k_held_out=1, purge_periods=0, embargo_periods=0,
        ).validate()


def test_cpcv_config_validate_rejects_k_held_out_equal_n_blocks():
    with pytest.raises(ValueError):
        CPCVConfig(
            n_blocks=10, k_held_out=10, purge_periods=0, embargo_periods=0,
        ).validate()


def test_cpcv_config_validate_rejects_k_held_out_greater_than_n_blocks():
    with pytest.raises(ValueError):
        CPCVConfig(
            n_blocks=10, k_held_out=11, purge_periods=0, embargo_periods=0,
        ).validate()


def test_cpcv_config_validate_rejects_k_held_out_zero():
    with pytest.raises(ValueError):
        CPCVConfig(
            n_blocks=10, k_held_out=0, purge_periods=0, embargo_periods=0,
        ).validate()


def test_cpcv_config_validate_rejects_negative_purge():
    with pytest.raises(ValueError):
        CPCVConfig(
            n_blocks=10, k_held_out=2, purge_periods=-1, embargo_periods=0,
        ).validate()


def test_cpcv_config_validate_rejects_negative_embargo():
    with pytest.raises(ValueError):
        CPCVConfig(
            n_blocks=10, k_held_out=2, purge_periods=0, embargo_periods=-1,
        ).validate()


# ── summarize() ──────────────────────────────────────────────────────────────

def test_summarize_returns_correct_shape():
    out = summarize([0.1, 0.5, 0.9, 1.3, 1.7])
    assert set(out.keys()) == {"mean", "std", "quantiles"}
    assert set(out["quantiles"].keys()) == {"p05", "p25", "p50", "p75", "p95"}


def test_summarize_quantiles_match_numpy_percentile():
    sharpes = [0.1, 0.5, 0.9, 1.3, 1.7, -0.2, 0.4, 1.0, 0.7, 1.1]
    out = summarize(sharpes)
    arr = np.asarray(sharpes, dtype=float)

    assert out["mean"] == pytest.approx(float(arr.mean()))
    assert out["std"] == pytest.approx(float(arr.std()))

    expected = np.percentile(arr, [5, 25, 50, 75, 95], method="linear")
    assert out["quantiles"]["p05"] == pytest.approx(float(expected[0]))
    assert out["quantiles"]["p25"] == pytest.approx(float(expected[1]))
    assert out["quantiles"]["p50"] == pytest.approx(float(expected[2]))
    assert out["quantiles"]["p75"] == pytest.approx(float(expected[3]))
    assert out["quantiles"]["p95"] == pytest.approx(float(expected[4]))


def test_summarize_raises_on_empty_input():
    with pytest.raises(ValueError):
        summarize([])


def test_summarize_handles_single_element_input():
    out = summarize([1.5])
    assert out["mean"] == 1.5
    assert out["std"] == 0.0
    for q_key in ("p05", "p25", "p50", "p75", "p95"):
        assert out["quantiles"][q_key] == 1.5


def test_summarize_returns_python_floats_not_numpy_scalars():
    """Sanity: trials.log JSON serialisation needs plain floats, not
    numpy scalars (which json.dumps would reject without coercion)."""
    out = summarize([0.1, 0.5, 0.9, 1.3, 1.7])
    assert type(out["mean"]) is float
    assert type(out["std"]) is float
    for v in out["quantiles"].values():
        assert type(v) is float


# ── Block construction: single-symbol ────────────────────────────────────────

def _make_ohlcv(n_rows: int, freq: str = "1h", start: str = "2024-01-01") -> pd.DataFrame:
    idx = pd.date_range(start=start, periods=n_rows, freq=freq, tz="UTC")
    idx.name = "timestamp"
    rng = np.random.default_rng(42)
    close = 100.0 + np.cumsum(rng.normal(0, 0.5, n_rows))
    return pd.DataFrame(
        {
            "open":   close + rng.normal(0, 0.1, n_rows),
            "high":   close + np.abs(rng.normal(0, 0.3, n_rows)),
            "low":    close - np.abs(rng.normal(0, 0.3, n_rows)),
            "close":  close,
            "volume": np.full(n_rows, 1000.0),
        },
        index=idx,
    )


def test_split_blocks_returns_n_contiguous_blocks_concatenable_to_input():
    df = _make_ohlcv(1000)
    blocks = _split_blocks(df, n_blocks=4)
    assert len(blocks) == 4
    rejoined = pd.concat(blocks)
    pd.testing.assert_frame_equal(rejoined, df)


def test_split_blocks_last_block_absorbs_remainder():
    df = _make_ohlcv(1003)  # 1003 / 4 = 250 rem 3
    blocks = _split_blocks(df, n_blocks=4)
    assert [len(b) for b in blocks] == [250, 250, 250, 253]


def test_split_blocks_evenly_divisible_lengths():
    df = _make_ohlcv(1000)
    blocks = _split_blocks(df, n_blocks=4)
    assert [len(b) for b in blocks] == [250, 250, 250, 250]


# ── Block construction: multi-symbol ─────────────────────────────────────────

def _make_multi_df(n_rows: int, symbols: list[str]) -> pd.DataFrame:
    parts = []
    for sym in symbols:
        df = _make_ohlcv(n_rows)
        df = df.copy()
        df["symbol"] = sym
        parts.append(df)
    return pd.concat(parts).sort_index()


def test_split_blocks_multi_partitions_by_symbol():
    multi = _make_multi_df(800, ["BTC/USDT", "ETH/USDT"])
    blocks = _split_blocks_multi(multi, n_blocks=4, symbols=["BTC/USDT", "ETH/USDT"])
    assert len(blocks) == 4
    for block in blocks:
        assert set(block.keys()) == {"BTC/USDT", "ETH/USDT"}


def test_split_blocks_multi_lockstep_timestamps_across_symbols():
    multi = _make_multi_df(800, ["BTC/USDT", "ETH/USDT"])
    blocks = _split_blocks_multi(multi, n_blocks=4, symbols=["BTC/USDT", "ETH/USDT"])
    for i, block in enumerate(blocks):
        btc_idx = block["BTC/USDT"].index
        eth_idx = block["ETH/USDT"].index
        assert btc_idx.equals(eth_idx), (
            f"block {i}: BTC and ETH timestamps differ"
        )


def test_split_blocks_multi_block_size_consistency():
    """Per-symbol blocks have identical lengths (intersection-based split)."""
    multi = _make_multi_df(800, ["BTC/USDT", "ETH/USDT", "BNB/USDT"])
    blocks = _split_blocks_multi(
        multi, n_blocks=4, symbols=["BTC/USDT", "ETH/USDT", "BNB/USDT"]
    )
    for block in blocks:
        sizes = {sym: len(df) for sym, df in block.items()}
        assert len(set(sizes.values())) == 1, (
            f"per-symbol block sizes differ: {sizes}"
        )


# ── Sharpe-from-returns ──────────────────────────────────────────────────────

def test_sharpe_from_returns_matches_engine_formula_for_constant_zero_returns():
    """For an exactly-zero return series, std is exactly 0 (no
    floating-point fuzz), so vol=0 → Sharpe defined as 0.0.

    Note: a non-zero constant series like [0.001]*100 would NOT pass
    this test — std evaluates to a tiny non-zero number under float
    arithmetic, and the Sharpe formula divides a non-zero ann_return
    by that tiny vol, producing a huge value.  This mirrors the
    engine's own behaviour (`engine._compute_metrics` uses the same
    `if vol > 0` branch with no epsilon), so we keep the formula
    untouched and document the float-precision corner explicitly.
    """
    sharpe = _sharpe_from_returns(
        np.array([0.0] * 100, dtype=float), candle_duration_h=1.0
    )
    assert sharpe == 0.0


def test_sharpe_from_returns_matches_engine_formula_on_synthetic():
    """Compare against the engine's own formula on a non-constant series."""
    rng = np.random.default_rng(7)
    rets = rng.normal(0.001, 0.005, 252)
    candle_h = 24.0  # daily

    n = rets.size
    years = (n * candle_h) / (365.25 * 24)
    total = float(np.prod(1.0 + rets) - 1.0)
    ann_pct = ((1.0 + total) ** (1.0 / years) - 1.0) * 100.0
    cpy = (365.25 * 24) / candle_h
    vol_pct = float(rets.std()) * math.sqrt(cpy) * 100.0
    expected = ann_pct / vol_pct if vol_pct > 0 else 0.0

    actual = _sharpe_from_returns(rets, candle_duration_h=candle_h)
    assert actual == pytest.approx(expected, rel=1e-9, abs=1e-9)


def test_sharpe_from_returns_returns_zero_when_std_is_zero():
    sharpe = _sharpe_from_returns(
        np.array([0.0] * 50, dtype=float), candle_duration_h=1.0
    )
    assert sharpe == 0.0


def test_sharpe_from_returns_returns_zero_on_empty_input():
    sharpe = _sharpe_from_returns(np.array([], dtype=float), candle_duration_h=1.0)
    assert sharpe == 0.0


def test_sharpe_from_returns_returns_zero_on_catastrophic_loss():
    """A series whose product crosses below -100 % return is undefined
    for Sharpe (negative-base power).  The helper returns 0.0."""
    sharpe = _sharpe_from_returns(
        np.array([-1.01], dtype=float), candle_duration_h=1.0
    )
    assert sharpe == 0.0


# ── _infer_candle_hours ──────────────────────────────────────────────────────

def test_infer_candle_hours_1h_index():
    df = _make_ohlcv(100, freq="1h")
    assert _infer_candle_hours(df) == pytest.approx(1.0)


def test_infer_candle_hours_4h_index():
    df = _make_ohlcv(100, freq="4h")
    assert _infer_candle_hours(df) == pytest.approx(4.0)


def test_infer_candle_hours_1d_index():
    df = _make_ohlcv(100, freq="1D")
    assert _infer_candle_hours(df) == pytest.approx(24.0)


def test_infer_candle_hours_floors_at_one_minute():
    """Sub-minute deltas are floored to 1 minute (1/60 h)."""
    idx = pd.date_range(start="2024-01-01", periods=10, freq="1s", tz="UTC")
    df = pd.DataFrame({"close": np.arange(10, dtype=float)}, index=idx)
    assert _infer_candle_hours(df) == pytest.approx(1 / 60)


def test_infer_candle_hours_short_index_returns_one():
    df = _make_ohlcv(1, freq="1h")
    assert _infer_candle_hours(df) == 1.0


# ── Purge / embargo ──────────────────────────────────────────────────────────

def test_apply_purge_zeros_first_n_returns():
    arr = np.array([0.1, 0.2, 0.3, 0.4, 0.5], dtype=float)
    out = _apply_purge_embargo(arr, purge=2, embargo=0)
    assert out[0] == 0.0 and out[1] == 0.0
    assert out[2] == 0.3 and out[3] == 0.4 and out[4] == 0.5


def test_apply_embargo_zeros_last_n_returns():
    arr = np.array([0.1, 0.2, 0.3, 0.4, 0.5], dtype=float)
    out = _apply_purge_embargo(arr, purge=0, embargo=2)
    assert out[0] == 0.1 and out[1] == 0.2 and out[2] == 0.3
    assert out[3] == 0.0 and out[4] == 0.0


def test_apply_purge_and_embargo_together():
    arr = np.array([0.1, 0.2, 0.3, 0.4, 0.5], dtype=float)
    out = _apply_purge_embargo(arr, purge=1, embargo=1)
    assert out[0] == 0.0
    assert out[1] == 0.2 and out[2] == 0.3 and out[3] == 0.4
    assert out[4] == 0.0


def test_apply_purge_embargo_no_op_when_both_zero():
    arr = np.array([0.1, 0.2, 0.3], dtype=float)
    out = _apply_purge_embargo(arr, purge=0, embargo=0)
    np.testing.assert_array_equal(out, arr)


def test_apply_purge_embargo_handles_empty_input():
    out = _apply_purge_embargo(np.array([], dtype=float), purge=2, embargo=2)
    assert out.size == 0


# ── Synthetic strategy used by run_cpcv end-to-end tests ─────────────────────

class _PeriodicStrategy(BaseStrategy):
    """Deterministic synthetic strategy: BUY every N candles, hold for
    `hold_candles`, then SELL.  Used to control trade-count per block.

    The internal counter starts at 0 on construction and is incremented
    once per generate_signal call, so the same recipe — running on a
    block of size B with warm_up W — produces a predictable number of
    trades per block.
    """

    def __init__(
        self,
        symbol: str = "BTC/USDT",
        timeframe: str = "1h",
        trade_period: int = 30,
        hold_candles: int = 5,
    ):
        super().__init__(name="Periodic", symbol=symbol, timeframe=timeframe)
        self.trade_period = trade_period
        self.hold_candles = hold_candles
        self._counter: int = 0
        self._in_position: bool = False
        self._held: int = 0

    def generate_signal(self, df: pd.DataFrame):
        self._counter += 1
        price = float(df["close"].iloc[-1])
        if self._in_position:
            self._held += 1
            if self._held >= self.hold_candles:
                self._in_position = False
                self._held = 0
                return self.sell(price=price, reason="time_exit")
            return self.hold(price=price, reason="holding")
        if self._counter % self.trade_period == 0:
            self._in_position = True
            self._held = 0
            return self.buy(price=price, reason="periodic_entry")
        return self.hold(price=price, reason="waiting")


# ── Fixtures: monkey-patched holdout for run_cpcv end-to-end ─────────────────

@pytest.fixture
def patch_holdout_for_cpcv(monkeypatch):
    """Return a closure that wires up holdout.load_manifest and
    holdout.load_dev to test-supplied data.

    Usage:
        patch_holdout_for_cpcv(manifest, dev_df)
    """
    def _wire(manifest: dict, dev_df: pd.DataFrame):
        monkeypatch.setattr(holdout, "load_manifest", lambda: manifest)
        monkeypatch.setattr(holdout, "load_dev", lambda sid: dev_df)
        try:
            holdout.load_manifest.cache_clear()  # type: ignore[attr-defined]
        except AttributeError:
            pass
    return _wire


# ── _run_engine_per_block sanity ─────────────────────────────────────────────

def test_run_engine_per_block_single_symbol_returns_one_result_per_block():
    df = _make_ohlcv(800)
    blocks = _split_blocks(df, n_blocks=4)
    results = _run_engine_per_block(
        strategy_factory=lambda: _PeriodicStrategy(trade_period=30, hold_candles=5),
        blocks=blocks,
        primary_symbol=None,
        is_multi_symbol=False,
    )
    assert len(results) == 4
    for r in results:
        # Each block should have produced an equity curve for the
        # post-warm-up portion (warm_up=50 → ~150 candles per 200-row block).
        assert len(r.equity_curve) > 0


# ── End-to-end run_cpcv on a synthetic single-symbol dev window ──────────────

def _single_symbol_manifest(dev_df: pd.DataFrame, name: str = "TestStrat") -> dict:
    return {
        name: {
            "symbol": "BTC/USDT",
            "timeframe": "1h",
            "data_start": str(dev_df.index[0]),
            "data_end":   str(dev_df.index[-1]),
            "dev_end":    str(dev_df.index[-1]),
            "holdout_start": str(dev_df.index[-1]),
        }
    }


def test_run_cpcv_single_symbol_end_to_end(patch_holdout_for_cpcv):
    """1000-candle synthetic dev window split into 4 blocks of 250.
    The synthetic strategy fires several trades per block, every
    block clears the 5-trade minimum, and run_cpcv returns a populated
    CPCVResult with one Sharpe per block."""
    dev_df = _make_ohlcv(1000)
    patch_holdout_for_cpcv(_single_symbol_manifest(dev_df), dev_df)

    config = CPCVConfig(
        n_blocks=4, k_held_out=2, purge_periods=0, embargo_periods=0,
    )
    factory = lambda: _PeriodicStrategy(
        symbol="BTC/USDT", trade_period=30, hold_candles=5,
    )

    result = run_cpcv("TestStrat", {"trade_period": 30}, config, factory)

    assert isinstance(result, CPCVResult)
    assert result.n_paths == 4  # = config.n_blocks under block-Sharpe mode
    assert len(result.per_path_sharpes) == 4
    assert len(result.trades_per_path) == 4

    # Distribution shape matches the trials.log schema.
    assert set(result.sharpe_distribution.keys()) == {"mean", "std", "quantiles"}
    assert set(result.sharpe_distribution["quantiles"].keys()) == {
        "p05", "p25", "p50", "p75", "p95"
    }

    # No NaN blocks — synthetic strategy meets the 5-trade minimum
    # in every block.
    assert not any(math.isnan(s) for s in result.per_path_sharpes)

    # The per_block_sharpes alias points at the same data.
    assert result.per_block_sharpes is result.per_path_sharpes

    # Trade counts are positive and match the synthetic strategy's
    # trade rate (counter-based, so the per-block totals are
    # comparable across blocks).
    assert all(t > 0 for t in result.trades_per_path)


def test_run_cpcv_per_block_sharpes_have_nonzero_variance(patch_holdout_for_cpcv):
    """Block-Sharpe variance check.

    This is the test that catches a regression to the chunk-6
    path-CPCV degeneracy: under cached engine + LdP path reassembly,
    every "path" had identical returns and the Sharpe distribution
    collapsed to std=0.  In block-Sharpe mode each block is an
    independent run on different data, so the per-block Sharpes
    should differ.

    Threshold is intentionally loose (any non-zero std) because the
    point is to confirm real cross-block dispersion, not a calibrated
    magnitude.
    """
    dev_df = _make_ohlcv(1000)
    patch_holdout_for_cpcv(_single_symbol_manifest(dev_df), dev_df)

    config = CPCVConfig(
        n_blocks=4, k_held_out=2, purge_periods=0, embargo_periods=0,
    )
    factory = lambda: _PeriodicStrategy(
        symbol="BTC/USDT", trade_period=30, hold_candles=5,
    )

    result = run_cpcv("TestStrat", {}, config, factory)

    valid = np.array(
        [s for s in result.per_path_sharpes if not math.isnan(s)],
        dtype=float,
    )
    assert valid.size >= 2, (
        "Need ≥ 2 valid block Sharpes to measure variance; "
        f"got {valid.size}"
    )
    assert float(valid.std()) > 0.0, (
        f"per-block Sharpe std is 0.0 (sharpes={valid.tolist()}); "
        "block-Sharpe distribution is degenerate — this would replicate "
        "the chunk-6 path-CPCV failure."
    )
    # And the summarised distribution reflects that.
    assert result.sharpe_distribution["std"] > 0.0


def test_run_cpcv_purge_embargo_changes_results(patch_holdout_for_cpcv):
    """Setting non-zero purge / embargo zeros real returns at block
    boundaries, which must shift the per-block Sharpes.  If the
    Sharpes were unchanged, the purge / embargo trim would be a no-op
    in practice — a silent correctness regression."""
    dev_df = _make_ohlcv(1000)
    patch_holdout_for_cpcv(_single_symbol_manifest(dev_df), dev_df)

    factory = lambda: _PeriodicStrategy(
        symbol="BTC/USDT", trade_period=30, hold_candles=5,
    )

    cfg_no_trim = CPCVConfig(
        n_blocks=4, k_held_out=2, purge_periods=0, embargo_periods=0,
    )
    cfg_trim = CPCVConfig(
        n_blocks=4, k_held_out=2, purge_periods=20, embargo_periods=20,
    )

    r_no = run_cpcv("TestStrat", {}, cfg_no_trim, factory)
    r_yes = run_cpcv("TestStrat", {}, cfg_trim, factory)

    # At least one block's Sharpe must differ between the two runs.
    differs = False
    for a, b in zip(r_no.per_path_sharpes, r_yes.per_path_sharpes):
        if math.isnan(a) and math.isnan(b):
            continue
        if math.isnan(a) or math.isnan(b):
            differs = True
            break
        if a != b:
            differs = True
            break
    assert differs, (
        f"purge/embargo did not change any block Sharpe: "
        f"no_trim={r_no.per_path_sharpes} trim={r_yes.per_path_sharpes}. "
        "purge/embargo trim is silently a no-op."
    )


def test_run_cpcv_raises_when_blocks_too_small(patch_holdout_for_cpcv):
    # 200 candles / 4 blocks = 50 per block — below the 60 minimum.
    dev_df = _make_ohlcv(200)
    patch_holdout_for_cpcv(_single_symbol_manifest(dev_df, "TinyStrat"), dev_df)
    config = CPCVConfig(n_blocks=4, k_held_out=2)
    factory = lambda: _PeriodicStrategy(symbol="BTC/USDT")

    with pytest.raises(CPCVError, match="block too small"):
        run_cpcv("TinyStrat", {}, config, factory)


def test_run_cpcv_raises_when_more_than_half_blocks_have_low_trades(
    patch_holdout_for_cpcv,
):
    """A strategy that never trades produces 0 trades per block, every
    block's NaN-trigger fires, and the > 50 % NaN guard raises."""
    dev_df = _make_ohlcv(1000)
    patch_holdout_for_cpcv(_single_symbol_manifest(dev_df, "InertStrat"), dev_df)

    config = CPCVConfig(n_blocks=4, k_held_out=2)
    # trade_period = 100_000 → never fires within any block.
    factory = lambda: _PeriodicStrategy(
        symbol="BTC/USDT", trade_period=100_000, hold_candles=5,
    )

    with pytest.raises(CPCVError, match="more than 50%"):
        run_cpcv("InertStrat", {}, config, factory)


def test_run_cpcv_rejects_unknown_strategy(patch_holdout_for_cpcv):
    dev_df = _make_ohlcv(1000)
    patch_holdout_for_cpcv({}, dev_df)
    config = CPCVConfig(n_blocks=4, k_held_out=2)
    factory = lambda: _PeriodicStrategy(symbol="BTC/USDT")

    with pytest.raises(holdout.StrategyNotInManifest):
        run_cpcv("Ghost", {}, config, factory)


# ── per_block_returns invariants ─────────────────────────────────────────────

def test_per_block_returns_length_invariant(patch_holdout_for_cpcv):
    """`len(per_block_returns) == n_blocks` after any successful run.
    Consumed by `backtest.dsr.dsr_from_cpcv_result`."""
    dev_df = _make_ohlcv(1000)
    patch_holdout_for_cpcv(_single_symbol_manifest(dev_df), dev_df)

    config = CPCVConfig(n_blocks=4, k_held_out=2)
    factory = lambda: _PeriodicStrategy(
        symbol="BTC/USDT", trade_period=30, hold_candles=5,
    )
    result = run_cpcv("TestStrat", {}, config, factory)

    assert len(result.per_block_returns) == config.n_blocks
    # All entries are np.ndarrays (never None, never missing).
    for arr in result.per_block_returns:
        assert isinstance(arr, np.ndarray)


def test_per_block_returns_empty_for_nan_blocks(patch_holdout_for_cpcv):
    """Blocks below `_MIN_TRADES_PER_BLOCK` produce both a NaN Sharpe
    AND an empty (`size == 0`) returns array.  This is the contract
    DSR relies on to skip insufficient blocks via
    `arr.size > 0` filtering."""
    dev_df = _make_ohlcv(1000)
    patch_holdout_for_cpcv(_single_symbol_manifest(dev_df), dev_df)

    config = CPCVConfig(n_blocks=4, k_held_out=2)

    # Factory that yields an "active" strategy for blocks {0, 1, 2}
    # and an "inactive" strategy for block 3.  run_cpcv calls the
    # factory once at the top of the function for primary-symbol
    # detection (throwaway), then once per block.  The block_idx
    # mapping below accounts for that throwaway.  Three valid blocks
    # + one NaN block = 25 % NaN, safely below the > 50 % guard.
    inactive_blocks = {3}
    n_calls = [0]

    def mixed_factory():
        call_idx = n_calls[0]
        n_calls[0] += 1
        # First call is run_cpcv's throwaway primary-symbol probe;
        # block calls start at call_idx == 1.
        block_idx = call_idx - 1
        if block_idx in inactive_blocks:
            # trade_period larger than per-block candle count so the
            # strategy never fires a BUY → 0 trades.
            return _PeriodicStrategy(
                symbol="BTC/USDT", trade_period=100_000, hold_candles=5,
            )
        return _PeriodicStrategy(
            symbol="BTC/USDT", trade_period=30, hold_candles=5,
        )

    result = run_cpcv("TestStrat", {}, config, mixed_factory)

    nan_indices = [
        i for i, s in enumerate(result.per_path_sharpes) if math.isnan(s)
    ]
    valid_indices = [
        i for i, s in enumerate(result.per_path_sharpes) if not math.isnan(s)
    ]
    assert nan_indices == [3], (
        f"expected only block 3 to be NaN; got nan_indices={nan_indices}"
    )
    assert sorted(valid_indices) == [0, 1, 2]

    for i in nan_indices:
        assert result.per_block_returns[i].size == 0, (
            f"block {i} has NaN Sharpe but per_block_returns is "
            f"non-empty (size={result.per_block_returns[i].size})"
        )
    for i in valid_indices:
        assert result.per_block_returns[i].size > 0, (
            f"block {i} has valid Sharpe but per_block_returns is empty"
        )


def test_per_block_returns_matches_pct_change_shape(patch_holdout_for_cpcv):
    """For valid blocks (purge=embargo=0), the returns array length
    equals the engine's equity_curve length minus 1 — the bar lost to
    `pct_change().dropna()`."""
    from backtest.cpcv import _run_engine_per_block, _split_blocks

    dev_df = _make_ohlcv(1000)
    patch_holdout_for_cpcv(_single_symbol_manifest(dev_df), dev_df)

    config = CPCVConfig(
        n_blocks=4, k_held_out=2, purge_periods=0, embargo_periods=0,
    )
    factory = lambda: _PeriodicStrategy(
        symbol="BTC/USDT", trade_period=30, hold_candles=5,
    )
    result = run_cpcv("TestStrat", {}, config, factory)

    # Reconstruct the engine results to compare equity-curve lengths.
    blocks = _split_blocks(dev_df, n_blocks=config.n_blocks)
    engine_results = _run_engine_per_block(
        strategy_factory=factory,
        blocks=blocks,
        primary_symbol=None,
        is_multi_symbol=False,
    )

    for i, (engine_r, block_arr) in enumerate(
        zip(engine_results, result.per_block_returns)
    ):
        # Only check valid (non-empty) blocks.
        if block_arr.size == 0:
            continue
        assert block_arr.size == len(engine_r.equity_curve) - 1, (
            f"block {i}: per_block_returns size {block_arr.size} != "
            f"equity_curve len - 1 ({len(engine_r.equity_curve) - 1})"
        )
