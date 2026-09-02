# SPDX-License-Identifier: LicenseRef-Proprietary
# Copyright (c) 2026 Kanin Srijundorn. All rights reserved.
"""enforcement_policy.py — the finding-policy map (playbooks port V6 (BK-0016)).

A single policy surface that decides, for every REGISTERED finding code, whether
it BLOCKS, merely ADVISES, or is OFF. Re-implemented (clean-room) from OpenLore's
`enforcement-policy.ts` (MIT). No source copied.

The load-bearing idea is a decoupling:

  * a check's intrinsic **severity** is owned by the check (informational here);
  * a finding's enforcement **class** is owned by repo config (this map).

So the same finding can gate one repo and merely advise another, without
touching the check. Two more OpenLore invariants we keep:

  * **Advisory by default.** Blocking is opt-in. The one exception is
    ``POLICY_RULE_BLOCK`` (a policy ``block`` rule that fired IS the opt-in),
    whose source default is blocking.
  * **Producers are advisory-safe.** A finding whose producer raised degrades to
    an advisory caveat and never blocks — a crashing check can't wedge the gate.

Precedence (``resolve_class``):  off > blocking > advisory > source-default.
An explicit class in the policy wins over the registry's source default; an
absent/invalid explicit value falls through to the source default; an unknown
code falls through to the global advisory default and never hard-blocks.

This module is PURE (no I/O beyond ``load_policy`` reading the JSON config), so
it is trivially testable (``tests/unit/test_enforcement_policy.py``) and
harness-drivable (``recon_e2e_harness.py`` stage ``enforcement_policy.classify``).
The first real producer through this map is the policy engine
(``scripts/policy_engine.py``); existing hooks stay hardcoded-blocking (they
guard sacred invariants and should never be soft) and may emit codes for
observability later.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
DEFAULT_POLICY_PATH = REPO_ROOT / ".claude" / "rules" / "enforcement_policy.json"

VALID_CLASSES: tuple[str, ...] = ("off", "blocking", "advisory")

# The global fallback for an UNREGISTERED code: advisory (never blocks). This is
# the advisory-by-default posture made concrete.
GLOBAL_DEFAULT_CLASS = "advisory"

# ── the registry (source of truth for registered codes + their source default)─
#
# ``default_class`` is the SOURCE default (the check's intrinsic disposition).
# Repo config (.claude/rules/enforcement_policy.json) may override it per code.
# EVERY code here MUST have a matching entry in that JSON; the rule doc
# (.claude/rules/enforcement.md) documents each. To register a new code: add it
# here, add it to the JSON, document it in the rule doc, add a fixture.
FINDING_REGISTRY: dict[str, dict[str, str]] = {
    "POLICY_RULE_BLOCK": {
        "default_class": "blocking",
        "description": "A policy-engine `block` rule matched a tool call. The "
        "block verb IS the opt-in to blocking, so this is the one "
        "code whose source default gates.",
    },
    "POLICY_RULE_NOTIFY": {
        "default_class": "advisory",
        "description": "A policy-engine `notify` rule matched a tool call — "
        "surfaced as an advisory caveat, never blocks.",
    },
    "POLICY_ENGINE_DISARMED": {
        "default_class": "advisory",
        "description": "A .policy file failed to compile; the engine refused to "
        "arm and surfaced loudly (fail-closed at the ENGINE level, "
        "advisory at the GATE level — it must not wedge every tool "
        "call on a typo).",
    },
    "POLICY_ENGINE_PRODUCER_ERROR": {
        "default_class": "advisory",
        "description": "A policy producer raised while evaluating a tool call; "
        "the finding degraded to an advisory caveat rather than "
        "crashing or hard-blocking the gate.",
    },
}


def resolve_class(code: str, policy: dict | None, registry: dict | None = None) -> str:
    """Return the enforcement class for ``code`` under ``policy``.

    Precedence off > blocking > advisory > source-default (OpenLore
    ``applyPolicyPrecedence``): an explicit, VALID class in ``policy`` wins;
    otherwise the registry source default; otherwise the global advisory default.
    """
    registry = FINDING_REGISTRY if registry is None else registry
    explicit = policy.get(code) if isinstance(policy, dict) else None
    if explicit in VALID_CLASSES:
        return explicit
    entry = registry.get(code)
    if entry and entry.get("default_class") in VALID_CLASSES:
        return entry["default_class"]
    return GLOBAL_DEFAULT_CLASS


def _finding_key(rec: dict) -> tuple:
    """A total, deterministic sort key so classify output is stable regardless of
    input order (OpenLore sorts before consuming). All-string tuple to avoid
    type-comparison errors on heterogeneous findings."""
    return (
        str(rec.get("code", "")),
        str(rec.get("source", "")),
        str(rec.get("message", "")),
        str(rec.get("id", "")),
    )


def classify_findings(
    findings: list[dict] | None, policy: dict | None, registry: dict | None = None
) -> dict[str, Any]:
    """Classify a finding list into blocking / advisory / off partitions.

    Returns ``{classified, blocking, advisory, off, gated}`` where ``classified``
    is every finding stamped with its resolved ``class`` (+ ``caveat`` /
    ``caveat_reason``), the three partitions are lists, and ``gated`` is True iff
    at least one BLOCKING finding survived. Two safety degradations, both to an
    advisory caveat, never a block:

      * a finding carrying a truthy ``error`` (its producer raised);
      * a finding whose ``code`` is not in the registry (unless the policy
        explicitly silences it with ``off``).
    """
    registry = FINDING_REGISTRY if registry is None else registry
    classified: list[dict] = []
    for raw in findings or []:
        f = dict(raw) if isinstance(raw, dict) else {"code": str(raw)}
        code = f.get("code") or "UNKNOWN"
        f["code"] = code
        caveat = False
        caveat_reason: str | None = None

        if f.get("error"):
            # Producer raised: advisory-safe. Never blocks, whatever the policy.
            cls = "advisory"
            caveat = True
            caveat_reason = f"producer error, degraded to advisory: {f['error']}"
        elif code not in registry:
            # Unregistered code: never hard-block on it. Honor an explicit `off`
            # (lets a repo silence a noisy unknown); otherwise advisory caveat.
            explicit = policy.get(code) if isinstance(policy, dict) else None
            cls = "off" if explicit == "off" else "advisory"
            caveat = cls != "off"
            if caveat:
                caveat_reason = f"unregistered finding code {code!r}; treated as advisory"
        else:
            cls = resolve_class(code, policy, registry)

        f["class"] = cls
        f["caveat"] = caveat
        f["caveat_reason"] = caveat_reason
        classified.append(f)

    classified.sort(key=_finding_key)
    blocking = [r for r in classified if r["class"] == "blocking"]
    advisory = [r for r in classified if r["class"] == "advisory"]
    off = [r for r in classified if r["class"] == "off"]
    return {
        "classified": classified,
        "blocking": blocking,
        "advisory": advisory,
        "off": off,
        "gated": len(blocking) > 0,
    }


def load_policy(path: str | Path | None = None) -> dict[str, str]:
    """Load the policy map (the ``policy`` sub-object) from the JSON config.

    Fail-safe: a missing/unreadable/malformed config returns ``{}`` (so every
    code resolves to its registry source default — advisory-by-default holds even
    if the config is gone). Only valid class values are kept.
    """
    path = Path(path) if path is not None else DEFAULT_POLICY_PATH
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    policy = obj.get("policy") if isinstance(obj, dict) else None
    if not isinstance(policy, dict):
        return {}
    return {str(k): v for k, v in policy.items() if v in VALID_CLASSES}


def registry_codes() -> set[str]:
    return set(FINDING_REGISTRY)


def _cli(argv: list[str]) -> int:
    """Tiny inspector: print the resolved class for each registered code under
    the shipped policy. Read-only; useful for eyeballing the surface."""
    policy = load_policy()
    print(f"# enforcement policy — {DEFAULT_POLICY_PATH}")
    for code in sorted(FINDING_REGISTRY):
        src = FINDING_REGISTRY[code]["default_class"]
        eff = resolve_class(code, policy)
        flag = " (overridden)" if code in policy and policy[code] != src else ""
        print(f"  {code:32} source={src:9} effective={eff}{flag}")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_cli(sys.argv))
