# enforcement.md — the finding-policy map + the policy DSL gate

Sacred file (Tier 1; human-only edits, or an authorized `SACRED_OVERRIDE_FILES`
scope). Read by any agent or hook that PRODUCES a finding for the gate layer,
and by any planner wiring a new check.

Ported 2026-09-02 from siamese-reconcile `.claude/rules/enforcement.md`
(HEAD 2f13045) and re-targeted at crypto-bot's gates. Companion code:
`scripts/enforcement_policy.py` (the map), the machine-readable map
`.claude/rules/enforcement_policy.json`, `scripts/policy_engine.py` (the
engine) + `.claude/hooks/policy-engine.py` (the adapter). Re-implemented
(clean-room) from OpenLore's `enforcement-policy.ts` (MIT) and ActPlane's
policy DSL (MIT) via the playbooks template; no source copied.

## Why this exists

Before this port, every crypto-bot hook decided block/allow on its own
hardcoded logic, in bash, parsing stdin with `jq`. Two consequences, both
measured on 2026-09-02:

1. **They were not running.** `jq` is not installed on this machine. Every one
   of the fifteen jq-based hooks exited 127, which Claude Code treats as
   non-blocking — so `sacred-block.sh`, `no-secrets-in-bash.sh`, `no-deploy.sh`
   and `path-allowlist.sh` all FAILED OPEN on every call.
2. **They had already drifted.** `sacred-block.sh` guarded `^MASTER_PLAN\.md$`
   — a path that does not exist; the real file is `docs/MASTER_PLAN.md`, which
   `.githooks/pre-commit` guarded correctly. Two copies of one list, out of
   agreement, neither of them running.

The Python layer fixes (1) by removing the dependency and failing CLOSED, and
(2) by putting the tier map in exactly one place (`.claude/hooks/file_tiers.py`).
This file + its JSON are the surface that says which finding codes block and
which merely warn.

## The decoupling (severity is not class)

- A check's intrinsic **severity** ("this looks high-risk") is owned by the
  check. It is informational to the gate.
- A finding's enforcement **class** — `blocking` | `advisory` | `off` — is
  owned by repo config (`enforcement_policy.json`). The gate consumes the
  class.

## Advisory by default (blocking is opt-in)

Every registered code's SOURCE default is `advisory` **except**
`POLICY_RULE_BLOCK` — a policy `block` rule that fired IS the opt-in to
blocking, so its source default is `blocking`.

Two safety degradations, both to an advisory caveat, never a block:

- a finding whose producer **raised** (carries a truthy `error`) — a crashing
  check cannot wedge the gate;
- a finding whose `code` is **unregistered** — an unknown code never
  hard-blocks (a repo may still silence it with an explicit `off`).

## Precedence (`resolve_class`)

```
off  >  blocking  >  advisory  >  source-default
```

An explicit, valid class in `enforcement_policy.json` wins over the registry
source default; an absent/invalid explicit value falls through to the source
default; an unknown code falls through to the global `advisory` default.
`gated == (len(blocking) > 0)` — only blocking findings gate.

## Registered codes

Every code below appears in `FINDING_REGISTRY` (`enforcement_policy.py`) AND
in the `policy` block of `enforcement_policy.json`.

| code | source default | meaning |
|---|---|---|
| `POLICY_RULE_BLOCK` | blocking | A policy-engine `block` rule matched a tool call. The block verb is the opt-in to blocking. |
| `POLICY_RULE_NOTIFY` | advisory | A policy-engine `notify` rule matched — surfaced, never blocks. |
| `POLICY_ENGINE_DISARMED` | advisory | A `.policy` file failed to compile; the engine refused to arm and surfaced loudly. Advisory at the GATE so a policy typo can't wedge every tool call. |
| `POLICY_ENGINE_PRODUCER_ERROR` | advisory | A policy producer raised while evaluating a tool call; degraded to an advisory caveat. |

## How to register a new finding code

1. Add it to `FINDING_REGISTRY` in `scripts/enforcement_policy.py` with a
   `default_class` (prefer `advisory`) and a one-line `description`.
2. Add the same code to the `policy` block of `enforcement_policy.json`.
3. Document it in the table above.
4. Add a fixture exercising it under `eval/fixtures/hooks/`.
5. Re-run `python eval/run_tier1.py`.

Default posture: a code not registered here never hard-blocks. The hardcoded
guards are the documented exception — `path-allowlist.py`,
`no-secrets-in-bash.py`, `no-deploy.py`, `commit-guard.py`,
`citation-required.py`, `curator-write-allowlist.py` and `provenance-guard.py`
guard sacred invariants and stay hardcoded-blocking.

---

# The policy DSL gate

Engine: `scripts/policy_engine.py` (pure). Adapter:
`.claude/hooks/policy-engine.py`, wired PreToolUse **and** PostToolUse on
Bash|Write|Edit. Policies: `.claude/rules/policies/*.policy` — under
`.claude/rules/` **deliberately**: sacred by pattern (`file_tiers.py`), so an
agent cannot soften its own gate.

## Grammar (subset)

```
# comment
source NAME = (file|exec) "PATTERN"
rule NAME:
  (block|notify) (exec|write|read) "PATTERN" ["ARG"]
    [if LABEL]
    [unless after (exec|write|read) "PATTERN" [exits N]
            [since (exec|write|read) "PATTERN" (or (exec|write|read) "PATTERN")*]]
  because "REASON"
```

Fail-closed at compile: unknown keyword/op/effect, duplicate rule or source,
unquoted pattern/reason, missing `because`, an ARG on a non-exec rule, an
`if LABEL` naming no declared source, an unterminated quote, and `kill`
("not portable at the tool layer; use block") all DISARM the whole policy
set. A disarmed engine never blocks — it surfaces `POLICY_ENGINE_DISARMED`
loudly on every call (never silent). Globs: `*` does not span `/`; `**`
does; `**/x` also matches bare `x`; a pattern with no `/` matches the
basename.

