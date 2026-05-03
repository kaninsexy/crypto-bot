# Sacred-harness — Mandate S

## Rule
Two tiers of files have hard edit restrictions. Sacred-harness
(never edit) and schema-stable code (edit cautiously, contract-
preserving only).

## Tier 1: Never edit (runtime artifacts + meta-docs)
Changes here break the multiple-testing correction or the holdout
single-access guarantee.

- `backtest/trials.log` — every backtest, every parameter variation,
  every exploratory test appends a row here. The file is gitignored
  (the local file IS the audit trail; never committed). Schema is
  defined by `trials.py`'s writer; do not write directly.
- `backtest/holdout_manifest.json` — substrate truth: per-strategy
  timeframe, symbol/symbols/legs, dev/holdout boundaries.
- `backtest/holdout_access.log` — gitignored single-access audit.
- The schema fields of `backtest/holdout.py` (the data contract,
  not the implementation).
- `CLAUDE.md`, `docs/MASTER_PLAN.md`, `docs/validation_framework.md`.

## Tier 2: Schema-stable code (edit cautiously)
Bug fixes that preserve function contracts (signatures, return shapes,
schema fields) are agent-autonomous. Contract changes require human
approval.

- `backtest/cpcv.py`, `backtest/dsr.py`, `backtest/verdict.py`,
  `backtest/engine.py`, `backtest/trials.py`, `backtest/holdout.py`
  (implementation; schema is sacred per Tier 1).

## Pre-authorization exception
Claude Code may edit Tier 1 meta-docs (CLAUDE.md, MASTER_PLAN.md,
validation_framework.md) when the user explicitly pre-authorizes the
edit in the prompt's AUTONOMY section (e.g., "Kanin pre-authorizes
edits to docs/MASTER_PLAN.md for this prompt scope"). The auto-accept-
edits review-before-commit workflow IS the human-in-the-loop the rule
protects. Without explicit pre-authorization, the default Human-only
rule applies and the agent must refuse the edit and surface the
restriction.

Tier 1 runtime artifacts (trials.log, holdout_manifest.json,
holdout_access.log, holdout.py schema) cannot be unlocked by
pre-authorization — schema changes require human-only review per
core principles.

## Promotion into T3 procedural
Promotion happens via human edit + git commit. Agents cannot promote
into T3 even with approval. Agents may **propose** T3 promotions via
`.memory/_proposals/T3_promotion_<id>.md`.

## Enforcement
- Layer 1 (advisory): this file + CLAUDE.md "Core principles" section.
- Layer 2 (deterministic): `sacred-block.sh` PreToolUse hook on
  Edit|Write — blocks any path matching the sacred regex.
- Layer 4 (commit gate): `pre-commit` reads staged diff; if any sacred
  file modified AND commit author is agent identity, abort.

See also: CLAUDE.md "Core principles"; architecture.md §A.3, §E.1
mandate S, §E.2 sacred-block.sh.
