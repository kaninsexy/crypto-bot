# Playbooks → crypto-bot port plan

> **SUPERSEDED 2026-09-02** by the siamese-reconcile governance port
> (commits `6a564ec` S1.1 rules layer, and the S1.2–S1.5 guard/backlog layer).
> Chunks 1–7 of this plan were never executed. In the intervening two months
> `siamese-reconcile` matured the *same* components past what this plan
> describes and pinned them with an eval harness, so the port was taken from
> there (HEAD `2f13045`) rather than from the 2026-07-03 playbooks snapshot.
>
> What landed instead, and where to read it now:
>
> | this plan's chunk | superseded by |
> |---|---|
> | guard hooks (jq → Python) | `.claude/hooks/*.py`, old→new map in `.claude/hooks/_archive_bash_2026-09/README.md` |
> | single tier map | `.claude/hooks/file_tiers.py` |
> | eval harness | `eval/run_tier1.py` + `eval/fixtures/hooks/*.jsonl` |
> | schemas | `schemas/backlog.schema.json`, `schemas/agent_frontmatter.schema.json` |
> | backlog (Mandate L) | `.memory/T2_semantic/backlog.jsonl`, `scripts/pre_commit_backlog_check.sh`, `scripts/post_commit_verify.sh`, `scripts/install_git_hooks.sh` |
> | policy / enforcement layer | `.claude/rules/enforcement.md`, `enforcement_policy.json`, `policies/*.policy` |
>
> Kept unedited below for provenance: the four drift bugs it identified were
> real, and three of them are the same defects the port fixed (jq fail-open,
> duplicated sacred lists, unwired `sacred-block.sh`).

Date: 2026-07-03
Author: Chat (planning session)
Status: **SUPERSEDED** (was: planning complete; execution routes to Claude Code, chunk by chunk)
Repo HEAD at planning time: `d6b471f`

> **What this is.** crypto-bot forked from the `playbooks` starter template
> early and has diverged. Playbooks has since matured the shared control-plane
> patterns (guard hooks, memory/curator, eval, schemas, backlog, security
> audit). This plan ports the *improvements* into crypto-bot — copying what
> already works in playbooks, not inventing new features — and fixes four live
> drift bugs the audit surfaced. Each chunk below is one reviewable Claude Code
> prompt = one commit. **Chat plans; Claude Code implements.**

> **Scope discipline.** We do NOT replace crypto-bot's bash hook layer with
> playbooks' Python hooks. crypto-bot's bash hooks are wired, working, and
> referenced throughout CLAUDE.md/architecture.md; wholesale replacement is the
> risky rewire we are avoiding. We port the *fixes and additions* into bash to
> match the existing style.

---

## 1. Live drift bugs found (strongest reason to port)

1. **Dangling reference.** 5 agent files (`curator.md`, `implementer.md`,
   `research-manager.md`, `strategist.md`, `_research_manager_prompt.txt`) read
   `.memory/T2_semantic/decisions_log.jsonl` — **the file was never created**.
   The curator's retired-strategy cooldown step reads a nonexistent file.
2. **Sacred regex drifted.** `.claude/hooks/sacred-block.sh` and
   `.githooks/pre-commit` each hardcode the sacred-file regex with *different
   anchoring*. They must agree and provably don't. Playbooks centralized exactly
   this into `file_tiers.py` after hitting the same bug.
3. **`jq` fail-open (15 hooks).** Every guard does `jq -r '.tool_input…'`. On a
   jq-less shell (Windows Git-Bash), `jq` exits 127 and **the guard silently
   passes** — fail-open. crypto-bot already learned this once
   (`commit-scope-audit.sh` uses Python "because jq isn't always available") but
   never propagated the fix.
4. **`sacred-block.sh` doesn't scan Bash.** An agent can `echo x > CLAUDE.md` in
   a Bash call and bypass the PreToolUse sacred gate entirely (only caught at
   commit, and only if `.githooks` are installed). Playbooks' `path-allowlist.py`
   scans Bash write-targets.

---

## 2. Port map summary

