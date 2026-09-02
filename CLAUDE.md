# CLAUDE.md — Agent operating rules for crypto-bot
FIRST ACTION EVERY CHAT: bash_tool on repomix-output.xml. Not project_knowledge_search.

Last updated: 2026-09-02 (v2 — governance port from siamese-reconcile:
agent-autonomous push of gated work, mega-loop posture, drift signal,
Mandate L backlog discipline, discovery/confirmation pointer)

This file is read by Claude Code and other agents when working in this repo.
Read it before starting any task.

READ FIRST at chat start: `docs/handoff_template.md` — workflow
procedure for chat handoffs, Claude Code prompt construction, and
project-knowledge management. CLAUDE.md holds project-wide
behavioral mandates; the handoff template holds task-specific
workflow that doesn't need to compete for attention in every chat.

## Project overview

Cryptocurrency trading bot running on OKX paper mode. Multi-strategy portfolio with
regime-aware Kelly sizing. Currently in **Phase 4.F — Perp-structural batch**
(Binance USDT-M perpetual substrate; discovery/confirmation split). Phase 4.E
Microstructure / Order-Flow **closed with 0 of 4 passers**. See
`docs/MASTER_PLAN.md` and `docs/bot_status.md` for current state.

## Core principles

- **Sacred-harness files (never edit; human approval required for any
  schema or content change).** Runtime artifacts and the holdout split
  are the audit-critical core. Changes to these break the
  multiple-testing correction or the holdout single-access guarantee:
  `backtest/trials.log`, `backtest/holdout_manifest.json`,
  `backtest/holdout_access.log`, and the schema of `backtest/holdout.py`.
- **Sacred-harness documents (never edit without explicit pre-authorization).**
  Project-constitution documents whose content is the canonical record of
  agent rules, plan state, validation methodology, and architectural design.
  Changes require human pre-authorization in the prompt's AUTONOMY section
  (per the Pre-authorization exception below) OR a human-only edit:
  `CLAUDE.md`, `docs/MASTER_PLAN.md`, `docs/architecture.md`,
  `docs/validation_framework.md`. The existing `.claude/hooks/sacred-block.sh`
  blocks edits by default; pre-authorization works via the
  `SACRED_OVERRIDE_FILES` env var on the CC invocation line, which lives
  only for that session.
- **Schema-stable code (edit cautiously; contract-preserving changes
  proceed, contract changes need approval).** Validation harness modules
  whose interfaces feed the sacred-harness files. Bug fixes that preserve
  function contracts (signatures, return shapes, schema fields) are agent-
  autonomous; changes that alter contracts require human approval:
  `backtest/cpcv.py`, `backtest/dsr.py`, `backtest/verdict.py`,
  `backtest/engine.py`, `backtest/trials.py`, `backtest/holdout.py`
  (implementation; schema is sacred per above).
- **Every experiment counts.** Every backtest, every parameter variation,
  every exploratory test appends a row to `trials.log`. Multiple-testing
  correction via DSR uses this count. Do not bypass.
- **Discovery screens are the one exception, and they are ledgered.**
  On a substrate whose manifest declares a `discovery_end`, exploratory
  screens inside the sealed discovery window write a
  `research/discovery/<family>.md` row instead of a `trials.log` row,
  and their count `N_disc` haircuts the confirmation DSR. See
  `.claude/rules/backtest.md` § "Discovery / confirmation split".
- **Trial intentionality.** Commits to the repo are the deliberate human
  act that marks a trial as "this is what I tested, this is the variation
  I am claiming." Autonomous test execution does not extend to autonomous
  commits — the commit gate is what makes the trial record interpretable.
  See "Human only" below.
- **Paper mode is the guard.** `paper_mode=True` must remain the default.
  Any code path that would trigger real OKX API calls requires human
  approval.

## Agent autonomy rules

Agents decide autonomously when data clearly answers the question.
Agents consult with the human when the decision changes what's being tested
or affects money/deployment.
Agents commit autonomously with heredoc-embedded messages per mandate G.
Agents never push or deploy autonomously.

