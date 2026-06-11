"""
backtest/tests/conftest.py — suite-wide fixtures.
"""

import pytest

import backtest.per_bar_store as _per_bar_store


@pytest.fixture(autouse=True)
def _redirect_per_bar_store(tmp_path, monkeypatch):
    """Keep gate-spec-v2 per-bar persistence out of the real
    backtest/reports/per_bar_returns/ directory during tests.  Any
    test that exercises record_trial / runner paths would otherwise
    write parquet artifacts into the repo (observed 2026-06-11).
    Tests that need their own destination simply monkeypatch over
    this redirect."""
    monkeypatch.setattr(
        _per_bar_store, "_PER_BAR_DIR", tmp_path / "_per_bar_returns",
    )
