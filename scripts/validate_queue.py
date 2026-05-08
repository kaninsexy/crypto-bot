"""scripts/validate_queue.py -- queue-health validator.

Five checks against backtest/trial_queue.json. Exits 0 if clean,
1 if any violation. Output: structured JSON to stdout, one object
per violation. Wired into a pre-commit hook so a malformed queue
cannot land via commit.

Usage:
    python scripts/validate_queue.py          # bare mode: hard-block on any violation
    python scripts/validate_queue.py --fix    # auto-patch status_drift only;
                                              # hard-block stays on unique_ids and
                                              # no_unknown_sid

Checks:
  1. unique_ids       -- no duplicate "id" fields.
  2. no_unknown_sid   -- strategy_id is not "Unknown" / not missing
                         / not the proposal-agent malformed-id pattern.
  3. script_exists    -- when needs_trial_script=False AND merged
                         status=="queued", script_path must exist on disk.
  4. manifest_entry   -- when merged status=="queued", strategy_id
                         must be a key in backtest/holdout_manifest.json.
  5. status_drift     -- if research/<literature-doc> exists AND its
                         "Trial outcomes" table has a populated row
                         (date != "(pending)"), but the EFFECTIVE
                         (merged definitions+state) status is still
                         "queued", flag drift.

Effective status is the merged view that the orchestrator uses
(state file wins over definitions for runtime fields). Reading
state directly here avoids importing run_trial_queue.py.

ASCII-only output (Windows cp1252 safe).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
QUEUE_PATH = ROOT / "backtest" / "trial_queue.json"
STATE_PATH = ROOT / "backtest" / "trial_queue_state.json"
MANIFEST_PATH = ROOT / "backtest" / "holdout_manifest.json"
TRIALS_LOG_PATH = ROOT / "backtest" / "trials.log"
RESEARCH_DIR = ROOT / "research"


def _emit(violation: dict) -> None:
    """Print one structured-JSON violation line to stdout."""
    print(json.dumps(violation, ensure_ascii=True), flush=True)


def _load_queue() -> dict | None:
    if not QUEUE_PATH.exists():
        _emit({
            "check": "load_queue", "id": None,
            "detail": f"queue file missing: {QUEUE_PATH}",
        })
        return None
    try:
        return json.loads(QUEUE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        _emit({
            "check": "load_queue", "id": None,
            "detail": f"trial_queue.json JSON parse error: {exc}",
        })
        return None


def _load_manifest_keys() -> set[str]:
    if not MANIFEST_PATH.exists():
        return set()
    try:
        m = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return set()
    return set(m.keys()) if isinstance(m, dict) else set()


def _load_state() -> dict:
    """Return the parsed state-file dict, or an empty skeleton when
    the file is absent or unreadable. Never raises -- callers operate
    on the dict's `items` map and tolerate emptiness.
    """
    if not STATE_PATH.exists():
        return {"schema_version": 1, "items": {}}
    try:
        text = STATE_PATH.read_text(encoding="utf-8").strip()
        if not text:
            return {"schema_version": 1, "items": {}}
        d = json.loads(text)
    except (OSError, json.JSONDecodeError):
        return {"schema_version": 1, "items": {}}
    if not isinstance(d, dict):
        return {"schema_version": 1, "items": {}}
    if not isinstance(d.get("items"), dict):
        d["items"] = {}
    return d


def _save_state(state: dict) -> None:
    """Atomic write to STATE_PATH (tmp + os.replace)."""
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, STATE_PATH)


def _migrate_state_from_definitions(definitions: dict) -> dict:
    """Build a fresh state file from trial_queue.json runtime fields.
    Mirrors run_trial_queue.py._load_or_migrate_state's first-run
    path so we don't have to import the orchestrator module.
    """
    legacy_runtime_fields = (
        "status", "started_at", "finished_at", "verdict",
        "trial_id", "error", "email_sent",
    )
    state = {
        "schema_version": 1,
        "last_digest_sent_at": None,
        "last_run_at": None,
        "items": {},
    }
    for item in definitions.get("queue", []):
        if not isinstance(item, dict):
            continue
        item_id = item.get("id")
        if not isinstance(item_id, str):
            continue
        entry = {f: item.get(f) for f in legacy_runtime_fields}
        entry.setdefault("retry_count", 0)
        entry.setdefault("last_fetch_attempt", None)
        entry.setdefault("needs_script_digested", False)
        entry.setdefault("retry_after", None)
        entry.setdefault("build_attempts", 0)
        entry.setdefault("run_attempts", 0)
        state["items"][item_id] = entry
    return state


def _effective_status(item: dict, state_items: dict) -> str | None:
    """State-file status wins; definitions-file status is fallback."""
    iid = item.get("id")
    if isinstance(iid, str) and iid in state_items:
        s = state_items[iid].get("status")
        if isinstance(s, str):
            return s
    s = item.get("status")
    return s if isinstance(s, str) else None


# Markdown row pattern reference: a populated row has a non-placeholder
# date in column 2.
def _literature_has_completed_row(lit_path: Path, variation_id: str) -> bool:
    """True if the Trial outcomes table in `lit_path` carries a row
    matching `variation_id` whose date column is a real ISO-ish date
    (not "(pending)" / "TBD" / empty)."""
    if not lit_path.exists() or not variation_id:
        return False
    try:
        text = lit_path.read_text(encoding="utf-8")
    except OSError:
        return False
    in_outcomes = False
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("##"):
            in_outcomes = "trial outcomes" in line.lower()
            continue
        if not in_outcomes:
            continue
        if not line.lstrip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        if cells[0] != variation_id:
            continue
        date_cell = cells[1]
        if not date_cell:
            continue
        if date_cell.lower() in ("(pending)", "tbd", "pending"):
            continue
        return True
    return False


def _load_trials_log_index() -> dict[tuple[str, str], dict]:
    """Index trials.log rows by (strategy_id, variation_id). Last
    occurrence wins (latest re-run for the same key). Returns an
    empty dict on missing / malformed log."""
    out: dict[tuple[str, str], dict] = {}
    if not TRIALS_LOG_PATH.exists():
        return out
    try:
        with TRIALS_LOG_PATH.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                sid = e.get("strategy_id")
                vid = e.get("variation_id")
                if isinstance(sid, str) and isinstance(vid, str):
                    out[(sid, vid)] = e
    except OSError:
        return out
    return out


# Verdict-to-status mapping. Mirrors the orchestrator's
# verdict_to_status; surfaced verdict strings come from the state
# file (which the orchestrator wrote at trial completion).
_VERDICT_TO_STATUS = {
    "keep": "done",
    "keep_holdout": "done",
    "retire": "retired",
    "retire_holdout": "retired",
    "under_tested": "under_tested",
    "dry-run": "done",
}


def _verdict_to_status(verdict: str | None) -> str | None:
    if not isinstance(verdict, str):
        return None
    return _VERDICT_TO_STATUS.get(verdict)


def _run_checks(verbose_emit: bool = True) -> tuple[int, list[dict]]:
    """Run all 5 checks. Return (violation_count, violation_list).
    When verbose_emit=True, each violation is also printed to stdout
    via _emit; bare-mode callers want this, --fix re-runs with
    verbose_emit=True to print the post-fix violation set if any.
    """
    data = _load_queue()
    if data is None:
        return 1, [{"check": "load_queue", "id": None, "detail": "load failed"}]
    queue = data.get("queue", [])
    if not isinstance(queue, list):
        v = {"check": "load_queue", "id": None, "detail": "queue field is not a list"}
        if verbose_emit:
            _emit(v)
        return 1, [v]

    manifest_keys = _load_manifest_keys()
    state = _load_state()
    state_items = state.get("items", {}) if isinstance(state, dict) else {}

    violations: list[dict] = []

    # 1. unique_ids
    seen: dict[str, int] = {}
    for i, it in enumerate(queue):
        if not isinstance(it, dict):
            v = {"check": "load_queue", "id": None, "detail": f"non-dict queue entry at index {i}"}
            violations.append(v)
            if verbose_emit: _emit(v)
            continue
        item_id = it.get("id")
        if not isinstance(item_id, str) or not item_id:
            v = {"check": "unique_ids", "id": None, "detail": f"missing id at index {i}"}
            violations.append(v)
            if verbose_emit: _emit(v)
            continue
        if item_id in seen:
            v = {
                "check": "unique_ids", "id": item_id,
                "detail": f"duplicate id; first seen at index {seen[item_id]}, again at {i}",
            }
            violations.append(v)
            if verbose_emit: _emit(v)
        else:
            seen[item_id] = i

    # 2. no_unknown_sid
    for it in queue:
        if not isinstance(it, dict):
            continue
        sid = it.get("strategy_id")
        if not isinstance(sid, str) or not sid:
            v = {"check": "no_unknown_sid", "id": it.get("id"),
                 "detail": f"strategy_id missing or empty: {sid!r}"}
            violations.append(v)
            if verbose_emit: _emit(v)
            continue
        if "unknown" in sid.lower():
            v = {"check": "no_unknown_sid", "id": it.get("id"),
                 "detail": f"strategy_id contains 'Unknown': {sid!r}"}
            violations.append(v)
            if verbose_emit: _emit(v)

    # 3. script_exists -- effective status = "queued"
    for it in queue:
        if not isinstance(it, dict):
            continue
        if _effective_status(it, state_items) != "queued":
            continue
        if it.get("needs_trial_script"):
            continue
        sp = it.get("script_path")
        if not isinstance(sp, str) or not sp:
            v = {"check": "script_exists", "id": it.get("id"),
                 "detail": "script_path missing on queued item with needs_trial_script=false"}
            violations.append(v)
            if verbose_emit: _emit(v)
            continue
        if not (ROOT / sp).exists():
            v = {"check": "script_exists", "id": it.get("id"),
                 "detail": f"script_path does not exist on disk: {sp}"}
            violations.append(v)
            if verbose_emit: _emit(v)

    # 4. manifest_entry -- effective status = "queued"
    # Skip items with needs_trial_script=True: the manifest entry is
    # created BY the auto-build path AFTER the queue entry exists
    # (that's the proposal-agent -> auto-build lifecycle). Flagging
    # these would block the orchestrator's startup gate every time
    # the proposal agent queues a fresh candidate.
    for it in queue:
        if not isinstance(it, dict):
            continue
        if _effective_status(it, state_items) != "queued":
            continue
        if it.get("needs_trial_script"):
            continue
        sid = it.get("strategy_id")
        if not isinstance(sid, str) or not sid:
            continue
        if sid not in manifest_keys:
            v = {"check": "manifest_entry", "id": it.get("id"),
                 "detail": f"strategy_id {sid!r} is not a key in holdout_manifest.json"}
            violations.append(v)
            if verbose_emit: _emit(v)

    # 5. status_drift -- literature shows real outcome row but
    # effective status is still "queued". State-file status wins
    # over definitions-file status.
    for it in queue:
        if not isinstance(it, dict):
            continue
        if _effective_status(it, state_items) != "queued":
            continue
        lit = it.get("literature_doc")
        vid = it.get("variation_id")
        if not isinstance(lit, str) or not lit:
            continue
        if not isinstance(vid, str) or not vid:
            continue
        if _literature_has_completed_row(ROOT / lit, vid):
            v = {"check": "status_drift", "id": it.get("id"),
                 "detail": (
                     f"literature {lit} carries a populated outcome row for "
                     f"variation_id={vid!r}, but effective status is still queued"
                 )}
            violations.append(v)
            if verbose_emit: _emit(v)

    return (0 if not violations else 1), violations


def _auto_fix_status_drift(violations: list[dict]) -> int:
    """Patch the state file's status field for each status_drift item.

    Per the spec: read trials.log to confirm the trial actually ran
    (matching strategy_id+variation_id), then determine the corrected
    status. Verdict precedence:
      - State file's existing verdict (orchestrator-recorded; this is
        the authoritative runtime source)
      - Trials.log row's verdict field (legacy; rows since 2026-04
        carry verdict=None at the top level, so this path falls
        through to "error")
      - Else: "error"

    Maps verdict -> status:
      keep / keep_holdout       -> done
      retire / retire_holdout   -> retired
      under_tested              -> under_tested
      <null / unrecognised>     -> error

    Idempotent: writing the same status the file already has is a no-op
    on disk content but still emits the audit line.

    Hard-block items (unique_ids / no_unknown_sid) are NOT patched;
    they require human attention.

    Returns count of items patched.
    """
    drift_violations = [v for v in violations if v.get("check") == "status_drift"]
    if not drift_violations:
        return 0

    queue_data = _load_queue() or {"queue": []}
    queue = queue_data.get("queue", [])
    by_id = {
        it.get("id"): it
        for it in queue
        if isinstance(it, dict) and isinstance(it.get("id"), str)
    }

    state = _load_state()
    if not state.get("items"):
        # First-run path: bootstrap state from definitions.
        state = _migrate_state_from_definitions(queue_data)
        print(
            f"[validate_queue --fix] bootstrapped state file from "
            f"definitions ({len(state['items'])} items)"
        )

    trials_idx = _load_trials_log_index()

    patched = 0
    for v in drift_violations:
        sq_id = v.get("id")
        item = by_id.get(sq_id)
        if not item:
            print(f"[validate_queue --fix] {sq_id}: NOT in queue; skipping")
            continue
        sid = item.get("strategy_id")
        vid = item.get("variation_id")
        if not isinstance(sid, str) or not isinstance(vid, str):
            print(f"[validate_queue --fix] {sq_id}: malformed strategy_id/variation_id; skipping")
            continue

        trials_row = trials_idx.get((sid, vid))
        if trials_row is None:
            print(f"[validate_queue --fix] {sq_id}: no trials.log row for "
                  f"({sid}, {vid}); skipping (literature claims a row but "
                  "trials.log does not)")
            continue

        # State-file verdict is authoritative; trials.log row's verdict
        # field is informational (and currently always null at the top
        # level for full_cpcv rows -- the verdict is recorded in the
        # orchestrator's TRIAL SUMMARY JSON sentinel, not the trial
        # row itself).
        existing = state["items"].get(sq_id) or {}
        verdict = existing.get("verdict")
        if not isinstance(verdict, str):
            verdict = trials_row.get("verdict")  # almost always None today

        target_status = _verdict_to_status(verdict)
        if target_status is None:
            target_status = "error"

        # Apply patch.
        entry = state["items"].setdefault(sq_id, {})
        prev_status = entry.get("status")
        entry["status"] = target_status
        if not isinstance(entry.get("trial_id"), str):
            tid = trials_row.get("trial_id")
            if isinstance(tid, str):
                entry["trial_id"] = tid
        # Preserve any existing verdict in state; if absent, set from
        # mapping when verdict was provided (else leave None).
        if not isinstance(entry.get("verdict"), str) and isinstance(verdict, str):
            entry["verdict"] = verdict

        print(
            f"[validate_queue --fix] {sq_id} ({sid} / {vid}): "
            f"status {prev_status!r} -> {target_status!r}; "
            f"verdict={verdict!r}; trial_id={entry.get('trial_id')!r}"
        )
        patched += 1

    if patched > 0:
        _save_state(state)
    return patched


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--fix",
        action="store_true",
        help=(
            "Auto-patch status_drift violations into trial_queue_state.json. "
            "unique_ids and no_unknown_sid stay hard-block. After patching, "
            "all 5 checks re-run; exit 0 only when post-fix state is clean."
        ),
    )
    args = parser.parse_args()

    if not args.fix:
        # Bare mode: print all violations, exit non-zero on any.
        rc, _ = _run_checks(verbose_emit=True)
        return rc

    # --fix mode: hard-block on unhealable types BEFORE attempting fixes.
    # Run a silent first pass to enumerate violations.
    _, violations = _run_checks(verbose_emit=False)
    hard_block = [
        v for v in violations
        if v.get("check") in ("load_queue", "unique_ids", "no_unknown_sid")
    ]
    if hard_block:
        for v in hard_block:
            _emit(v)
        print(
            f"[validate_queue --fix] {len(hard_block)} hard-block "
            "violation(s) require manual fix; auto-patch SKIPPED.",
            file=sys.stderr,
        )
        return 1

    n_patched = _auto_fix_status_drift(violations)
    print(f"[validate_queue --fix] patched {n_patched} status_drift item(s)")

    # Re-run all 5 checks against the post-fix state.
    rc, post_violations = _run_checks(verbose_emit=True)
    if rc != 0:
        print(
            f"[validate_queue --fix] {len(post_violations)} violation(s) "
            "remain after auto-fix; exit 1",
            file=sys.stderr,
        )
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