### Agent decides (no approval needed)

- Fix bugs in strategy implementations
- Propose and test parameter variations within theoretically-justified ranges
- **Run pre-justified test batches end-to-end without per-test approval**
  (see `.claude/rules/backtest.md` for full discipline rules)
- Run backtests, CPCV, DSR computation
- Retire strategies when data is clearly negative (DSR well below threshold,
  no variation improves it within theoretical bounds)
- Empirically calibrate thresholds (DSR cutoff, CPCV path count, etc.)
- Research alternative pairs, regimes, or filters for a strategy
- Run exploratory tests on alternative pairs as additional data points
- Fix tooling, infrastructure, caches
- Investigate performance issues and implement fixes
- Archive retired experiments (move to `strategies/archive/`)
- Add new strategy variations to `backtest/trial_queue.json` when `scripts/propose_next_variation.py` produces a qualifying proposal (citation score >= 3.0, not previously tested per trials.log)
- **Paper deploy / paper-mode start / kill / restart** (added 2026-05-08).
  Paper mode is reversible (process termination); agent monitors and rolls
  back. Live deploy remains Human-only.
- **`docs/MASTER_PLAN.md` outcome-row edits** (added 2026-05-08): post-hoc
  retire/keep notes, status updates, batch-completion summaries, sq-row
  verdict reconciliation. New strategy CATEGORIES still Human-only.
- **`docs/architecture.md` and `docs/validation_framework.md` edits within
  their existing autonomy carve-outs** (added 2026-05-08). The autonomy
  framework itself (the lists in this file) remains Human-only — agents do
  not grant themselves more autonomy.
- **Paper-mode capital allocation experiments and risk-parameter sweeps**
  (added 2026-05-08). Live capital changes remain Human-only above.
- **Push its own finished, gated work** (added 2026-09-02). CC pushes its
  own finished, gated work: `bash scripts/post_commit_sync.sh` (no flag)
  after a stage whose VERIFY is green (pytest green, eval/run_tier1.py
  green, commit hooks clean, no sacred path staged without
  SACRED_OVERRIDE_FILES). The PostToolUse hook runs the same script with
  `--no-push`, so routine commits never push. Force-push, branch deletion,
  pushing any branch but `main`, live deploy, and live capital remain
  Human-only. (Owner direction 2026-09-02, ported from siamese-reconcile
  §6 rule 1, owner direction there 2026-06-17.)

### Agent consults the human (present findings, wait for decision)

- Pair substitution (swap the canonical pair of a strategy)
- Adding a new strategy category not in the original portfolio
- Adding a new test or strategy variation not in MASTER_PLAN.md
  (see `.claude/rules/backtest.md` proposal agent exception)
- Modifying the validation harness or `trials.log` schema
- Borderline retire/keep calls (DSR within ±0.05 of threshold on holdout)
- Scope changes that increase the multiple-testing count meaningfully
- Any permanent deletion of code, strategies, or data
- Live capital changes (paper-mode parameter changes are agent-autonomous
  per the expansion above)

### Human only (agents must not perform)

Updated 2026-05-08: autonomy expansion. The principle is
**reversible-or-paper-only ⇒ agent; irreversible-or-live-or-audit-
corrupting ⇒ human.** Items moved to "Agent decides" are listed
below in the next section.

- Force operations (force-push, hard reset on main, etc.), branch
  deletion, and pushing any branch other than `main`
- Live deploy to production (real OKX API or any non-paper venue)
- Live capital allocation or risk-parameter changes that affect
  real-money execution
- Adding NEW strategy CATEGORIES to `docs/MASTER_PLAN.md` (scope
  expansion). Outcome rows, status updates, and verdict
  reconciliation within an existing category are agent-autonomous
  per the "Agent decides" list.
- Modifying the SCHEMA of sacred-harness FILES (`trials.log` column
  structure, `holdout_manifest.json` field shape, `holdout.py`
  interface). Append-only data writes within the established schema
  are governed elsewhere (e.g., the trial-queue orchestrator
  exception for trials.log appends).
