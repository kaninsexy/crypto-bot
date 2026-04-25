"""
backtest/tests/test_cpcv.py — Tests for the CPCV skeleton.

Covers only the implemented surface area: CPCVConfig.validate, the
summarize() helper, and the NotImplementedError contract on run_cpcv.
The iterative path-construction implementation lands in Phase 3b
chunk 6 — its tests will land alongside.
"""

import numpy as np
import pytest

from backtest.cpcv import CPCVConfig, run_cpcv, summarize


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


# ── run_cpcv NotImplementedError contract ────────────────────────────────────

def test_run_cpcv_raises_not_implemented_with_helpful_message():
    config = CPCVConfig()
    with pytest.raises(NotImplementedError) as excinfo:
        run_cpcv("VWAP", {"lookback": 20}, config)
    msg = str(excinfo.value)
    assert "Phase 3b chunk 6" in msg
    assert "validation_framework.md" in msg
