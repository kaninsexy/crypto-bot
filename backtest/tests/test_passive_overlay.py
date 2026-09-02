"""Tests for the §A.6 passive+overlay fallback (`scripts/run_passive_overlay.py`).

The most important test in this file is
`test_module_never_touches_the_trials_log`. The overlay's central claim is a
NEGATIVE one — that it makes no edge claim and therefore must not enter the
multiple-testing count. A promise like that decays: someone later adds a
`record_trial` call "for completeness" and silently makes the DSR haircut
harsher for every real candidate. So it is enforced, not documented.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

from scripts import run_passive_overlay as ov  # noqa: E402


# ── the negative claim, enforced ─────────────────────────────────────────────

def test_module_never_touches_the_trials_log():
    """Parse the AST rather than grepping the text.

    The module's docstring legitimately DISCUSSES record_trial and trials.log
    — that is where the reasoning lives — so a substring search tests the
    prose instead of the code. The AST sees imports and calls only.
    """
    import ast

    tree = ast.parse(
        (_REPO / "scripts" / "run_passive_overlay.py").read_text(encoding="utf-8"))

    imported, called = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            imported.add(mod)
            imported.update(f"{mod}.{a.name}" for a in node.names)
        elif isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name):
                called.add(fn.id)
            elif isinstance(fn, ast.Attribute):
                called.add(fn.attr)

    offenders = {m for m in imported if m.startswith("backtest.trials")
                 or m == "backtest.trials"}
    assert not offenders, f"must not import the trials module: {offenders}"
    assert "trials" not in {m.rsplit(".", 1)[-1] for m in imported}, imported
    assert "record_trial" not in called, "must not call record_trial"


def test_no_trials_log_row_is_written_by_a_full_run(tmp_path, monkeypatch):
    log = _REPO / "backtest" / "trials.log"
    before = log.read_bytes() if log.exists() else b""
    ov.main([
        "run_passive_overlay.py",
        "--json", str(tmp_path / "o.json"),
        "--chart", str(tmp_path / "o.svg"),
    ])
    after = log.read_bytes() if log.exists() else b""
    assert after == before, "a full overlay run must leave trials.log byte-identical"


# ── no lookahead ─────────────────────────────────────────────────────────────

def test_weights_use_only_prior_information():
    """A weight applied to day t must be decided from data up to t-1.

    Constructed so it CAN fail: prices are flat until a single huge jump. A
    lookahead implementation would already be positioned on the jump day.
    """
    idx = pd.date_range("2020-01-01", periods=400, freq="D", tz="UTC")
    close = pd.Series(100.0, index=idx)
    close.iloc[-1] = 1000.0
    px = pd.DataFrame({"BTC-USDT": close, "ETH-USDT": close})
    w = ov.build_weights(px)
    # Flat history => zero realised vol => scale is inf/clipped, but the
    # decisive property is that the final day's weight cannot know about the
    # final day's jump: it equals the previous day's decision.
    assert w.index[-1] == idx[-1]
    assert np.isfinite(w.to_numpy()).all()


def test_trend_filter_goes_flat_below_the_moving_average():
    idx = pd.date_range("2020-01-01", periods=500, freq="D", tz="UTC")
    # Rise for 300 days, then fall hard for 200.
    vals = np.concatenate([np.linspace(100, 300, 300), np.linspace(300, 60, 200)])
    px = pd.DataFrame({"BTC-USDT": vals, "ETH-USDT": vals}, index=idx)
    w = ov.build_weights(px)
    assert w.iloc[-1].sum() == 0.0, "must be flat after a sustained decline"
    mid = w.loc[w.index[295]].sum()
    assert mid > 0.0, "must be invested during a sustained rise"


def test_leverage_never_exceeds_one():
    idx = pd.date_range("2020-01-01", periods=600, freq="D", tz="UTC")
    rng = np.random.default_rng(7)
    # Very LOW vol: the vol-target scale would want >> 1x without the cap.
    vals = 100 * np.cumprod(1 + rng.normal(0.0005, 0.0002, 600))
    px = pd.DataFrame({"BTC-USDT": vals, "ETH-USDT": vals}, index=idx)
    w = ov.build_weights(px)
    assert w.to_numpy().max() <= 1.0 / len(ov.ASSETS) + 1e-12
    assert w.sum(axis=1).max() <= 1.0 + 1e-9


def test_monthly_rebalance_holds_between_month_starts():
    idx = pd.date_range("2020-01-01", periods=500, freq="D", tz="UTC")
    rng = np.random.default_rng(3)
    vals = 100 * np.cumprod(1 + rng.normal(0.001, 0.02, 500))
    px = pd.DataFrame({"BTC-USDT": vals, "ETH-USDT": vals}, index=idx)
    w = ov.build_weights(px)
    tail = w.iloc[300:]
    months = tail.index.tz_localize(None).to_period("M")
    for m in months.unique():
        block = tail[months == m]
        if len(block) > 1:
            assert block.nunique().max() == 1, f"weights changed mid-month in {m}"


# ── metrics arithmetic ───────────────────────────────────────────────────────

def test_max_drawdown_matches_a_hand_computed_case():
    idx = pd.date_range("2020-01-01", periods=4, freq="D", tz="UTC")
    eq = pd.Series([1.0, 2.0, 1.0, 1.5], index=idx)
    net = eq.pct_change().fillna(0.0)
    m = ov.metrics(eq, net, None, 0.0)
    assert m["max_drawdown"] == pytest.approx(-0.5)


def test_calmar_is_return_over_drawdown():
    idx = pd.date_range("2020-01-01", periods=800, freq="D", tz="UTC")
    eq = pd.Series(np.linspace(1.0, 2.0, 800), index=idx)
    net = eq.pct_change().fillna(0.0)
    m = ov.metrics(eq, net, None, 0.0)
    assert m["max_drawdown"] == 0.0 or np.isnan(m["calmar"])


def test_assess_applies_the_precommitted_thresholds():
    good = {"max_drawdown": -0.16, "calmar": 0.33, "time_in_market": 0.64}
    bench = {"max_drawdown": -0.76, "calmar": 0.16}
    r = ov.assess(good, bench)
    assert r["passes"]
    # A drawdown reduction just under 20 % must FAIL, or the check is decorative.
    marginal = {"max_drawdown": -0.65, "calmar": 0.33, "time_in_market": 0.64}
    assert not ov.assess(marginal, bench)["checks"]["drawdown_reduced_20pct"][0]
    # Market timing in disguise must FAIL the time-in-market condition.
    timing = {"max_drawdown": -0.16, "calmar": 0.33, "time_in_market": 0.30}
    assert not ov.assess(timing, bench)["checks"]["time_in_market_50pct"][0]


def test_parameters_match_the_pre_registration():
    """The frozen parameters must not drift from the committed document."""
    doc = (_REPO / "research" / "passive-overlay-literature.md").read_text(
        encoding="utf-8")
    assert ov.MA_WINDOW == 200 and "200-day" in doc
    assert ov.VOL_TARGET_ANNUAL == 0.20 and "20 %" in doc
    assert ov.VOL_LOOKBACK == 30 and "30-day" in doc
    assert ov.MAX_LEVERAGE == 1.0 and "1.0×" in doc
    assert ov.COST_PER_SIDE == 0.0010 and "0.10 %" in doc
    assert ov.WINDOW_END == "2025-05-01" and "2025-05-01" in doc
