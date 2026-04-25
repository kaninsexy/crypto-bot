"""
backtest/trials.py — trials.log writer (sacred-harness-adjacent).

Owns the schema for backtest/trials.log: every iteration-phase backtest,
every parameter variation, every smoke run, and every final-gate pass
appends one JSONL row through this module.  The trial count feeds the
multiple-testing correction in the Deflated Sharpe Ratio
(`backtest/dsr.py`, next chunk), so the writer is authoritative for
schema, deduplication, and the final-gate guard.

Schema changes here require human approval per CLAUDE.md.

Schema (one JSONL row per trial)
─────────────────────────────────
Always required:
  schema_version: int (writer always sets to _SCHEMA_VERSION)
  ts: str (ISO-8601 UTC; writer fills if absent)
  trial_id: str (uuid4 hex; writer always sets)
  strategy_id: str (must exist in holdout manifest)
  variation_id: str (free-form caller tag for this parameter set)
  params_hash: str (sha256 hex of canonical-serialised params; writer
                    always recomputes)
  trial_type: str (one of _VALID_TRIAL_TYPES)
  params: dict (must be JSON-serialisable without coercion)
  hypothesis: str (non-empty; required by CLAUDE.md no-p-hacking rule)
  git_commit: str (writer fills if absent)
  split_holdout_start: str (ISO-8601; the manifest's holdout_start at
                            the time of the run — caller-supplied so it
                            survives later regenerations)
  symbols: list[str]
  n_trades: int
  sharpe: float

Required when trial_type ∈ {full_cpcv, final_gate}:
  cpcv: dict {
    n_paths: int
    n_blocks: int
    k_held_out: int
    purge_periods: int
    embargo_periods: int
    sharpe_distribution: dict {
      mean: float
      std: float
      quantiles: dict {p05: float, p25: float, p50: float, p75: float,
                        p95: float}
    }
  }
  dsr_validation: float

Required when trial_type == "final_gate":
  dsr_holdout: float

Optional any time:
  mintrl: float | null
  buy_and_hold_sharpe: float | null
  notes: str | null

Final-gate guard
────────────────
A strategy may have at most one `final_gate` row per split epoch.  The
guard scans trials.log for prior final_gate rows and cross-references
backtest/holdout_access.log for `regenerated=true` events: a
regeneration resets the guard the same way it resets the holdout-access
window.  See `_has_prior_final_gate`.

Variation drift
───────────────
If two rows share `(strategy_id, variation_id)` but disagree on
`params_hash`, the second write succeeds but the writer prints a
warning to stderr.  Drift indicates the caller is reusing a variation
label across distinct parameter sets, which would corrupt the iteration
cap and DSR multiple-testing accounting.
"""

import hashlib
import json
import subprocess
import sys
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import backtest.holdout as _holdout
from backtest.logs import append_jsonl, iter_jsonl_filtered


# ── Module-level path configuration ──────────────────────────────────────────
# Override in tests via monkeypatch.setattr.

_TRIALS_LOG_PATH: Path = Path("backtest/trials.log")
_SCHEMA_VERSION: int = 1
_VALID_TRIAL_TYPES: frozenset[str] = frozenset({"smoke", "full_cpcv", "final_gate"})


# ── Exceptions ────────────────────────────────────────────────────────────────

class TrialSchemaError(ValueError):
    """A trial event failed schema validation (missing field, wrong type,
    unknown trial_type, non-serialisable params, empty hypothesis)."""

class TrialStrategyNotInManifest(KeyError):
    """The trial's strategy_id is not a key in the holdout manifest."""

class FinalGateAlreadyRecorded(RuntimeError):
    """A final_gate trial already exists for this strategy since the most
    recent regenerated=true event in holdout_access.log (or ever, if the
    strategy has never been regenerated)."""


# ── Validation helpers ────────────────────────────────────────────────────────

_QUANTILE_KEYS: tuple[str, ...] = ("p05", "p25", "p50", "p75", "p95")
_CPCV_INT_KEYS: tuple[str, ...] = (
    "n_paths", "n_blocks", "k_held_out", "purge_periods", "embargo_periods",
)