- Editing or copying secrets / keys / passwords (anything in
  `~/.crypto-bot.env`, any file containing an API key, or any
  Resend / OKX / Anthropic credential).
- Modifying `CLAUDE.md` itself, `docs/architecture.md`, or
  `docs/validation_framework.md` without explicit pre-authorization
  in the prompt's AUTONOMY section. (`docs/MASTER_PLAN.md` outcome-
  row edits are agent-autonomous; new-category edits remain
  human-only above.)

> **Pre-authorization exception.** Claude Code may edit any file in
> this list when the user explicitly pre-authorizes the edit in the
> prompt's AUTONOMY section (e.g., "Kanin pre-authorizes edits to
> docs/validation_framework.md for this prompt scope"). The auto-
> accept-edits review-before-commit workflow IS the human-in-the-loop
> the rule protects. Without explicit pre-authorization, the default
> Human-only rule applies and the agent must refuse the edit and
> surface the restriction.

> **Trial queue orchestrator exception.** See
> `.claude/rules/backtest.md` for the full scoped commit rules
> for `scripts/run_trial_queue.py`. Push remains human-only.

### Agent edits documents autonomously (no approval needed)

Every doc not in the "Human only" list above is agent-editable when the
data clearly answers the question. Specifically: `docs/MASTER_PLAN.md`,
`docs/bot_status.md`, `docs/open_questions.md`, `docs/strategies.md`,
`docs/research_log.md`, `docs/strategy_evidence_audit_2026-04-26.md`,
and any future audit or per-strategy doc. Sacred-harness rule (above)
covers runtime artifacts, not docs. Do not gate doc edits on judgment
that doesn't apply.

## Code conventions

- Python 3.12
- Use explicit `is None` / `== 0` checks; never `or` on numeric or DataFrame values
- `self._slots` not `self.slots` in `portfolio/manager.py`
- Never change `total_capital` semantics
- Server deploy uses `sudo bash -c` (NOT `sudo -u botuser`)
- `holdout_manifest.json` is source of truth for BOTH timeframe
  AND symbol/symbols/legs per strategy. When auditing a trial,
  check the manifest entry, not the variation_id label.
- `portfolio/regime_detector.py` is the regime detector module
  (NOT `strategies/regime.py`). Public API: `RegimeDetector`
  class with `detect(df) -> RegimeReading` and `current_regime`
  property; module exports `REGIME_STRONG_BULL`/`BULL`/`RANGE`/
  `VOLATILE`/`BEAR`/`CRASH` constants and `ALL_REGIMES` list.

## When to use which tool

- **Claude Code.** All implementation work, in chunks. Multi-file or
  single-file, simple or complex — CC is the route. Cowork retired
  2026-05-03; one tool eliminates the routing decision.
- **Chat with human.** Decisions, design reviews, retire/keep calls,
  scope choices. Plan together; execute via Claude Code.

## Behavioral mandates

These rules govern how agents communicate work, not what work to do.

> Communication and output rules: see `.claude/rules/communication.md`
> (always loaded). Mandates F and G remain here as core invariants.

**F. Decision authority — design choices are agent calls.**
When the data answers the question (project files + past
chats + handoff prompt), the agent decides and executes — no
option-A/B/C menus, no "pick one and confirm" loops back to
the human. Sign-off is reserved exclusively for: git push, deploy,
and sacred-harness file schema changes per CLAUDE.md "Human only"
list. Design choices like which
abstraction layer, dispatch pattern, manifest field shape,
module location, naming convention are agent calls when the
evidence answers them. Bit-by-bit sign-off cycles waste time
and tokens on already-planned work and create the failure
mode where a chat fragments a decided plan into N approval
rounds. The user has stated this preference repeatedly; the
mandate persists it in the repo so it doesn't depend on
chat-side memory.

