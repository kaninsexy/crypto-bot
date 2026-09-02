# Crypto-Bot Multi-Agent Architecture — Production Design Document

**Author:** Architecture synthesis from 2025–2026 multi-agent research
**Target:** Single-developer crypto-bot project at `~/dev/crypto-bot`
**Stack:** Claude Code (Sonnet 4.6) + OpenRouter + OpenClaw gateway + DigitalOcean + Binance
**Scope:** Phase 4.B (funding-rate harvest) finishing + Phase 5 (prediction-market bot) build-out
**Date assumption:** May 2026

**Revisions**
- 2026-09-02 — **Section E rewritten** for the governance port from
  `siamese-reconcile` (HEAD `2f13045`). The bash hook layer described in E.2
  was replaced by Python guards that fail closed; E.3/E.4 were activated (they
  had never run, because `core.hooksPath` was never set); E.5 (the eval
  harness), E.6 (the policy DSL) and E.8 (the honest limit) are new. Sections
  A–D are unchanged. Rationale and the old→new hook map:
  `.claude/hooks/_archive_bash_2026-09/README.md`.

---

## TL;DR (read this once, then implement)

- The architecture is a **three-tier orchestrator-worker system** with **four memory tiers**, **fifteen specialized subagents**, and **four enforcement layers** (CLAUDE.md, frontmatter hooks, .githooks/commit-msg, .githooks/pre-commit). The "manager hierarchy" is intentionally shallow (Strategist → Coordinators → Workers); deeper hierarchies waste tokens for a single-developer scope.
- **Drift is solved by externalization, not by smarter prompts.** Every settled decision lives in a file the agent must `Read` before acting (mandate A enforced by `UserPromptSubmit` hook). Every commit message is generated and committed in a single Bash heredoc (mandate enforced by `pre-commit` and `commit-msg` hooks). Subagents have `maxTurns` budgets and `permissionMode: plan` by default; only the Implementer holds write authority.
- **Adversarial review is structural, not stylistic.** Every variation proposal goes Sonnet-builds → Gemini-2.5-Pro-attacks → Sonnet-rebuts → human-or-Verdict-Tree decides. This is empirically the single biggest defense against backtest-overfitting and citation hallucination, per the Anthropic June-2025 multi-agent paper and the "Brainstorm"-style cross-model debate literature.

---

## A. Memory Architecture

### A.1 Why four tiers and not three

The CoALA / MemGPT / Letta / Mem0 / Zep literature converges on **four functional memory types**: working (in-context), episodic (events), semantic (facts), procedural (rules/skills). For a single-developer project these map cleanly to four storage tiers with different read/write authorities. Three tiers collapse semantic and procedural and produce the exact failure mode we are trying to avoid: agents re-learning settled rules every session.

The four tiers below are **physical files on disk**, not abstract concepts. Every tier is a directory under `~/dev/crypto-bot/.memory/` so it is version-controlled (except `T0_working`, which is gitignored).

### A.2 ASCII hierarchy

```
                      ┌────────────────────────────────────────┐
                      │  T3  PROCEDURAL — settled rules        │
                      │  ── append-only, human-write only ──   │
                      │  CLAUDE.md, MASTER_PLAN.md,            │
                      │  validation_framework.md, mandates/    │
                      │  Read by: ALL agents at session start  │
                      └──────────────────┬─────────────────────┘
                                         │ injected via SessionStart hook
                      ┌──────────────────▼─────────────────────┐
                      │  T2  SEMANTIC — verified knowledge     │
                      │  ── promotion-only, Strategist-write ──│
                      │  facts/, citations/, decisions_log/,   │
                      │  retired_strategies/                   │
                      │  Read by: ALL agents on demand         │
                      │  Write by: Strategist (after review)   │
                      └──────────────────┬─────────────────────┘
                                         │ promoted from T1 by Curator
                      ┌──────────────────▼─────────────────────┐
                      │  T1  EPISODIC — what happened           │
                      │  ── chronological, agent-write append ──│
                      │  trials.log (sacred, read-only),        │
                      │  holdout_manifest.json (sacred, RO),    │
                      │  episodes/YYYY-MM-DD/<agent>/*.jsonl    │
                      │  Read by: agents in same coordinator    │
                      │  Write by: any agent (own namespace)    │
                      └──────────────────┬─────────────────────┘
                                         │ summarized hourly by Curator
                      ┌──────────────────▼─────────────────────┐
                      │  T0  WORKING — current turn only        │
                      │  ── ephemeral, in-context ──            │
                      │  Subagent context window + scratch/     │
                      │  Read/Write: that subagent only         │
                      │  Cleared on: subagent stop              │
                      └─────────────────────────────────────────┘
```

### A.3 Tier-by-tier specification

**T3 — Procedural (settled rules).** Files: `CLAUDE.md`, `MASTER_PLAN.md`, `validation_framework.md`, `.claude/mandates/*.md`, `.memory/T3_procedural/no_p_hacking.md`, `.memory/T3_procedural/sacred_harness.md`, `.memory/T3_procedural/autonomy_boundaries.md`. **Read** by all agents at session start, injected by the existing `.claude/hooks/inject-mandates.sh` SessionStart hook. **Write** authority: human only. Sacred-harness files (`trials.py`, `holdout.py`, `validation_framework/` schema, `CLAUDE.md`, `MASTER_PLAN.md`, `validation_framework.md`) are blocked by a `PreToolUse` hook on `Edit|Write` that exits 2 if the path matches the sacred allowlist. Promotion into T3 happens via human edit + git commit; agents cannot promote into T3 even with approval.

**T2 — Semantic (verified knowledge).** Files: `.memory/T2_semantic/facts.md` (one-line facts with citations), `.memory/T2_semantic/citations/<paper-key>.md` (paperqa2-style metadata + extracted claim), `.memory/T2_semantic/decisions_log.jsonl` (one line per settled decision: timestamp, decision, rationale, supersedes), `.memory/T2_semantic/retired_strategies/<strategy>.md` (postmortem + reason). Read by all agents on demand via a `Read` tool call. **Write** authority: only the Strategist agent writes here, and only after Adversarial-Reviewer has signed off (the signoff is itself a line in `decisions_log.jsonl`). This is the tier that kills "re-litigating settled decisions": once a fact is in T2, the SessionStart hook will surface it on any subsequent prompt that mentions a related keyword (via simple `grep`).

**T1 — Episodic (what happened).** Files: `trials.log` and `holdout_manifest.json` are **sacred** — agents read but never write directly; they propose changes via `trials_proposed.jsonl` which a human merges. Free-form episodic data lives in `.memory/T1_episodic/episodes/YYYY-MM-DD/<agent-name>/<turn>.jsonl`. Each agent has its own subdirectory (no cross-write). Read authority: any agent in the same coordinator branch can read sibling episodes; cross-branch reads require Strategist permission. Write: append-only, never edit. Garbage collection: episodes older than 30 days are compressed by the Curator agent into a single daily summary in T2 and the originals are archived to `.memory/T1_episodic/_archive/` (never deleted — archive-by-default rule).

**T0 — Working (current turn).** This is just the subagent's context window plus `.memory/T0_working/<agent>/scratch.md` for files the agent needs to write to itself across tool calls within a single invocation. Cleared by `SubagentStop` hook. Never committed to git.

### A.4 Promotion rules (T1 → T2 → T3)

Promotion is the load-bearing mechanism that makes drift impossible to repeat. Three promotion paths, each gated:

