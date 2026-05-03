# Borderline protocol — what to do when unclear

## Missing-or-stale evidence — STOP and surface
When project files contradict the handoff prompt, when load-bearing
fields are absent (manifest schema slots, commit hashes, citations),
or when sources disagree on a locked decision: STOP.

- Do not fill the gap with judgment.
- Do not assume the newer-looking source wins.
- Surface the discrepancy explicitly.
- Resolve via `conversation_search` if past chats answer it.
- Else ask the user.

Does NOT fire on routine search-empties or expected lag (bot_status
updates, log appends).

## Pushback re-check — re-read evidence first, then judge
When the user pushes back ("is X right?", "shouldn't this be Y?"),
do NOT immediately validate or flip the answer. First re-read evidence
end-to-end (handoff verbatim, project files, past chats via
`conversation_search`). Then judge if pushback is right, partially
right, or wrong. Reflexive flipping creates wrong-fix loops. If right,
say so after verifying. If partially right, separate right from wrong.
Better to take a turn re-checking than flip twice.

## Anti-wavering
Once a project file is read and its state reported, that state stands
for the chat. Do not later call it "unverifiable" because of secondary
indicators. If new info contradicts a prior reading, name the
contradiction explicitly.

## Borderline retire/keep decisions
DSR within ±0.05 of threshold on holdout = consult the human. Do not
auto-retire or auto-keep. Surface the borderline result with full
context (dev_cpcv distribution, holdout point estimate, CPCV path
count, MinTRL bound) and let the human decide.

## Conflict between project files and chat handoff
Surface immediately, do not paper over. The handoff is one source of
truth; project files are another. When they disagree:
1. Quote both verbatim.
2. Say which appears newer, with timestamp evidence.
3. Ask the user to resolve before any state-changing action.

## Read evidence end-to-end before acting (mandate A)
When working on a strategy, trial, or harness component, read each of
the following before responding or acting (each is a separate authority,
skipping any produces drift):
1. `research/<strategy>-literature.md` (hypothesis-of-record + locked
   pre-trial gates).
2. `backtest/holdout_manifest.json` entry (substrate truth).
3. `backtest/trials.log` rows for the strategy (ground truth on what's
   been tested).
4. `docs/bot_status.md` row (running results table + forensic links).
5. `docs/strategies.md` section (Phase 3c verdict + diagnosis).
6. `docs/research_log.md` relevant section (why the hypothesis was
   chosen).
7. Past chats via `conversation_search` / `recent_chats` (rationale,
   scoping, pre-trial gates often live in chats and don't always make
   it into MASTER_PLAN.md).

See also: CLAUDE.md "Drift prevention" mandates A, B, C, D, E;
"Pushback re-check"; "Missing-or-stale evidence".
