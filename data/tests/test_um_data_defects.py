"""Tests for the Binance UM data-defects registry (I5, 2026-09-02).

Registry: `docs/data_defects_binance_um.md`.

The load-bearing test is `test_zero_oi_frame_is_dangerous_without_clean`: it
constructs the exact failure that cost run 2 -- a zero open-interest reading at
day-end -- and asserts it produces a spurious -100 % 24h change when
`clean=False`, and does not when `clean=True`. A guard whose absence is
harmless is not a guard.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from data.binance_vision_um import (  # noqa: E402
    DEFECT_KINDS,
    clean_metrics,
    defect_report,
    resample_metrics,
)


def _metrics_frame_with_zero_at_day_end() -> pd.DataFrame:
    """Two days of 5-minute OI. The LAST reading of day 1 is a feed zero."""
    idx = pd.date_range("2022-05-09", periods=576, freq="5min", tz="UTC", name="ts")
    oi = pd.Series(1_000_000.0, index=idx)
    oi.iloc[287] = 0.0                      # last 5-min bar of day 1
    return pd.DataFrame({"sum_open_interest": oi,
                         "sum_open_interest_value": oi * 30_000.0}, index=idx)


# ── the discriminating pair ──────────────────────────────────────────────────

def test_zero_oi_frame_is_dangerous_without_clean():
    """Reproduces the run-2 failure exactly: a day-end zero becomes a
    fabricated -100 % 24h OI change, five times past a -20 % event bar."""
    df = _metrics_frame_with_zero_at_day_end()
    daily = resample_metrics(df, "1D")["sum_open_interest"]
    chg = daily.pct_change()
    assert daily.iloc[0] == 0.0, "day 1 resamples to the feed zero"
    assert np.isinf(chg.iloc[1]) or chg.iloc[1] > 10, (
        "an uncleaned zero must produce an absurd next-day change -- if this "
        "assertion stops holding, the guard below is no longer testing anything"
    )


def test_clean_removes_the_fabricated_event():
    df = clean_metrics(_metrics_frame_with_zero_at_day_end())
    daily = resample_metrics(df, "1D")["sum_open_interest"]
    chg = daily.pct_change()
    assert daily.iloc[0] == pytest.approx(1_000_000.0), (
        "the daily value must fall back to the last VALID reading")
    assert abs(chg.iloc[1]) < 1e-9, "no spurious event survives the guard"


# ── clean_metrics behaviour ──────────────────────────────────────────────────

def test_clean_metrics_does_not_mutate_its_input():
    df = _metrics_frame_with_zero_at_day_end()
    before = df["sum_open_interest"].copy()
    clean_metrics(df)
    pd.testing.assert_series_equal(df["sum_open_interest"], before)


def test_clean_metrics_masks_only_nonpositive_values():
    df = _metrics_frame_with_zero_at_day_end()
    out = clean_metrics(df)
    assert out["sum_open_interest"].isna().sum() == 1
    assert (out["sum_open_interest"].dropna() > 0).all()


def test_clean_metrics_excludes_rather_than_interpolates():
    """NaN, not a filled value: repairing would invent open interest that was
    never observed."""
    out = clean_metrics(_metrics_frame_with_zero_at_day_end())
    assert np.isnan(out["sum_open_interest"].iloc[287])


def test_clean_metrics_is_a_noop_on_a_clean_frame():
    idx = pd.date_range("2022-05-09", periods=10, freq="5min", tz="UTC")
    df = pd.DataFrame({"sum_open_interest": np.arange(1.0, 11.0)}, index=idx)
    pd.testing.assert_frame_equal(clean_metrics(df), df)


def test_clean_metrics_tolerates_a_missing_column():
    idx = pd.date_range("2022-05-09", periods=5, freq="5min", tz="UTC")
    df = pd.DataFrame({"count_long_short_ratio": np.ones(5)}, index=idx)
    pd.testing.assert_frame_equal(clean_metrics(df), df)


# ── defect_report ────────────────────────────────────────────────────────────

def test_defect_report_counts_zero_open_interest():
    r = defect_report(_metrics_frame_with_zero_at_day_end(), "metrics")
    assert r["rows"] == 576 and r["zero_open_interest"] == 1


def test_defect_report_counts_zero_volume_klines():
    idx = pd.date_range("2022-01-01", periods=5, freq="D", tz="UTC")
    df = pd.DataFrame({"volume": [1.0, 0.0, 2.0, 0.0, 3.0],
                       "close": [1.0, 1.0, 1.0, 1.0, 1.0]}, index=idx)
    r = defect_report(df, "klines")
    assert r["zero_volume_bars"] == 2 and r["nonpositive_close"] == 0


def test_defect_report_surfaces_non_8h_funding_intervals():
    idx = pd.date_range("2022-11-01", periods=6, freq="4h", tz="UTC")
    df = pd.DataFrame({"funding_interval_hours": [8, 8, 4, 4, 2, 8],
                       "last_funding_rate": np.zeros(6)}, index=idx)
    r = defect_report(df, "funding")
    assert r["distinct_funding_intervals"] == [2.0, 4.0, 8.0]
    assert r["non_8h_settlements"] == 3


def test_defect_report_on_an_empty_frame_is_just_the_row_count():
    assert defect_report(pd.DataFrame(), "metrics") == {"rows": 0}


def test_defect_report_rejects_an_unknown_kind():
    with pytest.raises(ValueError, match="kind must be one of"):
        defect_report(pd.DataFrame(), "orderbook")


def test_defect_kinds_are_the_documented_ones():
    assert set(DEFECT_KINDS) == {"metrics", "klines", "funding"}


def test_registry_document_exists_and_lists_every_kind():
    """The code and the registry must not drift apart -- that is the whole
    failure this investigation exists to prevent."""
    doc = (Path(__file__).resolve().parents[2]
           / "docs" / "data_defects_binance_um.md").read_text(encoding="utf-8")
    for kind in DEFECT_KINDS:
        assert kind in doc, f"{kind} is in DEFECT_KINDS but not in the registry"
    assert "clean_metrics" in doc and "defect_report" in doc
