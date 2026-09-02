# Megaloop prompt — governance port + Phase 4.F end-to-end (2026-09-02, v2)

v2 supersedes v1: stage S1 is now a port of the current governance layer
from `C:\siamese-reconcile` (HEAD 2f13045, 2026-08-27 — the live, newer
rule set) into crypto-bot, replacing crypto-bot's stale "push is
human-only" rule and its half-wired bash hook layer. crypto-bot's July
"playbooks port plan" chunks 1–7 were never executed; siamese-reconcile
holds the finished, eval-pinned versions of the same components.

Verified facts the prompt relies on (checked 2026-09-02):
- siamese-reconcile `CLAUDE.md` §6 rule 1 / §13 / §17 / §18: CC pushes its
  own finished, gated work via `bash scripts/post_commit_sync.sh` (no flag);
  the PostToolUse hook calls it with `--no-push`; deploys and posting to
  external systems stay owner-only. Mega-loop is the default posture in
  dispatcher mode with three halt conditions (`escalation.md` §13), a
  time backstop, a hard ceiling, deterministic completion via harness, and
  escalation to `.memory/_inbox/human_needed.md`.
- siamese-reconcile guard layer: Python hooks (`file_tiers.py` single
  tier map; `path-allowlist.py` incl. Bash write-target scan;
  `no-secrets-in-bash.py`; `policy-engine.py` + `.claude/rules/enforcement_policy.json`;
  `provenance-guard.py`; `observe.py`; `session-start.py`; `session-end.py`;
  `pre-compact.py`; `post-commit-sync.py`), wired in `.claude/settings.json`
  for PreToolUse/PostToolUse/Stop/SessionStart/PreCompact, with
  `eval/run_tier1.py` + `eval/fixtures/hooks/*.jsonl` pinning behaviour, and
  Mandate L backlog discipline (`schemas/backlog.schema.json`,
  `.memory/T2_semantic/backlog.jsonl`, `scripts/pre_commit_backlog_check.sh`,
  `scripts/post_commit_verify.sh`, `scripts/install_git_hooks.sh`).
- crypto-bot today: `.claude/settings.json` wires only `inject-mandates.sh`
  and `commit-scope-audit.sh`; `sacred-block.sh` is not wired; jq-based
  hooks fail open on a jq-less shell; `.memory/T2_semantic/decisions_log.jsonl`
  is referenced by 5 agents but does not exist; HEAD 1b6005a, 13 commits
  unpushed.

Launch from `C:\crypto-bot` in Git Bash:

```bash
cd /c/crypto-bot && SACRED_OVERRIDE_FILES="CLAUDE.md,.claude/rules/backtest.md,.claude/rules/communication.md,.claude/rules/prompts.md,.claude/settings.json,.claude/hooks,docs/MASTER_PLAN.md,docs/architecture.md,docs/validation_framework.md,docs/handoff_template.md,backtest/holdout_manifest.json,backtest/holdout.py" CLAUDE_HUMAN_OVERRIDE=1 claude --model opus
```

Model: **Opus**. Expected wall-clock 4–8 h. Non-monitor.

