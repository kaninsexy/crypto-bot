# Megaloop status — 2026-09-02 (governance port + Phase 4.F)

Prompt: `docs/megaloop_prompt_2026-09-02.md` (v2).
Mode: MEGALOOP, non-monitor. Model: Opus 5.
Outcome: **HALTED at the S1.5 / S1.6 boundary.** Halt condition 2 — a §5
tripwire fired (5.6, sacred-path edit outside the effective authorization
scope). Escalation: `.memory/_inbox/human_needed.md`, id
`sacred-override-absent-2026-09-02`.

---

## Stage results

| stage | outcome |
|---|---|
| S0 preflight | **green** — pytest 446 passed / 6 skipped; the three `scripts/discovery_*.py --selftest` all exit 0. HEAD was `6a564ec`, i.e. S1.1 already committed, so the run resumed at S1.2 per the prompt's idempotence rule. |
| S1.1 rules layer | **already done** (commit `6a564ec`): CLAUDE.md, `.claude/rules/{backtest,escalation,vertical_slice_loops}.md`, MASTER_PLAN, handoff_template, validation_framework. |
| S1.2 hook layer | **done** — see below. |
| S1.3 sync script | **done** — `scripts/post_commit_sync.sh`. |
| S1.4 backlog discipline | **done** — Mandate L gate, schemas, chained git hooks, seeded backlog. |
| S1.5 agents + validators | **done** — 21/21 agent frontmatter valid; hook-path check added. |
| S1.6 docs | **partial** — `docs/bot_status.md` and the `playbooks_port_plan` supersession landed; `docs/architecture.md` section E is BLOCKED (Tier 1). |
| S2 manifest + loader | **blocked** (Tier 1: `holdout_manifest.json`, `holdout.py`). |
| S3–S7 | **not started** — each depends on S2. |

---

## What the port actually fixed

Two defects, both measured on 2026-09-02, neither previously visible because
nothing exercised the guard layer:

**1. Every bash hook failed open.** `command -v jq` is empty on this machine.
All fifteen bash hooks parsed stdin with `jq`, so each exited 127 — and Claude
Code treats any exit code other than 2 as non-blocking. `sacred-block.sh`
(whose own header says it "absolutely must not be bypassable"),
`no-secrets-in-bash.sh`, `no-deploy.sh` and `path-allowlist.sh` blocked
nothing.

**2. `.githooks/` was never activated.** `.githooks/pre-commit` (sacred diff
block + magic-number gate + `pytest -m fast`) and `.githooks/commit-msg`
(empty-message + `[mandate-H]` gate) both document "Activated via: git config
core.hooksPath .githooks" — but `core.hooksPath` was never set. git ran
`.git/hooks/`, which held only the trial-queue validator.

A third, smaller one: the two sacred lists had drifted. `sacred-block.sh`
guarded `^MASTER_PLAN\.md$`, a path that does not exist in this repo, while
`.githooks/pre-commit` guarded the real `docs/MASTER_PLAN.md`.

## S1.2 — what landed

New Python guards (fail CLOSED on any crash or unreadable input; no external
dependency):

| file | role |
|---|---|
| `file_tiers.py` | the ONE tier map — crypto-bot's sacred + schema-stable lists |
| `path-allowlist.py` | Tier gate on Write/Edit **and** Bash write-targets |
| `no-secrets-in-bash.py` | credential literals + `~/.crypto-bot.env` |
| `no-deploy.py` | live-venue verbs + force-push / branch-delete / `reset --hard main` |
| `commit-guard.py` | mandate G shape + mandate H content (merges two archived hooks) |
| `citation-required.py` | mandate P, now also gating `research/*-literature.md` |
| `curator-write-allowlist.py` | curator's single writable path (renamed to avoid confusion) |
| `provenance-guard.py` | origin × domain gate for background writes |
| `policy-engine.py` + `scripts/policy_engine.py` | the ordering-policy DSL |
| `observe.py` / `session-start.py` / `session-end.py` / `pre-compact.py` / `post-commit-sync.py` | observability + memory trio |

Eight superseded bash hooks moved to `.claude/hooks/_archive_bash_2026-09/`
with a README mapping old → new. Nothing deleted.