## Ordering epochs (with two documented deviations)

```
on a gate event that matches (and exits N, if constrained):  gate_epoch[G]  = ++epoch
on a since event that matches:                               inval_epoch[S] = ++epoch
`after G since S…` holds  iff  max(inval_epoch[S]) == 0                (deviation 1)
                           OR  gate_epoch[G] != 0 AND gate_epoch[G] > max(inval_epoch[S])
plain `after G` latches once gate_epoch[G] != 0 (strict, as in ActPlane)
```

**Deviation 1 (empty-invalidation).** ActPlane requires `gate_epoch != 0`
unconditionally, which would gate every guarded op in a fresh session —
including a docs-only commit before any harness run. The engine sees only
the command, never the staged set, so "docs-only commits are not gated" is
only expressible as: no matching invalidation this session ⇒ the unless
holds. Applies to `since`-form rules only; plain `after` stays strict.

**Deviation 2 (Pre/Post phases).** `exits N` needs the real exit code, which
exists only after the tool ran. So: PreToolUse **evaluates** (block/notify
against current state, no update); PostToolUse **updates** (gates,
invalidations, labels). A blocked call never reaches Post, so a denied
action can never latch a gate. The live Bash payload carries no numeric exit
code, so Post infers it: an explicit `exit_code`/`returncode` wins; else an
`is_error`/`error` boolean; else — the documented live shape —
arrival-means-success (PostToolUse fires only after a tool SUCCEEDS, so 0
unless `interrupted`); else unknown (`None`), which satisfies no `exits N`
gate. Consequence: a gate command chained INTO the commit command
(`eval && git commit`) cannot satisfy its own gate — run the gate as its own
tool call, then commit.

## Event model (tool layer)

Bash commands are heredoc-stripped, tokenized, and split on `&& || ; | &` —
each segment is an exec event (a `git commit` buried in a chain is seen).
Exec targets are interpreter-aware: `python eval/run_tier1.py` is
`eval/run_tier1.py`, not `python`. Bash write targets (shared extraction:
`.claude/hooks/bash_targets.py`) surface as write events, so write rules see
redirects. Per-session state lives in a gitignored runtime dir
(`.memory/T1_episodic/_state/policy_engine/`).

## The starter policy (crypto-bot binding)

`harness_before_commit.policy` mechanizes this repo's own discipline:

- **`tier1-eval-before-commit`** — an edit to `.claude/**`, `eval/**` or
  `schemas/**` blocks `git commit` until `python eval/run_tier1.py` has run
  green as its own tool call. This is escalation.md §13.2 ("a stage closes only
  when its VERIFY block is green") made mechanical.
- **`literature-lock-reminder`** — a `notify` on `research/**` writes,
  restating the no-p-hacking rule: Variation #1 is pre-registered BEFORE its
  trial script runs, and a threshold moved after seeing a statistic is
  p-hacking regardless of which window the data came from.
- **`trials-log-reminder`** — a `notify` on `backtest/trials.log` writes,
  restating escalation.md tripwire 1.

**Why there is no pytest rule.** crypto-bot runs `python -m pytest ...`, and
`effective_exec_target` resolves the `-m` module form to the INTERPRETER, not
to `pytest`. So a `**/pytest` pattern can never match, and the only pattern
that would (`python`) latches on any successful python call — a gate in name
only. pytest is already gated deterministically one layer down by
`.githooks/pre-commit` check 3, so a weak duplicate here would add nothing but
the risk of wedging commits. Recorded here rather than left as a silent
omission.

## What this layer did NOT absorb, and why

The port instruction was to express the archived bash guards through the
policy map. Three of the four are not expressible in this DSL, so they were
ported as dedicated Python hooks instead — the behaviour is preserved, the
jq fail-open is gone, and this section is the record of the deviation:

| archived bash hook | where it lives now | why not a policy rule |
|---|---|---|
| `no-deploy.sh` | `.claude/rules/policies/` could express it, but `no-deploy.py` carries it | the irreversible-git patterns need regex over the whole command string (`git push --force-with-lease`), not an argv0+subcommand match |
| `no-secrets-in-bash.sh` | `no-secrets-in-bash.py` | the DSL matches paths and argv, never the command's CONTENT — a secret literal is content |
| `commit-heredoc-required.sh` + `commit-format.sh` | `commit-guard.py` (merged) | requires parsing the heredoc BODY of the command; the DSL deliberately strips heredoc bodies as data |
| `citation-required.sh` | `citation-required.py` | requires reading `transcript_path`, which is not part of the tool event model |

## Honest limits (read before trusting this gate)

This is creation/tool-time enforcement, **bypassable by a determined agent** —
ActPlane's own authors deleted their tool-layer PreToolUse enforcement as
bypassable, and this is that layer. Known dodges are documented, not hidden:
`git -C <dir> commit` (ARG is positional), `sh -c "git commit"` (the `-c` body
is not re-parsed), a partial eval run stamps the same gate. It is
defense-in-depth: `path-allowlist.py` stays unconditional beneath it, and the
git hooks (`.githooks/pre-commit`, `commit-msg`,
`scripts/pre_commit_backlog_check.sh`) gate the commit itself one layer down,
where `--no-verify` is detected after the fact by
`scripts/post_commit_verify.sh`.

## Update history

- 2026-09-02: v1. Ported from siamese-reconcile (HEAD 2f13045). crypto-bot
  policy bindings replace the reconciliation ones; the pytest-gate omission
  and the four non-expressible bash guards are recorded above rather than
  dropped silently.
