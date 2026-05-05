"""scripts/test_trial_queue_parser.py — sanity tests for parse_json_summary.

Standalone script (not pytest-collected) that imports parse_json_summary
from scripts/run_trial_queue.py and exercises its four documented input
shapes.  Exits 0 on all pass, 1 on any failure.

Run:
    python scripts/test_trial_queue_parser.py
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ORCHESTRATOR_PATH = ROOT / "scripts" / "run_trial_queue.py"

spec = importlib.util.spec_from_file_location(
    "run_trial_queue", ORCHESTRATOR_PATH
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
parse_json_summary = mod.parse_json_summary


# ── Test fixtures ────────────────────────────────────────────────────────────

SINGLE_LINE_STDOUT = """\
some preamble
[trial] running
--- TRIAL SUMMARY JSON ---
{"verdict": "retire", "sr_observed": 0.123}
"""

PRETTY_PRINTED_STDOUT = """\
[trial] running
=== summary ===
--- TRIAL SUMMARY JSON ---
{
  "verdict": "keep",
  "sr_observed": 1.234,
  "block_sharpes": [0.1, 0.2, 0.3]
}
"""

NO_SENTINEL_STDOUT = """\
some preamble
[trial] done
{"verdict": "retire"}
"""

INVALID_JSON_STDOUT = """\
[trial] done
--- TRIAL SUMMARY JSON ---
{this is not valid json}
"""


# ── Tests ────────────────────────────────────────────────────────────────────

def test_single_line() -> bool:
    out = parse_json_summary(SINGLE_LINE_STDOUT)
    if not isinstance(out, dict):
        print(f"FAIL test_single_line: got {type(out).__name__}, want dict")
        return False
    if out.get("verdict") != "retire":
        print(f"FAIL test_single_line: verdict={out.get('verdict')!r}")
        return False
    if out.get("sr_observed") != 0.123:
        print(f"FAIL test_single_line: sr_observed={out.get('sr_observed')!r}")
        return False
    print("PASS test_single_line")
    return True


def test_pretty_printed() -> bool:
    out = parse_json_summary(PRETTY_PRINTED_STDOUT)
    if not isinstance(out, dict):
        print(f"FAIL test_pretty_printed: got {type(out).__name__}, want dict")
        return False
    if out.get("verdict") != "keep":
        print(f"FAIL test_pretty_printed: verdict={out.get('verdict')!r}")
        return False
    if out.get("sr_observed") != 1.234:
        print(f"FAIL test_pretty_printed: sr_observed={out.get('sr_observed')!r}")
        return False
    if out.get("block_sharpes") != [0.1, 0.2, 0.3]:
        print(
            f"FAIL test_pretty_printed: block_sharpes={out.get('block_sharpes')!r}"
        )
        return False
    print("PASS test_pretty_printed")
    return True


def test_no_sentinel() -> bool:
    out = parse_json_summary(NO_SENTINEL_STDOUT)
    if out is not None:
        print(f"FAIL test_no_sentinel: got {out!r}, want None")
        return False
    print("PASS test_no_sentinel")
    return True


def test_invalid_json() -> bool:
    out = parse_json_summary(INVALID_JSON_STDOUT)
    if out is not None:
        print(f"FAIL test_invalid_json: got {out!r}, want None")
        return False
    print("PASS test_invalid_json")
    return True


def main() -> int:
    results = [
        test_single_line(),
        test_pretty_printed(),
        test_no_sentinel(),
        test_invalid_json(),
    ]
    if all(results):
        print(f"\nAll {len(results)} tests passed.")
        return 0
    print(f"\n{sum(1 for r in results if not r)}/{len(results)} tests failed.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
