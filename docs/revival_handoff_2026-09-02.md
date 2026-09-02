# Revival handoff — 2026-09-02

Written by a Cowork read-only session for Kanin. Purpose: give the next
chat session (running Claude Fable 5.1) a verified picture of where the
project stands and a research brief for finding a genuinely new
approach. Nothing in the repo was edited except adding this file.

Every claim below was checked against the repo on this machine
(C:\crypto-bot, HEAD 8b779de, 2026-07-03). Where a doc and the repo
disagree, the disagreement is listed in §2 rather than silently
resolved. Unverifiable items are marked as such.

---

## 1. Verified state (what is actually true on disk)

**Repo / git.** HEAD = `8b779de` (detached, identical to local `main`).
Local `main` is **8 commits ahead of `origin/main`** (origin at
`03b31cd`) — the whole Phase 4.E implementation + outcomes is unpushed.
Working tree: only CRLF/mode noise plus two real diffs
(`.claude/settings.local.json` deny-list additions, `backtest/proposal_history.json`).
Untracked but referenced by committed docs:
`docs/gate_recalibration_audit_2026-06.md`, `docs/playbooks_port_plan_2026-07-03.md`,
`scratch/*.py` (audit scripts). Last activity 2026-07-03; idle for two months.
`repomix-output.xml` is dated **2026-05-09** — 23 commits stale.

**Harness health.** `pytest backtest/tests data/tests` → **397 passed,
5 skipped** (run in a sandbox copy with Python 3.10; repo targets 3.12).
The validation harness (block-Sharpe CPCV, DSR gate spec v2, MinTRL,
verdict tree, holdout single-access, per-bar persistence, event-mode
blocks) is working code and is the project's most valuable artifact.

**Data on disk (`backtest/cache`, 839 MB).** OKX spot OHLCV 1d/1h/4h for
11 majors back to 2021-01 (BNB spliced from Binance 2021-01→2022-12);
BTC perp + funding (from 2021-08-31); Google Trends; Fear&Greed; Binance
Vision **1m klines with taker buy/sell split for BTC/ETH/SOL/XRP/ADA
2021→2026-05** (816 MB); Polymarket snapshot. Manifest `data_end` is
2026-06-11 at latest — nothing after that has been fetched.

**Holdout split (manifest, 51 entries).** Regenerated once on
2026-06-11 (human pre-authorized): single global `dev_end = holdout_start
= 2025-05-01`, `data_start` back to 2021-01-01 where available. Dev ≈ 52
months for BTC/basket substrates, 44 months for funding. Contamination
disclosure in `docs/bot_status.md`: the new holdout's first ~4.5 months
were inside the OLD dev window, so **only ~2026-04/05 → 2026-06-11 is
truly virgin** for pre-June strategies. Four months of never-seen data
(2026-06-11 → today) now exist but are unfetched.

**Trial record (local `backtest/trials.log`, 48 rows).** 38 non-superseded
`full_cpcv` rows, 30 with finite Sharpe, 33 distinct strategy_ids,
2 `final_gate` rows (AttentionMomentum, FundingRateHarvest_BTC — both
retire), 8 superseded duplicates. Positive dev Sharpe in 18/30 finite rows;
**zero strategies have ever returned `keep` on holdout.** All 4 Phase 4.E
rows are `retire` with negative Sharpe (−0.87 to −7.75).

**Outcome by phase (from docs, cross-checked against trials.log where rows exist locally):**

| Phase | What was tested | Result |
|---|---|---|
| 3c (Apr 2026) | 10 original retail-template strategies, 1H single-pair | 9 retire + 1 under_tested |
| 4.A (Apr–May) | 5 resurrections at daily/weekly/multi-asset/residual | all retired (2 CPCVError, 3 lost to BTC B&H) |
| 4.B (May–Jun) | Funding-rate carry V1, V2b, V2b extended re-test | dev keep ×2, holdout retire ×2, extended-window Sharpe +0.50 → archived |
| 4.C queue (May) | ~27 proposal-agent SSRN-cited anomalies, mostly 1d long-only baskets | 21 finite: 1 dev keep (retired on holdout), 2 under_tested, rest retire; 8 CPCVError |
| Gate v2 re-run (Jun 11) | 4 near-misses on 52-month window | CSMom +0.94, AltSeason +0.84 — pass alpha/IR gate, fail family DSR; NewsSent/AttentionMom under_tested (MinTRL) |
| 4.E (Jul 3) | 7 microstructure/order-flow hypotheses on Binance 1m data, 15m–1h, long-only BTC | 4 run, all wrong-signed **gross of fees**; batch stopped on 3-failure rule; 3 never run |