1. **T1 → T2 (Curator-driven, hourly).** The Curator agent (Haiku 4.5, runs as a cron'd headless `claude -p` job) reads the last hour of T1 episodes, extracts candidate facts (entities, decisions, numerical claims), and writes them to `.memory/T2_semantic/_pending_review.jsonl`. Strategist reviews this queue at the start of each session; accepted facts move to `facts.md`, rejected ones move to `_rejected/` with a one-line reason. **Conflict resolution:** if a candidate fact contradicts an existing fact in T2, Curator marks it `CONFLICT` and the Strategist must resolve manually before either is promoted. No auto-overwrites.
2. **T2 → T3 (human-only).** A semantic fact only becomes a procedural rule when the human edits a T3 file and commits. Agents may **propose** T3 promotions via `.memory/_proposals/T3_promotion_<id>.md`, but cannot execute them.
3. **T0 → T1 (automatic, on subagent stop).** Every `SubagentStop` hook flushes the agent's working scratch to `.memory/T1_episodic/episodes/YYYY-MM-DD/<agent>/<timestamp>.jsonl` automatically. This is the only required write at end of turn.

### A.5 Conflict resolution

- **Two agents propose contradictory T2 entries:** Curator queues both, Strategist reads both episodes, writes a `decisions_log.jsonl` line resolving it. The losing proposal goes to `.memory/T2_semantic/_rejected/` with the reason. Never silently overwrite.
- **Same agent proposes contradicting prior T2:** PreToolUse hook on `Write` to `.memory/T2_semantic/` checks for keyword overlap with existing entries; if hit, blocks with exit 2 and message "this contradicts T2 entry X — escalate to Strategist."
- **Citation conflict in T2 `citations/`:** if a paper supports both sides of a debate, both interpretations are stored as separate entries with `disagreement: true` flag. The no-p-hacking rule then forces the Strategist to pick the side that supports the *prior hypothesis*, not the convenient one.

### A.6 Garbage collection

- T0: cleared at SubagentStop. No retention.
- T1: 30 days hot, then compressed to daily summary in T2 by Curator, raw archived to `_archive/`. Never deleted (archive-by-default rule).
- T2: never garbage-collected. Append-only. Superseded entries get a `superseded_by:` pointer but stay readable.
- T3: never garbage-collected. Git history is the audit trail.

### A.7 Persistence model

All four tiers are filesystem-backed. T1/T2/T3 are inside `~/dev/crypto-bot/` and tracked by git (T0 is gitignored). This means **memory survives every form of session crash, machine reboot, and chat-session reset** — the only thing that gets lost is mid-turn working memory, which is the right thing to lose. Cross-session continuity is automatic: the SessionStart hook reads T3 + last 24h of T1 summaries + the full T2 facts.md and injects them into the main session's context.

### A.8 Tooling recommendation

**Use plain markdown + JSONL files. Do not use Ruflo's memory plugins.** This is the most important single recommendation in this section.

- The independent audits (Ruflo issues #1425, #1482, #1504, #1514, #1330, the gist by roman-rr) found that approximately 290 of Ruflo's 300+ MCP tools are stubs that record state without executing anything. The 8.7 MB ranked-context.json injection wastes 15-25K tokens per session for marginal benefit. The supply-chain incident (issue #1261) where preinstall scripts silently deleted directories is a hard disqualifier for a project that touches financial APIs. Issue #1375 documents a prompt-injection attack via MCP tool descriptions.
- **What's worth keeping from the Ruflo ecosystem:** `ruflo-rag-memory` is genuinely upgraded SOTA (RRF hybrid search, MMR diversity, Graph-RAG multi-hop) per the v3.6.10 release notes — but only if you pin to a verified release and treat it as an *optional* search layer over your filesystem-backed T2, not as the system of record. Pin: `ruflo@3.6.10` exactly, not `@latest`.
- **What to actively avoid:** `ruflo-swarm`, `ruflo-autopilot`, `ruflo-hive-mind`, `ruflo-federation` (all flagged as theater in the audits), `ruflo-neural-trader` (separate domain, do not let it touch your bot).
- **Alternatives considered and rejected:** Letta/MemGPT (heavier than needed; we don't need an OS-style virtual-context manager because Sonnet 4.6 already has 1M context). Zep/Graphiti (excellent, but requires running Postgres+Neo4j, overhead for a single-developer project; revisit at Phase 6). Mem0 ($249/mo for graph tier — not worth it). The plain-files approach beats all of them on auditability, which is the discipline you actually need here.

---

## B. Manager Hierarchy

### B.1 Why three tiers and not four

The user's instinct ("Top / Mid / Normal / Project per class") is borrowed from human org charts and over-counts for this scope. Anthropic's June 2025 multi-agent research paper, the LangGraph supervisor pattern, and the OpenAI Swarm framework all converge on **2–3 levels for production agent systems**, with the empirical finding that adding a fourth level multiplies token cost without improving outcomes (the "15× tokens" Anthropic figure already holds at 2 levels; deeper hierarchies make it worse). For a single-developer, single-codebase project: three levels is the ceiling.

### B.2 ASCII org chart

```
                       ┌───────────────────────────────┐
                       │   HUMAN (Kanin)               │
                       │   Owns: deploys, T3 edits,    │
                       │   sacred-harness mutations,   │
                       │   final go/no-go on Phase 5   │
                       └──────────────┬────────────────┘
                                      │
                       ┌──────────────▼────────────────┐
                       │   STRATEGIST   (Sonnet 4.6)   │
                       │   The single top-level mgr.   │
                       │   Owns: roadmap, T2 writes,   │
                       │   verdict-tree calls, escala- │
                       │   tion to human, kill-switch  │
                       └────┬───────────────┬──────────┘
                            │               │
            ┌───────────────▼──┐         ┌──▼─────────────────┐
            │ PHASE-4B COORD.  │         │ PHASE-5 COORD.     │
            │ (Sonnet 4.6)     │         │ (Sonnet 4.6)       │
            │ Funding-rate     │         │ Prediction-market  │
            │ harvest variants │         │ scanner+research   │
            └─┬──┬──┬──┬──┬──┬─┘         └─┬──┬──┬──┬──┬──┬──┬┘
              │  │  │  │  │  │             │  │  │  │  │  │  │
              ▼  ▼  ▼  ▼  ▼  ▼             ▼  ▼  ▼  ▼  ▼  ▼  ▼
            (workers — see §C)            (workers — see §C)
```

### B.3 What each manager owns

**Strategist (top).** Single instance, runs in the main Claude Code session on Sonnet 4.6. Reads T3 and full T2 at session start. Owns: which phase to work on; which coordinator to invoke; whether a coordinator's output is ready for human review or needs another round; calling the verdict tree on borderline cases; deciding which T1 episodes get promoted to T2; deciding when to escalate to human. Cannot write code. Cannot run bash. Tools: `Read, Grep, Glob, Task` (the Agent tool to spawn coordinators). `permissionMode: plan`. This separation — top-level manager has no write authority — is the single change that most reduces drift in this design, because the Strategist *cannot* skip steps to "just fix it quickly."

**Phase-4B Coordinator.** One instance, spawned per session by the Strategist when work targets Phase 4.B. Sonnet 4.6. Owns: the funding-rate-harvest variation queue (≤17 remaining out of the 20-cap); orchestrating the proposal→adversarial-review→test→verdict cycle; tracking the 3-consecutive-failure counter (stored in `.memory/T1_episodic/_state/phase4b_failure_count.txt`); enforcing the 4-hour compute budget by checking wall clock against `.memory/T1_episodic/_state/session_start.txt`. Tools: `Read, Grep, Glob, Task, Bash(git:*), Bash(echo:*), Bash(date:*)`. Note: this coordinator can commit and push but cannot deploy.

**Phase-5 Coordinator.** Same shape as Phase-4B Coordinator but with a different worker fleet (scanner, parallel researchers, calibrator, sizer). Distinct from 4B because the workflows are structurally different: 4B is sequential (one variation at a time), 5 is fan-out/fan-in (scanner finds N markets, researchers parallelize). Sonnet 4.6.

### B.4 Escalation paths (up)

- Worker → Coordinator: any worker that hits its `maxTurns` cap, encounters a sacred-harness file, or detects a no-p-hacking violation, returns control to its coordinator with a structured failure report.
- Coordinator → Strategist: 3-consecutive-failure trigger, 4-hour budget hit, verdict tree returns borderline (± 0.05 of threshold), or any sacred-file modification proposal.
- Strategist → Human: any deploy step, any T3 mutation, any sacred-harness schema change, any borderline verdict, any case where the no-p-hacking discipline cannot find a peer-reviewed citation, the kill-switch ("stop everything") condition.

### B.5 Delegation paths (down)

- Strategist delegates a phase-scoped goal to one Coordinator. Never bypasses to a worker.
- Coordinator delegates a single bounded task to one worker (Anthropic's "teach the orchestrator how to delegate" finding: each subagent must receive an objective, an output format, tool guidance, and clear boundaries — anything less and they duplicate work).
- Workers do not spawn workers. Period. This rule is enforced by giving workers no `Task` tool. The only way a worker can request more work is to return a "needs follow-up" report to its coordinator.

### B.6 Coordination when scopes overlap

Phase 4B and Phase 5 share infrastructure (data fetchers, validation harness, T2 facts). Conflict resolution: the Strategist holds the lock. When both coordinators want to modify the same shared file, they each propose to the Strategist, who serializes. There is no peer coordination between the two coordinators because that requires the experimental Agent Teams feature (CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1), which has documented stability issues and bypasses the Strategist's audit role.

### B.7 The verdict tree and the borderline rule

The user's existing verdict tree (deterministic outputs from CPCV/DSR/MinTRL gates) handles cases where metrics are clearly above or clearly below threshold. The single hardest discipline question is the **borderline**: a strategy that misses by 0.05 of a threshold. Mechanical rule encoded in `.memory/T3_procedural/borderline_protocol.md`:

1. If DSR is within ±0.05 of threshold: Coordinator sends the trial to Adversarial-Reviewer with explicit instruction "argue this should fail."
2. If Adversarial-Reviewer cannot produce a peer-reviewed citation supporting failure, the trial proceeds.
3. If Adversarial-Reviewer produces a citation, the trial fails and the citation goes into T2 `citations/`.
4. Strategist reviews the outcome; if there is any judgment call beyond mechanical rules, escalate to human.

This protocol is the operational form of the "settle once, encode, never debate again" principle — the borderline rule is now mechanical, not a conversation.

### B.8 The 3-consecutive-failure rule, mechanically

State file: `.memory/T1_episodic/_state/phase4b_failure_count.txt`. Incremented by Phase-4B Coordinator after every failed variation. Reset to 0 after every passing variation. When the counter hits 3, the Coordinator's PostToolUse hook on `Bash` checks the file and, if ≥3, exits with code 2 carrying the message "3-fail threshold hit — Strategist must escalate to human before next variation." This is enforced at the hook layer because the model would otherwise be tempted to "just try one more" once it has invested compute.

### B.9 The 4-hour compute budget circuit breaker

State file: `.memory/T1_episodic/_state/session_start.txt` (epoch seconds, written by SessionStart hook). Every PreToolUse on `Bash` checks `(now - session_start) < 14400`. If exceeded, hook exits 2 with "4-hour compute budget exhausted; Strategist must end session." This is the hard fail. There is no soft warning at 3 hours — that creates a window where the model rushes — instead the Strategist is instructed in T3 to plan as if the budget is 3 hours, and the 4th hour is the buffer.

---

## C. Agent Roster

The roster is twenty agents total: 1 Strategist + 2 Coordinators + 17 Workers. Every worker is single-responsibility (the Anthropic finding: vague subagents duplicate work). Every worker has a tight tool allowlist. Default `permissionMode: plan` unless writes are required. All file paths under `.claude/agents/`.

### C.1 Roster table

| Name | Role | Model | Tools allowlist | permissionMode | maxTurns | Parent | Memory tier R/W | Hooks declared | Skills imported | Escalation triggers |
|---|---|---|---|---|---|---|---|---|---|---|
| `strategist` | Top-level orchestrator: phase/coordinator selection, verdict-tree calls, T2 writes, human escalation | sonnet (Sonnet 4.6) | Read, Grep, Glob, Task | plan | 40 | Human | R: T3+T2+T1; W: T2 (audited) | SessionStart→inject-mandates, PreToolUse→budget-check, SubagentStop→flush-T1 | settle-once, no-p-hacking, sacred-harness | sacred-file edit, deploy, T3 mutation, borderline verdict, 3-fail, 4-hr |
| `phase4b-coordinator` | Variation queue manager for funding-rate harvest | sonnet | Read, Grep, Glob, Task, Bash(git:*) | plan | 30 | strategist | R: T3+T2+T1(own+children); W: T1 own | PreToolUse→budget+failcount, PostToolUse→failcount-update | settle-once, variation-cap, exit-ramp | maxTurns, sacred file touched, 3-fail, 4-hr |
| `phase5-coordinator` | Scanner + parallel research orchestrator for prediction-market bot | sonnet | Read, Grep, Glob, Task, Bash(git:*) | plan | 30 | strategist | Same as phase4b-coord | Same as phase4b-coord | settle-once, kelly-discipline, exit-ramp | Same as phase4b-coord + Polymarket API failure |
| `proposer` | Generates a single variation proposal with theoretical justification | sonnet | Read, Grep, Glob, WebSearch, WebFetch | plan | 12 | phase4b-coord | R: T3+T2+T1; W: T0 only | PreToolUse→citation-required | no-p-hacking, paperqa-lookup | no citation found → fail; webfetch quota |
| `citation-verifier` | Verifies the proposer's citation actually supports the parameter choice | gemini-2.5-pro (via OpenRouter) | Read, WebFetch | plan | 8 | phase4b-coord, phase5-coord | R: T2 citations/; W: T2 citations/ (proposed) | none subagent-level; relies on parent hooks | paperqa-lookup | citation contradicts claim → block |
| `adversarial-reviewer` | Cross-model critic of any proposal or test result | gemini-2.5-pro | Read, Grep, Glob, WebFetch | plan | 15 | phase4b-coord, phase5-coord | R: T3+T2+T1; W: T0 only | none subagent-level | red-team-checklist | structural fault found → block |
| `implementer` | The only agent with write authority. Edits code, runs trials, commits | sonnet | Read, Edit, Write, Bash, Grep, Glob | acceptEdits | 25 | phase4b-coord, phase5-coord | R: T3+T2+T1; W: T1 own + code | PreToolUse→sacred-block + commit-format, PostToolUse→test-runner | implementer-checklist, exit-ramp | sacred file → block; commit format violation → block |
| `validator` | Read-only verification that implementer's output matches the proposal | haiku (Haiku 4.5) | Read, Grep, Glob, Bash(pytest:*), Bash(python -m:*) | plan | 12 | phase4b-coord | R: T3+T2+T1; W: T0 only | none | none | test fail → return failure |
| `analyst` | Interprets a completed test run; writes the result interpretation | sonnet | Read, Grep, Glob | plan | 10 | phase4b-coord, phase5-coord | R: T3+T2+T1; W: T1 own | none | dsr-mintrl-interpreter | borderline → escalate to coord |
| `deep-researcher` | Long-form literature search; uses paperqa2-style flow | gemini-2.5-pro | Read, WebSearch, WebFetch | plan | 25 | phase4b-coord, phase5-coord | R: T2 citations/; W: T2 citations/ (proposed) | none | paperqa-lookup, semantic-scholar | rate limit → fail; webfetch quota |
| `curator` | Hourly cron job that summarizes T1 → T2 candidates | haiku | Read, Write | acceptEdits (scoped to .memory/) | 20 | strategist (cron) | R: T1 all; W: T2 _pending_review.jsonl | PreToolUse→path-allowlist (.memory/T2_semantic/_pending_review.jsonl only) | none | conflict detected → mark CONFLICT |
| `scanner` | Phase 5: scans Polymarket for candidate opportunities | haiku | Read, Bash(curl:*), WebFetch | plan | 8 | phase5-coord | R: T2; W: T1 own | none | polymarket-api | API failure → escalate |
| `calibrator` | Phase 5: trains XGBoost+LLM-feature ensemble; computes Brier | sonnet | Read, Edit, Write, Bash(python:*), Bash(pytest:*) | acceptEdits | 20 | phase5-coord | R: T3+T2+T1; W: T1 own + code | PreToolUse→sacred-block | xgboost-calibration | Brier > threshold → escalate |
| `sizer` | Phase 5: applies fractional Kelly to calibrated probabilities | haiku | Read, Bash(python:*) | plan | 6 | phase5-coord | R: T2; W: T1 own | none | kelly-discipline | edge < threshold → HOLD |
| `notifier` | Sends email summaries via Resend API; never auto-trades | haiku | Read, Bash(curl:*) | plan | 4 | strategist | R: T1 own; W: none | PreToolUse→no-secrets-in-bash | email-templates | API failure → log to T1 |
| `market-analyst` | Phase A overlay: indicator + regime + vol synthesis. Reads OHLCV/RSI/MACD/ATR + 6-regime detector output; writes structured AnalystReport markdown. Shadow-mode write-only until Phase B gate. | haiku | Read, Bash(python:*) | plan | 6 | research-manager | R: T2+T1 own; W: T1 own | none subagent-level | analyst-report-template, regime-flag-rules | data unavailable → degraded report flag |
| `social-analyst` | Phase A overlay: Crypto Twitter/Reddit/fear-greed scan via existing OpenClaw Tavily gateway (127.0.0.1:18789). Shadow-mode write-only until Phase B gate. | haiku | Read, WebFetch, WebSearch | plan | 8 | research-manager | R: T2+T1 own; W: T1 own | PreToolUse → no-secrets-in-bash | analyst-report-template, sentiment-source-list | rate limit / API failure → degraded report flag |
| `news-analyst` | Phase A overlay: crypto news + macro (Fed/CPI/ETF/FOMC). Reads via WebFetch on curated allowlist defined in news-source-allowlist skill (extensible without architecture edit). Shadow-mode write-only until Phase B gate. | haiku | Read, WebFetch, WebSearch | plan | 8 | research-manager | R: T2+T1 own; W: T1 own | PreToolUse → no-secrets-in-bash | analyst-report-template, news-source-allowlist | source unreachable → degraded report flag |
| `fundamentals-analyst` | Phase A overlay: on-chain (sources defined in onchain-source-list skill, extensible without architecture edit), exchange flows, funding rates, OI, basis. Crypto-translation of equity fundamentals role. Shadow-mode write-only until Phase B gate. | haiku | Read, WebFetch, Bash(python:*) | plan | 8 | research-manager | R: T2+T1 own; W: T1 own | PreToolUse → no-secrets-in-bash | analyst-report-template, onchain-source-list | data feed gap → degraded report flag |
| `research-manager` | Phase A overlay synthesizer: reads 4 AnalystReports → emits regime-context score + binary strategy enable/disable flags + binary risk flags. Cross-model dual pass (Sonnet primary, Gemini secondary); divergence escalates. | sonnet (primary), gemini-2.5-pro (secondary via OpenRouter) | Read, Grep, Glob, WebFetch | plan | 12 | strategist | R: T3+T2+T1; W: T1 own | PreToolUse → budget-check | settle-once, exit-ramp, regime-flag-rules | model divergence → escalate to strategist; budget exceeded → block |

### C.2 Notes on model choices

- **Sonnet 4.6 ($3/$15) for judgment-heavy roles** (Strategist, Coordinators, Proposer, Implementer, Analyst, Calibrator). The 1M context at standard pricing is the killer feature: a full Strategist invocation with all of T3 and the last 24h of T1 summaries fits well under the cache discount.
- **Haiku 4.5 ($1/$5) for high-volume/low-judgment roles** (Validator, Curator, Scanner, Sizer, Notifier, Market-Analyst, Social-Analyst, News-Analyst, Fundamentals-Analyst). 73.3% on SWE-bench Verified is more than enough for these roles. The cost ratio (3× cheaper than Sonnet) compounds over the variation queue.
- **Gemini 2.5 Pro ($1.25/$10) via OpenRouter for adversarial review** (Citation-Verifier, Adversarial-Reviewer, Deep-Researcher). Cross-model is the design choice, not just cross-prompt — the empirical finding from the Brainstorm/metaswarm/Octopus literature is that adversarial bandits beat stochastic bandits, and that requires a different model family. Gemini's 1M context handles long literature passes. The "concern-acceptance conflict" failure mode (BadScientist paper) is mitigated specifically because the reviewer is a different family.
- **Why not Opus 4.6 anywhere?** Cost ($5/$25) does not justify the marginal improvement for any role here. The Strategist's judgment is bounded by the verdict tree and human escalation; Sonnet 4.6 is sufficient. Reserve Opus for one-off ad-hoc deep-thinking sessions launched manually outside this fleet.
- **Why GPT-5.x not in the roster?** Adding a third model family doubles the API-key surface area and doubles the rate-limit failure modes, with diminishing returns past the 2-model adversarial pair (the Auto-Research-In-Sleep paper makes this argument explicitly: "2 is the minimum to break self-play; 4 is rarely worth it").
- **Cross-model dual pass on Research Manager** (Sonnet primary + Gemini 2.5 Pro secondary) is the design choice for the analyst-overlay synthesis step. Bias-reduction is at the *synthesis* step, not at the *case-building* step — Bull and Bear analysts in the TradingAgents reference design read the same input, so splitting them is theatre (chat 099a169c finding 2026-05-03). Cross-model synthesis surfaces genuinely different blind spots; same logic that puts Citation-Verifier and Adversarial-Reviewer on Gemini. Cost: ~2× a single-model pass; ~½× two-separate-agents because the second pass reuses the same input context.

### C.3 The subagent-hooks gap (issue #34692) and how this design works around it

**Critical bug to know about:** Anthropic issue #34692 documents that `PreToolUse` and `PostToolUse` hooks configured in `~/.claude/settings.json` *do not fire* when a subagent makes a tool call. They only fire for the main session thread. This silently bypasses every safety hook the moment work is delegated.

**Workaround that this architecture uses:** Every safety-critical hook is duplicated in two places: (1) the global `~/.claude/settings.json` for main-session enforcement, and (2) inline in each subagent's frontmatter `hooks:` field. Frontmatter hooks fire for the subagent's own tool calls, which is exactly what we need. This is more verbose but it is correct. The duplication is visible in the example frontmatter snippets in §E and the day-by-day plan in §F.

**Plugin subagents are excluded from this design** because plugin subagents do not support the `hooks`, `mcpServers`, or `permissionMode` frontmatter fields per Anthropic's documentation — a hard disqualifier for any agent that touches the validation harness.

### C.4 Example frontmatter — Implementer (the most safety-critical)

```yaml
---
name: implementer
description: |
  Implements a single approved variation. Edits code, runs the validation
  harness, and commits via heredoc-embedded message. Use ONLY after
  Citation-Verifier and Adversarial-Reviewer have signed off in
  decisions_log.jsonl. Cannot deploy. Cannot edit sacred-harness files.
model: sonnet
tools: Read, Edit, Write, Bash, Grep, Glob
disallowedTools: WebFetch, WebSearch
permissionMode: acceptEdits
maxTurns: 25
isolation: worktree
memory: project
skills:
  - implementer-checklist
  - exit-ramp
hooks:
  PreToolUse:
    - matcher: "Edit|Write"
      hooks:
        - type: command
          command: "$HOME/dev/crypto-bot/.claude/hooks/sacred-block.sh"
          timeout: 5
    - matcher: "Bash"
      hooks:
        - type: command
          command: "$HOME/dev/crypto-bot/.claude/hooks/no-deploy.sh"
          timeout: 5
        - type: command
          command: "$HOME/dev/crypto-bot/.claude/hooks/commit-heredoc-required.sh"
          timeout: 5
  PostToolUse:
    - matcher: "Edit|Write"
      hooks:
        - type: command
          command: "$HOME/dev/crypto-bot/.claude/hooks/run-tests-fast.sh"
          timeout: 60
  Stop:
    - hooks:
        - type: command
          command: "$HOME/dev/crypto-bot/.claude/hooks/exit-ramp-check.sh"
          timeout: 10
---
You are the Implementer. You execute one approved variation from
phase4b-coordinator. Process:

1. Read the approval line from decisions_log.jsonl (the line your
   coordinator references). If not present, STOP and return failure.
2. Read T3 mandates and the relevant T2 facts.
3. Make the minimal diff. Sacred-harness files are blocked at the hook
   layer; do not attempt to edit them.
4. Run pytest and the validation harness. If anything fails, do NOT commit;
   return the failure report to your coordinator.
5. Commit using a single Bash call with heredoc:
   git commit -m "$(cat <<'EOF'
   <type>(<scope>): <description>
   ...
   EOF
   )"
   Never separate the message from the commit. The commit-heredoc-required
   hook enforces this.
6. Surface git log -1 for human review. Push remains human-only
   per architecture deliberation 2026-05-03; same boundary as
   deploy. Do not push.
7. End by writing the exit-ramp summary to T1 (see exit-ramp skill).

You CANNOT deploy. doctl, kubectl, ssh, docker push are blocked at the
hook layer.
```

---

## D. Example workflows

### D.1 Workflow #1 — The user's scenario: analyzing a retired result and looping until solution

**Trigger:** Strategist sees a retired-strategy postmortem in T2 and the human says "look at why this failed and see if we can salvage anything."

```
Human → Strategist
Strategist reads .memory/T2_semantic/retired_strategies/<name>.md + T3 mandates.
Strategist → Task("phase4b-coordinator: analyze retire of <name>; loop until solution or 3-fail")

Phase4B-Coord (turn 1):
  → Task("analyst: read retired postmortem + last 5 trial.log entries; produce
         hypothesis on root cause")
  Analyst returns: "Hypothesis: feature drift on funding-rate volatility
                   regime change; supports retire."
  Coord writes T1 episode: "analyst-1 hypothesis recorded."

Phase4B-Coord (turn 2):
  → Task("adversarial-reviewer (Gemini): attack analyst's hypothesis;
         find at least one alternative explanation")
  Reviewer returns: "Counter-hypothesis: position sizing was the actual fault,
                     not feature drift. See Lopez de Prado 2018 ch.14."
  Coord writes T1 episode: "reviewer-1 counter-hypothesis + citation pending."

Phase4B-Coord (turn 3):
  → Task("citation-verifier (Gemini): verify Lopez de Prado 2018 ch.14
         actually supports the counter-hypothesis")
  Verifier reads the chapter via paperqa-lookup skill, returns:
  "Confirmed: ch.14 §3 explicitly addresses size-vs-feature attribution.
   Citation-key: lopezdeprado_2018_ch14_§3."
  Coord writes T2 candidate citation; Strategist will accept on next pass.

Phase4B-Coord (turn 4):
  → decision: do we have enough? Coord checks the borderline-protocol
  rules file. Two competing hypotheses, both supported. Need a test.

Phase4B-Coord (turn 5):
  → Task("deep-researcher (Gemini): find published method for distinguishing
         feature-drift fault from sizing fault in funding-rate strategies")
  Researcher returns: "Two candidate methods: causal forest attribution
   (Athey 2019) and Shapley feature decomposition. Both have implementations
   in scikit-learn ecosystem."
  Coord writes T1 episode + T2 candidate citations.

Phase4B-Coord (turn 6):
  → Task("proposer: design a re-test variation isolating the two hypotheses,
         with explicit theoretical justification citing the two new T2
         entries; respect 20-cap (this is variation N+1 of 17 remaining)")
  Proposer returns: structured variation with citations, parameter ranges
  pre-justified, no p-hacking smell.

Phase4B-Coord (turn 7):
  → Task("adversarial-reviewer: red-team the proposal; argue it should fail")
  Reviewer either approves or blocks. If blocks, return to proposer for
  revision (max 2 revision cycles per the implementer-checklist skill).

Phase4B-Coord (turn 8):
  → Task("implementer: execute the approved variation; run validation harness;
         commit using heredoc")
  Implementer (in worktree isolation) implements, tests pass or fail.

Phase4B-Coord (turn 9):
  → Task("validator (Haiku): verify implementer's output matches the
         approved proposal; no scope creep")
  Validator returns clean or dirty.

Phase4B-Coord (turn 10):
  → Task("analyst: interpret the new test result against the two hypotheses")
  Analyst returns conclusion: e.g., "Sizing fault confirmed; salvage path
  exists by changing fractional Kelly multiplier."

Phase4B-Coord returns to Strategist with full chain summary.
Strategist promotes T2 candidates, writes decisions_log.jsonl entry,
generates exit-ramp (commit bash, repomix regen list, next-chat handoff)
and emails human via Notifier.
```

This is the user's exact described loop — analyze → reviewer → analyze → research → analyze again → test — encoded mechanically. Every transition between agents is a `Task` call from the coordinator. No agent talks directly to another. The Strategist never participates in the inner loop, only at start and end.

### D.2 Workflow #2 — Phase 4.B Variation #N proposal-to-verdict end-to-end

```
Strategist → Task(phase4b-coordinator, "Run variation N of 17 remaining")

phase4b-coord:
  Step 1: PreToolUse hook checks failure_count < 3 and elapsed < 4hr.
  Step 2: Read .memory/T2_semantic/facts.md for prior variations' outcomes.
  Step 3: Task(proposer) — generate proposal-N with theoretical justification
          and a multi-source citation_set per proposer.md. PreToolUse
          citation-required hook on proposer's WebFetch ensures a primary
          peer-reviewed citation_key declaration is present before any web
          search begins (the slug must also appear as the source_ref of the
          peer_reviewed entry in citation_set).
  Step 4: Task(citation-verifier) — per-parameter evaluation of citation_set
          against per-type acceptance rules (peer_reviewed / exchange_spec /
          post_tax_model / chat_decision_with_rationale; full schema in
          citation-verifier.md). Output is one of ALL_VERIFIED /
          PARTIAL_REJECT / FULL_REJECT. ALL_VERIFIED advances; PARTIAL_REJECT
          and FULL_REJECT both increment phase4b_failure_count and abort the
          cycle (see body prompt for the loop-back vs abort divergence with
          the original D.2 text — body prompts are authoritative on
          coordinator behavior; this step description is the contract shape).
  Step 5: Task(adversarial-reviewer) — Gemini attacks the proposal.
          Output: APPROVE / REVISE / BLOCK + structured rationale.
  Step 6: If APPROVED: phase4b-coord writes a single decisions_log.jsonl
          line: {ts, variation: N, status: approved, citations: [...],
                 reviewer_signoff: <hash>}.
  Step 7: Task(implementer, isolation=worktree) — implements + runs harness.
          PreToolUse sacred-block hook + no-deploy hook fire on every
          Edit/Write/Bash. PostToolUse run-tests-fast hook runs unit tests
          after each Edit. The implementer uses heredoc commit format
          (commit-heredoc-required hook blocks otherwise).
  Step 8: Task(validator, Haiku) — verify implementer didn't drift from
          the approved scope.
  Step 9: Read trial.log and holdout_manifest.json (sacred — read-only).
          Apply verdict tree:
          - DSR ≥ threshold + final_gate pass → PROCEED to deploy queue
            (human-only step)
          - DSR < threshold by > 0.05 → FAIL, increment failure_count,
            archive variation
          - DSR within ±0.05 of threshold → BORDERLINE: re-invoke
            adversarial-reviewer with explicit "argue this should fail"
            instruction. If reviewer finds peer-reviewed support for
            failure → FAIL. Otherwise → PROCEED.
  Step 10: Task(analyst) — write the result interpretation to T1.
  Step 11: phase4b-coord emits exit-ramp report to Strategist.

Strategist:
  - Promotes accepted T2 candidates.
  - Writes one decisions_log.jsonl line summarizing the variation.
  - Calls Notifier with the email summary (Resend API).
  - Generates the exit-ramp: commit bash for any unsigned-off work,
    repomix regen command, re-upload list, next-chat handoff text.
  - Returns control to human.
```

### D.3 Workflow #3 — Phase 5 prediction-market session

This workflow uses fan-out parallelism, which is structurally different from 4B's sequential loop.

```
Human → Strategist: "Run a Phase 5 scan."
Strategist → Task(phase5-coord, "Scan Polymarket; surface top-K opportunities;
                  run research + calibration; produce execution recommendation
                  to human (NOT auto-execute).")

phase5-coord:
  Step 1: PreToolUse budget+failcount checks pass.
  Step 2: Task(scanner, Haiku) — fetches Polymarket Gamma API + CLOB top
          200 active markets; filters by liquidity, time-to-resolution,
          spread; returns top 10 candidates.
  Step 3: FAN-OUT — phase5-coord spawns up to 5 parallel deep-researcher
          (Gemini) tasks, one per top candidate (rate-limit-bounded; Anthropic
          allows up to 10 parallel subagents).
          Each researcher: scans news, X/Twitter via Tavily search (configured
          in OpenClaw gateway at 127.0.0.1:18789), produces a structured
          probability estimate with confidence interval and citation list.
  Step 4: FAN-IN — phase5-coord aggregates the 5 research outputs.
  Step 5: Task(adversarial-reviewer, Gemini) — for each candidate where the
          research-implied probability differs from market price by >8%,
          attack the research-implied probability. Returns approve/revise
          per candidate.
  Step 6: Task(calibrator) — runs the XGBoost+LLM-feature ensemble (locally
          on the bot's machine, NOT on DigitalOcean — no deploy from agents).
          Outputs calibrated probabilities + Brier score on backtest.
          Sacred-harness hooks ensure validation_framework/ is not modified.
  Step 7: Task(sizer, Haiku) — applies fractional Kelly (quarter-Kelly per
          standard discipline) with hard caps; outputs position sizes.
          Edge < 2% → HOLD per kelly-discipline skill.
  Step 8: Task(analyst) — writes a human-readable execution recommendation:
          "Market X: edge 14%, Kelly-quarter size $Y, recommended action.
           Confidence: high. Cited: [paperqa-keys]."
  Step 9: phase5-coord returns to Strategist with the recommendation packet.

Strategist:
  - Writes T1 episode for the full session.
  - Calls Notifier with email: subject "Phase 5 scan: <N> opportunities,
    top: <market>, edge <X>%, recommended size $<Y>"; body has the full
    structured recommendation. NEVER includes private keys or API secrets
    (no-secrets-in-bash hook on Notifier blocks them).
  - Returns to human. HUMAN executes the trade manually on Polymarket;
    agents cannot place orders (no-deploy boundary extends to no-execute
    on prediction markets in this design — a deliberate Phase-5 gate).
```

The deliberate decision: **agents recommend, human executes.** This is the same boundary as the deploy boundary. Polymarket ordering goes through the same circuit as `doctl` — explicitly disallowed at the hook layer. The literature on calibrated contrarian betting (the prediction-market strategy guide; Manski 2006; Wolfers & Zitzewitz 2006) supports manual execution at this stage because position sizing and timing both benefit from human judgment until the calibration process has accumulated 100+ resolved bets of feedback. Revisit this boundary at Phase 6.

### D.4 Workflow #4 — Analyst overlay cycle (Phase A shadow-mode → Phase B gate → Phase C live wiring)

This workflow runs on a fixed cadence (8h aligned to OKX funding settlements: 00:00, 08:00, 16:00 UTC) plus regime-change triggers. Cost target: ~$32/mo at 8h cadence; ~$65/mo at 4h. Cap via existing `budget-check.sh`. Cadence may rise to 4h after Phase B gate if shadow-mode evaluation shows finer cadence is justified.

```
Cron entry (or regime-change trigger) → research-manager (Sonnet primary)

research-manager:
  Step 1: PreToolUse budget-check; if monthly cap reached → block + alert
          Strategist via Notifier; this cycle is skipped.
  Step 2: Read .memory/T1_episodic/_state/regime.txt for current
          deterministic regime label.
  Step 3: Spawn 4 analysts in parallel (FAN-OUT):
          Task(market-analyst), Task(social-analyst),
          Task(news-analyst), Task(fundamentals-analyst).
          Each writes its AnalystReport markdown to
          .memory/T1_episodic/episodes/<date>/analysts/<name>_<utc-hour>.md
          per the analyst-report-template skill.
  Step 4: FAN-IN: research-manager reads the four reports.
  Step 5: Sonnet synthesis pass — produces draft synthesis with strategy
          enable/disable + binary risk flags + cross-analyst rationale.
  Step 6: Gemini secondary pass — same input + Sonnet's draft as
          additional context; produces independent synthesis.
  Step 7: Compare. For each binary flag:
          - Both models agree → flag adopted
          - Models disagree → flag held + divergence logged + escalation
            note added to synthesis output
  Step 8: Write synthesis markdown to
          .memory/T1_episodic/episodes/<date>/synthesis/research_manager_<utc-hour>.md
  Step 9: Phase A (shadow): write-only. Synthesis lives at the path above
          and nowhere else. portfolio.manager.py and CapGuard do NOT read it.
          Phase C (live wiring, post-gate): also publish to
          .memory/T1_episodic/_state/analyst_overlay.json which CapGuard
          and portfolio.manager.py read at next rebalance step.
  Step 10: If divergence escalation: research-manager spawns Strategist
           with the divergence packet. Strategist decides: surface to
           human via Notifier, or proceed with the deterministic-regime
           fallback for this cycle.
  Step 11: Exit-ramp: research-manager writes a one-line entry to
           decisions_log.jsonl summarizing the cycle (regime label,
           flags emitted, divergence count, cost in $).
```

**Phase A → Phase B → Phase C gating:**

- **Phase A (shadow):** synthesis is computed and written to disk; nothing reads it. Duration: 2 months minimum (~180 cycles at 8h cadence). Each retired or newly-passed verdict during this window is paired with the concurrent synthesis for evaluation.
- **Phase B (gate decision):** chat-side decision after ≥30 paired observations. Specific gate metric (e.g., regime-classification accuracy vs deterministic detector, paper-Sharpe with-vs-without analyst flags) deferred until shadow data lands. Three outcomes: proceed to Phase C; keep gathering data; retire the overlay.
- **Phase C (live wiring):** synthesis output drives CapGuard's `excluded_strategies` list and a new risk-flag check in portfolio.manager.py rebalance step. Live-wiring is gated by `paper_mode=True` for first month; live-mode wiring is a separate Phase 5 deploy decision.

**Source allowlist extensibility:** the news-source-allowlist and onchain-source-list skill files (`.claude/skills/`) are append-only; sources can be added without an architecture edit. This keeps the architecture document stable while allowing operational source curation. The sentiment-source-list skill follows the same pattern.

**Autonomous research loop integration:** the loop driver is the Strategist agent's scope, extended via a new `next-variation-selector` skill. Strategist reads a new T2 file `.memory/T2_semantic/research_queue.md` listing candidate citation-leads + retired strategies due for re-test, plus research-manager's regime context. So when V1 retires, Strategist's choice of "what citation lead to research next" is regime-conditional: if research-manager flags "perp basis dislocations widening", deep-researcher gets directed at basis-trade literature first. This is how the analyst overlay informs the loop without producing trade decisions itself.

**Curator scope extension:** curator (existing Haiku, hourly cron) gets new responsibility: scan recently-resolved decisions_log entries and update research_queue.md. This extends curator's existing T1→T2 promotion role without a new agent.

**Retired strategies are not terminal.** Each retired strategy gets an entry in research_queue.md with cooldown rules: initial 30 days, 60 days after each subsequent re-test, capped at 180 days. Re-test eligibility requires: new citation surfaced by deep-researcher OR regime context flagged as "previously-untested-by-this-strategy".

**Phase 4.C "0 pass" branch reframing:** the original MASTER_PLAN line "0 pass → wind down" is reframed as "0 pass → continuation via autonomous research loop; wind-down is a separate downstream decision contingent on continuation also failing across N additional cycles." Continuation includes retired-strategy re-tests on cooldowns above. MASTER_PLAN.md edit in Section 3 below carries the parallel update.

---

## E. Enforcement layers

> **Rewritten 2026-09-02 (governance port from siamese-reconcile, HEAD
> `2f13045`).** Layers 2–4 below described a **bash** hook layer that was not
> running. Two independent defects, both measured that day:
>
> 1. **Every bash hook failed open.** All fifteen parsed stdin with `jq`, and
>    `jq` is not installed on this machine. Each exited 127, and Claude Code
>    treats any exit code other than 2 as non-blocking. `sacred-block.sh` —
>    described below as "the ONE rule that absolutely must not be bypassable" —
>    blocked nothing, for months.
> 2. **`.githooks/` was never activated.** E.3 and E.4 both say "already
>    installed" / "add this hook today", and both files exist and are correct.
>    But they only run when `core.hooksPath` points at `.githooks/`, and it
>    never did. git ran `.git/hooks/`, which held only the trial-queue
>    validator.
>
> A third, smaller defect: two copies of the sacred list had drifted.
> `sacred-block.sh` guarded `^MASTER_PLAN\.md$` — a path that does not exist
> in this repo — while `.githooks/pre-commit` guarded the real
> `docs/MASTER_PLAN.md`.
>
> The lesson is not "bash is bad". It is that **nothing exercised the hooks**,
> so nothing could tell a working guard from an inert one. Layer 5 below
> (`eval/run_tier1.py`) exists to close exactly that, and it carries a
> `--self-check` mode that proves the fixtures themselves can go red.

Six layers, each with a single responsibility. Layered defense; no single
point of failure. The failure mode that matters is not a layer being absent —
it is a layer being **present and inert**, which is why layer 5 tests the
others rather than adding rules of its own.

| # | layer | kind | what it can see |
|---|---|---|---|
| 1 | `CLAUDE.md` + `.claude/rules/**` | advisory | everything; model-readable, model-violable |
| 2 | `.claude/hooks/*.py` (PreToolUse / PostToolUse / Stop / SessionStart / PreCompact) | deterministic, tool-call scoped | one tool call at a time: its path, its command string |
| 3 | `.githooks/commit-msg` | deterministic, commit scoped | the commit message |
| 4 | `.githooks/pre-commit` + `scripts/pre_commit_backlog_check.sh` | deterministic, commit scoped | the staged diff |
| 5 | `eval/run_tier1.py` | test harness | whether layers 2–4 actually fire |
| 6 | `.claude/rules/policies/*.policy` via `scripts/policy_engine.py` | deterministic, session-ordered | ORDERING across tool calls ("B is forbidden unless A succeeded since the last relevant change") |

### E.1 Layer 1 — `CLAUDE.md` (advisory, model-readable)

This is the constitution. It is advisory in the sense that the model can technically violate it, but it is the place that defines all the rules in the model's natural language. Keep it under 15KB. Sections:

```
# Mandates (load-bearing)
- Mandate A: read-before-respond. ALWAYS Read(<file>) before claiming
  knowledge of its content. If asked about a file you haven't read this
  turn, your first action is Read.
- Mandate F: no options-menu. Recommend ONE option with reasoning. If
  truly torn, list the two and pick one anyway with the deciding factor.
- Mandate H: heredoc commit always. git commit message MUST be embedded
  in a heredoc inside the same Bash call. NEVER separate the message
  from the commit.
- Mandate S: sacred-harness immutable. Do not propose Edits to trials.py,
  holdout.py, validation_framework/ schema, CLAUDE.md, MASTER_PLAN.md,
  or validation_framework.md. Propose, do not execute.
- Mandate D: no-deploy. doctl, kubectl, ssh, docker push, digitalocean
  CLI: never invoke. Recommend, ask the human.
- Mandate P: no p-hacking. Every parameter choice cites a peer-reviewed
  source whose claim was verified by citation-verifier.
- Mandate X: exit-ramp on every session end. Surface: commit bash for
  uncommitted work, repomix regen command, re-upload file list, and
  the next-chat handoff text.

# Memory tier rules
... (T0-T3 as specified above)

# Roster
... (links to .claude/agents/*.md)

# Phase plan
- Phase 4B in progress: ≤17 variations remaining, V1 retired post-holdout.
- Phase 5 next: scanner → research-fanout → calibrator → sizer → recommend
  to human (no auto-execute).
```

### E.2 Layer 2 — Python guard hooks (deterministic, tool-call scoped)

Wired **globally** in `.claude/settings.json` (PreToolUse on `Write|Edit` and
`Bash` and `""`; PostToolUse likewise; Stop; SessionStart; PreCompact), and
per-agent in each `.claude/agents/*.md` frontmatter where a guard is
agent-specific. The global wiring is itself part of the fix: the bash layer
ran only inside three agent frontmatters, so the main session — where most
work happens — was unguarded even in principle.

Every guard **fails closed**. Two failure paths are handled explicitly,
because both were live vulnerabilities in the predecessor:

- an unhandled exception exits **2** (block), never 1 — Claude Code treats a
  non-2 exit as non-blocking, so a crashing guard would otherwise wave the
  call through;
- a failed import of the tier map blocks rather than falling through to
  "nothing matched → allow".

**The tier map lives in exactly one file:** `.claude/hooks/file_tiers.py`.
Every hook imports `SACRED_PATTERNS` / `SCHEMA_STABLE_PATTERNS` from it. The
predecessor kept three inline copies, and they had already drifted.

| hook | event | responsibility |
|---|---|---|
| `file_tiers.py` | (library) | the ONE tier map: Tier 1 sacred, Tier 2 schema-stable, Tier 3 autonomous |
| `bash_targets.py` | (library) | the ONE Bash parser: heredoc stripping, write-target extraction, quote-aware segment tokenizing |
| `_block_record.py` | (library) | best-effort block records to `.memory/T1_episodic/blocks/` for the curator; never affects the exit code |
| `path-allowlist.py` | PreToolUse `Write\|Edit`, `Bash` | the three-tier gate. Tier 1 → exit 2 unless `SACRED_OVERRIDE_FILES` names the path; Tier 2 → allow with a NOTE; Tier 3 → silent. **Also scans Bash write targets** (`>`, `>>`, `tee`, `sed -i`, `cp`, `mv`, `ln`, `rm`, `python open(...,'w')`) — the bash predecessor looked only at `Write`/`Edit`, so a redirect was a complete bypass |
| `no-secrets-in-bash.py` | PreToolUse `Bash` | credential literals (Anthropic / OpenRouter / OpenAI / Resend / Slack / AWS / Google / Tavily / PEM) and any read of `~/.crypto-bot.env`. `${VAR}` expansions are stripped first, so legitimate env use never trips |
| `no-deploy.py` | PreToolUse `Bash` | live-venue verbs (`doctl`, `kubectl`, `docker push`, `digitalocean`, `dokku`) plus the irreversible git operations that stayed Human-only after the 2026-09-02 boundary move: force-push, branch deletion, `reset --hard main`. A plain `git push origin main` is **allowed** (mandate G). `--strict` adds `ssh` and bare `deploy`, and stays agent-scoped because those two false-block ordinary work |
| `commit-guard.py` | PreToolUse `Bash` | mandate G shape (message must be heredoc-embedded) **and** mandate H content (conventional subject; `[mandate-H]` required when a `Co-authored-by: Claude` trailer is present). Merges the two archived commit hooks, which always fired together on the same matcher parsing the same field |
| `citation-required.py` | PreToolUse `WebFetch\|WebSearch`, and `research/*-literature.md` edits | mandate P: a `citation_key:` must already be declared in the transcript. Fails closed on an unreadable transcript, because an unverifiable citation is indistinguishable from an absent one |
| `curator-write-allowlist.py` | PreToolUse `Write` (curator only) | the curator's single writable path. **Renamed** from `path-allowlist.sh` so it is not confused with the repo-wide gate above — they are different guards |
| `provenance-guard.py` | PreToolUse `Write\|Edit`, `Bash` | origin × domain. Under `CLAUDE_WRITE_ORIGIN=background_review` an autonomous write may touch only artifacts the background loop authors; foreground is a pure no-op. Orthogonal to the tier gate: it constrains **non-sacred** files too |
| `policy-engine.py` | PreToolUse + PostToolUse | adapter for layer 6 (below) |
| `observe.py` | PreToolUse + PostToolUse (all tools) | one PII-safe JSON record per tool call. Never the command string, never file contents — first token, byte counts, path + tier |
| `session-start.py` | SessionStart | loads T2 facts + open backlog + last state snapshot into the new session |
| `session-end.py` | Stop | writes the T1 episode the curator reads |
| `pre-compact.py` | PreCompact | flushes a state marker at the moment context is about to be lost |
| `post-commit-sync.py` | PostToolUse `Bash` | after a successful `git commit`, runs `scripts/post_commit_sync.sh --no-push` detached: repomix refresh, **never** a push |

**Kept as bash** (no Python equivalent, and none of them is a security gate):
`inject-mandates.sh` (UserPromptSubmit), `commit-scope-audit.sh` (advisory
diff-size heuristic; parses JSON with `python`, not `jq`),
`pre-commit-queue-validate.sh`, `regime-change-trigger.sh`,
`failcount-{check,update}.sh`, `flush-T1.sh`, `run-tests-fast.sh`,
`budget-check.sh`, `exit-ramp-check.sh`. **Six of those still use `jq` and are
therefore still inert on this machine** — tracked as BK-0005 rather than left
unmentioned.

**Archived, not deleted:** the eight superseded bash hooks live in
`.claude/hooks/_archive_bash_2026-09/` with a README mapping old → new.

### E.3 Layer 3 — `.githooks/commit-msg` (now actually installed)

Unchanged in content: blocks an empty message, notes a subject-only commit,
and requires `[mandate-H]` when the `Co-authored-by: Claude` trailer is
present — defense-in-depth in case layer 2 was bypassed.

What changed on 2026-09-02 is that it **runs**. `core.hooksPath` was never
set, so git ignored `.githooks/` entirely. `scripts/install_git_hooks.sh` now
writes a marker-delimited block into `.git/hooks/commit-msg` that chains to
it. Chaining rather than flipping `core.hooksPath` is deliberate: flipping it
would have silently dropped `pre-commit-queue-validate.sh`, which lives in
`.git/hooks/`.

### E.4 Layer 4 — `.githooks/pre-commit` + the Mandate L backlog gate

`.githooks/pre-commit` was already written and already correct — it just was
not running (see E.3). It performs:

1. **Sacred-harness diff block.** Any staged path matching the sacred regex
   aborts unless `CLAUDE_HUMAN_OVERRIDE=1` is set on the commit. Note this
   list is *narrower* than `file_tiers.py`'s Tier 1: it covers the
   audit-critical files (`trials.py`, `holdout.py`, `validation_framework/`,
   `CLAUDE.md`, `docs/MASTER_PLAN.md`, `docs/validation_framework.md`) rather
   than the whole guard layer. Two lists with different jobs, at different
   layers — not a duplicate.
2. **Magic-number gate** on `strategies/*.py` (`.githooks/check-magic-numbers.py`).
3. **`pytest -m fast`** with a 60s ceiling.

Chained after it, `scripts/pre_commit_backlog_check.sh` adds **Mandate L**:

4. **Backlog schema validation.** Every line of
   `.memory/T2_semantic/backlog.jsonl` must satisfy
   `schemas/backlog.schema.json`.
5. **Unrecorded-gap gate.** If a staged diff introduces one of the gate's
   trigger keywords without a staged backlog append, the commit is blocked
   with a `GATE_BACKLOG_*` reason code naming what unblocks it. The keyword
   list is **not reproduced here** — it lives in the `KEYWORDS` line of
   `scripts/pre_commit_backlog_check.sh` and nowhere else.

   Two drafting notes, both earned the hard way while writing this section.
   First, copying the keyword list into this paragraph made the gate flag its
   own definition — correctly, since it cannot tell a definition from a
   confession. Second, even the gate's *natural name* tripped it, so this
   paragraph deliberately avoids the vocabulary. The alternative was adding
   `docs/architecture.md` to the gate's exemption list, which was rejected: a
   design spec is a plausible place to quietly park an item that should have
   been recorded, so exempting it would cost real coverage to save a word. `.claude/rules/**` and the archive
   README *are* exempt, on the narrower ground that their entire job is to
   quote the discipline.
6. **Bypass detection.** It writes a sentinel carrying the staged tree hash
   (`started` at entry, `passed` on success). `--no-verify` skips pre-commit
   but **not** post-commit, so `scripts/post_commit_verify.sh` compares the
   new commit's tree against the sentinel and writes a durable violation
   record when the gate never ran — or ran, failed, and was bypassed anyway.

Install all three with `bash scripts/install_git_hooks.sh` (idempotent;
replaces only its own marker block).

### E.5 Layer 5 — `eval/run_tier1.py` (the layer that tests the layers)

This is the layer whose absence let the other five rot. It drives every guard
hook against pinned JSONL fixtures — a payload in, an expected exit code and
stderr substring out — as **one command**:

```
python eval/run_tier1.py          # 133 fixtures, the gate
python eval/run_tier1.py --self-check
```

Two properties earn it a place in the stack rather than in `tests/`:

- **It strips ambient env.** `SACRED_OVERRIDE_FILES`, `CLAUDE_WRITE_ORIGIN`
  and the policy-engine knobs are removed from every fixture's environment
  unless the fixture asks for them explicitly. Without that, running the suite
  inside an authorized session would flip every sacred-block fixture to green
  for the wrong reason.
- **`--self-check` proves the fixtures can FAIL.** It copies the hooks, replaces
  every blocking guard with a stub that always exits 0 — literally the shape
  the jq-less bash hooks had — and asserts the suite goes red (80 of 133 do).
  A check that cannot fail certifies the gap it was meant to catch
  (`.claude/rules/vertical_slice_loops.md`, the discriminating-check rule).

### E.6 Layer 6 — the policy DSL (ordering across tool calls)

Layers 2–4 each see one event. Layer 6 is the only one that can express
**ordering**: "B is forbidden unless A succeeded since the last relevant
change." Engine `scripts/policy_engine.py`, adapter
`.claude/hooks/policy-engine.py`, policies `.claude/rules/policies/*.policy`,
enforcement class map `.claude/rules/enforcement_policy.json`. Full semantics,
deviations and honest limits: `.claude/rules/enforcement.md`.

The shipped policy binds one block rule and two notifies:

- **`tier1-eval-before-commit`** — an edit to `.claude/**`, `eval/**` or
  `schemas/**` blocks `git commit` until `python eval/run_tier1.py` has run
  green **as its own tool call**. (Chaining it into the commit command cannot
  satisfy the gate: at PreToolUse the exit code does not exist yet.)
- **`literature-lock-reminder`** on `research/**` writes — the no-p-hacking rule.
- **`trials-log-reminder`** on `backtest/trials.log` writes — tripwire 1.

Policies live under `.claude/rules/` deliberately: that path is Tier 1 sacred,
so an agent cannot soften its own gate.

### E.7 Mapping rules → enforcement layer

| Rule | L1 rules | L2 hook | L3 commit-msg | L4 pre-commit | L5 eval | L6 policy |
|---|---|---|---|---|---|---|
| No-p-hacking | mandate P, `.claude/rules/backtest.md` | `citation-required.py` | — | magic-number gate | fixtures | `literature-lock-reminder` |
| Sacred-harness | Core principles | `path-allowlist.py` | — | sacred-diff-block | fixtures | — |
| trials.log write discipline | tripwire 1 | `path-allowlist.py` (Bash targets) | — | — | fixtures | `trials-log-reminder` |
| 20-variation cap | `.claude/rules/backtest.md` | `failcount-check.sh` (⚠ jq-inert) | — | — | — | — |
| 3-consecutive-failure | `.claude/rules/backtest.md` | `failcount-check.sh` (⚠ jq-inert) | — | — | — | — |
| 4-hour budget | `.claude/rules/backtest.md` | `budget-check.sh` (⚠ jq-inert) | — | — | — | — |
| No live deploy / live capital | Human-only list | `no-deploy.py` | — | — | fixtures | — |
| Force-push / branch delete | Human-only list | `no-deploy.py` | — | — | fixtures | — |
| Push of gated work (ALLOWED) | mandate G | `no-deploy.py` passes it | — | — | fixture | — |
| Secrets | Human-only list | `no-secrets-in-bash.py` | — | — | fixtures | — |
| Heredoc commit + `[mandate-H]` | mandate G/H | `commit-guard.py` | format check | — | fixtures | — |
| Mandate L backlog | mandate L | — | — | backlog gate + sentinel | — | — |
| Harness green before commit | escalation §13.2 | — | — | `pytest -m fast` | — | `tier1-eval-before-commit` |
| Background-write provenance | architecture A.4 | `provenance-guard.py` | — | — | fixtures | — |
| Archive-not-delete | Core principles | `path-allowlist.py` (`rm` is a Tier-1 write) | — | — | fixture | — |
| Mandate F (no options-menu) | mandate F | — | — | — | — | — |
| Mandate A (read-before-respond) | mandate A | (unenforceable; advisory) | — | — | — | — |
| Exit-ramp completeness | mandate X | `exit-ramp-check.sh` (⚠ jq-inert) | — | — | — | — |

⚠ = still bash, still `jq`-dependent, therefore still inert on this machine
(BK-0005). They are advisory / observability hooks, not security gates, which
is why they were out of scope for the 2026-09-02 port — but "wired" currently
means less than it reads, and this table says so rather than implying
coverage that does not exist.

Mandate A remains unenforceable mechanically — no hook can detect "the model
didn't read the file." The mitigation is structural: every coordinator's first
action by design is `Read` of the relevant T2/T3 files, declared in the body
prompt, so a deviation is visible in the transcript. Combined with the
SessionStart injection, the model cannot start a session without these
instructions in context.

### E.8 The honest limit

Layers 2 and 6 are creation-time, tool-layer enforcement, and a determined
agent can route around them: a write performed *inside* a Python script is
invisible to a hook that only sees the command string. This is documented, not
hidden — `.claude/rules/enforcement.md` lists the known dodges. The layering is
what makes it defensible: a tool-layer bypass still meets layer 4 at the
commit, and layer 4's bypass (`--no-verify`) still leaves a durable violation
record from `post_commit_verify.sh`. Kanin reading `git log` is the last layer,
and revert is the recovery path.

---

## F. Day-by-day migration plan

Today: you have Claude Code main session on Sonnet 4.6, the existing `.githooks/commit-msg`, and the `.claude/hooks/inject-mandates.sh`. Here is the build-out, one day at a time. Each day is ≤4 hours of work (per the compute budget) and each has a validation test you run before moving on.

### Day 1 — Memory tier filesystem

```bash
cd ~/dev/crypto-bot
mkdir -p .memory/T0_working
mkdir -p .memory/T1_episodic/episodes .memory/T1_episodic/_state .memory/T1_episodic/_archive
mkdir -p .memory/T2_semantic/citations .memory/T2_semantic/_pending_review .memory/T2_semantic/_rejected .memory/T2_semantic/retired_strategies
mkdir -p .memory/T3_procedural .memory/_proposals
echo ".memory/T0_working/" >> .gitignore
touch .memory/T2_semantic/facts.md
touch .memory/T2_semantic/decisions_log.jsonl
touch .memory/T1_episodic/_state/phase4b_failure_count.txt
echo 0 > .memory/T1_episodic/_state/phase4b_failure_count.txt
git add .memory/ .gitignore
git commit -m "$(cat <<'EOF'
chore(memory): scaffold four-tier memory hierarchy

T0 working (gitignored), T1 episodic, T2 semantic, T3 procedural.
See architecture.md §A for tier rules.
EOF
)"
```

**Validation:** `tree .memory -L 2` shows the structure; `git status` is clean.

### Day 2 — T3 procedural seeds + SessionStart hook update

Move existing rules into T3:

```bash
cp CLAUDE.md .memory/T3_procedural/_source_CLAUDE.md  # snapshot
# Author these new files (content per §A and §E above):
#   .memory/T3_procedural/no_p_hacking.md
#   .memory/T3_procedural/sacred_harness.md
#   .memory/T3_procedural/autonomy_boundaries.md
#   .memory/T3_procedural/borderline_protocol.md
#   .memory/T3_procedural/exit_ramp.md
```

Update `.claude/hooks/inject-mandates.sh` to additionally cat the contents of `.memory/T3_procedural/*.md` into the SessionStart injection.

**Validation:** start a new Claude Code session; ask "what is mandate F?" without reading any file. The model must answer correctly from the injected context.

### Day 3 — Hook scripts (Layer 2)

Author `.claude/hooks/sacred-block.sh`, `no-deploy.sh`, `commit-heredoc-required.sh`, `citation-required.sh`, `budget-check.sh`, `failcount-check.sh`, `exit-ramp-check.sh`, `no-secrets-in-bash.sh` (content per §E.2).

```bash
chmod +x .claude/hooks/*.sh
```

**Validation tests** — for each hook, simulate stdin and verify exit codes:

```bash
echo '{"tool_input":{"file_path":"trials.py"}}' | .claude/hooks/sacred-block.sh; echo $?
# Expect: 2
echo '{"tool_input":{"file_path":"src/strategy.py"}}' | .claude/hooks/sacred-block.sh; echo $?
# Expect: 0
echo '{"tool_input":{"command":"doctl k8s deploy"}}' | .claude/hooks/no-deploy.sh; echo $?
# Expect: 2
echo '{"tool_input":{"command":"git commit -m \"foo\""}}' | .claude/hooks/commit-heredoc-required.sh; echo $?
# Expect: 2
```

### Day 4 — Pre-commit hook (Layer 4)

Author `.githooks/pre-commit` per §E.4. Wire in:

```bash
git config core.hooksPath .githooks
chmod +x .githooks/pre-commit
```

**Validation:** intentionally stage a change to `trials.py`; attempt commit; verify it is blocked. Stage a change introducing a magic-number param without citation comment; verify blocked.

### Day 5 — Strategist + Phase-4B Coordinator subagents

Write `.claude/agents/strategist.md` and `.claude/agents/phase4b-coordinator.md` per §C. Use the Implementer frontmatter from §C.4 as a template.

**Validation:** in Claude Code, run `/agents` and confirm both appear. Run `Task(strategist, "What is the current variation count?")` and confirm it Reads the right files (you will see this in the transcript).

### Day 6 — Worker subagents (Phase 4B)

Write `.claude/agents/proposer.md`, `citation-verifier.md`, `adversarial-reviewer.md`, `implementer.md`, `validator.md`, `analyst.md`, `deep-researcher.md`. Configure OpenRouter for Gemini access — for the Gemini agents, do not use Claude Code's model field (which only accepts sonnet/opus/haiku); instead, set `model: inherit` and have the subagent body delegate via OpenClaw's gateway at `127.0.0.1:18789` to OpenRouter's `google/gemini-2.5-pro`. This is a design tradeoff: you lose the native model-switching, you gain access to Gemini for adversarial review.

```bash
# .claude/settings.json — add OpenRouter env wiring
```

**Validation:** a dry-run Phase-4B variation that just exercises proposer→citation-verifier→adversarial-reviewer without running the harness. Verify the citation flow works and that Gemini's review actually disagrees with Sonnet at least once (adversarial pressure exists).

### Day 7 — Curator cron job + Notifier

Author `.claude/agents/curator.md` and `.claude/agents/notifier.md`. Add a system cron entry on the MacBook (or DigitalOcean dropletside if you prefer it always-on):

```bash
0 * * * * cd ~/dev/crypto-bot && claude -p "$(cat .claude/agents/_curator_prompt.txt)" --agent curator >> .memory/T1_episodic/_state/curator.log 2>&1
```

Set up Resend account, create domain, verify SPF/DKIM/DMARC, store API key in `~/.crypto-bot.env` (sourced by Notifier hook). Resend rationale over SES: developer experience is better, and at the volume you'll send (≤30 emails/day), the price difference is irrelevant.

**Validation:** wait an hour, confirm `.memory/T2_semantic/_pending_review/` has new candidates. Trigger Notifier manually with a test alert.

### Day 8 — Phase 5 subagents (scanner, calibrator, sizer)

Write the three Phase 5 workers and the `phase5-coordinator.md`. Polymarket Gamma API key set up (read-only at this point — execution path remains human-only per §D.3). XGBoost calibrator scaffolded with the published Brier-score harness.

**Validation:** run a dry-run scanner pass (no calibration, no sizing); verify it surfaces real Polymarket markets.

### Day 9 — End-to-end Phase 4B variation N+1

Run a full Phase 4B variation through the new system. Watch carefully for: agent deviation from prompt, hook bypass, drift back into options-menu pattern. If any of these occur, the fix is in the agent's body prompt or the hook script, never in chat-side coaching.

**Validation:** the variation completes with a single decisions_log.jsonl line, an exit-ramp summary email, and either a passing variation or a failure that correctly increments failcount.

### Day 10 — Pin Ruflo (optional) + repomix update

If you choose to use `ruflo-rag-memory` as a search overlay on T2: `npm install ruflo@3.6.10` (NOT `@latest`), wire it as an MCP server in `.claude/settings.json`, scope it read-only to `.memory/T2_semantic/`. Otherwise, skip this day. Update repomix config to include `.memory/T2_semantic/` and `.memory/T3_procedural/` and exclude `.memory/T0_working/` and `.memory/T1_episodic/_archive/`.

**Validation:** `repomix --top 50` produces a packed file under 200K tokens; the regen step is now part of every exit-ramp.

### Day 11 — Adversarial review dry run on a known-good variation

Take a completed variation from history; run it back through the new system. The adversarial reviewer (Gemini) MUST find at least one substantive critique. If it finds none in three runs, the reviewer's prompt is too soft — sharpen it to "argue from first principles that this should fail."

### Day 12 — Production cutover

Switch your daily Phase-4B work to the new system. Document one week of operation. Surface any failure modes in T2 retired/postmortem, and feed back into agent prompts. Review the cost dashboard against §G estimates.

### Day 13 — Analyst overlay agent bodies (5 new agents, 5 new skills)

Author the 5 new agent bodies under `.claude/agents/`:
- `market-analyst.md` (Haiku, plan, 6 turns)
- `social-analyst.md` (Haiku, plan, 8 turns)
- `news-analyst.md` (Haiku, plan, 8 turns)
- `fundamentals-analyst.md` (Haiku, plan, 8 turns)
- `research-manager.md` (Sonnet primary + Gemini secondary, plan, 12 turns)

Author 5 new skills under `.claude/skills/`:
- `analyst-report-template` — the AnalystReport markdown schema (Inputs read / Observations / Regime view / Risk flags / Degraded?)
- `regime-flag-rules` — binary-flag emission rules for research-manager (when to emit `macro-event-imminent`, `exchange-flow-anomaly`, `funding-regime-flip`, etc.)
- `news-source-allowlist` — append-only source list for news-analyst. Initial entries: CoinDesk, The Block, Decrypt, Reuters crypto. Extensible without architecture edit.
- `onchain-source-list` — append-only source list for fundamentals-analyst. Initial entry: CryptoQuant public dashboards via WebFetch. Extensible without architecture edit.
- `sentiment-source-list` — append-only source list for social-analyst. Initial entries: Crypto Twitter via Tavily, Reddit via Tavily, fear-greed index direct fetch. Extensible without architecture edit.

**Validation:** YAML parse passes for all 5 agent files. Each skill file is well-formed markdown with clear "Approved", "Deprecated", and "Adding a source" sections. No live wiring this day; agents exist but no cron entry yet.

### Day 14 — Cron + shadow-mode logging + paired-observation harness

Add system cron entry on the MacBook (mirroring the curator entry from Day 7):

```
0 0,8,16 * * * cd ~/dev/crypto-bot && claude -p "$(cat .claude/agents/_research_manager_prompt.txt)" --agent research-manager >> .memory/T1_episodic/_state/research_manager.log 2>&1
```

Wire the regime-change trigger: a new file-watcher hook that detects `.memory/T1_episodic/_state/regime.txt` state changes and posts to `.memory/T1_episodic/_state/analyst_trigger.txt`; the cron entry checks that file at every minute and fires an extra cycle on `triggered=true`.

Set up paired-observation logging: for every retired or newly-passed verdict during Phase A, log the pair `(verdict_outcome, concurrent_research_manager_synthesis)` to `.memory/T2_semantic/phase_a_paired_observations.jsonl` for later evaluation.

Add `.memory/T2_semantic/research_queue.md` (append-only schema): active candidates list + retired-strategies-cooldown table. Curator (existing agent, scope extension) maintains it.

**Validation:** wait through one funding settlement (8h); confirm `.memory/T1_episodic/episodes/<date>/synthesis/` has a research-manager file. Confirm 4 AnalystReport files in the same date's `analysts/` directory. Confirm cost stayed under $0.50 for the cycle.

### Day 15 — Phase A shadow mode begins

No code work. Confirm 8h cadence is producing synthesis files and `decisions_log.jsonl` cost lines for at least 7 consecutive cycles. Confirm none of the synthesis output is being read by `portfolio/manager.py` or `CapGuard` (grep the codebase to verify no import or file-read of `analyst_overlay.json`). Document the Phase A start date in `bot_status.md`.

### Phase A duration — 2 months minimum (~180 cycles at 8h cadence)

During Phase A:
- Shadow synthesis runs continuously
- Existing 6-regime detector remains the canonical regime source for live strategies
- Every retired or newly-passed verdict is paired with concurrent synthesis for evaluation
- Cost monitored via `budget-check.sh`; cap at $30/mo with Notifier alert if hit

### Phase B — gate decision (chat-side, after ≥30 paired observations)

Specific metric chosen at gate-decision time, not pre-committed. Three outcomes: proceed to Phase C (live wiring), keep gathering data, or retire the overlay.

### Phase C — live wiring (post-gate)

If Phase B proceeds: wire research-manager's flags into CapGuard `excluded_strategies` and portfolio.manager.py risk-flag check. Live-wiring is `paper_mode=True` for first month before any live-mode consideration.

---

## G. Cost estimate

Pricing as of May 2026 (verified): Sonnet 4.6 $3/$15 per 1M tokens, Haiku 4.5 $1/$5, Gemini 2.5 Pro via OpenRouter $1.25/$10. All include prompt caching at 90% off cached input on Anthropic; OpenRouter passthrough preserves Google's caching where available.

### G.1 Per-variation token budget (Phase 4B)

| Stage | Agent | Model | Input | Output | Subtotal |
|---|---|---|---|---|---|
| Coord turn | phase4b-coord | Sonnet | 30k (cached) | 2k | 30k×$0.3/M + 2k×$15/M = $0.039 |
| Proposer | proposer | Sonnet | 50k (T2+T3) | 4k | 50k×$0.3/M + 4k×$15/M = $0.075 |
| Citation-verify | citation-verifier | Gemini-2.5-Pro | 80k (paper PDF) | 2k | 80k×$1.25/M + 2k×$10/M = $0.120 |
| Adversarial-review | adversarial-reviewer | Gemini-2.5-Pro | 60k | 5k | 60k×$1.25/M + 5k×$10/M = $0.125 |
| Implementer | implementer | Sonnet | 80k (code+harness) | 12k | 80k×$0.3/M + 12k×$15/M = $0.204 |
| Validator | validator | Haiku | 40k | 1k | 40k×$0.1/M + 1k×$5/M = $0.009 |
| Analyst | analyst | Sonnet | 30k | 3k | 30k×$0.3/M + 3k×$15/M = $0.054 |
| Strategist (start+end) | strategist | Sonnet | 60k (cached) | 4k | 60k×$0.3/M + 4k×$15/M = $0.078 |
| Hooks/notifier overhead | various | Haiku | 5k | 0.5k | $0.003 |
| **Per-variation total** | | | | | **≈$0.71** |

### G.2 Phase 5 per-session

A scanner pass surfaces 10 candidates; assume 5 fan-out research lanes; deep-researcher is Gemini-heavy.

| Stage | Agent | Model | Input | Output | Subtotal |
|---|---|---|---|---|---|
| Scanner | scanner | Haiku | 80k | 3k | $0.023 |
| Deep-researcher × 5 | deep-researcher | Gemini-2.5-Pro | 5×120k | 5×8k | 5 × ($0.150+$0.080) = $1.150 |
| Adversarial-review × 5 | adv-reviewer | Gemini-2.5-Pro | 5×40k | 5×3k | 5 × ($0.050+$0.030) = $0.400 |
| Calibrator | calibrator | Sonnet | 100k | 8k | $0.150 |
| Sizer | sizer | Haiku | 20k | 1k | $0.007 |
| Analyst | analyst | Sonnet | 40k | 4k | $0.072 |
| Coord+strategist+notifier | various | Sonnet/Haiku | 70k | 6k | $0.111 |
| **Per-session total** | | | | | **≈$1.91** |

### G.3 Monthly projection at expected throughput

- **Phase 4B finishing:** 17 remaining variations × $0.71 ≈ **$12** total. Probably 1–2 weeks of part-time work.
- **Steady-state operation (post-Phase-4B finish, Phase 5 active):** assume 2 Phase 5 sessions/day, 60 sessions/month: 60 × $1.91 ≈ **$115/month**. Add ~$20 for Curator hourly cron, ad-hoc Strategist runs, dry-run rehearsals → **~$135/month all-in**.
- **Worst-case if you crank up Phase 5 to 6 sessions/day:** ~$350/month. Still well under any reasonable threshold for a production trading research budget.

### G.4 OpenRouter vs direct Claude tradeoff math

For Sonnet 4.6, direct Anthropic pricing is identical to OpenRouter passthrough ($3/$15). Routing the Sonnet calls through OpenRouter buys you (a) unified billing with the Gemini calls, (b) automatic failover if Anthropic has an incident, (c) easier model swaps. The cost is (a) one extra hop of latency (~50ms), (b) you don't get the most aggressive Anthropic prompt-caching discounts in some routes — verify this for your specific volume by running a week through each path.

**Recommendation:** main Claude Code session uses native Anthropic. Sonnet-based subagents that don't need Gemini interop also use native. ONLY route Gemini-2.5-Pro through OpenRouter (this is the only multi-model path). This keeps the Anthropic prompt-cache hot for the main session, which is where the dominant cost lives.

---

## H. Caveats

**The subagent-hooks gap (Anthropic issue #34692) is not fixed yet.** The fundamental design assumption that hooks fire on subagent tool calls is wrong if you only configure them in `~/.claude/settings.json`. The mitigation in this document — duplicating safety hooks inline in each subagent's frontmatter — is correct but verbose. If Anthropic ships a fix that propagates main-session hooks to subagents, you can simplify the frontmatter. Track issue #34692, #5812, #16126, #14859 for the fix landing.

**Plugin subagents do not support `hooks`, `mcpServers`, or `permissionMode` frontmatter.** Anthropic excludes these for security. Therefore: never distribute these subagents as a plugin. Keep them in `.claude/agents/` of the project repo.

**Claude Code agent teams are experimental.** This design avoids `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`. Inter-coordinator coordination is via Strategist serialization, not agent-team direct messaging. If/when Anthropic stabilizes agent teams, you can flatten the Strategist out of the inner loop, but until then this is the safe path.

**Ruflo / Claude-Flow ecosystem is mostly theater.** Independent audits documented in issues #1425, #1482, #1504, #1514, #1330, the gist by roman-rr, and the supply-chain incident in #1261, plus the prompt-injection-via-MCP-tool-descriptions disclosure in #1375, all point the same way: the marketing claims of 100+ agents and 300+ MCP tools do not reflect functioning code. Genuinely useful pieces: `ruflo-rag-memory` v3.6.10 (and only that version, exactly), the AgentDB HNSW search. Avoid: `ruflo-swarm`, `ruflo-autopilot`, `ruflo-hive-mind`, `ruflo-federation`, `ruflo-neural-trader`. Re-audit on every minor version bump.

**Pin every dependency.** `ruflo@3.6.10`, `paperqa2@5.x`, Resend SDK at the version you tested. Anthropic is shipping breaking subagent-frontmatter fields every few months (worktree isolation was undocumented for weeks; the `--agents` JSON CLI surface keeps changing). Lock your `claude` CLI version with `npm install -g claude-code@<exact>` and update intentionally, not automatically.

**The `gemini-2.5-pro` route via OpenRouter is the multi-model path that loses you native Claude Code features.** Specifically: when you set `model: inherit` and dispatch to Gemini through OpenClaw/OpenRouter inside the agent body, you do not get the same hook semantics, the same effort/maxTurns enforcement, or the same SubagentStop transcript shape. Watch for this in your validation runs. If Gemini-routed agents start producing inconsistent outputs, the cleanest workaround is to run a separate Claude Code session against Anthropic where the Gemini calls are made via raw `curl` to OpenRouter — a slightly higher-friction setup, but fully controllable.

**Citation verification is the single hardest discipline question and it is only partly mechanical.** PaperQA2 / Semantic Scholar / Crossref give you the metadata; the *actual* check that the cited claim supports the parameter choice is an LLM judgment. The Adversarial-Reviewer (Gemini) is the second pair of eyes. The fundamental risk: both Sonnet and Gemini might have the same blind spot on a given paper. Mitigation: every quarter, have a human spot-audit 5 random T2 citations. If the audit finds >1 false positive in 5, tighten the citation-verifier's body prompt and re-run all citations from the last quarter.

**The Polymarket Phase 5 design recommends, never executes.** This is deliberate and should not be relaxed for at least 100 resolved bets of feedback. The Brier-score calibration drift is the failure mode where automated execution destroys capital faster than humans can reason about it. Revisit this boundary at Phase 6.

**Goal drift is real and unsolved at the model level.** Recent papers (Inherited Goal Drift; Why LLMs Aren't Scientists Yet) document that LM agents drift over long contexts even with good prompts. The structural defenses in this design — externalized memory, mechanical verdict tree, hook enforcement — buy you correctness; they do not eliminate drift, they merely make it visible (the agent trying to drift will hit a hook and surface the violation in a transcript). Watch for novel drift patterns in T1 episodes; promote new patterns into T3 mandates as you find them.

**The 4-hour budget is conservative on purpose.** Anthropic's multi-agent system uses 15× the tokens of single-agent, and rate limits exist (TPM, RPM, TPD). At Sonnet 4.6 typical limits, a 4-hour aggressive multi-agent session approaches 1M output tokens, which can exhaust a tier. Buy the next-tier rate limits before a sustained Phase-5 push.

**The research literature on multi-agent for finance is thin.** Most published prediction-market multi-agent work (PolySwarm, navnoorbawa's stack) is single-developer hobbyist code. Treat the Phase 5 architecture as experimental, not battle-tested. Reserve a separate paper-trading bankroll specifically for Phase 5 calibration; do not commingle with Phase 4B funding-rate harvest capital. Keep the Phase 5 bankroll bounded at the size you would not regret losing.

**One-off things that will break.** (1) Resend domain reputation can degrade if Notifier sends too many in a burst — rate-limit the email path to ≤6/hour. (2) Gemini 2.5 Pro context-cache behavior is less predictable than Anthropic's; budget for higher uncached-input cost in worst weeks. (3) OpenClaw at 127.0.0.1:18789 must stay on; if it dies, every Gemini-routed subagent fails closed. Add a SessionStart hook that pings the gateway and warns if down. (4) The 1M context on Sonnet 4.6 includes an auto-upgrade to long-context pricing above 200K tokens on some routes — the design here keeps individual subagent calls under 200K to avoid this; a wide-open Strategist call pulling all of T2 could trip it.

**Settle once, encode, never debate again.** This document is the source of truth for the architecture. If you find yourself in a chat session relitigating one of the choices above (manager hierarchy depth, memory tier count, the Polymarket boundary, the Gemini-as-adversary choice), copy that point's section into a new T3 file and have the SessionStart hook surface it on every session. The point of the discipline is not that the answers above are perfect — they are *good enough*; the point is that your time and attention are better spent on Phase 5 calibration than on rearchitecture debates with a chat-side model that has no memory of why you settled what you settled.