| # | Component (playbooks) | crypto-bot today | Call | Sacred? |
|---|---|---|---|---|
| Chunk 1 | Centralized sacred regex + jq-fail-open fix + seed `decisions_log.jsonl` | older bash, drifted; file missing | **PORT (bash)** | No |
| Chunk 2 | `schemas/agent_frontmatter.schema.json` + `validate_agent_frontmatter.py` | 21 agents, no validation | **PORT (adapt fields)** | No |
| Chunk 3 | `eval/run_tier1.py` + JSONL fixtures | ad-hoc `_test_hooks.sh` | **PORT (adapt)** | No |
| Chunk 4 | `.claude/skills/security-scan/SKILL.md` (offline fallback) | none | **PORT** | No (reads only) |
| Chunk 5 | `memory_decay.py` + `_fact_health.jsonl` + `## Suspect` + `fact_anchors.py` | facts never re-verified for staleness | **PORT (scripts)** | No |
| Chunk 6 | Bash write-target scan + PreCompact snapshot hook | bypass hole; no compaction guard | **PORT** | **Yes — settings.json** |
| Chunk 7 | Backlog discipline + Mandate L | no backlog; loose `open_questions.md` | **PORT (adapt/merge)** | **Yes — CLAUDE.md, T3, commit chain** |
| Chunk 8 | `session_search.py`, `.claude/contexts/`, `frozen_snapshot.py`, `background_review.py`, notifier local fallback | missing / older | **ADAPT (follow-on)** | Mixed |

### Explicitly skipped (do not port)

- **crypto-bot is ahead:** `run_trial_queue.py` (parallel ProcessPool + cross-platform
  locking + Resend escalation + stale-lock PID recovery), the 21-agent specialized
  fleet, and worktree isolation all exceed the playbooks template.
- **Domain misfit:** all `vision_verify_*` scripts, `policy_engine.py`,
  `cc_warm_pool.py`, `shared_context_cache.py`, `e2e_harness.py`,
  `lifecycle_guard.py` — generic UI / self-driving-loop infra that doesn't map to
  the backtest-CPCV domain, where deterministic CPCV/DSR already **is** the
  fail-closed empirical gate.

### Dependency / sequencing

Chunks 1–5 are independent and agent-autonomous → run in any order, or batch.
Chunk 6 and 7 require a `SACRED_OVERRIDE_FILES` pre-authorization on the CC
invocation (they edit `settings.json` / `CLAUDE.md` / `.memory/T3_procedural/`).
Chunk 8 is a follow-on after 1–7 land. Recommended order: **1 → 2 → 3 → 4 → 5 →
6 → 7 → 8.**

---

## 3. Claude Code prompts (paste-ready, one commit each)

> Every prompt below already carries: the evidence-read opener, verbatim
> constraints, "proceed without asking" autonomy framing, a verification step,
> and the commit-and-stop-short-of-push instruction (mandate G). Prompts that
> edit a sacred file include the `SACRED_OVERRIDE_FILES` invocation line and an
> AUTONOMY block per the CLAUDE.md pre-authorization exception.

---

### Chunk 1 — Fix the four hook-layer drift bugs (autonomous)

