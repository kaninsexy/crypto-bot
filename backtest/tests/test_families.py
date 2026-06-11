"""
backtest/tests/test_families.py — Family taxonomy + cross-trial stats
(gate spec v2, 2026-06-11).
"""

import json

import pytest

import backtest.families as families
import backtest.trials as trials


@pytest.fixture
def patched_families(tmp_path, monkeypatch):
    """Redirect the taxonomy file + trials.log to tmp_path."""
    fam_path = tmp_path / "strategy_families.json"
    log_path = tmp_path / "trials.log"
    log_path.touch()
    monkeypatch.setattr(families, "_FAMILIES_PATH", fam_path)
    monkeypatch.setattr(trials, "_TRIALS_LOG_PATH", log_path)
    return {"families": fam_path, "trials_log": log_path}


def _write_families(path, mapping: dict) -> None:
    path.write_text(json.dumps(mapping), encoding="utf-8")


def _append_row(path, strategy_id, sharpe, trial_type="full_cpcv",
                superseded=None) -> None:
    row = {
        "strategy_id": strategy_id,
        "trial_type": trial_type,
        "sharpe": sharpe,
    }
    if superseded:
        row["superseded_by"] = superseded
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def test_load_families_strips_metadata_and_validates(patched_families):
    _write_families(patched_families["families"], {
        "_comment": "meta",
        "A": {"family": "reversal"},
        "B": {"family": "carry", "neutral": True},
    })
    fams = families.load_families()
    assert set(fams) == {"A", "B"}
    assert families.family_of("A") == "reversal"
    assert families.family_of("missing") is None
    assert families.is_neutral("B") is True
    assert families.is_neutral("A") is False
    assert families.is_neutral("missing") is False


def test_unknown_family_name_rejected(patched_families):
    _write_families(patched_families["families"], {
        "A": {"family": "not-a-family"},
    })
    with pytest.raises(families.FamilyConfigError, match="unknown family"):
        families.load_families()


def test_family_stats_pools_across_members(patched_families):
    """N and V come from ALL family members' finite, non-superseded
    full_cpcv/final_gate rows — not just the queried strategy."""
    _write_families(patched_families["families"], {
        "A": {"family": "reversal"},
        "B": {"family": "reversal"},
        "C": {"family": "carry"},
    })
    log = patched_families["trials_log"]
    _append_row(log, "A", 1.0)
    _append_row(log, "B", -1.0)
    _append_row(log, "B", 2.0)
    _append_row(log, "C", 9.9)                       # other family — excluded
    _append_row(log, "A", 5.0, trial_type="smoke")   # smoke — excluded
    _append_row(log, "A", 7.0, superseded="abc123")  # superseded — excluded
    _append_row(log, "A", float("nan"))              # CPCVError — excluded

    stats = families.family_sharpe_stats("A")
    assert stats.family == "reversal"
    assert stats.n_trials == 3
    assert stats.used_fallback is False
    # Population variance of [1.0, -1.0, 2.0]: mean 2/3,
    # var = ((1-2/3)^2 + (-1-2/3)^2 + (2-2/3)^2)/3 = 14/9.
    assert stats.sr_var == pytest.approx(14.0 / 9.0)


def test_family_stats_thin_family_falls_back(patched_families):
    _write_families(patched_families["families"], {
        "A": {"family": "seasonality"},
    })
    _append_row(patched_families["trials_log"], "A", 0.7)
    with pytest.warns(UserWarning, match="falling back"):
        stats = families.family_sharpe_stats("A")
    assert stats.used_fallback is True
    assert stats.sr_var == 1.0
    assert stats.n_trials == 1


def test_family_stats_unmapped_strategy_falls_back(patched_families):
    _write_families(patched_families["families"], {
        "A": {"family": "reversal"},
    })
    with pytest.warns(UserWarning, match="no entry"):
        stats = families.family_sharpe_stats("Unmapped")
    assert stats.family is None
    assert stats.used_fallback is True
    assert stats.sr_var == 1.0


def test_repo_taxonomy_covers_every_trials_log_strategy():
    """The committed strategy_families.json must map every
    strategy_id that appears in the committed trials.log (work-order
    item 2)."""
    import math
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    fams = json.loads(
        (root / "backtest" / "strategy_families.json").read_text(
            encoding="utf-8"
        )
    )
    log = root / "backtest" / "trials.log"
    if not log.exists():
        pytest.skip("repo trials.log absent")
    sids = {
        json.loads(l)["strategy_id"]
        for l in log.read_text(encoding="utf-8").splitlines() if l.strip()
    }
    missing = {s for s in sids if s not in fams}
    assert not missing, f"strategy_ids missing from taxonomy: {sorted(missing)}"
