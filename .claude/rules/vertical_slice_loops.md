# vertical_slice_loops.md — how autonomous loops are framed and verified

Sacred file (Tier 1; human-only edits, or an authorized `SACRED_OVERRIDE_FILES`
scope). Read by whoever drafts a dispatcher-mode / megaloop start prompt, and by
the agent at the start of any `monitoring_mode=off` loop.

Ported 2026-09-02 from siamese-reconcile `.claude/rules/vertical_slice_loops.md`
and adapted to crypto-bot. This CONSTRAINS how a loop's stage list and
acceptance criteria are written. It does NOT replace the tripwire / hard-stop
architecture in `.claude/rules/escalation.md`.

---

## The disease this rule treats

An agent fixes the point it was handed instead of finishing the whole process.
Root causes, all observed:

- **R1 — start prompts enumerate BUGS, not FLOWS.** A symptom list framed as
  the task definition *is* the point-fixing. The agent closes each bullet and
  the pipeline still does not run end to end.
- **R2 — no gate exercises the whole path.** `pytest backtest/tests` covers one
  engine function; a change that breaks manifest loading, the ledger write, or
  the verdict tree passes every unit gate.
- **R3 — runtime not detected.** "Fixing" a screen without first checking
  whether the data was actually prefetched, the cache warm, or the substrate
  branch wired.
- **R4 — the verifier judges text, not outcome.** An LLM verifier that reads
  "pytest passed" in the agent's own summary rubber-stamps closure. crypto-bot
  has no LLM in the gate path precisely because of this; see
  `.claude/rules/escalation.md` §0.

## The rule

> **One loop = one VERTICAL made to work end-to-end, framed by 3–5
> owner-observable acceptance criteria, gated by a runnable harness that
> exercises the WHOLE path. The bug list is supporting detail, never the task
> definition.**

For crypto-bot a "vertical" is the research pipeline:

```
data prefetch  ->  discovery screen (ledger row, no trials.log)
               ->  confirmation trial (dev CPCV, one trials.log row)
               ->  holdout final_gate (single access)
               ->  paper deploy
```

…or a sub-flow of it (one family end-to-end; the manifest+loader path; the
verdict tree at both fee levels).

Every loop, in order:

1. **Detect runtime state first.** Which cache is warm, which manifest entries
   exist, which env is live, what HEAD is. Never "fix" something that was
   merely un-prefetched or mis-wired.
2. **Establish the runnable gate** that drives the vertical: `pytest`,
   `python eval/run_tier1.py`, the selftests, and — for any stage that produces
   a verdict — the verdict tree at BOTH fee levels. Deterministic, so a passing
   run is ground truth rather than an opinion.
3. **Fix against the gate and the acceptance criteria**, not against the
   symptom list. A symptom is "fixed" only when the gate stays green AND its
   criterion passes.
4. **Verify before the loop may close** — backed by a command whose output is
   in the close report, not by prose claims.

## Why a gate and not just more unit tests

A unit test exercises one function with one fixture. The gate drives the
*chain*: prefetch → screen → ledger → trial → verdict → holdout. One command,
asserting the seam between every stage. That is where the next regression
hides.

## The claim–medium rule (hard corollary)

An acceptance criterion whose **claim** is owner-observable (a ledger row a
human would read, a verdict a human would act on, a number in a status doc)
MUST be backed by a check that exercises that **medium** — an actual run
producing that artifact — NOT a source grep or a unit-test exit code alone.

Grep- and exit-code-shaped checks are legitimate for code-state and regression
criteria. Any loop touching the discovery / confirmation / holdout verticals
must include at least one criterion backed by a real run.

## The discriminating-check rule

Any check added to guard a claim must be able to FAIL. Construct the broken
case and prove the check goes red before trusting it. A check that cannot fail
is worse than no check, because it certifies the gap it was meant to catch.

## Loop-specific hard constraints (crypto-bot)

These bind inside every loop and are not negotiable by a start prompt:

- A discovery screen writes NO `trials.log` row; its ledger row is its record.
- A confirmation trial writes EXACTLY ONE row, via
  `backtest.trials.record_trial`.
- `load_holdout` is called only from a `final_gate`, once per strategy.
- Discovery never reads data at or after `2023-01-01`.
- Every verdict is computed at standard fees AND at 2× fees.
- `paper_mode=True` remains the default; live deploy and live capital are
  human-only.

## Relationship to existing machinery

Does not replace `.claude/rules/escalation.md` (§5 tripwires, §13 mega-loop) or
`.claude/rules/backtest.md` (the p-hacking rule, the variation cap, the
discovery/confirmation split). It constrains how the stage list and acceptance
criteria are WRITTEN — flow-shaped, gate-backed. Legitimate exit still requires
every stage's VERIFY block green.

## Update history

- 2026-09-02: v1. Ported from siamese-reconcile (HEAD 2f13045), adapted: the
  vertical is the research pipeline rather than a reconciliation flow; the
  harness is pytest + eval/run_tier1.py + the verdict tree; the LLM verifier
  layer is dropped entirely.