```
Read repomix-output.xml with bash_tool as your FIRST action, then CLAUDE.md,
then .claude/rules/communication.md. Confirm when done.

TASK: Fix hook-layer drift ported from the playbooks template. Four sub-fixes,
one commit.

1. Seed the missing decisions_log ledger. Create
   `.memory/T2_semantic/decisions_log.jsonl` as an empty (0-byte or single
   header-comment) append-only file. It is referenced by curator.md,
   implementer.md, research-manager.md, strategist.md, and
   _research_manager_prompt.txt but was never created — the curator's
   retired-strategy cooldown step currently reads a nonexistent file. Do not
   change the agents; just create the file they already expect.

2. Centralize the sacred-file regex. `.claude/hooks/sacred-block.sh` and
   `.githooks/pre-commit` each hardcode the sacred-file regex with DIFFERENT
   anchoring — they must agree and don't. Create ONE shared source of truth
   (e.g. `.claude/hooks/sacred_paths.sh` exporting a `SACRED_PATHS_RE` var) and
   have BOTH files source it. The unified regex must be the UNION of both
   current lists (do not drop any path either currently guards). This mirrors
   playbooks' file_tiers.py centralization.

3. Kill the jq fail-open. 14 hooks + _test_hooks.sh use `jq -r '.tool_input…'`.
   On a jq-less shell jq exits 127 and the guard SILENTLY PASSES (fail-open).
   crypto-bot already solved this once — commit-scope-audit.sh parses stdin with
   `python` "because jq is not always available on Windows Git-Bash". Propagate
   that pattern: add a jq-or-python fallback (or a shared helper the hooks
   source) so a missing jq FAILS CLOSED, not open. Apply to every hook that
   currently pipes stdin through jq.

4. Close the Bash sacred-write bypass. sacred-block.sh only inspects Write/Edit
   file_path, so `echo x > CLAUDE.md` in a Bash call bypasses it. Extend
   sacred-block.sh (or add a Bash-matcher helper it calls) to scan Bash command
   strings for write redirections to sacred paths (`>`, `>>`, `tee`, `sed -i`,
   `cp`, `mv`, `python - <<`/`open(...,'w')`) using the centralized regex from
   step 2, stripping heredoc bodies first. NOTE: wiring a new Bash-matcher hook
   into settings.json is Chunk 6 (sacred, separate). For THIS chunk, only add
   the scanning logic to the already-wired sacred-block.sh path; do not edit
   settings.json.

CONSTRAINTS (verbatim from CLAUDE.md / rules):
- Sacred-harness files (trials.log, holdout_manifest.json, holdout_access.log,
  holdout.py schema) and sacred-harness docs (CLAUDE.md, MASTER_PLAN.md,
  architecture.md, validation_framework.md): DO NOT EDIT. This chunk touches
  none of them.
- Archive by default, delete only with approval. You are creating/editing hooks,
  not deleting.
- Runnable artifacts only; bundle independent shell commands into one block.

AUTONOMY: Proceed without asking for any action covered by CLAUDE.md "Agent
decides" (fix tooling/infrastructure/hooks). Do not edit settings.json,
CLAUDE.md, or any sacred file. Commit autonomously with a heredoc message; STOP
short of git push.

VERIFY before committing:
- `bash .claude/hooks/_test_hooks.sh` passes (update it if the jq→python change
  alters expected stdin handling; add fixtures, do not weaken assertions).
- Prove fail-closed: temporarily shadow jq (`PATH=/nonexistent-shim` or a stub
  that exits 127) and confirm a sacred-write payload is BLOCKED, not allowed.
- Prove the Bash bypass is closed: feed sacred-block.sh a Bash payload doing
  `echo x > CLAUDE.md` and confirm exit code 2 (block).
- Confirm `.githooks/pre-commit` and sacred-block.sh now source the same regex
  file and both still block a staged CLAUDE.md edit.

Commit message (heredoc) should record: the four fixes, that the regex is now
single-source, and that jq no longer fails open.
```

---

### Chunk 2 — Agent-frontmatter schema + validator (autonomous)

```
Read repomix-output.xml with bash_tool as your FIRST action, then CLAUDE.md.
Confirm when done.

TASK: Port playbooks' agent-frontmatter contract enforcement, ADAPTED to
crypto-bot's actual frontmatter fields. crypto-bot has 21 agents in
.claude/agents/*.md with load-bearing frontmatter and NO validator.

1. Read 4-5 representative agent files (curator.md, implementer.md,
   strategist.md, phase4b-coordinator.md, adversarial-reviewer.md) to catalog
   the REAL field set in use: name, description, model, tools, disallowedTools,
   permissionMode, maxTurns, isolation, hooks, parent, skills, memory, etc. Do
   NOT copy playbooks' schema fields (inputs/outputs/cost_guardrails) blindly —
   crypto-bot's fields differ. Base the schema on what crypto-bot actually uses.

2. Create `schemas/agent_frontmatter.schema.json` (draft-07) encoding the
   observed contract: required fields, a `model` enum matching the models
   crypto-bot uses, allowed `permissionMode`/`isolation` values, type checks.
   Make genuinely-optional fields optional so existing agents aren't forced to
   change unless they're actually malformed.

3. Port `scripts/validate_agent_frontmatter.py` from
   playbooks/template/scripts/, adapting it to load the crypto-bot schema and
   iterate .claude/agents/*.md. Keep its PyYAML-only fallback if jsonschema is
   unavailable. Keep the "YAML `: ` / `{ }` parses-as-map" pitfall check.

4. Run the validator against all 21 agents. REPORT failures in your final
   message — do NOT mass-rewrite agent bodies to force conformance (some fields
   are behaviorally load-bearing). If an agent fails only on a trivially-missing
   optional field, note it; leave substantive fixes for human review.

CONSTRAINTS (verbatim):
- Do not edit sacred files (CLAUDE.md, .claude/rules/*, docs/MASTER_PLAN.md,
  architecture.md, validation_framework.md, sacred-harness files).
- schemas/ and scripts/ are new/agent-editable. Adding them is agent-autonomous.
- Do not alter agent behavior; the validator observes, it does not fix.

AUTONOMY: Proceed without asking. Commit the schema + validator autonomously
with a heredoc message; STOP short of push. Do NOT commit any agent-file edits —
surface needed frontmatter fixes as a list for human review instead.

VERIFY: `python scripts/validate_agent_frontmatter.py` runs and exits cleanly on
conforming agents; failures (if any) are listed with agent name + field.
```