**Deviation from the prompt, recorded rather than silent.** S1.2 asked for the
secrets / deploy / heredoc-commit / citation rules to be expressed through
`enforcement_policy.json`. Three of the four are not expressible in the ported
DSL — it matches paths and argv, never command CONTENT, heredoc BODIES (which
it strips as data by design), or `transcript_path`. They were ported as
dedicated Python hooks instead; behaviour preserved, jq fail-open gone. The
table in `.claude/rules/enforcement.md` § "What this layer did NOT absorb"
records each one and why. The policy file carries what the DSL *can* express:
a `tier1-eval-before-commit` ordering gate plus two `notify` rules. There is
deliberately no pytest rule — `effective_exec_target` resolves `python -m
pytest` to the interpreter, so a `**/pytest` pattern can never match, and
pytest is already gated by `.githooks/pre-commit` check 3.

## Harness results (the VERIFY block)

```
python -m pytest backtest/tests data/tests -q   ->  446 passed, 6 skipped
python eval/run_tier1.py                        ->  122/122 passed
python eval/run_tier1.py --self-check           ->  80/122 go RED against
                                                    fail-open stubs  =>  OK
python scripts/validate_backlog.py ...          ->  13 records, all valid
python scripts/validate_agent_frontmatter.py    ->  21/21 valid
python -c "json.load(open('.claude/settings.json'))"  ->  parses
```

`--self-check` exists because of the discriminating-check rule in
`.claude/rules/vertical_slice_loops.md`: it copies the hooks, replaces every
blocking guard with a stub that always exits 0 — literally the shape the
jq-less bash hooks had — and asserts the suite FAILS. A gate that cannot go
red certifies the gap it was meant to catch.

Git-hook gates, proven by running them against a deliberately broken staged
tree rather than by reading the source:

```
staged CLAUDE.md, no CLAUDE_HUMAN_OVERRIDE
  -> bash .git/hooks/pre-commit   EXIT=1
     "BLOCKED: sacred-harness paths in staged diff: CLAUDE.md"

staged file containing "TODO fix later", no backlog append
  -> bash scripts/pre_commit_backlog_check.sh   EXIT=1
     "BLOCKED (GATE_BACKLOG_MISSING_ENTRY)"
```

Both probes were reverted; `git diff HEAD -- CLAUDE.md` is empty.

## Why the run halted

`path-allowlist.py` reads `SACRED_OVERRIDE_FILES` from its own process
environment, inherited from the Claude Code process. This session was not
started from the prompt's Git Bash launch line, so the variable is unset
(`CLAUDE_HUMAN_OVERRIDE` too). The prompt's AUTONOMY block *does* pre-authorize
`docs/architecture.md`, `backtest/holdout_manifest.json` and
`backtest/holdout.py` — CLAUDE.md's "Pre-authorization exception" accepts a
prompt-side grant — but the transport that carries it to the hook is missing,
and an agent must not be able to supply it from inside the session. A guard
that reads its authorization from anywhere the agent can write is not a guard.

So: authorization present, mechanism absent, and the mechanism is exactly the
thing that must not be agent-writable. That is a halt, not a judgement call.

S1.2 also made `schemas/*.schema.json` Tier 1, which the AUTONOMY list
predates — so `schemas/` needs adding to the scope on relaunch.

## Open items raised (all in `.memory/T2_semantic/backlog.jsonl`)

| id | sev | item |
|---|---|---|
| BK-0011 | high | **Regression I introduced and cannot fix**: the Resend pattern `re_[a-zA-Z0-9_-]{20,}` in `no-secrets-in-bash.py` has no left boundary and matches inside `scripts/p·re_commit_backlog_check.sh`. Any Bash command naming that script is blocked. Commits are unaffected. One-line fix. |
| BK-0012 | medium | `schemas/agent_frontmatter.schema.json` is still siamese's verbatim copy and disagrees with the re-fitted validator. |
| BK-0013 | critical | S1.6/S2–S7 blocked on `SACRED_OVERRIDE_FILES` (this document). |
| BK-0001..BK-0010 | — | seeded from `docs/open_questions.md` OPEN items. |

## Resuming

`.memory/_inbox/human_needed.md` carries the exact relaunch line. S0–S1.5 are
committed and verified; resume at S1.6. Fix BK-0011 first — one line, removes
a live papercut.

No trial ran. `backtest/trials.log` is untouched, `load_holdout` was never
called, no ledger row was written, `paper_mode` is unchanged.