```
Read repomix-output.xml with bash_tool as your FIRST action, then
CLAUDE.md, then docs/handoff_template.md, then userMemories.
Confirm when done.

Then read end to end, in this order:
  docs/revival_handoff_2026-09-02.md, docs/research_revival_2026-09.md,
  docs/recon_binance_um_2026-09.md, docs/proposed_backtest_rule_discovery_2026-09.md,
  docs/playbooks_port_plan_2026-07-03.md (the unexecuted July plan — you are
  superseding it), backtest/proposed_manifest_entries_binance_um.json,
  research/discovery/README.md, backtest/engine_cs.py (docstring +
  run_engine_cs), scripts/phase4e_trial_common.py, scripts/run_trial_queue.py
  (header + orchestrator commit exception), backtest/holdout.py
  (_load_substrate_df, _MANIFEST_SUBSTRATE_FIELDS), backtest/generate_holdout_manifest.py,
  .claude/settings.json, every file in .claude/hooks/ and .githooks/.
Then read, READ-ONLY, from C:\siamese-reconcile (never write there):
  CLAUDE.md (§5, §6, §11–§14, §16–§18), .claude/rules/escalation.md (§0, §2,
  §3, §5, §8, §9, §13), .claude/rules/vertical_slice_loops.md,
  .claude/rules/enforcement.md, .claude/rules/communication.md,
  .claude/settings.json, .claude/hooks/*.py, eval/run_tier1.py,
  eval/fixtures/hooks/*.jsonl, schemas/backlog.schema.json,
  scripts/post_commit_sync.sh, scripts/pre_commit_backlog_check.sh,
  scripts/post_commit_verify.sh, scripts/install_git_hooks.sh,
  .memory/_inbox/human_needed.md (format only — it contains hotel context;
  do not copy its content).

MODE: MEGALOOP, NON-MONITOR (siamese escalation.md §13 semantics, adopted
here by this prompt): monitoring_mode off; time_backstop 6 h; hard ceiling
8 h; never ask a question; halt ONLY on (1) Kanin stopping you, (2) a
tripwire below, (3) a judgement the project files cannot settle. On halt:
append a HUMAN NEEDED block (siamese §8 format, adapted) to
.memory/_inbox/human_needed.md, write docs/megaloop_status_2026-09.md
(stage, failure, command + last 40 lines, what you tried), commit, push,
exit. Every stage ends with commit + push so a later failure never loses
verified work. Completion is deterministic: a stage is done only when its
VERIFY block is green; "I think it's done" is not an exit.

TRIPWIRES (hard stop, no improvisation): any write to backtest/trials.log
except through backtest.trials.record_trial from a confirmation trial; any
load_holdout call outside a final_gate; any real OKX order path or
paper_mode=False; any new paid API or dependency; force-push, branch
deletion, or push of anything but main; a sacred-path edit outside the
AUTONOMY list; 3 consecutive speculative fixes for one symptom (siamese
§18 drift signal → write docs/investigations/<date>-<slug>.md before a
4th attempt).

AUTONOMY (pre-authorization, verbatim — this IS the human-in-the-loop):
Kanin pre-authorizes, for this prompt scope, edits to CLAUDE.md,
.claude/rules/** (backtest.md, communication.md, prompts.md, plus NEW
escalation.md / vertical_slice_loops.md / enforcement.md /
enforcement_policy.json), .claude/settings.json, .claude/hooks/**,
docs/MASTER_PLAN.md, docs/architecture.md (section E hooks/enforcement
only), docs/validation_framework.md (one paragraph: the pre-4.E fee-model
caveat), docs/handoff_template.md, backtest/holdout_manifest.json
(additive rows only), backtest/holdout.py (contract-preserving substrate
branch only), and `git push origin main` after every verified stage.
No other sacred edit. validation_framework.md beyond that paragraph,
trials.py, cpcv*.py, dsr.py, verdict.py, engine.py, engine_multi.py,
engine_perp.py: do not modify. Archive by default; delete nothing
(replaced bash hooks move to .claude/hooks/_archive_bash_2026-09/).

CONSTRAINTS (verbatim from CLAUDE.md / .claude/rules/backtest.md and the
discovery rule you will install in S1):
- Every experiment counts: a confirmation trial appends exactly one
  trials.log row via backtest.trials.record_trial; a discovery screen
  writes NO trials.log row — its ledger row is its record.
- Discovery never reads data >= 2023-01-01. Confirmation dev is
  2023-01-01 -> 2025-05-01. Holdout (2025-05-01 -> 2026-08-31) is read only
  by a final_gate through load_holdout, once per strategy.
- No p-hacking: one pre-registered Variation #1 per family locked in
  research/<name>-literature.md BEFORE its trial script runs; no parameter
  search; 20-variation cap; 3 consecutive failures stop the batch;
  borderline (DSR within ±0.05 of 0.95 on holdout) -> halt.
- Fee gate: OKX perp taker 0.05% + slippage 0.05% AND 2x fee; retire
  unless the edge survives both.
- paper_mode=True stays the default. Live deploy and live capital remain
  Human-only.
- Heredoc commit messages with "Co-authored-by: Claude <noreply@anthropic.com>"
  and "[mandate-H]"; runnable artifacts; bundle shell commands.

STAGES

S0 Preflight. `git status --short | grep -v '^ M'` shows only the two
.memory untracked dirs; HEAD 1b6005a on main; `python -m pytest
backtest/tests data/tests -q` -> 446 passed / 6 skipped; the three
`scripts/discovery_*.py --selftest` exit 0. If HEAD differs, read
`git log -20 --oneline` and resume at the first stage not yet committed
(stages are idempotent; commit subjects are the markers).

S1 Governance port from siamese-reconcile (one commit per sub-step,
push after S1.6):
 S1.1 Rules. CLAUDE.md: (a) move "Pushes to any remote" from Human-only to
   Agent-decides with this text: "CC pushes its own finished, gated work:
   `bash scripts/post_commit_sync.sh` (no flag) after a stage whose VERIFY
   is green (pytest green, eval/run_tier1.py green, commit hooks clean, no
   sacred path staged without SACRED_OVERRIDE_FILES). The PostToolUse hook
   runs the same script with --no-push, so routine commits never push.
   Force-push, branch deletion, pushing any branch but main, live deploy,
   and live capital remain Human-only. (Owner direction 2026-09-02,
   ported from siamese-reconcile §6 rule 1, owner direction there
   2026-06-17.)" Rewrite mandate G's closing sentences to match. (b) Add
   "## Mega-loop posture" (siamese §18 last paragraph + escalation §13.0
   halt conditions, adapted: harness = pytest + eval/run_tier1.py + the
   trial verdict tree; judgement escalations go to
   .memory/_inbox/human_needed.md). (c) Add the drift-signal rule (three
   speculative fixes -> experiments doc). (d) Add the two-line discovery
   pointer (Proposal 2 in docs/proposed_backtest_rule_discovery_2026-09.md).
   (e) Project overview: Phase 4.F active, 4.E closed 0/4. (f) Bump "Last
   updated" and add an update-history line. .claude/rules/backtest.md:
   insert Proposal 1 verbatim. NEW .claude/rules/escalation.md: port
   siamese §0, §2, §3, §5, §8, §9, §13 with crypto-bot tripwires (the
   TRIPWIRES list above) replacing reconciliation-specific ones; drop
   chat-advisor/behavior-verifier references (crypto-bot's harness is
   deterministic already). NEW .claude/rules/vertical_slice_loops.md: port
   the "start prompts enumerate FLOWS not bugs" discipline, adapted (a loop
   = data -> discovery -> confirmation -> holdout -> paper, gated by the
   verdict tree). docs/MASTER_PLAN.md: insert Proposal 3 as "#### Phase 4.F".
   docs/handoff_template.md: replace the "push is human" lines with the new
   rule; add `.memory/_inbox/human_needed.md` to the pre-action checklist.
   docs/validation_framework.md: one paragraph under Gate spec v2 recording
   the pre-4.E 0.04% vs 0.10% fee caveat. Update-history lines everywhere.
 S1.2 Hook layer. Copy siamese `.claude/hooks/file_tiers.py, path-allowlist.py,
   no-secrets-in-bash.py, policy-engine.py, observe.py, session-start.py,
   session-end.py, pre-compact.py, post-commit-sync.py, provenance-guard.py,
   bash_targets.py, _block_record.py` into .claude/hooks/, then ADAPT
   file_tiers.py to crypto-bot: Tier 1 = CLAUDE.md, .claude/rules/**,
   .claude/settings.json, .claude/hooks/*.py, docs/MASTER_PLAN.md,
   docs/architecture.md, docs/validation_framework.md, docs/handoff_template.md,
   backtest/trials.log, backtest/holdout_manifest.json, backtest/holdout_access.log,
   backtest/holdout.py, backtest/trials.py, schemas/*.schema.json,
   .memory/T3_procedural/**, .env*, any *crypto-bot.env; Tier 2 =
   backtest/cpcv.py, cpcv_common.py, cpcv_multi.py, cpcv_perp.py, dsr.py,
   verdict.py, engine.py, engine_multi.py, engine_perp.py, engine_cs.py,
   families.py, strategies/base.py, portfolio/manager.py, config.py; Tier 3 =
   rest. Adapt policy-engine's enforcement_policy.json to crypto-bot
   (secrets patterns from the existing no-secrets-in-bash.sh; deploy
   patterns from no-deploy.sh; heredoc-commit rule from
   commit-heredoc-required.sh; citation-required for research/*-literature.md
   edits). Remove hotel-specific patterns (runs/, tests/fixtures/real,
   Camen). Move every superseded bash hook to .claude/hooks/_archive_bash_2026-09/
   with a README mapping old -> new; keep bash hooks with no Python
   equivalent (failcount-*, regime-change-trigger, flush-T1,
   run-tests-fast, budget-check, exit-ramp-check, inject-mandates) and wire
   them alongside. Rewrite .claude/settings.json on the siamese shape
   (PreToolUse Write|Edit + Bash + "", PostToolUse Bash + Write|Edit + "",
   Stop, SessionStart, PreCompact) with crypto-bot's permissions allow list
   (git add/commit/status/log/diff/ls-files/push origin main, python*,
   pytest*, repomix, bash scripts/post_commit_sync.sh*). Port
   eval/run_tier1.py + fixtures, rewritten for crypto-bot paths: must prove
   (i) a Write to CLAUDE.md is blocked without override and allowed with
   SACRED_OVERRIDE_FILES, (ii) `echo x > CLAUDE.md` in Bash is blocked,
   (iii) a Bash `cat ~/.crypto-bot.env` is blocked, (iv) ambient
   SACRED_OVERRIDE_FILES in the launching shell does not flip a
   block-fixture (harness strips it), (v) a Write to backtest/cpcv.py
   passes with a Tier-2 NOTE. Also port the jq-fail-open fix implicitly
   (Python hooks need no jq).
 S1.3 Sync script. NEW scripts/post_commit_sync.sh adapted from siamese:
   regen repomix-output.xml when include paths changed (repomix.config.json
   is the include spec) and commit it as a chore commit; `--no-push` =
   local only (the hook's mode); no flag = `git push origin main`; claudesync
   step only if `claudesync` is on PATH AND .claudesync/config.local.json
   exists — otherwise print "claudesync not configured, skipped" (Kanin
   syncs project knowledge manually today). Wire post-commit-sync.py to call
   it with --no-push.
 S1.4 Backlog discipline (Mandate L). Port schemas/backlog.schema.json,
   scripts/pre_commit_backlog_check.sh, scripts/post_commit_verify.sh,
   scripts/install_git_hooks.sh, chained ADDITIVELY with the existing
   .githooks/pre-commit and commit-msg (do not weaken the sacred or
   heredoc checks). Seed .memory/T2_semantic/backlog.jsonl from
   docs/open_questions.md OPEN items (BK-0001..; severity + owner cc|kanin)
   and create the missing .memory/T2_semantic/decisions_log.jsonl (empty,
   append-only) that 5 agent files already reference. Add "Mandate L"
   to CLAUDE.md behavioural mandates (siamese §14 wording, adapted).
 S1.5 Agents. Update .claude/agents/*.md frontmatter that names removed
   bash hooks; verify with a frontmatter validator if one exists in
   siamese (schemas/agent_frontmatter.schema.json + scripts/validate_agent_frontmatter.py
   — port both). Do not rewrite agent bodies.
 S1.6 Docs. docs/architecture.md section E: replace the bash-hook layer
   description with the Python layer (map old hook -> new). Mark
   docs/playbooks_port_plan_2026-07-03.md "SUPERSEDED 2026-09-02 by the
   siamese-reconcile governance port (this commit)". docs/bot_status.md
   Current state.
 VERIFY S1: `python eval/run_tier1.py` green (all five proofs);
   `bash scripts/install_git_hooks.sh` then a staged CLAUDE.md edit WITHOUT
   CLAUDE_HUMAN_OVERRIDE is BLOCKED by .githooks/pre-commit and a commit
   with "TODO fix later" and no backlog append is BLOCKED; pytest green;
   `python -c "import json;json.load(open('.claude/settings.json'))"`;
   `bash .claude/hooks/_archive_bash_2026-09/../_test_hooks.sh` (if kept)
   green or archived with the rest; commit; then push with
   `bash scripts/post_commit_sync.sh` (no flag) — this is the first
   agent push under the new rule; confirm `git status -sb` shows main ==
   origin/main.

S2 Manifest + loader (pre-authorized). Append the three entries from
backtest/proposed_manifest_entries_binance_um.json["entries"] via the
generator's additive path (or a script that loads, adds only the three
keys, validates against backtest.holdout's field rules, writes — never
touching an existing entry); append one "added": true event per entry to
backtest/holdout_access.log via the existing writer, caller
"phase4f.manifest_add". backtest/holdout.py: contract-preserving branch —
when notes contain "substrate=binance_um", route load_dev/load_holdout
through data.binance_vision_um (klines at the entry timeframe + funding +
metrics when present); signatures, return shapes, single-access
enforcement and audit log unchanged; tests. VERIFY: pytest green;
`python -c "from backtest.holdout import load_manifest as m; print(sorted(k for k in m() if k in ('FundingDispersionCarry','DeleveragingReversal','ListingFlow')))"`
prints all three; `git diff HEAD~1 -- backtest/holdout_manifest.json`
additions only. Commit; push via post_commit_sync.sh.

S3 Full archive prefetch. `python scripts/prefetch_binance_um.py --all
--start 2020-01 --until 2026-08-31 --intervals 1d,1h --funding --metrics
--metrics-max-days 3000`, then metrics for the top-50 symbols by 2024-12
trailing-30d quote volume (--symbols). Resume on interruption. VERIFY:
coverage table shows BTCUSDT 1d 2020-01-01 -> 2026-08-30, funding rows >
7000, universe.parquet (perp_only) > 400 active symbols. Write the table
into docs/megaloop_status_2026-09.md; commit; push.

S4 Discovery kill tests (screens, not trials). Run each with
--append-ledger on < 2023-01-01 data: scripts/discovery_funding_dispersion.py,
scripts/discovery_deleveraging_reversal.py, scripts/discovery_listing_flow.py.
Record N_disc per ledger (exactly 1 unless the script reports otherwise).
PASS per family iff statistic clears its §C.4 threshold AND t > 3. Commit
"discovery: Phase 4.F kill-test results (ledger rows, no trials)"; push.
If ZERO families pass: status file says "substrate shows no
pre-registered structural edge in 2020-22; next step is
research_revival §A.6 (passive + overlay) — human decision"; append a
HUMAN NEEDED block; commit; push; exit.

S5 Confirmation trials for each PASSING family (max 3 this run):
  a. research/<family>-literature.md: hypothesis-of-record = the §C.4
     confirmation design with the discovery statistic and N_disc pasted
     verbatim, locked parameters (simplest values consistent with the
     kill test; no tuning), pre-trial gates (neutral PSR gate for hedged
     designs, 2x fee gate, event floor 30, units-correct MinTRL, N_disc
     haircut per the discovery rule).
  b. strategies/<family>.py implementing target_weights() for
     backtest.engine_cs (beta_hedge=True for FundingDispersionCarry and
     the hedged DeleveragingReversal; ListingFlow long-only under the
     gate-v2 alpha/IR gate).
  c. scripts/run_<family>_trial.py on the phase4e_trial_common pattern:
     headline + CPCV via run_engine_cs on the confirmation dev window read
     through backtest.holdout.load_dev, both fee levels, verdict via
     backtest.verdict, record_trial once with per-bar series persisted;
     CPCVError handling per backtest.md.
  d. Queue it in backtest/trial_queue.json and run ONLY via
     `python scripts/run_trial_queue.py --once`; the orchestrator's scoped
     commit rule applies; push after each.
  e. Update docs/bot_status.md and docs/strategies.md outcome rows; commit;
     push. Append a backlog line for any deferred follow-up (Mandate L).

S6 Holdout + paper (only for a dev `keep`). One final_gate per strategy
via load_holdout (caller phase4.<StrategyId>.final_dsr); borderline ->
halt. Holdout keep -> paper-deploy on OKX perps in paper mode with the
existing paper stack (agent-autonomous per CLAUDE.md 2026-05-08); write
the §C.5 forward-test plan (PSR >= 0.9 at 12 months; fail-fast SR < 0.5
at 6 months) into docs/bot_status.md; commit; push. Holdout retire ->
archive per rule, document, commit, push.

S7 Close. docs/megaloop_status_2026-09.md: stage reached, every pushed
commit hash, eval/pytest summaries, kill-test statistics, trial verdicts,
open items appended to backlog.jsonl, HUMAN NEEDED items (only genuine
human-only ones: live deploy / capital / borderline). `repomix`. Commit
"docs: megaloop 2026-09 close"; push; exit.
```

## Chat verification after the run

1. `git log origin/main --oneline -40` matches the status file; `main`
   only; no force-push.
2. Sacred diffs limited to the AUTONOMY list; validation_framework.md
   diff is the single fee-caveat paragraph.
3. `python eval/run_tier1.py` green on pushed HEAD; a manual `echo x >
   CLAUDE.md` through a hooked Bash call is blocked.
4. Ledger rows exist for all three families with data ranges < 2023-01-01;
   `backtest/trials.log` gained exactly one row per confirmation trial and
   none from discovery; `holdout_access.log` shows at most one `final_dsr`
   per new strategy.
5. `.claude/hooks/_archive_bash_2026-09/` holds every replaced bash hook;
   nothing deleted.