---

### Chunk 3 — Tier-1 eval harness for the hooks (autonomous)

```
Read repomix-output.xml with bash_tool as your FIRST action, then CLAUDE.md,
then .claude/hooks/_test_hooks.sh. Confirm when done.

TASK: Port playbooks' Tier-1 gate-fixture eval harness, migrating crypto-bot's
existing ad-hoc bash hook tests into declarative fixtures with ambient-env
isolation.

1. Read playbooks/template/eval/run_tier1.py and eval/fixtures/*.jsonl to learn
   the fixture format (hook + stdin payload + expected exit code + eval_type
   tag) and the ambient-env isolation (it strips SACRED_OVERRIDE_FILES,
   CLAUDE_WRITE_ORIGIN, etc. so the launching shell can't silently flip a
   block-fixture to allow — a real correctness bug crypto-bot's bash tests have,
   since crypto-bot USES SACRED_OVERRIDE_FILES).

2. Create `eval/run_tier1.py` + `eval/fixtures/` adapted to crypto-bot's bash
   hooks. Migrate every case currently in `.claude/hooks/_test_hooks.sh` into
   JSONL fixtures: failcount-update, sacred-block (Write/Edit AND the new Bash
   scan from Chunk 1), no-secrets-in-bash, path-allowlist, no-deploy,
   citation-required, commit-format. One fixture per veto category + clean
   passes. Tag each checkpoint vs continuous.

3. Keep _test_hooks.sh working (or make run_tier1.py the canonical runner and
   have _test_hooks.sh call it) — do not lose coverage in the migration.

CONSTRAINTS (verbatim):
- Do not edit sacred files. eval/ is a new dir; adding it is agent-autonomous.
- The hooks under test are NOT modified by this chunk.
- Discipline to encode in a README/docstring: "when a gate regresses, add a
  fixture that would have caught it BEFORE fixing the gate."

AUTONOMY: Proceed without asking. Commit autonomously with heredoc; STOP short
of push.

VERIFY: `python eval/run_tier1.py` exits 0 with all fixtures green; prove
ambient-env isolation by exporting SACRED_OVERRIDE_FILES=CLAUDE.md and
confirming a sacred-block fixture STILL blocks under the harness.
```

---

### Chunk 4 — security-scan skill (autonomous)

```
Read repomix-output.xml with bash_tool as your FIRST action, then CLAUDE.md,
then .claude/settings.json and .claude/settings.local.json. Confirm when done.

TASK: Port playbooks' repeatable harness self-audit as a skill. crypto-bot has
real attack surface (Resend/OKX/Anthropic/OpenRouter keys in ~/.crypto-bot.env,
settings.json, 21 agent bodies) and NO repeatable config audit.

1. Read playbooks/template/.claude/skills/security-scan/SKILL.md. Port it to
   `.claude/skills/security-scan/SKILL.md`, prioritizing the OFFLINE
   DETERMINISTIC FALLBACK (crypto-bot may not have the npx ecc-agentshield tool):
   - secret sweep: `git grep` over TRACKED files for key shapes + env-file reads
     (reuse the patterns already in no-secrets-in-bash.sh so they stay in sync).
   - permissions audit: report `.claude/settings.json` permissions.allow
     (currently empty `{}` — confirm that's intentional, not a gap).
   - sacred-map review: list the sacred paths from the centralized regex
     (Chunk 1) and confirm the hooks reference them.
   - hook-injection sweep: flag any hook command doing unquoted `$(...)` on
     tool-input.
   - agent-config review: flag agents with permissionMode bypassing gates.
2. Keep the npx AgentShield path as an optional upgrade, documented, not required.

CONSTRAINTS (verbatim):
- The audit READS sacred files (CLAUDE.md, settings.json, rules) but MUST NOT
  edit them. It reports; it does not fix.
- Adding a skill under .claude/skills/ is agent-autonomous.

AUTONOMY: Proceed without asking. Commit autonomously with heredoc; STOP short
of push.

VERIFY: run the skill's offline fallback end-to-end; confirm it finds zero
tracked secrets, prints the (empty) permissions.allow with the "intentional?"
note, and exits non-zero ONLY if a real finding exists.
```