**G. Trial intentionality boundary at push, not at commit.**
Trial intentionality is preserved by mandatory heredoc-embedded
commit messages with full context, enforced at the hook layer
(commit-heredoc-required.sh, sacred-block.sh, commit-msg,
pre-commit). The boundary moved at architecture.md commit
831be25 (chat 2026-05-03 deliberation): mandatory message
embedding via hooks is safer than manual commit typing, which
twice produced empty commits when humans skipped the editor.
Agents commit autonomously — git add, git commit with heredoc
message. **Revised 2026-09-02 (owner direction):** the boundary
moved again, from the push to the GATE. CC pushes its own finished,
gated work via `bash scripts/post_commit_sync.sh` (no flag) once a
stage's VERIFY block is green; the PostToolUse hook runs the same
script with `--no-push`, so routine and intermediate commits never
push. What makes a trial record interpretable is the green gate and
the heredoc message, not a human typing `git push`. The genuinely
irreversible steps — force-push, branch deletion, pushing any branch
but `main`, live deploy, live capital — remain Human-only, which is
the same boundary as mandate F drawn at the point where state
actually becomes unrecoverable.

Historical drift cases and worked examples: see
`docs/drift_history.md` (do not load unless investigating a
specific past failure pattern).

## Mega-loop posture

When CC runs a dispatcher-mode / megaloop prompt, **mega-loop is the
default operating posture** unless the start prompt explicitly opts out.
CC walks the enumerated stage list without per-stage human confirmation
and halts on exactly three conditions:

1. Kanin stops it (or sends any drift indicator — "stop", "wait", "let
   me see", "you're spinning", "the diff doesn't match what you said").
2. A tripwire in `.claude/rules/escalation.md` fires.
3. CC hits a JUDGEMENT the project files cannot settle.

On halt, CC appends a HUMAN NEEDED block to `.memory/_inbox/human_needed.md`,
writes a status doc, commits, pushes, and exits. **Completion is
deterministic, never self-assessed:** a stage is done only when its VERIFY
block is green. "I think it's done" is not an exit. The harness here is
`pytest`, `python eval/run_tier1.py`, and the trial verdict tree
(`backtest/verdict.py` + CPCV/DSR) — not an LLM's opinion of its own work.
Full rule body: `.claude/rules/escalation.md` §13. Loop framing:
`.claude/rules/vertical_slice_loops.md`.

## Drift signal — three speculative fixes in a row

If CC has attempted three fixes for the same symptom and the symptom
persists, **stop the speculation loop.** Run experiments instead: build a
minimal reproducer, change one variable at a time, log what you observe,
and write the observations to `docs/investigations/<date>-<slug>.md`
BEFORE attempting a fourth fix. Three speculative fixes without controlled
observation is a recognised failure mode, not diligence.

## Execution autonomy

Claude Code proceeds without approval on everything in "Agent
decides" above. Claude Code must not proceed without approval on
everything in "Human only" above. When verification passes, commit
autonomously with a heredoc message; push the finished, gated work with
`bash scripts/post_commit_sync.sh` per the "Agent decides" list. Live
deploy, live capital, force operations and non-`main` pushes remain
human-only.

## Update history

- 2026-09-02: v2. Governance port from siamese-reconcile (HEAD 2f13045).
  Push of finished, gated work moved Human-only → Agent-decides (mandate G
  boundary moved from the push to the gate). Added mega-loop posture, the
  three-speculative-fixes drift signal, the discovery/confirmation pointer
  (Proposal 2), and Mandate L backlog discipline. New rules:
  `.claude/rules/escalation.md`, `.claude/rules/vertical_slice_loops.md`,
  `.claude/rules/enforcement.md`. Python hook layer replaces the
  half-wired bash hooks (`.claude/hooks/_archive_bash_2026-09/`).
  Supersedes `docs/playbooks_port_plan_2026-07-03.md`.
- 2026-05-08: autonomy expansion (paper deploy, MASTER_PLAN outcome rows,
  paper-mode capital sweeps moved Human-only → Agent-decides).
- 2026-05-06: v1.