def _is_real_number(x: object) -> bool:
    """True for int or float, excluding bool (which is an int subclass)."""
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _check_str(event: dict, key: str) -> None:
    if key not in event:
        raise TrialSchemaError(f"missing required field {key!r}")
    if not isinstance(event[key], str):
        raise TrialSchemaError(
            f"{key!r} must be a string; got {type(event[key]).__name__}"
        )


def _check_int(event: dict, key: str) -> None:
    if key not in event:
        raise TrialSchemaError(f"missing required field {key!r}")
    v = event[key]
    if not isinstance(v, int) or isinstance(v, bool):
        raise TrialSchemaError(
            f"{key!r} must be int; got {type(v).__name__}"
        )


def _check_float(event: dict, key: str) -> None:
    if key not in event:
        raise TrialSchemaError(f"missing required field {key!r}")
    if not _is_real_number(event[key]):
        raise TrialSchemaError(
            f"{key!r} must be a number; got {type(event[key]).__name__}"
        )


def _validate_cpcv_block(event: dict) -> None:
    if "cpcv" not in event:
        raise TrialSchemaError("missing required field 'cpcv'")
    cpcv = event["cpcv"]
    if not isinstance(cpcv, dict):
        raise TrialSchemaError("'cpcv' must be a dict")

    for k in _CPCV_INT_KEYS:
        if k not in cpcv:
            raise TrialSchemaError(f"missing required field 'cpcv.{k}'")
        if not isinstance(cpcv[k], int) or isinstance(cpcv[k], bool):
            raise TrialSchemaError(f"'cpcv.{k}' must be int")

    if "sharpe_distribution" not in cpcv:
        raise TrialSchemaError(
            "missing required field 'cpcv.sharpe_distribution'"
        )
    sd = cpcv["sharpe_distribution"]
    if not isinstance(sd, dict):
        raise TrialSchemaError("'cpcv.sharpe_distribution' must be a dict")

    for k in ("mean", "std"):
        if k not in sd:
            raise TrialSchemaError(
                f"missing required field 'cpcv.sharpe_distribution.{k}'"
            )
        if not _is_real_number(sd[k]):
            raise TrialSchemaError(
                f"'cpcv.sharpe_distribution.{k}' must be a number"
            )

    if "quantiles" not in sd:
        raise TrialSchemaError(
            "missing required field 'cpcv.sharpe_distribution.quantiles'"
        )
    q = sd["quantiles"]
    if not isinstance(q, dict):
        raise TrialSchemaError(
            "'cpcv.sharpe_distribution.quantiles' must be a dict"
        )
    for k in _QUANTILE_KEYS:
        if k not in q:
            raise TrialSchemaError(
                f"missing required field 'cpcv.sharpe_distribution.quantiles.{k}'"
            )
        if not _is_real_number(q[k]):
            raise TrialSchemaError(
                f"'cpcv.sharpe_distribution.quantiles.{k}' must be a number"
            )


def _validate_event(event: dict) -> None:
    """Raise TrialSchemaError if `event` does not satisfy the per-trial_type
    schema.  Called after default fields are filled but before params_hash
    is computed and before any side-effects.
    """
    # Always-required scalar fields
    _check_str(event, "ts")
    _check_str(event, "trial_id")
    _check_str(event, "strategy_id")
    _check_str(event, "variation_id")
    _check_str(event, "trial_type")
    _check_str(event, "git_commit")
    _check_str(event, "split_holdout_start")

    trial_type = event["trial_type"]
    if trial_type not in _VALID_TRIAL_TYPES:
        raise TrialSchemaError(
            f"unknown trial_type {trial_type!r}; expected one of "
            f"{sorted(_VALID_TRIAL_TYPES)}"
        )

    # hypothesis must be a non-empty string (CLAUDE.md no-p-hacking rule)
    if "hypothesis" not in event:
        raise TrialSchemaError("missing required field 'hypothesis'")
    h = event["hypothesis"]
    if not isinstance(h, str):
        raise TrialSchemaError(
            f"'hypothesis' must be a string; got {type(h).__name__}"
        )
    if h == "":
        raise TrialSchemaError("'hypothesis' must be non-empty")

    if "symbols" not in event:
        raise TrialSchemaError("missing required field 'symbols'")
    syms = event["symbols"]
    if not isinstance(syms, list) or not all(isinstance(s, str) for s in syms):
        raise TrialSchemaError("'symbols' must be a list of strings")

    _check_int(event, "n_trades")
    _check_float(event, "sharpe")

    # params must be a dict and JSON-serialisable without coercion.
    if "params" not in event:
        raise TrialSchemaError("missing required field 'params'")
    params = event["params"]
    if not isinstance(params, dict):
        raise TrialSchemaError("'params' must be a dict")
    try:
        json.dumps(params)
    except TypeError as e:
        raise TrialSchemaError(
            f"'params' is not JSON-serialisable: {e}"
        ) from e

    # Per-trial_type sub-matrix
    if trial_type in {"full_cpcv", "final_gate"}:
        _validate_cpcv_block(event)
        _check_float(event, "dsr_validation")

    if trial_type == "final_gate":
        _check_float(event, "dsr_holdout")

    # Optional fields are type-validated only when present.
    for opt_key in ("mintrl", "buy_and_hold_sharpe"):
        if opt_key in event and event[opt_key] is not None:
            if not _is_real_number(event[opt_key]):
                raise TrialSchemaError(
                    f"{opt_key!r} must be a number or null"
                )
    if "notes" in event and event["notes"] is not None:
        if not isinstance(event["notes"], str):
            raise TrialSchemaError("'notes' must be a string or null")