---

### Chunk 5 — Memory staleness: decay + anchors + Suspect (autonomous scripts)

```
Read repomix-output.xml with bash_tool as your FIRST action, then CLAUDE.md,
then .claude/agents/curator.md and .memory/T2_semantic/facts.md. Confirm when
done.

TASK: Port playbooks' fact-staleness machinery as standalone scripts + a facts.md
convention. crypto-bot promotes facts with ZERO re-verification — a stale/wrong
fact lives forever, which matters in a money/compliance domain.

1. Port `scripts/memory_decay.py` + `schemas/fact-health.schema.json` from
   playbooks/template. Seed `.memory/T2_semantic/_fact_health.jsonl`. Keep the
   pure-function age transitions (active→stale at 30d→archived at 90d),
   confidence reinforcement, archive-not-delete with `absorbed_into` pointer,
   and DRY-RUN default. Must be backward-compatible: records with no confidence
   fields decay on age only.
2. Port `scripts/fact_anchors.py` (content-hash anchors) — adapt the anchor
   targets to crypto-bot harness modules that facts commonly cite
   (backtest/cpcv.py, backtest/dsr.py, backtest/verdict.py, portfolio/*). A fact
   anchored to a file that changed gets labeled `[drifted]`; a vanished file →
   `[orphaned]`.
3. Add a `## Suspect` section convention to `.memory/T2_semantic/facts.md`
   (facts.md is agent-editable): decay-prone / un-reverifiable facts get parked
   there; document that handoff authors scan `## Suspect` before relying on a
   fact. Add the section header + a one-line explainer; do not invent facts.

NOTE — do NOT wire decay INTO the curator loop in this chunk. That edits
curator.md and references architecture.md (sacred) and needs separate
pre-authorization. This chunk ships the scripts + convention only; wiring is a
follow-on.

CONSTRAINTS (verbatim):
- Do not edit curator.md, architecture.md, or any sacred file this chunk.
- Archive by default, delete only with approval — memory_decay archives, never
  deletes.
- scripts/, schemas/, _fact_health.jsonl, and facts.md are agent-editable.

AUTONOMY: Proceed without asking. Commit autonomously with heredoc; STOP short
of push.

