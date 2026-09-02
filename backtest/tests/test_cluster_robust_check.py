"""Tests for the dependence correction (`scripts/cluster_robust_check.py`).

The load-bearing test is `test_detects_known_clustering`: a synthetic sample
with a KNOWN shared shock per cluster, where the naive t is large and the
clustered t must collapse. A correction that cannot show inflation on data
built to contain it would certify the very defect it claims to measure
(`.claude/rules/vertical_slice_loops.md`, the discriminating-check rule).
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pytest

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

from scripts.cluster_robust_check import (  # noqa: E402
    cluster_robust_mean,
    merge_dates_into_episodes,
    newey_west_mean,
)


# ── the discriminating pair ──────────────────────────────────────────────────

def test_detects_known_clustering():
    """Built to contain clustering: the check MUST show it."""
    rng = np.random.default_rng(0)
    G, per = 20, 30
    shock = rng.normal(0, 0.10, G)          # one shared shock per cluster
    vals, grp = [], []
    for i in range(G):
        vals.extend(shock[i] + rng.normal(0.02, 0.01, per))
        grp.extend([i] * per)

    r = cluster_robust_mean(vals, grp)
    assert r.n == G * per and r.n_groups == G and r.dof == G - 1
    assert r.se_robust > r.se_ordinary, "clustering must WIDEN the interval"
    assert r.design_effect > 3.0, (
        f"a shared per-cluster shock this large must inflate the naive t by "
        f"far more than {r.design_effect:.2f}x"
    )


def test_independent_data_is_barely_affected():
    """The other half of the discrimination: with genuinely independent data
    the correction must NOT manufacture an inflation factor, or it would
    'find' clustering everywhere and mean nothing."""
    rng = np.random.default_rng(1)
    vals = rng.normal(0.02, 0.05, 600)
    grp = np.arange(600) // 1            # every observation its own cluster
    r = cluster_robust_mean(vals, grp)
    assert r.design_effect == pytest.approx(1.0, abs=0.05)


def test_singleton_clusters_reduce_to_the_ordinary_se():
    rng = np.random.default_rng(2)
    vals = rng.normal(0, 1, 200)
    r = cluster_robust_mean(vals, np.arange(200))
    # CR1 with G=N differs from the ordinary SE only by the G/(G-1) factor.
    assert r.se_robust == pytest.approx(r.se_ordinary, rel=0.02)


def test_one_giant_cluster_destroys_all_significance():
    """Every observation in one cluster = one effective observation."""
    vals = np.full(500, 0.05) + np.random.default_rng(3).normal(0, 0.001, 500)
    r = cluster_robust_mean(vals, np.zeros(500, dtype=int) + np.arange(500) // 250)
    assert r.n_groups == 2
    assert abs(r.t_robust) < abs(r.t_ordinary)


# ── Newey-West ───────────────────────────────────────────────────────────────

def test_newey_west_lag_zero_matches_the_ordinary_se():
    rng = np.random.default_rng(4)
    x = rng.normal(0.01, 0.03, 500)
    r = newey_west_mean(x, lag=0)
    # lag 0 is the plain variance, up to the ddof=1 vs ddof=0 difference.
    assert r.se_robust == pytest.approx(r.se_ordinary, rel=0.01)


def test_newey_west_widens_on_a_persistent_series():
    """AR(1) with rho=0.6: HAC must widen the interval."""
    rng = np.random.default_rng(5)
    n, rho = 1200, 0.6
    e = rng.normal(0, 1, n)
    x = np.zeros(n)
    for i in range(1, n):
        x[i] = rho * x[i - 1] + e[i]
    x = x * 0.01 + 0.002
    r = newey_west_mean(x, lag=10)
    assert r.se_robust > r.se_ordinary
    assert r.design_effect > 1.4


def test_newey_west_on_white_noise_is_close_to_ordinary():
    rng = np.random.default_rng(6)
    x = rng.normal(0.001, 0.02, 2000)
    r = newey_west_mean(x, lag=10)
    assert r.design_effect == pytest.approx(1.0, abs=0.25)


def test_newey_west_rejects_a_negative_lag():
    with pytest.raises(ValueError):
        newey_west_mean([1.0, 2.0, 3.0], lag=-1)


# ── episode merging ──────────────────────────────────────────────────────────

def test_merge_dates_into_episodes_groups_a_long_weekend():
    dates = ["2022-05-09", "2022-05-10", "2022-05-12",   # one episode
             "2022-06-18", "2022-06-19",                 # another
             "2022-11-09"]                               # a third
    ids = merge_dates_into_episodes(dates, window_days=5)
    assert len(set(ids)) == 3
    assert ids[0] == ids[1] == ids[2]
    assert ids[3] == ids[4] and ids[3] != ids[0]
    assert ids[5] not in (ids[0], ids[3])


def test_merge_is_order_independent():
    a = ["2022-05-09", "2022-05-10", "2022-11-09"]
    b = ["2022-11-09", "2022-05-10", "2022-05-09"]
    assert len(set(merge_dates_into_episodes(a))) == len(set(merge_dates_into_episodes(b)))


# ── reporting contract ───────────────────────────────────────────────────────

def test_small_g_caveat_is_surfaced_not_suppressed():
    rng = np.random.default_rng(7)
    vals, grp = [], []
    for i in range(6):
        vals.extend(rng.normal(0.02, 0.01, 40))
        grp.extend([i] * 40)
    r = cluster_robust_mean(vals, grp)
    assert r.small_g
    assert "CAVEAT" in r.render() and "ANTI-conservative" in r.render()


def test_mde_robust_uses_the_project_bar():
    rng = np.random.default_rng(8)
    r = cluster_robust_mean(rng.normal(0.02, 0.05, 300), np.arange(300) // 10)
    assert r.mde_robust == pytest.approx(3.0 * r.se_robust)


def test_too_few_clusters_raises():
    with pytest.raises(ValueError):
        cluster_robust_mean([1.0, 2.0, 3.0], [0, 0, 0])