**Still alive (by the project's own rules):** CrossSectionalMomentum and
AltcoinSeasonRotation (the only strategies with statistically
significant alpha vs BTC B&H on the honest window, retired purely on the
family-multiplicity floor: DSR 0.787 / 0.690 vs 0.95); NewsSentimentMomentum
and AttentionMomentum (under_tested, ~7–10 months short of MinTRL);
CryptoDualMomentum (under_tested, 15 trades). Nothing else.

**Operational.** Droplet paused since 2026-04-29; `.cron-pause` present;
paper capital $100k reset 2026-04-26; no paper deployment running.
`REGIME_PRIORS` empty. Phase 4.D analyst overlay: designed, agent files
exist, `phase_a_paired_observations.jsonl` is empty (0 lines) — the
shadow-mode loop never ran.

**Agent fleet / memory infrastructure.** 21 agent definitions, 18 hook
scripts, 4-tier memory. Actual usage is thin: `research_queue.md` has no
candidates, cooldown table empty, `decisions_log.jsonl` does not exist,
T1 episodes dir empty, `.claude/settings.json` wires only 2 hooks. The
2026-07-03 playbooks port plan (8 chunks) was **never executed** (no
`schemas/`, no `eval/`).

---

## 2. Discrepancies and record-keeping gaps found (fix or acknowledge before new trials)

1. **Local `trials.log` is missing every row before 2026-05-05.** Phase 3c
   (10 rows incl. BearShort pair), 4.A (Supertrend/DualMomentum smoke,
   GridTrading, TrendFollowing_multi `746544526…`), IntradaySeasonalityEffects
   `d6d0e252…`, and the 4.B V1 chain (`f2c343c3…`, `199abc0a…`) are cited in
   docs but absent here. `trials.log` is gitignored, so those rows live only
   on the Mac (trials moved to the Windows PC ~2026-05-05). Consequence: the
   June audit ("38 rows") and gate-spec-v2 family variance/N were computed
   on the incomplete file. The multiple-testing count is understated relative
   to project history. Decide: merge the Mac file, or document the two-machine
   split as a permanent asterisk.
2. **Same-hypothesis re-runs count as new trials.** The 2026-06-11 extended-window
   re-runs used identical `variation_id`s but appended new `full_cpcv` rows,
   so `count_trials_for_dsr` counts them twice (e.g. cs-momentum family N=7
   for 5 designs). Conservative, but the policy is undocumented.
3. **`trial_queue.json` (tracked) disagrees with `trial_queue_state.json`
   (gitignored).** The tracked file shows sq-012/013/016/018/019/021/022/023/024/037
   as `queued`; the state file shows them `done`. sq-037 is `needs_rerun`,
   sq-031 (MarketStateConditionedMomentum) is genuinely never run,
   sq-034 (ExchangeNetflowReversal) `deferred_no_data`.
4. **Fee model inconsistency.** Every trial before Phase 4.E used
   `paper_trading/simulator.py` defaults `FEE_MARKET = 0.04%` (a Binance
   futures taker rate) + 0.05% slippage, on a spot substrate whose real
   taker fee is 0.10% (OKX/Binance spot). Phase 4.E corrected this to
   0.10% and added a 2× stress. High-turnover pre-4.E rows
   (CrossSectionalReversal 1432 trades, NewsSent 2071) are therefore
   fee-optimistic. Re-checking the two alpha near-misses at 0.10% is cheap
   and does not require a new hypothesis.
5. **Stale doc sections.** `bot_status.md` "Current state" still says
   commit `80fc2c9`/server on `4a51f0b`; `strategies.md` header says
   2026-05-08 and has no 4.E or gate-v2 outcome rows; `open_questions.md`
   last updated 2026-05-03 (pre-audit); `CLAUDE.md` project overview still
   says "Phase 4.A/4.B exploration". The audit doc that justifies gate v2 is
   untracked.
6. **Basket engine is long-only; single-symbol engine is not.**
   `backtest/engine.py` routes signals through `paper_trading.simulator`,
   which handles `is_short` (BearShort ran shorts through it; its 1H
   4-filter stack retired at −2.96 post-fix). `backtest/engine_multi.py`
   (every Phase 4.C basket trial) is BUY-opens/SELL-closes only, and
   `engine_perp.py` is the two-leg carry path. So every cross-sectional
   anomaly was tested as its long half on 5–11 coins, which collapses
   toward BTC beta (see §3). The original portfolio was regime-aware by
   design (`REGIME_ALLOCATIONS`, GridTrading/Supertrend regime gates,
   BearShort for bear regimes) — the long-only limitation is a Phase 4.C/4.E
   artifact of `engine_multi`, not a project philosophy. The 4.E literature
   stubs' phrase "engine is structurally long-only" is imprecise.
7. **Pre-June DSR/MinTRL numbers are units-invalid** (audit §1; annualised
   SR plugged into per-bar formulas). Any doc quoting `dsr=1.0000` or
   `mintrl=6 bars` from May is meaningless; the Sharpes and trade counts
   remain valid.

---

## 3. Why the citation-driven approach failed (structural, not bad luck)

These are the mechanisms, each verifiable in the repo:

- **The validatable zone is tiny.** Units-correct MinTRL at 95% is
  ≈ (1.645 / true SR)² years, frequency-independent. On the 52-month dev
  window the minimum detectable annualised Sharpe is ≈ 0.79; on 44 months
  ≈ 0.86; on the old 29-month window ≈ 1.05. Family multiplicity then
  raises it further (cs-momentum family null already 0.61 after 5–7 trials;
  program-layer null 2.64 — unclearable). Published crypto anomalies with
  true SR 0.5–0.8 cannot be confirmed here even if real.
- **Bull-window inflation was universal.** Every strategy re-tested on the
  2021→2025 window roughly halved its Sharpe (AttentionMom 2.11→0.77,
  CSMom 1.64→0.94, FRH 2.90→0.50). The 2023–24 dev window was 73% trend-up
  days; the 1.94-Sharpe BTC B&H baseline was the real reason 7 of 21
  positive-Sharpe designs died.
- **Long-only truncation + small cross-section (Phase 4.C/4.E).** Basket
  trials ran through `engine_multi`, which has no short leg: 5–11 coins,
  long leg only, BTC dominant ⇒ "hold the recent winner" ≈ BTC beta minus
  rebalance friction. The literature's cross-sectional effects live in
  50–500-coin universes with a short leg. (The single-symbol engine and
  the perp engine do support shorts; the basket path is the gap.)
- **Citation quality was low and stale.** Proposal-agent citations scored
  ~3.0 by count of sources, not by quality: arXiv, SSRN, master's theses,
  predatory-tier journals; many pre-2022 samples. No replication check was
  ever performed before a trial consumed budget.
- **Intraday long-only on BTC has no gross edge.** Phase 4.E showed the
  microstructure/ICT entry vocabulary is wrong-signed *before* fees on
  15m–1h BTC spot while B&H dev Sharpe was +0.49. This is a real negative
  result for that whole class, not a cost artifact.
- **Every survivor died on regime change.** Both dev-keeps (AttentionMom,
  FRH) failed holdout on 14 trades each; the June audit shows those
  holdout Sharpes are statistically indistinguishable from zero — the
  7-month holdout is too short to retire *or* confirm anything at these
  trade counts.

Net: the harness is correct and the substrate/design combination tested
so far has no provable edge at retail scale. "Better citations" is not
the fix; the same statistical arithmetic kills any daily long-only
directional design with true SR < 0.8.

---

## 4. Constraints any new approach must satisfy (the arithmetic, not opinion)

1. Expected **true net Sharpe ≥ ~1.0** on ≥ 4 years of *available* history,
   or the design is unvalidatable here by construction. Say which of these
   levers the design pulls: higher true SR (market-neutral, higher breadth),
   longer history (substrates with data back to 2019–2020), or an evaluation
   that does not depend on MinTRL at 95% (forward paper test).
2. **Benchmark honesty.** Directional/long-only designs must show NW-alpha
   > 0 and IR ≥ 0.5 vs same-instrument B&H (gate v2). If a design cannot
   plausibly beat holding BTC through a bull leg, it must be market-neutral
   (`"neutral": true`, PSR vs 0) — for basket designs that means adding a
   short leg to `engine_multi` (single-symbol engine and `engine_perp`
   already short; see §2.6).
3. **Multiplicity budget.** Each trial raises the family null for every
   sibling. A new family starts at N=1 only if its data/mechanism is
   genuinely distinct (the 4.E precedent). Budget ≤ 5–7 trials per family
   before the null makes SR 1.0 unclearable.
4. **Costs at spot taker 0.10% + slippage, with a 2× stress** (4.E gate).
   Turnover must be stated ex ante.
5. **Survivorship-bias-free universe** if any cross-sectional design is
   proposed (delisted coins must be in the history). This was never handled.
6. **No holdout regeneration without a written trigger.** Each regen shrinks
   the virgin window; the next one should coincide with a decision that
   needs it (e.g. making NewsSent/AttentionMom testable), not precede it.
7. Everything in `CLAUDE.md` still binds: pre-registration in
   `research/<name>-literature.md` before any run, 20-variation cap,
   3-failure stop, trials.log append, human-only push/deploy/schema.

---

## 5. Candidate directions for the research session to evaluate (unranked — questions, not answers)

The next session should research these against §4 and rank them with
evidence. It should not run any trial.

- **Forward paper test of CSMom + AltcoinSeason** (diagnosis Path A). Zero
  trial budget, reversible, agent-autonomous. Question: is 6–12 months of
  paper data even decisive at ~1 trade/week? Compute the expected power.
- **Long-short cross-sectional factors on a wide universe** (top-50/100 by
  liquidity, perp-based, both legs). This is the version the literature
  actually tests. Questions: data source with delistings (Binance Vision
  perp klines?), engine work required (short leg via `engine_perp`),
  funding cost of the short leg, and whether 2021→2025 history exists for
  enough names.
- **Market-neutral / structural income designs that do not need to beat
  B&H**: basis (cash-and-carry) across venues/tenors, cross-sectional
  funding carry with a real short leg (the long-only version `+1.10`
  died to B&H), stablecoin/lending-rate arbitrage, liquidation-cascade
  reversal on perp data. Questions: capacity at $10–100k, exchange risk,
  Thai PIT (already researched in `research_log.md`).
- **Extending the two under_tested strategies** by fetching 2026-06→now
  data and deciding a single, well-justified holdout regen.
- **Prediction markets (Phase 5 / Polymarket)** — infrastructure exists
  (`phase5/`, `data/polymarket.py`, scanner agent). The 2026-05-08
  "PROFITABLE_PROVISIONAL" verdict rests on 12 markets and LLM-opinion
  edges; treat as untested. Question: is there a defensible, testable
  edge mechanism (e.g. calibration vs market on resolved-market history),
  and does it fit the harness's DSR/MinTRL framing at all?
- **Accept the null**: passive BTC/ETH holding with a rules-based
  drawdown/regime overlay, evaluated as risk reduction rather than alpha.
  This is the honest default if nothing above clears §4.
- **What Fable 5.1 changes**: not signal generation. Use it for (a)
  replication-status vetting of every citation before it enters the queue,
  (b) adversarial review of each hypothesis against §4 before code is
  written, (c) statistics/validation design (path-CPCV for any fit/predict
  design, power analysis for forward tests), (d) audits like the June one.
  Cheaper models keep executing pre-justified batches.

---

## 6. Paste-ready prompt for the next chat session

Copy everything between the fences into a fresh chat (claude.ai project
with the crypto-bot knowledge, or Cowork on C:\crypto-bot). Regenerate
repomix first (see SYNC CHECKLIST) or the chat will read May-9 state.

```
Read repomix-output.xml with bash_tool as your FIRST action, then
CLAUDE.md, then docs/handoff_template.md, then userMemories.
Confirm when done.

Then read, end-to-end, in this order:
  docs/revival_handoff_2026-09-02.md   (this brief — treat as the map, not the territory)
  docs/project_diagnosis_2026-07-02.md
  docs/gate_recalibration_audit_2026-06.md
  docs/redesign_proposal_microstructure_2026-07-03.md
  docs/bot_status.md  (Phase 4.E + 2026-06-11 sections)
  docs/validation_framework.md  (Gate spec v2 section)
  docs/research_log.md  § "AI/algo trading viability" and § "Thai SEC venue"
  .memory/T1_episodic/2026-05-08/strategic-review-retire-pattern.md

ROLE. You are the research/planning chat for crypto-bot, running Claude
Fable 5.1. Chat plans; Claude Code executes. You will NOT run trials,
edit sacred files, or write strategy code in this session. Your
deliverable is a written research report + a ranked proposal that Kanin
can approve, and then Claude Code prompts for whatever he approves.

WHY YOU ARE HERE. Since April 2026 the project has pre-registered and
run roughly 44 strategy designs through a rigorous harness (block-Sharpe
CPCV, DSR gate spec v2, MinTRL, holdout single-access). Zero passed
holdout. The June audit found the earlier gates were units-buggy and
the dev window was bull-inflated; on the corrected 52-month window every
re-tested Sharpe roughly halved. The citation-driven proposal loop
(SSRN/arXiv anomalies, long-only, 5–11 coin baskets, daily bars) is
exhausted: the statistical arithmetic (MinTRL ≈ (1.645/SR)² years;
family multiplicity) kills any daily long-only directional design with
true Sharpe < ~0.8 regardless of how good the citation is. The July
microstructure batch (new data, intraday) was wrong-signed gross of
fees. We need a genuinely different approach, chosen with the
constraints in revival_handoff §4 applied BEFORE any budget is spent.

VERIFY BEFORE YOU TRUST. Past sessions drifted by pattern-matching
variation names and chat memory instead of reading files. Do not assume
the brief is right. In your first response, independently confirm from
the repo (bash_tool on repomix / project files):
  1. HEAD commit and that main is ahead of origin (expect 8 commits).
  2. trials.log row count, distinct strategy_ids, and that no row
     predates 2026-05-05 (the Mac/PC split in revival_handoff §2.1).
  3. holdout_manifest dev_end (expect 2025-05-01) and the latest data_end.
  4. That backtest/engine_multi.py is long-only (BUY opens / SELL
     closes) while backtest/engine.py + paper_trading/simulator.py
     handle is_short — read revival_handoff §2.6 and §7 Q2.
  5. simulator.py FEE_MARKET value vs the 0.10% used in Phase 4.E.
Report any mismatch with the brief before doing anything else.
Also call conversation_search / recent_chats for "crypto-bot" decisions
after 2026-07-03 — this brief was written from files only and cannot
see chats; if a later chat decided something, that wins.

RESEARCH TASK (use web search; cite primary sources with year, venue,
sample period, and replication status — say explicitly when a result
has NOT been replicated out of sample or post-2022).
  A. For each direction in revival_handoff §5, and any others you find
     defensible, answer: (i) what is the edge mechanism and who is the
     counterparty paying it; (ii) best published net Sharpe, sample,
     universe size, long-short or long-only, costs assumed; (iii) does
     it satisfy EVERY item of §4 (state the number for MinTRL years at
     the claimed SR, the data source and its start date, turnover and
     cost at 0.10% taker, survivorship handling, family/N budget);
     (iv) engine/data work required in this repo (name the modules);
     (v) capacity at $10k–$100k and Thai-resident constraints;
     (vi) the single cheapest experiment that could kill it.
  B. Give the same treatment to "accept the null" (passive + overlay)
     and "forward paper test the two alpha near-misses" — including a
     power calculation: at ~1 trade/week and IR 0.5, how many months
     of paper data distinguish real from zero at 90%?
  C. Rank all directions. Be honest and direct: if the answer is that
     no retail-accessible crypto design clears §4, say so and make the
     case for the null or for leaving the substrate. Do not soften.
  D. List record-keeping fixes that should land before ANY new trial
     (start from revival_handoff §2; add what you find).

DISCIPLINE (verbatim from CLAUDE.md / .claude/rules):
  - Sacred-harness files (trials.log, holdout_manifest.json,
    holdout_access.log, holdout.py schema) and sacred docs (CLAUDE.md,
    MASTER_PLAN.md, architecture.md, validation_framework.md): do not
    edit; adding a new strategy CATEGORY to MASTER_PLAN.md is human-only.
  - No p-hacking: no parameter search without a per-variation written,
    sourced hypothesis; 20-variation cap; 3 consecutive failures stop.
  - Every experiment appends to trials.log; a skip writes no row.
  - Holdout regeneration is human-only and must be justified in writing.
  - Pushes, deploys, live capital: human-only.
  - Runnable artifacts only; bundle commands; no "let me know" loops.
  - Mandate F: design choices are yours when the evidence answers them;
    sign-off is reserved for push/deploy/sacred changes and for the
    direction decision itself, which is Kanin's.

OUTPUT. One report (docs/research_revival_2026-09.md via a Claude Code
prompt, or as a chat artifact) with sections A–D, then an EXIT RAMP:
recommended direction, what Kanin must decide, and the first Claude
Code prompt(s) with model recommendation per userMemories (Sonnet for
mechanical, Opus for spec'd implementation, Fable only for
statistics/validation design). Take your time; do not rush to a
recommendation before A–C are complete. Stop at the exit ramp.
```

---

## 7. Answers to the research chat's scoping questions (verified 2026-09-02)

The research chat asked three things before scoping. Repo-verified
answers; the capital line is Kanin's to confirm.

**Q1 — Was the June revised gate spec implemented after May?** Yes, fully,
and trials ran on it. Commit `e2cb071` (the chat's memory) is a May
commit; 23 commits landed after it. Gate spec v2 is in code:
`2af7cd8` (2026-06-11: DSR units fix, family-scaled eq.7, verdict gates —
`backtest/verdict.py` now uses corrected DSR ≥ 0.95, Newey-West alpha,
IR ≥ 0.5, PSR for neutral), `ba8ca78` (per-bar return persistence),
`5836592` (event-based blocks), `f787145` (validation_framework.md v2
text, pre-authorized), `65ac97f` (holdout regeneration). Trials since:
2026-06-11 re-run batch (FRH V2b extended + CSMom/AltSeason/NewsSent/
AttentionMom on the 52-month window) and 2026-07-03 Phase 4.E (4 rows).
Project knowledge shows the old tree only because `repomix-output.xml`
was last generated 2026-05-09. Regenerate it.

**Q2 — Short path in the engine?** Partly exists already. `backtest/engine.py`
(single symbol) executes `is_short` signals via `paper_trading/simulator.py`
(BearShort used it). `backtest/engine_perp.py` handles a two-leg
perp+spot book. What is missing is a short leg in `backtest/engine_multi.py`,
the basket engine every Phase 4.C cross-sectional trial ran on. Adding it
is schema-stable-code work (contract-preserving extension → agent-
autonomous; anything changing CPCV/DSR/verdict contracts → human). Kanin's
answer: willing — the project was never long-only by philosophy
(regime-aware allocation, BearShort, regime-gated grid); the long-only
runs were the citation tests on the basket engine. Any short-leg design
must model perp funding cost and margin on the short leg (the Phase 4.B
risk model in `research/funding-rate-risk-model.md` is reusable).

**Q3 — Capital if something passes.** Paper only until results are good
enough to justify real money; no live deployment is planned on the first
pass. Research should assume a single retail venue (OKX, existing plumbing;
Thai PIT applies), taker fills at 0.10% spot / perp taker on the short
leg, and a deployable size in the **$10k–$100k** range (paper book is
$100k). Rule out anything needing multi-venue execution, maker-fill
economics, or sub-minute latency. Carry / market-neutral designs at
retail size are in scope. [Kanin: adjust the range if wrong.]

### CARRY-FORWARD STATE

- HEAD `8b779de` = local `main`; `origin/main` at `03b31cd` (8 unpushed
  commits: 4b9c3ab, 73fec0b, b91e387, 785c394, fd98c2a, 0054de8,
  61a6138, 8b779de — all Phase 4.E).
- Locked: gate spec v2 (2026-06-11); global `dev_end = holdout_start =
  2025-05-01`; family taxonomy in `backtest/strategy_families.json`
  (9 families incl. `microstructure-orderflow`); Phase 4.E fee gate
  (0.10% taker + 2× stress); 4.E batch closed with 3 hypotheses
  pre-registered but not run (HVNMeanReversion, DeltaDivergence,
  BreakoutDeltaConfirmed).
- In flight: nothing. Droplet paused; cron paused; no paper deploy.
- Parked: CSMom / AltcoinSeason (alpha-positive, DSR-floor retire);
  NewsSent / AttentionMom (under_tested, need ~7–10 more dev months);
  CryptoDualMomentum (under_tested, 15 trades); sq-031 never run;
  sq-037 needs_rerun; playbooks port plan chunks 1–8 unexecuted.
- Open human decisions: holdout regeneration timing; whether to merge
  the Mac-side trials.log; direction (this session's purpose).

### READ FIRST — partitioned by access method

PROJECT KNOWLEDGE / repomix (source + docs): `CLAUDE.md`,
`docs/handoff_template.md`, `docs/revival_handoff_2026-09-02.md`,
`docs/project_diagnosis_2026-07-02.md`, `docs/gate_recalibration_audit_2026-06.md`
(untracked — upload separately), `docs/redesign_proposal_microstructure_2026-07-03.md`,
`docs/MASTER_PLAN.md`, `docs/bot_status.md`, `docs/validation_framework.md`,
`docs/research_log.md`, `docs/strategies.md`, `backtest/strategy_families.json`,
`backtest/holdout_manifest.json`, `backtest/engine.py`, `paper_trading/simulator.py`,
`research/*-literature.md` (standalone uploads; repomix excludes research/).

LOCAL ONLY (gitignored; read via Claude Code / bash on the PC):
`backtest/trials.log` (48 rows), `backtest/holdout_access.log`,
`backtest/trial_queue_state.json`, `backtest/reports/per_bar_returns/*.parquet`,
`backtest/cache/**`, `logs/trial_queue.log`, `.memory/T1_episodic/2026-05-08/*`.
Mac-side `backtest/trials.log` (pre-2026-05-05 rows) — not on this machine.

### PROJECT-KNOWLEDGE SYNC CHECKLIST (run before opening the new chat)

```bash
cd /c/crypto-bot && git log -3 --oneline && git status --short | grep -v "^ M" \
&& git rev-list --count origin/main..main \
&& repomix && ls -la repomix-output.xml \
&& wc -l backtest/trials.log && python -c "import json;print(len(json.load(open('backtest/holdout_manifest.json'))))"
```

Re-upload to project knowledge after repomix: `repomix-output.xml`,
`docs/revival_handoff_2026-09-02.md`, `docs/gate_recalibration_audit_2026-06.md`,
`docs/playbooks_port_plan_2026-07-03.md`, `docs/strategies.md`, every
`research/*.md` touched since 2026-05-09 (all 4.E stubs, `funding-rate-*`,
the four near-miss files: `cross-sectional-momentum`, `altcoin-season-rotation`,
`news-sentiment-momentum`, `attention-momentum`). Decide whether to
`git add` the two untracked docs and push the 8 commits first (human-only).
