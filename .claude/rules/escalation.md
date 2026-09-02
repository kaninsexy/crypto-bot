# escalation.md — autonomous-dispatch / megaloop mode

Sacred file (Tier 1; human-only edits, or an authorized `SACRED_OVERRIDE_FILES`
scope). Loaded by every agent that runs a dispatcher-mode or megaloop prompt.

Ported 2026-09-02 from siamese-reconcile `.claude/rules/escalation.md`
(§0, §2, §3, §5, §8, §9, §13), adapted to crypto-bot. Nothing here weakens
`CLAUDE.md`, `.claude/rules/backtest.md`, `.claude/rules/communication.md`, or
`.memory/T3_procedural/*`.

---

## 0. Verification model — the harness is ground truth

crypto-bot's verification layer is **already deterministic**, so this port
drops siamese's LLM judgement layer (`chat-advisor`) and its
`behavior-verifier` agent entirely. There is no LLM verdict anywhere in the
gate path. Ground truth is, in order:

1. `python -m pytest backtest/tests data/tests -q` — the unit/integration gate.
2. `python eval/run_tier1.py` — the guard-layer gate: it proves the hooks
   actually block what they claim to block.
3. **The trial verdict tree** — `backtest/verdict.py` fed by
   `backtest/cpcv*.py` + `backtest/dsr.py`, at both fee levels. A verdict is
   computed, not argued.
4. The git hooks (`.githooks/pre-commit`, `commit-msg`,
   `scripts/pre_commit_backlog_check.sh`) — they gate the commit itself.

When a narrative and the harness disagree, **the harness wins.** An agent's
own summary of its work is never evidence. "I think it's done" is not an exit;
a green VERIFY block is.

---

## 2. Entering dispatcher / megaloop mode

Never the default. Entered only when Kanin explicitly says so in the current
session — "run this autonomously", "dispatcher mode", "megaloop", "go ahead
without checking back", or a start prompt that declares `MODE: MEGALOOP`.

