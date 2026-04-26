"""
backtest/tests/test_trials.py — Schema and behaviour tests for trials.py.

All file I/O goes to tmp_path fixtures — never the real
backtest/trials.log, backtest/holdout_access.log, or
backtest/holdout_manifest.json.  The patch_trials_paths fixture
(autouse) redirects every module-level path before each test and
clears the load_manifest lru_cache.
"""

import json
from pathlib import Path

import pytest

import backtest.holdout as holdout
import backtest.logs as logs
import backtest.trials as trials


# ── Fixture data ──────────────────────────────────────────────────────────────

HOLDOUT_START = "2025-09-12T14:12:00+00:00"

MANIFEST = {
    "VWAP": {
        "symbol": "ETH/USDT",
        "timeframe": "1h",
        "data_start": "2023-04-20T15:00:00+00:00",
        "data_end":   "2026-04-19T14:00:00+00:00",
        "dev_end":    HOLDOUT_START,
        "holdout_start": HOLDOUT_START,
    },
    "Supertrend": {
        "symbol": "ETH/USDT",
        "timeframe": "1h",
        "data_start": "2023-04-20T15:00:00+00:00",
        "data_end":   "2026-04-19T14:00:00+00:00",
        "dev_end":    HOLDOUT_START,
        "holdout_start": HOLDOUT_START,
    },
}


def _base_event(trial_type: str = "smoke", **overrides) -> dict:
    """Return a fully-populated, schema-valid event for trial_type."""
    ev: dict = {
        "strategy_id": "VWAP",
        "variation_id": "var-1",
        "trial_type": trial_type,
        "params": {"lookback": 20, "z": 2.0},
        "hypothesis": "VWAP reverts within 1 day",
        "split_holdout_start": HOLDOUT_START,
        "symbols": ["ETH/USDT"],
        "n_trades": 42,
        "sharpe": 1.23,
    }
    if trial_type in ("full_cpcv", "final_gate"):
        ev["cpcv"] = {
            "n_paths": 45,
            "n_blocks": 10,
            "k_held_out": 2,
            "purge_periods": 24,
            "embargo_periods": 6,
            "sharpe_distribution": {
                "mean": 0.9,
                "std": 0.4,
                "quantiles": {
                    "p05": 0.1, "p25": 0.5, "p50": 0.9,
                    "p75": 1.3, "p95": 1.7,
                },
            },
        }
        ev["dsr_validation"] = 0.85
    if trial_type == "final_gate":
        ev["dsr_holdout"] = 0.92
        # Schema v2 (chunk 11) — every final_gate row carries the
        # verdict + its component bools + at-eval thresholds + total
        # trade count.  Default to a happy-path "keep" so existing
        # tests stay readable; tests that probe the under_tested or
        # consistency rules override per-case.
        ev["verdict"] = "keep"
        ev["trade_count_pass"] = True
        ev["mintrl_pass"] = True
        ev["mt_mean_pass"] = True
        ev["baseline_pass"] = True
        ev["sr_zero_expected_at_eval"] = 1.78
        ev["mintrl_required_at_eval"] = 5.06
        ev["baseline_sharpe_at_eval"] = 0.5
        ev["total_trades"] = 42
    ev.update(overrides)
    return ev