# ── Canonical hashing ─────────────────────────────────────────────────────────

def _canonical_hash(params: dict) -> str:
    """Return sha256 hex of params under canonical JSON serialisation.

    Canonical form: keys sorted, no whitespace, ASCII-only.  This makes
    the hash invariant under key insertion order and Python dict
    rehashing.  Caller-supplied params_hash values are always replaced
    by this function's output.
    """
    canonical = json.dumps(
        params, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ── Git commit helper (mirrors holdout.py) ────────────────────────────────────

def _get_git_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


# ── Final-gate guard ──────────────────────────────────────────────────────────

def _has_prior_final_gate(strategy_id: str) -> bool:
    """Return True if a final_gate row exists for `strategy_id` since the
    most recent regenerated=true event for that strategy in
    backtest/holdout_access.log (or ever, if no regen has occurred).

    Cross-referencing the access log keeps the final-gate guard in
    lock-step with the holdout-access guard: a manifest regeneration
    that resets one resets the other, so a re-drawn split allows a
    fresh final_gate write.
    """
    last_regen_ts: str | None = None
    for ev in iter_jsonl_filtered(
        _holdout._ACCESS_LOG_PATH,
        lambda e: (
            e.get("strategy_id") == strategy_id
            and e.get("regenerated") is True
        ),
    ):
        ts = ev.get("ts")
        if isinstance(ts, str):
            last_regen_ts = ts

    for ev in iter_jsonl_filtered(
        _TRIALS_LOG_PATH,
        lambda e: (
            e.get("strategy_id") == strategy_id
            and e.get("trial_type") == "final_gate"
        ),
    ):
        if last_regen_ts is None:
            return True
        if ev.get("ts", "") > last_regen_ts:
            return True
    return False


# ── Variation drift warning ───────────────────────────────────────────────────

def _warn_on_variation_drift(event: dict) -> None:
    """Emit a stderr warning if any prior row with the same
    (strategy_id, variation_id) has a different params_hash.  Does not
    raise — the write succeeds either way.  The warning surfaces caller
    misuse (reusing a variation label for a different parameter set)
    without aborting an otherwise valid record."""
    sid = event["strategy_id"]
    vid = event["variation_id"]
    new_hash = event["params_hash"]
    for prior in iter_jsonl_filtered(
        _TRIALS_LOG_PATH,
        lambda e: (
            e.get("strategy_id") == sid
            and e.get("variation_id") == vid
        ),
    ):
        prior_hash = prior.get("params_hash")
        if isinstance(prior_hash, str) and prior_hash != new_hash:
            print(
                f"[trials] WARNING: variation drift for "
                f"strategy_id={sid!r} variation_id={vid!r}: "
                f"prior params_hash={prior_hash} new params_hash={new_hash}",
                file=sys.stderr,
            )
            return


# ── Public API ────────────────────────────────────────────────────────────────

def record_trial(event: dict) -> None:
    """Validate and append one trial event to backtest/trials.log.

    Mutates `event` in-place: schema_version, trial_id, ts (if absent),
    git_commit (if absent), and params_hash are written by this
    function and overwrite any caller-supplied values for
    schema_version, trial_id, and params_hash.

    Raises:
        TrialSchemaError              — schema validation failed.
        TrialStrategyNotInManifest    — strategy_id absent from manifest.
        FinalGateAlreadyRecorded      — second final_gate for the same
                                        strategy without an intervening
                                        regenerated=true event.
    """
    # 1. Canonical fields the writer owns.
    event["schema_version"] = _SCHEMA_VERSION
    event["trial_id"] = uuid.uuid4().hex

    # 2. Defaults for fields the writer fills only when absent.
    if event.get("ts") is None:
        event["ts"] = datetime.now(timezone.utc).isoformat()
    if event.get("git_commit") is None:
        event["git_commit"] = _get_git_commit()

    # 3. Schema validation — done before any side-effect or hashing so
    #    a malformed event cannot leave a partial trace.
    _validate_event(event)

    # 4. Compute canonical params_hash now that params is validated as
    #    a JSON-serialisable dict.  Caller-supplied params_hash is
    #    silently overwritten.
    event["params_hash"] = _canonical_hash(event["params"])

    # 5. Strategy must exist in the holdout manifest.
    manifest = _holdout.load_manifest()
    if event["strategy_id"] not in manifest:
        raise TrialStrategyNotInManifest(
            f"'{event['strategy_id']}' not found in holdout manifest."
        )

    # 6. Final-gate guard.
    if event["trial_type"] == "final_gate":
        if _has_prior_final_gate(event["strategy_id"]):
            raise FinalGateAlreadyRecorded(
                f"A final_gate trial has already been recorded for "
                f"'{event['strategy_id']}' since the most recent "
                "regenerated=true event in holdout_access.log. "
                "Regenerate the manifest to reset the gate."
            )

    # 7. Variation drift check (warning only, never blocks the write).
    _warn_on_variation_drift(event)

    # 8. Append.
    append_jsonl(_TRIALS_LOG_PATH, event)


def count_trials_for_dsr(strategy_id: str) -> int:
    """Count trials for `strategy_id` that contribute to the DSR
    multiple-testing correction.

    Smoke trials are excluded — they are diagnostic, not validation
    signal — per docs/validation_framework.md.
    """
    n = 0
    for _ in iter_jsonl_filtered(
        _TRIALS_LOG_PATH,
        lambda e: (
            e.get("strategy_id") == strategy_id
            and e.get("trial_type") in {"full_cpcv", "final_gate"}
        ),
    ):
        n += 1
    return n


def count_distinct_variations(strategy_id: str) -> int:
    """Count distinct variation_id values for `strategy_id`.

    Feeds the 20-variation iteration cap from CLAUDE.md.  All trial
    types contribute (a smoke run still counts as a variation tried).
    """
    seen: set[str] = set()
    for ev in iter_jsonl_filtered(
        _TRIALS_LOG_PATH,
        lambda e: e.get("strategy_id") == strategy_id,
    ):
        vid = ev.get("variation_id")
        if isinstance(vid, str):
            seen.add(vid)
    return len(seen)


def read_trials(
    strategy_id: str | None = None,
    variation_id: str | None = None,
    trial_type: str | None = None,
) -> Iterator[dict]:
    """Yield trial rows matching every non-None filter (logical AND).

    Streams from disk; the caller never holds the full log in memory.
    """
    def _predicate(e: dict) -> bool:
        if strategy_id is not None and e.get("strategy_id") != strategy_id:
            return False
        if variation_id is not None and e.get("variation_id") != variation_id:
            return False
        if trial_type is not None and e.get("trial_type") != trial_type:
            return False
        return True

    yield from iter_jsonl_filtered(_TRIALS_LOG_PATH, _predicate)


def latest_final_gate(strategy_id: str) -> dict | None:
    """Return the most recent (by ts) final_gate row for `strategy_id`,
    or None if no final_gate has ever been written for that strategy."""
    latest: dict | None = None
    latest_ts: str = ""
    for ev in iter_jsonl_filtered(
        _TRIALS_LOG_PATH,
        lambda e: (
            e.get("strategy_id") == strategy_id
            and e.get("trial_type") == "final_gate"
        ),
    ):
        ts = ev.get("ts", "")
        if isinstance(ts, str) and ts > latest_ts:
            latest = ev
            latest_ts = ts
    return latest