The work must be scoped when triggered. A vague scope ("keep working on Phase
4") gets a request for explicit scope first. A previous session having been in
dispatcher mode does not carry over.

---

## 3. Exiting

Exit is unconditional and non-negotiable — no "just one more stage". Exit when:

- A tripwire (§5) fires.
- A time backstop or hard ceiling fires (defaults: 6 h backstop, 8 h ceiling).
- A drift indicator (§9) fires.
- Kanin says "stop", "wait", "let me see", "pause", "hold on", or equivalent.
- The enumerated stages complete **and** each stage's VERIFY block was green.

On exit-by-halt: append a HUMAN NEEDED block (§8) to
`.memory/_inbox/human_needed.md`, write the status doc naming the stage, the
failing command and its last 40 lines, and what was tried; commit; push; stop.

---

## 5. Tripwires (hard stop, no improvisation)

Evaluated before each action. If any matches, halt per §3 — do not rationalize
past it. Rationalizing ("this isn't really a sacred edit because…") is itself
the signal that the case is ambiguous, which means halt.

1. **Any write to `backtest/trials.log`** except through
   `backtest.trials.record_trial` from a confirmation trial.
2. **Any `load_holdout` call outside a `final_gate`.** The holdout is
   single-access; a second read destroys the guarantee for that strategy.
3. **Any real OKX order path, or `paper_mode=False`.**
4. **Any new paid API or new dependency.**
5. **Force-push, branch deletion, or pushing anything but `main`.**
   (An ordinary `git push origin main` of finished, gated work is NOT a
   tripwire — it is agent-autonomous per CLAUDE.md "Agent decides".)
6. **A sacred-path edit outside the prompt's AUTONOMY list.** The sacred map
   is `.claude/hooks/file_tiers.py`; pre-authorization travels in
   `SACRED_OVERRIDE_FILES`.
7. **Three consecutive speculative fixes for one symptom** — the CLAUDE.md
   drift signal. Write `docs/investigations/<date>-<slug>.md` with controlled
   observations before a fourth attempt.
8. **Secrets, credentials, `~/.crypto-bot.env`,** or any file holding an API
   key.
9. **A borderline retire/keep call** — DSR within ±0.05 of threshold on
   holdout (`.memory/T3_procedural/borderline_protocol.md`).
10. **Scope changes that meaningfully increase the multiple-testing count**,
    or any new strategy CATEGORY not already pre-authorized.

The list is not closed. Adding a tripwire is an ordinary edit; REMOVING one is
a human-only edit to this file.

---

## 8. HUMAN NEEDED block format

Appended to `.memory/_inbox/human_needed.md`. Exactly this shape, no extra
commentary and no "let me know if you want me to continue":

```
=== HUMAN NEEDED ===
id:        <short-kebab-id>
raised:    <YYYY-MM-DD HHMM ICT>
stage:     <megaloop stage / task>
tripwire:  <section 5 number + one line, or "judgement">

What I was about to do
<one paragraph>

Why it stops here
<one paragraph: the specific file / gate / call / verdict that caused it>

Recommendation
<one of: skip and continue / needs a design discussion / split so the
non-tripwired part proceeds / I am not sure>

State
Stages completed: <list>
Last commit:      <SHA>
Harness:          pytest <result>, eval/run_tier1.py <result>
=== END ===
```

---

## 9. Drift indicators — immediate exit

Any of these from Kanin ends the loop at once:

- "wrap up", "fresh chat next", "let's restart"
- "you're repeating yourself"
- "did you forget what we decided about X?"
- "the diff doesn't match what you said you did"
- "you're spinning" / "stop"
- Any expression of confusion about what just happened

Kanin is the reliable detector of session degradation, not the agent's own
self-assessment.

---

## 13. Mega-loop operating mode

### 13.0 Posture

In dispatcher mode, **mega-loop is the default posture** unless the start
prompt opts out. The agent does not stop to ask between stages — it stops only
on the three halt conditions in CLAUDE.md "Mega-loop posture": Kanin stops it,
a section-5 tripwire fires, or a judgement the project files cannot settle.
Everything else it decides and executes.

The point is to remove the back-and-forth tax. Most decisions in a typical run
are already settled by the project files (CLAUDE.md, `.claude/rules/*`,
MASTER_PLAN, the T3 procedural memory). Stopping to ask about them wastes time
and burns context for no safety gain.

### 13.1 Defaults

- `monitoring_mode: off`
- `time_backstop_hours: 6`
- `hard_ceiling_hours: 8`
- Authorization scope: the enumerated stage list in the start prompt.

### 13.2 Completion is deterministic

The agent's own judgement that "all stages are done" is not a legitimate exit.
A stage closes only when its VERIFY block is green — pytest, `eval/run_tier1.py`,
the git hooks, and where applicable the verdict tree. If a VERIFY step cannot
be run, the stage is NOT done; that is a halt, not a pass.

### 13.3 Every stage ends committed and pushed

So that a later failure never loses verified work. Push is
`bash scripts/post_commit_sync.sh` (no flag) after a green VERIFY.

### 13.4 Verification stack

1. `.claude/hooks/path-allowlist.py` — blocks sacred edits outside authorization
2. `.claude/hooks/no-secrets-in-bash.py` — blocks credential leaks
3. `.claude/hooks/policy-engine.py` + `.claude/rules/enforcement_policy.json`
4. `.githooks/pre-commit` + `scripts/pre_commit_backlog_check.sh` (Mandate L)
5. `python eval/run_tier1.py` — proves 1-3 actually fire
6. `pytest` on every commit; the verdict tree on every trial
7. Kanin reads `git log` on wake; revert is the recovery path

### 13.5 When NOT to use mega-loop

- First-time wiring of a new tool, venue, or data feed whose failure modes the
  harness does not yet cover.
- Work needing human creative input mid-loop (naming, comms wording,
  retire/keep judgement calls). Those are section-5 hard-stops by design.
- Any stage that would touch live capital or a live venue.

---

## 14. Update history

- 2026-09-02: v1. Ported from siamese-reconcile (HEAD 2f13045) sections
  0/2/3/5/8/9/13. Adapted: crypto-bot tripwires replace the reconciliation
  ones; chat-advisor and behavior-verifier dropped (crypto-bot's harness is
  already deterministic); push of gated work removed from the tripwire list to
  match the CLAUDE.md 2026-09-02 boundary move.
