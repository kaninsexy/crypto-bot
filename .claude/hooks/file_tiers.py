#!/usr/bin/env python3
"""Single source of truth for the three-tier file-autonomy map (crypto-bot).

=============================================================================
THE ONE PLACE THE FILE TIERS LIVE. Every hook imports its tier map from here.
=============================================================================

Ported 2026-09-02 from siamese-reconcile ``.claude/hooks/file_tiers.py``
(HEAD 2f13045) and re-populated with crypto-bot's own sacred map. The port
exists because crypto-bot's predecessor guards were four separate bash
scripts that each carried their OWN inline sacred regex — and they had
already drifted: ``sacred-block.sh`` guarded ``^MASTER_PLAN\\.md$`` (a path
that does not exist; the real file is ``docs/MASTER_PLAN.md``) while
``.githooks/pre-commit`` guarded the correct one. Worse, every one of those
bash hooks parsed its stdin with ``jq``, which is NOT installed on this
machine — so each exited 127 and FAILED OPEN. The Python layer removes the
jq dependency and fails CLOSED.

The three tiers (CLAUDE.md "Core principles" is the prose spec)
---------------------------------------------------------------
- Tier 1 SACRED       : sacred-harness files + documents. ``path-allowlist.py``
                        blocks writes (exit 2) unless SACRED_OVERRIDE_FILES
                        names the path (the CLAUDE.md "Pre-authorization
                        exception").
- Tier 2 SCHEMA-STABLE: the validation harness contract modules. Bug fixes
                        that preserve function contracts proceed; contract
                        changes need human approval. ``path-allowlist.py``
                        lets them through with a NOTE on stderr.
- Tier 3 AUTONOMOUS   : everything else. Edited freely.

Patterns are Python ``re`` regexes matched with ``re.match`` semantics
against the repo-RELATIVE forward-slash path.
"""

from __future__ import annotations

import re

# --- Tier 1 SACRED -----------------------------------------------------------
# Two classes, both from CLAUDE.md "Core principles":
#   (a) sacred-harness FILES  — runtime audit artifacts + the holdout split.
#       A change here breaks the multiple-testing correction or the holdout
#       single-access guarantee.
#   (b) sacred-harness DOCUMENTS — the project constitution: agent rules,
#       plan state, validation methodology, architecture.
# Plus three additions so the guard layer guards itself (siamese precedent):
#   ^\.claude/hooks/.*\.py$    — an agent must not neuter a hook by editing it.
#   ^\.claude/rules/.*         — nor soften its own rules.
#   ^schemas/.*\.schema\.json$ — nor soften its own gate's contracts.
SACRED_PATTERNS: tuple[str, ...] = (
    # (b) constitution documents
    r"^CLAUDE\.md$",
    r"^docs/MASTER_PLAN\.md$",
    r"^docs/architecture\.md$",
    r"^docs/validation_framework\.md$",
    r"^docs/handoff_template\.md$",
    # (a) sacred-harness runtime artifacts + holdout split
    r"^backtest/trials\.log$",
    r"^backtest/holdout_manifest\.json$",
    r"^backtest/holdout_access\.log$",
    r"^backtest/holdout\.py$",
    r"^backtest/trials\.py$",
    # the guard layer guarding itself
    r"^\.claude/rules/.*",
    r"^\.claude/settings\.json$",
    r"^\.claude/hooks/.*\.py$",
    r"^schemas/.*\.schema\.json$",
    r"^\.memory/T3_procedural/.*",
    # credentials — CLAUDE.md "Human only": never edited or copied by an agent
    r"^\.env(\..+)?$",
    r".*\.crypto-bot\.env$",
    r".*/\.crypto-bot\.env$",
)

# --- Tier 2 SCHEMA-STABLE ----------------------------------------------------
# CLAUDE.md "Schema-stable code": validation-harness modules whose interfaces
# feed the sacred-harness files, plus the two portfolio contracts whose
# semantics CLAUDE.md pins by name (``self._slots``, ``total_capital``).
SCHEMA_STABLE_PATTERNS: tuple[str, ...] = (
    r"^backtest/cpcv\.py$",
    r"^backtest/cpcv_common\.py$",
    r"^backtest/cpcv_multi\.py$",
    r"^backtest/cpcv_perp\.py$",
    r"^backtest/dsr\.py$",
    r"^backtest/verdict\.py$",
    r"^backtest/engine\.py$",
    r"^backtest/engine_multi\.py$",
    r"^backtest/engine_perp\.py$",
    r"^backtest/engine_cs\.py$",
    r"^backtest/families\.py$",
    r"^strategies/base\.py$",
    r"^portfolio/manager\.py$",
    r"^config\.py$",
)


def classify(rel: str) -> str:
    """Return the tier label for a repo-relative path.

    One of: ``tier1_sacred`` | ``tier2_schema_stable`` | ``tier3_autonomous``.
    Used by the observe / session-end hooks to tag file touches.
    """
    for pat in SACRED_PATTERNS:
        if re.match(pat, rel):
            return "tier1_sacred"
    for pat in SCHEMA_STABLE_PATTERNS:
        if re.match(pat, rel):
            return "tier2_schema_stable"
    return "tier3_autonomous"
