"""Tests for the pre-flight power gate (.claude/rules/backtest.md, split item 5).

The load-bearing test here is `test_gate_refuses_the_real_2026_09_02_case`:
a check that cannot FAIL certifies the gap it was meant to catch
(`.claude/rules/vertical_slice_loops.md`, the discriminating-check rule). So
the suite pins both directions against the actual numbers that motivated the
gate, not against invented ones.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.screen_power_check import (  # noqa: E402
    UnderpoweredScreen,
    compute_power,
    require_power,
    unconditional_sigma,
)

# The 2026-09-02 deleveraging-reversal figures, measured from the cached
# Binance UM daily klines over 2021-12..2022-12.
SIGMA_3D = 0.0969          # unconditional 3-day return sd, 212 symbols
T_BAR = 3.0                # Harvey-Liu
EFFECT = 0.015             # pre-registered 1.5 % mean 3-day reversal


# ── the discriminating pair ──────────────────────────────────────────────────

def test_gate_refuses_the_real_2026_09_02_case():
    """30 symbols (~189 events) MUST be refused. This is the case the gate
    exists for: it was the plan of record until the power calc was run."""
    with pytest.raises(UnderpoweredScreen) as exc:
        require_power(sigma=SIGMA_3D, n_expected=189, t_bar=T_BAR,
                      effect_threshold=EFFECT, label="deleveraging_reversal",
                      verbose=False)
    msg = str(exc.value)
    assert "POWER GATE REFUSED" in msg
    assert "does NOT increment N_disc" in msg, (
        "the refusal must tell the reader that widening is completing the "
        "test, or they will log a second N_disc row for the same screen"
    )


def test_gate_passes_the_widened_case():
    """100 symbols (~630 events) MUST pass — otherwise the gate is not a
    gate, it is a wall, and it would block every screen."""
    res = require_power(sigma=SIGMA_3D, n_expected=630, t_bar=T_BAR,
                        effect_threshold=EFFECT, verbose=False)
    assert res.passes
    assert res.mde == pytest.approx(0.01158, abs=1e-4)


# ── the arithmetic ───────────────────────────────────────────────────────────

def test_mde_matches_the_closed_form():
    res = compute_power(sigma=SIGMA_3D, n_expected=189, t_bar=T_BAR,
                        effect_threshold=EFFECT)
    assert res.mde == pytest.approx(T_BAR * SIGMA_3D / math.sqrt(189))
    assert res.mde == pytest.approx(0.02115, abs=1e-4)
    assert not res.passes


def test_n_required_is_the_smallest_sufficient_n():
    res = compute_power(sigma=SIGMA_3D, n_expected=189, t_bar=T_BAR,
                        effect_threshold=EFFECT)
    n = res.n_required
    assert T_BAR * SIGMA_3D / math.sqrt(n) <= EFFECT
    assert T_BAR * SIGMA_3D / math.sqrt(n - 1) > EFFECT


def test_boundary_mde_exactly_equal_to_threshold_passes():
    """MDE == threshold is a pass: the rule says 'MDE <= threshold'."""
    n = int(math.ceil((T_BAR * SIGMA_3D / EFFECT) ** 2))
    res = compute_power(sigma=SIGMA_3D, n_expected=n, t_bar=T_BAR,
                        effect_threshold=EFFECT)
    assert res.mde <= EFFECT and res.passes


def test_the_refusal_message_states_the_t_a_true_effect_would_return():
    res = compute_power(sigma=SIGMA_3D, n_expected=189, t_bar=T_BAR,
                        effect_threshold=EFFECT)
    text = res.render("deleveraging_reversal")
    assert "2.13" in text, text  # the number that makes the point concrete


# ── sigma estimation ─────────────────────────────────────────────────────────

def test_unconditional_sigma_matches_numpy_and_drops_nans():
    rng = np.random.default_rng(0)
    x = rng.normal(0, 0.05, 500)
    with_nans = np.concatenate([x, [np.nan, np.inf, -np.inf]])
    assert unconditional_sigma(with_nans) == pytest.approx(float(x.std(ddof=1)))


def test_outcome_series_and_explicit_sigma_agree():
    rng = np.random.default_rng(1)
    x = rng.normal(0, 0.09, 4000)
    a = require_power(outcome=x, n_expected=630, t_bar=T_BAR,
                      effect_threshold=EFFECT, verbose=False)
    b = require_power(sigma=float(np.std(x, ddof=1)), n_expected=630,
                      t_bar=T_BAR, effect_threshold=EFFECT, verbose=False)
    assert a.mde == pytest.approx(b.mde)


# ── misuse is refused loudly ─────────────────────────────────────────────────

@pytest.mark.parametrize("kwargs", [
    {"sigma": 0.0, "n_expected": 100},
    {"sigma": -1.0, "n_expected": 100},
    {"sigma": float("nan"), "n_expected": 100},
    {"sigma": 0.05, "n_expected": 1},
])
def test_invalid_inputs_raise(kwargs):
    with pytest.raises(ValueError):
        compute_power(t_bar=T_BAR, effect_threshold=EFFECT, **kwargs)


def test_must_pass_exactly_one_of_sigma_or_outcome():
    with pytest.raises(ValueError, match="exactly one"):
        require_power(n_expected=100, t_bar=T_BAR, effect_threshold=EFFECT,
                      verbose=False)
    with pytest.raises(ValueError, match="exactly one"):
        require_power(n_expected=100, t_bar=T_BAR, effect_threshold=EFFECT,
                      sigma=0.05, outcome=[1.0, 2.0], verbose=False)


def test_refusal_is_a_systemexit_so_a_batch_runner_cannot_swallow_it():
    """A generic `except Exception` in a batch loop must NOT be able to catch
    the refusal and then log a null — which is exactly the outcome the gate
    exists to prevent."""
    assert issubclass(UnderpoweredScreen, SystemExit)
    assert not issubclass(UnderpoweredScreen, Exception)