VERIFY: `python scripts/memory_decay.py --dry-run` runs clean on the seeded
health file; `python scripts/fact_anchors.py` computes anchors for at least one
real fact citing a harness module; add + run a small unit test (mirror
playbooks' test_memory_decay.py / test_fact_anchors.py) and confirm it passes.
```

---

### Chunk 6 — Bash-scan wiring + PreCompact snapshot (SACRED: settings.json)

```
INVOCATION (run CC with the sacred override for this scope):
  SACRED_OVERRIDE_FILES=".claude/settings.json" claude   # (or your CC launcher)

Read repomix-output.xml with bash_tool as your FIRST action, then CLAUDE.md,
then .claude/settings.json. Confirm when done.

AUTONOMY (pre-authorization): Kanin pre-authorizes edits to
`.claude/settings.json` for this prompt scope ONLY, to wire two hooks. No other
sacred file may be edited. This IS the human-in-the-loop the sacred rule
protects (review-before-commit).

TASK:
1. Wire the Bash-matcher sacred-scan built in Chunk 1 as a PreToolUse Bash hook
   in settings.json, so `echo x > CLAUDE.md` is blocked at tool-call time, not
   only at commit. Add it alongside the existing PreToolUse wiring; do not remove
   or reorder existing hooks.
2. Port a PreCompact snapshot hook (bash, matching crypto-bot style — model it
   on flush-T1.sh + playbooks' pre-compact.py). It flushes a T1 state snapshot
   to .memory/T1_episodic/_state/ before context compaction. crypto-bot
   currently wires NO PreCompact and NO SessionStart event (only UserPromptSubmit
   + PreToolUse), so in-flight state is lost on compaction with no marker. Wire
   the PreCompact event in settings.json.

CONSTRAINTS (verbatim):
- Only settings.json is pre-authorized. Do NOT edit CLAUDE.md, rules, T3, or
  sacred-harness files.
- Do not weaken or remove any existing hook wiring.
- Runnable artifacts only.

VERIFY: `python3 -c "import json;json.load(open('.claude/settings.json'))"`
parses; the Bash sacred-write payload is blocked at PreToolUse; the PreCompact
hook runs and writes a snapshot when invoked manually; run eval/run_tier1.py
(Chunk 3) — still green.

Commit autonomously with a heredoc message noting the settings.json wiring was
pre-authorized in scope; STOP short of push.
```

---

### Chunk 7 — Backlog discipline + Mandate L (SACRED: CLAUDE.md, T3, commit chain)

```
INVOCATION:
  SACRED_OVERRIDE_FILES="CLAUDE.md,.memory/T3_procedural/mandates.md" claude

Read repomix-output.xml with bash_tool as your FIRST action, then CLAUDE.md,
then .memory/T3_procedural/ and the existing commit hooks
(.claude/hooks/commit-heredoc-required.sh, commit-scope-audit.sh,
.githooks/pre-commit). Confirm when done.

AUTONOMY (pre-authorization): Kanin pre-authorizes, for this prompt scope only,
adding a new mandate to `CLAUDE.md` and `.memory/T3_procedural/` (Mandate L —
backlog discipline). No other sacred edits. Review-before-commit is the
human-in-the-loop.

TASK: Port playbooks' backlog discipline so surfaced gaps stop depending on
handoff-doc attention.
1. Create `.memory/T2_semantic/backlog.jsonl` (append-only, BK-NNNN records) +
   `schemas/backlog.schema.json` (port + adapt from playbooks/template).
2. Port `scripts/pre_commit_backlog_check.sh`: scan staged diffs for surfacing
   keywords (TODO/FIXME/"defer"/"later"/"gap") and BLOCK the commit if no
   matching backlog append is staged. It MUST MERGE INTO crypto-bot's existing
   commit-hook chain (commit-heredoc-required.sh, sacred-block.sh,
   commit-scope-audit.sh, .githooks/pre-commit) — chain it, do not replace any
   existing gate.
3. Add "Mandate L — backlog discipline" to CLAUDE.md's behavioral mandates and
   mirror it in .memory/T3_procedural/ (short). Migrate the existing loose items
   from docs/open_questions.md into backlog.jsonl as the initial BK records.

CONSTRAINTS (verbatim):
- Only CLAUDE.md + .memory/T3_procedural + new backlog/schema/hook files are in
  scope. Do NOT touch MASTER_PLAN.md, architecture.md, validation_framework.md,
  or sacred-harness files.
- Do not replace or weaken any existing commit gate — additive chaining only.
- Trial intentionality boundary is at push, not commit (mandate G): commit
  autonomously, STOP short of push.

VERIFY: stage a diff containing "TODO" with NO backlog append → commit BLOCKED;
add the BK append → commit ALLOWED; run _test_hooks.sh and eval/run_tier1.py —
both green; confirm the heredoc + sacred + scope gates still fire.

Commit autonomously with a heredoc message noting the CLAUDE.md/T3 edits were
pre-authorized in scope; STOP short of push.
```

---

### Chunk 8 — Follow-ons (adapt after 1–7 land)

Lower-priority ports, each its own future CC prompt when the earlier chunks are
stable:

- **`session_search.py` recall** (FTS5/BM25 over T1 episodes; drop the Thai
  trigram half; derive queries from `research_queue.md` instead of backlog). Wire
  into a SessionStart hook (sacred — settings.json).
- **`.claude/contexts/{dev,review,research}.md`** dynamic per-mode system-prompt
  injection — maps to crypto-bot's trial-run / research / audit modes. Cheap,
  non-sacred dir; the invocation alias is documentation.
- **`frozen_snapshot.py`** — freeze in-prompt memory at session start so
  mid-session `_pending_review` writes don't invalidate the prompt-prefix cache.
  Token-cost win; wiring touches the injection path (care near T3).
- **`background_review.py`** — cadence-driven, provenance-guarded curator fork
  writing schema-validated candidates to `_pending_review.jsonl`. Adapt to
  crypto-bot's curator/strategist split; touches curator.md + architecture.md
  (sacred).
- **notifier local fallback** — append halts to `.memory/_inbox/human_needed.md`
  beneath the existing Resend path so a halt is never lost if Resend is down.
  Non-sacred.

---

## 4. Notes for the human

- Chunks 1–5 are fully agent-autonomous (no sacred edits) — safe to run back to
  back and review as five commits.
- Chunks 6 and 7 each need the `SACRED_OVERRIDE_FILES` line shown; without it
  the sacred-block hook will (correctly) refuse the edit.
- Nothing in this plan replaces crypto-bot's working bash hooks, trial-queue
  orchestrator, agent fleet, or worktree isolation — those are where crypto-bot
  already leads the template.
