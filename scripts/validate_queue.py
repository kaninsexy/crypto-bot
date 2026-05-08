"""scripts/validate_queue.py -- queue-health validator.

Five checks against backtest/trial_queue.json. Exits 0 if clean,
1 if any violation. Output: structured JSON to stdout, one object
per violation. Wired into a pre-commit hook so a malformed queue
cannot land via commit.

Usage:
    python scripts/validate_queue.py

Checks:
  1. unique_ids       -- no duplicate "id" fields.
  2. no_unknown_sid   -- strategy_id is not "Unknown" / not missing
                         / not the proposal-agent malformed-id pattern.
  3. script_exists    -- when needs_trial_script=False AND status="queued",
                         script_path must exist on disk.
  4. manifest_entry   -- when status="queued", strategy_id must be a
                         key in backtest/holdout_manifest.json.
  5. status_drift     -- if research/<literature-doc> exists AND its
                         "Trial outcomes" table has a populated row
                         (date != "(pending)"), but trial_queue status
                         is still "queued", flag drift.

ASCII-only output (Windows cp1252 safe).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
QUEUE_PATH = ROOT / "backtest" / "trial_queue.json"
MANIFEST_PATH = ROOT / "backtest" / "holdout_manifest.json"
RESEARCH_DIR = ROOT / "research"


def _emit(violation: dict) -> None:
    """Print one structured-JSON violation line to stdout."""
    print(json.dumps(violation, ensure_ascii=True), flush=True)


def _load_queue() -> dict | None:
    if not QUEUE_PATH.exists():
        _emit({
            "check": "load_queue",
            "id": None,
            "detail": f"queue file missing: {QUEUE_PATH}",
        })
        return None
    try:
        return json.loads(QUEUE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        _emit({
            "check": "load_queue",
            "id": None,
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


# Markdown row pattern for the "Trial outcomes" table. A populated row
# is one whose 2nd column (date) is NOT a placeholder.
_PENDING_DATE_PAT = re.compile(
    r"^\s*\|\s*\(pending\)\s*\|\s*$|^\s*\|\s*TBD\s*\|\s*$",
    re.IGNORECASE,
)


def _literature_has_completed_row(lit_path: Path, variation_id: str) -> bool:
    """True if the Trial outcomes table in `lit_path` carries a row
    matching `variation_id` whose date column is a real ISO-ish date
    (not "(pending)" / "TBD" / empty). False on any other state."""
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
        # Tokenise the markdown row.
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
        # Real-ish date present.
        return True
    return False


def main() -> int:
    data = _load_queue()
    if data is None:
        return 1
    queue = data.get("queue", [])
    if not isinstance(queue, list):
        _emit({"check": "load_queue", "id": None, "detail": "queue field is not a list"})
        return 1

    manifest_keys = _load_manifest_keys()
    violations = 0

    # 1. unique_ids
    seen: dict[str, int] = {}
    for i, it in enumerate(queue):
        if not isinstance(it, dict):
            _emit({"check": "load_queue", "id": None, "detail": f"non-dict queue entry at index {i}"})
            violations += 1
            continue
        item_id = it.get("id")
        if not isinstance(item_id, str) or not item_id:
            _emit({"check": "unique_ids", "id": None, "detail": f"missing id at index {i}"})
            violations += 1
            continue
        if item_id in seen:
            _emit({
                "check": "unique_ids",
                "id": item_id,
                "detail": f"duplicate id; first seen at index {seen[item_id]}, again at {i}",
            })
            violations += 1
        else:
            seen[item_id] = i

    # 2. no_unknown_sid
    for it in queue:
        if not isinstance(it, dict):
            continue
        sid = it.get("strategy_id")
        if not isinstance(sid, str) or not sid:
            _emit({
                "check": "no_unknown_sid",
                "id": it.get("id"),
                "detail": f"strategy_id missing or empty: {sid!r}",
            })
            violations += 1
            continue
        if "unknown" in sid.lower():
            _emit({
                "check": "no_unknown_sid",
                "id": it.get("id"),
                "detail": f"strategy_id contains 'Unknown': {sid!r}",
            })
            violations += 1

    # 3. script_exists -- only when needs_trial_script=false AND status=queued
    for it in queue:
        if not isinstance(it, dict):
            continue
        if it.get("status") != "queued":
            continue
        if it.get("needs_trial_script"):
            continue
        sp = it.get("script_path")
        if not isinstance(sp, str) or not sp:
            _emit({
                "check": "script_exists",
                "id": it.get("id"),
                "detail": "script_path missing on queued item with needs_trial_script=false",
            })
            violations += 1
            continue
        if not (ROOT / sp).exists():
            _emit({
                "check": "script_exists",
                "id": it.get("id"),
                "detail": f"script_path does not exist on disk: {sp}",
            })
            violations += 1

    # 4. manifest_entry -- only when status=queued
    for it in queue:
        if not isinstance(it, dict):
            continue
        if it.get("status") != "queued":
            continue
        sid = it.get("strategy_id")
        if not isinstance(sid, str) or not sid:
            continue  # already counted under no_unknown_sid
        if sid not in manifest_keys:
            _emit({
                "check": "manifest_entry",
                "id": it.get("id"),
                "detail": f"strategy_id {sid!r} is not a key in holdout_manifest.json",
            })
            violations += 1

    # 5. status_drift -- literature shows a real outcome row but queue
    # still says queued.
    for it in queue:
        if not isinstance(it, dict):
            continue
        if it.get("status") != "queued":
            continue
        lit = it.get("literature_doc")
        vid = it.get("variation_id")
        if not isinstance(lit, str) or not lit:
            continue
        if not isinstance(vid, str) or not vid:
            continue
        lit_path = ROOT / lit
        if _literature_has_completed_row(lit_path, vid):
            _emit({
                "check": "status_drift",
                "id": it.get("id"),
                "detail": (
                    f"literature {lit} carries a populated outcome row for "
                    f"variation_id={vid!r}, but trial_queue status is still queued"
                ),
            })
            violations += 1

    return 0 if violations == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