# ── Autouse fixture ───────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def patch_trials_paths(tmp_path, monkeypatch):
    manifest_path = tmp_path / "manifest.json"
    access_log_path = tmp_path / "holdout_access.log"
    trials_log_path = tmp_path / "trials.log"

    manifest_path.write_text(json.dumps(MANIFEST), encoding="utf-8")

    monkeypatch.setattr(holdout, "_MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(holdout, "_ACCESS_LOG_PATH", access_log_path)
    monkeypatch.setattr(trials, "_TRIALS_LOG_PATH", trials_log_path)

    holdout.load_manifest.cache_clear()
    yield
    holdout.load_manifest.cache_clear()


# ── _canonical_hash determinism ───────────────────────────────────────────────

def test_canonical_hash_invariant_under_key_order():
    a = trials._canonical_hash({"a": 1, "b": 2, "c": 3})
    b = trials._canonical_hash({"c": 3, "a": 1, "b": 2})
    assert a == b


def test_canonical_hash_invariant_with_nested_dicts():
    a = trials._canonical_hash({"outer": {"x": 1, "y": 2}, "z": 3})
    b = trials._canonical_hash({"z": 3, "outer": {"y": 2, "x": 1}})
    assert a == b


def test_canonical_hash_differs_on_value_change():
    a = trials._canonical_hash({"lookback": 20})
    b = trials._canonical_hash({"lookback": 21})
    assert a != b


def test_canonical_hash_returns_64_hex_chars():
    h = trials._canonical_hash({"x": 1})
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


# ── record_trial happy paths per trial_type ──────────────────────────────────

def test_record_trial_smoke_happy_path():
    trials.record_trial(_base_event("smoke"))
    rows = list(logs.read_jsonl(trials._TRIALS_LOG_PATH))
    assert len(rows) == 1
    assert rows[0]["trial_type"] == "smoke"
    assert rows[0]["schema_version"] == trials._SCHEMA_VERSION


def test_record_trial_full_cpcv_happy_path():
    trials.record_trial(_base_event("full_cpcv"))
    rows = list(logs.read_jsonl(trials._TRIALS_LOG_PATH))
    assert len(rows) == 1
    assert rows[0]["trial_type"] == "full_cpcv"
    assert rows[0]["dsr_validation"] == 0.85
    assert rows[0]["cpcv"]["n_paths"] == 45


def test_record_trial_final_gate_happy_path():
    trials.record_trial(_base_event("final_gate"))
    rows = list(logs.read_jsonl(trials._TRIALS_LOG_PATH))
    assert len(rows) == 1
    assert rows[0]["trial_type"] == "final_gate"
    assert rows[0]["dsr_holdout"] == 0.92


# ── Validation: hypothesis ────────────────────────────────────────────────────

def test_record_trial_rejects_non_string_hypothesis():
    ev = _base_event("smoke", hypothesis=123)
    with pytest.raises(trials.TrialSchemaError):
        trials.record_trial(ev)


def test_record_trial_rejects_empty_hypothesis():
    ev = _base_event("smoke", hypothesis="")
    with pytest.raises(trials.TrialSchemaError):
        trials.record_trial(ev)


# ── Validation: trial_type ────────────────────────────────────────────────────

def test_record_trial_rejects_unknown_trial_type():
    ev = _base_event("smoke")
    ev["trial_type"] = "quick_check"
    with pytest.raises(trials.TrialSchemaError):
        trials.record_trial(ev)


# ── Validation: missing required fields per trial_type ────────────────────────

def test_record_trial_rejects_missing_n_trades():
    ev = _base_event("smoke")
    del ev["n_trades"]
    with pytest.raises(trials.TrialSchemaError):
        trials.record_trial(ev)


def test_record_trial_rejects_missing_cpcv_for_full_cpcv():
    ev = _base_event("full_cpcv")
    del ev["cpcv"]
    with pytest.raises(trials.TrialSchemaError):
        trials.record_trial(ev)


def test_record_trial_rejects_missing_dsr_validation_for_full_cpcv():
    ev = _base_event("full_cpcv")
    del ev["dsr_validation"]
    with pytest.raises(trials.TrialSchemaError):
        trials.record_trial(ev)


def test_record_trial_rejects_missing_dsr_holdout_for_final_gate():
    ev = _base_event("final_gate")
    del ev["dsr_holdout"]
    with pytest.raises(trials.TrialSchemaError):
        trials.record_trial(ev)


def test_record_trial_rejects_missing_quantile_in_cpcv():
    ev = _base_event("full_cpcv")
    del ev["cpcv"]["sharpe_distribution"]["quantiles"]["p50"]
    with pytest.raises(trials.TrialSchemaError):
        trials.record_trial(ev)


def test_record_trial_smoke_does_not_require_cpcv():
    ev = _base_event("smoke")
    assert "cpcv" not in ev
    trials.record_trial(ev)
    rows = list(logs.read_jsonl(trials._TRIALS_LOG_PATH))
    assert len(rows) == 1


# ── Validation: non-JSON-serialisable params ─────────────────────────────────

def test_record_trial_rejects_non_json_serialisable_params():
    class Opaque:
        pass
    ev = _base_event("smoke", params={"obj": Opaque()})
    with pytest.raises(trials.TrialSchemaError):
        trials.record_trial(ev)


def test_record_trial_rejects_set_in_params():
    # set() is not JSON-serialisable without default coercion.
    ev = _base_event("smoke", params={"choices": {1, 2, 3}})
    with pytest.raises(trials.TrialSchemaError):
        trials.record_trial(ev)


# ── Validation: strategy_id not in manifest ──────────────────────────────────

def test_record_trial_rejects_unknown_strategy_id():
    ev = _base_event("smoke", strategy_id="GhostStrategy")
    with pytest.raises(trials.TrialStrategyNotInManifest):
        trials.record_trial(ev)


# ── Writer overwrites canonical fields ───────────────────────────────────────

def test_record_trial_overwrites_caller_canonical_fields():
    ev = _base_event(
        "smoke",
        schema_version=999,
        trial_id="caller-supplied-id",
        params_hash="deadbeef",
    )
    trials.record_trial(ev)
    rows = list(logs.read_jsonl(trials._TRIALS_LOG_PATH))
    assert len(rows) == 1
    row = rows[0]

    # schema_version forced to writer's version
    assert row["schema_version"] == trials._SCHEMA_VERSION

    # trial_id replaced with a fresh uuid hex (32 hex chars, never the
    # caller-supplied placeholder)
    assert row["trial_id"] != "caller-supplied-id"
    assert len(row["trial_id"]) == 32
    assert all(c in "0123456789abcdef" for c in row["trial_id"])

    # params_hash recomputed canonically
    expected = trials._canonical_hash(row["params"])
    assert row["params_hash"] == expected
    assert row["params_hash"] != "deadbeef"


def test_record_trial_fills_ts_git_commit_trial_id_when_absent():
    ev = _base_event("smoke")
    assert "ts" not in ev
    assert "git_commit" not in ev
    assert "trial_id" not in ev

    trials.record_trial(ev)

    rows = list(logs.read_jsonl(trials._TRIALS_LOG_PATH))
    assert len(rows) == 1
    row = rows[0]
    assert isinstance(row["ts"], str) and len(row["ts"]) > 0
    assert isinstance(row["git_commit"], str) and len(row["git_commit"]) > 0
    assert isinstance(row["trial_id"], str) and len(row["trial_id"]) == 32


# ── FinalGateAlreadyRecorded guard ───────────────────────────────────────────

def test_second_final_gate_raises_when_no_regen():
    trials.record_trial(_base_event("final_gate"))
    with pytest.raises(trials.FinalGateAlreadyRecorded):
        trials.record_trial(_base_event("final_gate", variation_id="var-2"))


def test_final_gate_succeeds_after_regen_event():
    # First final_gate write.
    trials.record_trial(_base_event(
        "final_gate", ts="2026-01-01T00:00:00+00:00"
    ))

    # Regeneration event in holdout_access.log between the two writes.
    logs.append_jsonl(holdout._ACCESS_LOG_PATH, {
        "ts": "2026-02-01T00:00:00+00:00",
        "strategy_id": "VWAP",
        "regenerated": True,
        "old_holdout_start": HOLDOUT_START,
        "new_holdout_start": "2025-10-01T00:00:00+00:00",
    })

    # Second final_gate write — should succeed because the regen reset
    # the gate.
    trials.record_trial(_base_event(
        "final_gate", ts="2026-03-01T00:00:00+00:00", variation_id="var-2"
    ))

    rows = list(logs.read_jsonl(trials._TRIALS_LOG_PATH))
    final_gates = [r for r in rows if r["trial_type"] == "final_gate"]
    assert len(final_gates) == 2


def test_final_gate_guard_is_per_strategy():
    """A final_gate for VWAP must not block one for Supertrend."""
    trials.record_trial(_base_event("final_gate", strategy_id="VWAP"))
    trials.record_trial(_base_event("final_gate", strategy_id="Supertrend"))
    rows = list(logs.read_jsonl(trials._TRIALS_LOG_PATH))
    assert len(rows) == 2


# ── Variation drift warning ──────────────────────────────────────────────────

def test_variation_drift_emits_stderr_warning_but_writes(capsys):
    trials.record_trial(_base_event(
        "smoke", variation_id="var-A", params={"lookback": 20}
    ))

    # Same variation_id, different params → drift.  Write should
    # succeed; warning should land on stderr.
    trials.record_trial(_base_event(
        "smoke", variation_id="var-A", params={"lookback": 30}
    ))

    captured = capsys.readouterr()
    assert "variation drift" in captured.err
    assert "var-A" in captured.err

    rows = list(logs.read_jsonl(trials._TRIALS_LOG_PATH))
    assert len(rows) == 2


def test_no_drift_warning_when_params_identical(capsys):
    trials.record_trial(_base_event(
        "smoke", variation_id="var-B", params={"lookback": 20}
    ))
    trials.record_trial(_base_event(
        "smoke", variation_id="var-B", params={"lookback": 20}
    ))
    captured = capsys.readouterr()
    assert "variation drift" not in captured.err


# ── count_trials_for_dsr ─────────────────────────────────────────────────────

def test_count_trials_for_dsr_excludes_smoke():
    trials.record_trial(_base_event("smoke", variation_id="s1"))
    trials.record_trial(_base_event("smoke", variation_id="s2"))
    trials.record_trial(_base_event("full_cpcv", variation_id="c1"))
    trials.record_trial(_base_event("final_gate", variation_id="g1"))

    assert trials.count_trials_for_dsr("VWAP") == 2


def test_count_trials_for_dsr_filters_by_strategy():
    trials.record_trial(_base_event(
        "full_cpcv", strategy_id="VWAP", variation_id="v1"
    ))
    trials.record_trial(_base_event(
        "full_cpcv", strategy_id="Supertrend", variation_id="s1"
    ))
    assert trials.count_trials_for_dsr("VWAP") == 1
    assert trials.count_trials_for_dsr("Supertrend") == 1


# ── count_distinct_variations ────────────────────────────────────────────────

def test_count_distinct_variations_counts_unique():
    trials.record_trial(_base_event("smoke", variation_id="v1"))
    trials.record_trial(_base_event("smoke", variation_id="v2"))
    trials.record_trial(_base_event(
        "smoke", variation_id="v1", params={"lookback": 99}
    ))  # repeat of v1 (with drift) — should not double-count
    assert trials.count_distinct_variations("VWAP") == 2


def test_count_distinct_variations_includes_all_trial_types():
    trials.record_trial(_base_event("smoke", variation_id="a"))
    trials.record_trial(_base_event("full_cpcv", variation_id="b"))
    trials.record_trial(_base_event("final_gate", variation_id="c"))
    assert trials.count_distinct_variations("VWAP") == 3


# ── read_trials filter composition ───────────────────────────────────────────

def test_read_trials_no_filter_returns_all():
    trials.record_trial(_base_event("smoke", variation_id="a"))
    trials.record_trial(_base_event("full_cpcv", variation_id="b"))
    rows = list(trials.read_trials())
    assert len(rows) == 2


def test_read_trials_filters_compose_by_strategy_and_trial_type():
    trials.record_trial(_base_event(
        "smoke", strategy_id="VWAP", variation_id="v1"
    ))
    trials.record_trial(_base_event(
        "full_cpcv", strategy_id="VWAP", variation_id="v2"
    ))
    trials.record_trial(_base_event(
        "smoke", strategy_id="Supertrend", variation_id="s1"
    ))

    rows = list(trials.read_trials(
        strategy_id="VWAP", trial_type="full_cpcv",
    ))
    assert len(rows) == 1
    assert rows[0]["variation_id"] == "v2"


def test_read_trials_filter_by_variation_id():
    trials.record_trial(_base_event("smoke", variation_id="alpha"))
    trials.record_trial(_base_event(
        "smoke", variation_id="alpha", params={"lookback": 99}
    ))
    trials.record_trial(_base_event("smoke", variation_id="beta"))

    rows = list(trials.read_trials(variation_id="alpha"))
    assert len(rows) == 2


# ── latest_final_gate ────────────────────────────────────────────────────────

def test_latest_final_gate_returns_none_when_no_row():
    assert trials.latest_final_gate("VWAP") is None


def test_latest_final_gate_returns_only_row_when_one_exists():
    trials.record_trial(_base_event("final_gate"))
    row = trials.latest_final_gate("VWAP")
    assert row is not None
    assert row["trial_type"] == "final_gate"


def test_latest_final_gate_returns_most_recent_after_regen_between():
    # First final_gate.
    trials.record_trial(_base_event(
        "final_gate", ts="2026-01-01T00:00:00+00:00"
    ))
    # Regen resets the gate.
    logs.append_jsonl(holdout._ACCESS_LOG_PATH, {
        "ts": "2026-02-01T00:00:00+00:00",
        "strategy_id": "VWAP",
        "regenerated": True,
        "old_holdout_start": HOLDOUT_START,
        "new_holdout_start": "2025-10-01T00:00:00+00:00",
    })
    # Second final_gate, more recent ts.
    trials.record_trial(_base_event(
        "final_gate",
        ts="2026-03-01T00:00:00+00:00",
        variation_id="var-2",
        dsr_holdout=0.97,
    ))

    row = trials.latest_final_gate("VWAP")
    assert row is not None
    assert row["ts"] == "2026-03-01T00:00:00+00:00"
    assert row["dsr_holdout"] == 0.97


def test_latest_final_gate_filters_by_strategy():
    trials.record_trial(_base_event(
        "final_gate", strategy_id="VWAP", ts="2026-01-01T00:00:00+00:00"
    ))
    trials.record_trial(_base_event(
        "final_gate", strategy_id="Supertrend", ts="2026-02-01T00:00:00+00:00"
    ))
    row = trials.latest_final_gate("VWAP")
    assert row is not None
    assert row["strategy_id"] == "VWAP"


# ── Schema v2 (chunk 11) ──────────────────────────────────────────────────────
# final_gate rows now carry verdict + components + at-eval thresholds.
# Each missing required field rejects; the verdict↔component bools
# consistency rule rejects mismatched shapes.

def test_schema_version_is_v2():
    assert trials._SCHEMA_VERSION == 2
    trials.record_trial(_base_event("final_gate"))
    rows = list(logs.read_jsonl(trials._TRIALS_LOG_PATH))
    assert rows[0]["schema_version"] == 2


def test_final_gate_records_verdict_field():
    trials.record_trial(_base_event("final_gate"))
    rows = list(logs.read_jsonl(trials._TRIALS_LOG_PATH))
    assert rows[0]["verdict"] == "keep"


@pytest.mark.parametrize("missing_key", [
    "verdict",
    "trade_count_pass",
    "mintrl_pass",
    "mt_mean_pass",
    "baseline_pass",
    "sr_zero_expected_at_eval",
    "mintrl_required_at_eval",
    "baseline_sharpe_at_eval",
    "total_trades",
])
def test_final_gate_rejects_v2_missing_field(missing_key):
    ev = _base_event("final_gate")
    del ev[missing_key]
    with pytest.raises(trials.TrialSchemaError, match=f"missing required field {missing_key!r}"):
        trials.record_trial(ev)


def test_final_gate_rejects_unknown_verdict():
    ev = _base_event("final_gate")
    ev["verdict"] = "monitor"  # not in {keep, retire, under_tested}
    with pytest.raises(trials.TrialSchemaError, match="unknown verdict"):
        trials.record_trial(ev)


def test_final_gate_under_tested_requires_quality_bools_none():
    """For verdict=under_tested, mt_mean_pass and baseline_pass MUST
    be None (the fields were not computed; False would be a lie)."""
    ev = _base_event("final_gate")
    ev["verdict"] = "under_tested"
    ev["trade_count_pass"] = False
    ev["mintrl_pass"] = True
    ev["mt_mean_pass"] = False  # WRONG — should be None
    ev["baseline_pass"] = None
    with pytest.raises(trials.TrialSchemaError, match="must be None for verdict=under_tested"):
        trials.record_trial(ev)


def test_final_gate_keep_requires_quality_bools_non_none():
    """For verdict=keep / retire, all four component bools MUST be
    non-None — they were computed."""
    ev = _base_event("final_gate")
    ev["verdict"] = "keep"
    ev["mt_mean_pass"] = None  # WRONG — must be bool when not under_tested
    with pytest.raises(trials.TrialSchemaError, match="must be bool"):
        trials.record_trial(ev)


def test_final_gate_under_tested_happy_path():
    """A well-formed under_tested row writes successfully when both
    quality bools are None."""
    ev = _base_event("final_gate")
    ev["verdict"] = "under_tested"
    ev["trade_count_pass"] = False
    ev["mintrl_pass"] = True
    ev["mt_mean_pass"] = None
    ev["baseline_pass"] = None
    # NaN floats are acceptable for under_tested rows where the quality
    # gates didn't compute their thresholds.
    ev["sr_zero_expected_at_eval"] = float("nan")
    trials.record_trial(ev)
    rows = list(logs.read_jsonl(trials._TRIALS_LOG_PATH))
    assert rows[0]["verdict"] == "under_tested"


def test_final_gate_rejects_non_bool_precondition():
    ev = _base_event("final_gate")
    ev["trade_count_pass"] = "true"  # str, not bool
    with pytest.raises(trials.TrialSchemaError, match="trade_count_pass.*must be bool"):
        trials.record_trial(ev)


def test_final_gate_rejects_non_int_total_trades():
    ev = _base_event("final_gate")
    ev["total_trades"] = 42.0  # float, not int
    with pytest.raises(trials.TrialSchemaError, match="total_trades.*must be int"):
        trials.record_trial(ev)


def test_smoke_row_unchanged_in_v2():
    """smoke rows do NOT need v2 fields; only schema_version bumps."""
    trials.record_trial(_base_event("smoke"))
    rows = list(logs.read_jsonl(trials._TRIALS_LOG_PATH))
    assert rows[0]["trial_type"] == "smoke"
    assert rows[0]["schema_version"] == 2
    # No v2 fields should be present (helper doesn't populate them
    # for smoke).
    for k in ("verdict", "trade_count_pass", "sr_zero_expected_at_eval"):
        assert k not in rows[0]


def test_full_cpcv_row_unchanged_in_v2():
    trials.record_trial(_base_event("full_cpcv"))
    rows = list(logs.read_jsonl(trials._TRIALS_LOG_PATH))
    assert rows[0]["trial_type"] == "full_cpcv"
    assert rows[0]["schema_version"] == 2
    for k in ("verdict", "trade_count_pass"):
        assert k not in rows[0]


# ── Policy (c): superseded_by tooling-fix invalidation tag ───────────────────

def test_superseded_by_round_trips_on_full_cpcv():
    """A full_cpcv row carrying superseded_by writes and reads back cleanly."""
    ev = _base_event("full_cpcv", variation_id="v1")
    ev["superseded_by"] = "25bd843"
    trials.record_trial(ev)
    rows = list(logs.read_jsonl(trials._TRIALS_LOG_PATH))
    assert len(rows) == 1
    assert rows[0]["superseded_by"] == "25bd843"


def test_count_trials_for_dsr_excludes_superseded_rows():
    """A superseded full_cpcv row does not count toward the DSR trial budget."""
    ev_pre = _base_event("full_cpcv", variation_id="v-pre")
    ev_pre["superseded_by"] = "25bd843"
    trials.record_trial(ev_pre)

    ev_post = _base_event("full_cpcv", variation_id="v-post")
    trials.record_trial(ev_post)

    # 2 rows on disk, but only the post-fix one counts toward DSR.
    rows = list(logs.read_jsonl(trials._TRIALS_LOG_PATH))
    assert len(rows) == 2
    assert trials.count_trials_for_dsr("VWAP") == 1


def test_count_trials_for_dsr_includes_non_superseded_rows_same_strategy():
    """The supersession filter is per-row, not per-strategy: a strategy
    with a tagged row plus an untagged row contributes its untagged row."""
    ev_tagged = _base_event("full_cpcv", variation_id="vA")
    ev_tagged["superseded_by"] = "25bd843"
    trials.record_trial(ev_tagged)

    ev_clean_1 = _base_event("full_cpcv", variation_id="vB")
    trials.record_trial(ev_clean_1)

    ev_clean_2 = _base_event("full_cpcv", variation_id="vC")
    trials.record_trial(ev_clean_2)

    assert trials.count_trials_for_dsr("VWAP") == 2


def test_count_distinct_variations_unaffected_by_supersession():
    """Regression guard for the intentional asymmetry: supersession does
    not reduce the iteration-cap count. Two rows sharing variation_id
    still collapse to one variation regardless of supersession state."""
    ev_pre = _base_event("full_cpcv", variation_id="rescue-default")
    ev_pre["superseded_by"] = "25bd843"
    trials.record_trial(ev_pre)

    # Same variation_id, post-fix run.
    ev_post = _base_event("full_cpcv", variation_id="rescue-default")
    trials.record_trial(ev_post)

    # A genuinely new variation.
    ev_new = _base_event("full_cpcv", variation_id="rescue-vol-scaled")
    trials.record_trial(ev_new)

    assert trials.count_distinct_variations("VWAP") == 2


def test_superseded_by_rejected_on_final_gate():
    """final_gate rows must not carry superseded_by — supersession of a
    deploy-decision boundary would silently rewrite the audit trail."""
    ev = _base_event("final_gate")
    ev["superseded_by"] = "25bd843"
    with pytest.raises(trials.TrialSchemaError, match="not allowed on final_gate"):
        trials.record_trial(ev)


def test_superseded_by_rejected_when_empty_string():
    ev = _base_event("full_cpcv")
    ev["superseded_by"] = ""
    with pytest.raises(trials.TrialSchemaError, match="must be non-empty"):
        trials.record_trial(ev)


def test_superseded_by_rejected_when_non_string():
    ev = _base_event("full_cpcv")
    ev["superseded_by"] = 25
    with pytest.raises(trials.TrialSchemaError, match="must be a string"):
        trials.record_trial(ev)
