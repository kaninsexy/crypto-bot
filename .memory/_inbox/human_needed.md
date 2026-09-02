# human_needed.md

Escalations from autonomous / megaloop runs. Format: `.claude/rules/escalation.md` §8.
Append-only; Kanin clears an entry by acting on it and deleting the block.

=== HUMAN NEEDED ===
id:        sacred-override-absent-2026-09-02
raised:    2026-09-02 1730 ICT
stage:     megaloop 2026-09-02 — S1.5 / S1.6 boundary (governance port)
tripwire:  5.6 — a sacred-path edit outside the effective authorization scope

What I was about to do
Finish S1.6 (rewrite `docs/architecture.md` section E to describe the new
Python guard layer instead of the archived bash one) and then run S2, which
appends the three Binance UM entries to `backtest/holdout_manifest.json` and
adds the contract-preserving `substrate=binance_um` branch to
`backtest/holdout.py`. Both files are pre-authorized in the prompt's AUTONOMY
block.

Why it stops here
The guard layer I installed in S1.2 is now live and correctly refuses every
Tier 1 write, because `SACRED_OVERRIDE_FILES` is **unset in this session**.
`.claude/hooks/path-allowlist.py` reads that variable from its OWN process
environment, which it inherits from the Claude Code process; the prompt's
Git Bash launch line sets it, but this session was not started that way
(`CLAUDE_HUMAN_OVERRIDE` is unset too). An agent cannot set it from inside a
running session — and must not be able to, because a hook that reads its
authorization from anywhere an agent can write is not a guard. So the
authorization exists (in the prompt, which CLAUDE.md's "Pre-authorization
exception" accepts) but the transport for it does not.

Confirmed empirically, not inferred: a `Write` to
`schemas/agent_frontmatter.schema.json` and a Bash `echo ... >
.claude/hooks/file_tiers.py` both returned exit 2 with
`Authorized this session: [<empty>]`.

Two consequences, both recorded in `.memory/T2_semantic/backlog.jsonl`:

- **BK-0013 (critical)** — S1.6's `architecture.md` edit and all of S2 are
  blocked, and S2 gates S3–S7, so the entire Phase 4.F arc is blocked.
- **BK-0011 (high)** — I shipped one real regression I cannot fix myself: the
  Resend key pattern in `.claude/hooks/no-secrets-in-bash.py` is
  `re_[a-zA-Z0-9_-]{20,}` with no left boundary, so it matches inside the path
  `scripts/p·re_commit_backlog_check.sh`. Any Bash command naming that script
  is blocked. Commits are unaffected (git invokes the script itself). The fix
  is one character class: `(?<![A-Za-z0-9_-])re_[A-Za-z0-9]{20,}`.
- **BK-0012 (medium)** — `schemas/agent_frontmatter.schema.json` is still
  siamese's verbatim copy and disagrees with the validator I re-fitted.

Recommendation
Relaunch and continue — nothing here is a design question. Run the launch line
from `docs/megaloop_prompt_2026-09-02.md`, adding `schemas/` to the scope
(S1.2 made `schemas/*.schema.json` Tier 1, which the AUTONOMY list predates):

    cd /c/crypto-bot && SACRED_OVERRIDE_FILES="CLAUDE.md,.claude/rules/,.claude/hooks/,.claude/settings.json,schemas/,docs/MASTER_PLAN.md,docs/architecture.md,docs/validation_framework.md,docs/handoff_template.md,backtest/holdout_manifest.json,backtest/holdout.py" CLAUDE_HUMAN_OVERRIDE=1 claude --model opus

Note the TRAILING SLASHES on the directory entries — `.claude/hooks` without
one authorises nothing under exact-match semantics. (I did add a
directory-scope adaptation to `path-allowlist.py` that treats a bare entry
naming a real directory as a scope, and pinned it with a fixture, so the
prompt's original line would also work; the trailing slash is the explicit
form and is what the block message now tells you to use.)

Then resume at S1.6 — S0 through S1.5 are committed and verified. Fix BK-0011
first; it costs one line and removes a live papercut.

State
Stages completed: S0 (preflight), S1.1 (rules layer, commit 6a564ec),
                  S1.2 (Python hook layer + eval harness),
                  S1.3 (scripts/post_commit_sync.sh),
                  S1.4 (Mandate L backlog discipline + chained git hooks),
                  S1.5 (agent frontmatter + validators)
Blocked:          S1.6 (architecture.md only), S2, and therefore S3–S7
Last commit:      e325f5d (pushed; main == origin/main)
Harness:          pytest 446 passed / 6 skipped; eval/run_tier1.py 122/122 passed;
                  eval/run_tier1.py --self-check OK (80 fixtures go red against
                  fail-open stubs, so the gate discriminates)
=== END ===
