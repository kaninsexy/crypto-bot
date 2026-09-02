# Archived bash hooks — superseded 2026-09-02 by the Python guard layer

Nothing here is deleted (CLAUDE.md: **archive by default, delete only with
approval**). These files are the pre-port originals, kept so the diff between
old and new behaviour stays readable.

## Why they were replaced

Two independent defects, both measured on 2026-09-02:

1. **They were not running.** Every file in this directory parsed its stdin
   with `jq`. `jq` is **not installed** on this machine (`command -v jq` →
   empty). A hook that cannot find its interpreter exits 127, and Claude Code
   treats any exit code other than 2 as non-blocking. So each of these guards
   **failed open on every call** — including `sacred-block.sh`, the one hook
   whose own header says it "absolutely must not be bypassable".
2. **They had already drifted.** `sacred-block.sh` guarded `^MASTER_PLAN\.md$`,
   a path that does not exist in this repo (the real file is
   `docs/MASTER_PLAN.md`, which `.githooks/pre-commit` guarded correctly). Two
   copies of one sacred list, disagreeing, neither enforcing.

The Python layer removes the `jq` dependency, **fails closed** on any crash or
unreadable input, and keeps the tier map in exactly one place
(`.claude/hooks/file_tiers.py`).

## Old → new map

| archived bash hook | replaced by | notes |
|---|---|---|
| `sacred-block.sh` | `path-allowlist.py` | Tier-1 block, now with the correct `docs/MASTER_PLAN.md` path and a `SACRED_OVERRIDE_FILES` pre-authorization path (CLAUDE.md "Pre-authorization exception"). Adds Tier-2 NOTEs and a Bash write-target scan the bash version never had. |
| `path-allowlist.sh` | `curator-write-allowlist.py` | **Renamed** so it is not confused with the new repo-wide `path-allowlist.py`. Same single-path curator allowlist, plus `.memory/T1_episodic/**` for the curator's own episodes. |
| `no-secrets-in-bash.sh` | `no-secrets-in-bash.py` | Pattern set is the union of the bash list and siamese's, plus `~/.crypto-bot.env`. Keeps the `${VAR}`-stripping behaviour so legitimate env-var use never trips. |
| `no-deploy.sh` | `no-deploy.py` | Split into an always-on set (doctl/kubectl/dokku/docker push/digitalocean + force-push, branch delete, `reset --hard main`) wired globally, and a `--strict` set (`ssh`, bare `deploy`) kept agent-scoped, because those two false-block ordinary work. The bash version ran only inside three agent frontmatters, leaving the main session unguarded. |
| `commit-heredoc-required.sh` | `commit-guard.py` | Merged — shape half. |
| `commit-format.sh` | `commit-guard.py` | Merged — content half (conventional subject + `[mandate-H]` on `Co-authored-by: Claude` commits). They always fired together on the same matcher parsing the same field. Subject-type list extended with the types this repo actually uses: `trials`, `governance`, `data`, `retire`, `discovery`, `harness`. |
| `citation-required.sh` | `citation-required.py` | Mandate P. Now also gates `research/*-literature.md` edits, not just WebFetch/WebSearch. |
| `_test_hooks.sh` | `eval/run_tier1.py` + `eval/fixtures/hooks/*.jsonl` | The bash test harness exercised the archived hooks. Its replacement is fixture-driven and is itself pinned by a discrimination check (`--hooks-dir` pointed at a deliberately broken copy must go RED). |

## Bash hooks that were KEPT (not archived)

These have no Python equivalent and stay wired as-is:

`inject-mandates.sh` (UserPromptSubmit, no jq), `commit-scope-audit.sh`
(PreToolUse Bash, parses JSON with `python`, no jq),
`pre-commit-queue-validate.sh` (git pre-commit, no jq),
`regime-change-trigger.sh`, `failcount-check.sh`, `failcount-update.sh`,
`flush-T1.sh`, `run-tests-fast.sh`, `budget-check.sh`, `exit-ramp-check.sh`.

**Caveat, recorded rather than hidden:** six of those kept hooks
(`failcount-check`, `failcount-update`, `flush-T1`, `run-tests-fast`,
`budget-check`, `exit-ramp-check`) still use `jq` and therefore still fail
open on this machine. They are advisory / observability hooks bound to agent
frontmatter, not security gates, so they were out of scope for this port —
tracked as a backlog item (Mandate L) rather than left unmentioned.

## Restoring one

They are ordinary files; `git mv` one back up a directory and re-wire it in
`.claude/settings.json`. Install `jq` first, or it will fail open exactly as
before.
