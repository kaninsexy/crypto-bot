# Project diagnosis — 2026-07-02 (plain-language)

Written for Kanin after a full read of CLAUDE.md, bot_status.md,
MASTER_PLAN.md, gate_recalibration_audit_2026-06.md, and the four
near-miss literature files. Purpose: explain in simple terms why the
project has produced zero deployable strategies, what is actually
still alive, and what the realistic paths forward are. This is a
diagnosis document, not a plan; no decisions are made here.

## 1. What happened, in one paragraph

Roughly 30 strategy ideas were tested across Phase 3c and Phase 4
inside a rigorous statistical harness (CPCV block Sharpes, Deflated
Sharpe Ratio, MinTRL, holdout split, trials.log multiple-testing
accounting). Zero strategies passed the deploy gate. Two passed
development and then failed holdout. The June 2026 audit then found
that (a) the original DSR math had a units bug that made all
pre-June pass/fail numbers unreliable, (b) the dev window was
bull-dominated so every "edge" was partly the 2023–24 bull market in
disguise — extended-window re-tests roughly halved every Sharpe —
and (c) the data window is structurally too short to statistically
prove any edge whose true Sharpe is below ~1.0.

## 2. The three distinct causes of "failure"

**Cause 1 — most strategies genuinely had no edge.** The original 10
retail-template strategies included several outright money-losers
(Supertrend −46%, TrendFollowing −38% on 3-year OOS). The harness
retiring them was the system working correctly.

**Cause 2 — bull-window inflation.** 73% of dev-window daily bars
were trend-up. On the extended 2021–2025 window every re-tested
Sharpe roughly halved (e.g. AttentionMomentum +2.11 → +0.77,
FundingRateHarvest +2.90 → +0.50). The audit's bull-inflation thesis
held for every single strategy re-tested.

**Cause 3 — the gate is close to unpassable with this data.** Two
findings from the June audit:
- MinTRL at true Sharpe 1.0 needs ~2.7 years of dev history
  regardless of bar frequency. Realistic crypto edges (true Sharpe
  0.5–0.8) cannot be validated at 95% confidence on any window this
  project can assemble. More bars at higher frequency do not help;
  the requirement is calendar time.
- Multiple-testing correction grows with every trial. At the
  program layer (N≈21 trials) the null Sharpe is 2.64 — nothing real
  clears that, ever. Even at the family layer, the cs-momentum
  family's five trials push its null to 0.61 and its DSR≥0.95 bar
  out of reach of a Sharpe-0.9 strategy.

Bottom line: this is not primarily an engineering failure. It is a
correct negative result on weak strategies, plus a statistical bar
that the available data length makes nearly impossible to clear.

## 3. What is actually still alive (the near-misses)

All four were re-run 2026-06-11b on the full 2021→2025-05 dev window
under corrected gate spec v2, after the BNB history gap was fixed by
splicing Binance data (so the "BNB blocker" is ALREADY SOLVED — do
not re-litigate it).

| Strategy | Full-window Sharpe | Alpha vs BTC B&H | Status | What blocks it |
|---|---|---|---|---|
| CrossSectionalMomentum | +0.94 | PASS (p=0.0153, IR 0.53) | retired | DSR 0.787 < 0.95 (family multiplicity floor) |
| AltcoinSeasonRotation | +0.84 | PASS (p=0.0155, IR 0.51) | retired | DSR 0.690 < 0.95 (same floor) |
| NewsSentimentMomentum | +0.72 | n/a | under_tested | needs 1,876 daily bars, has 1,571 (~10 months short) |
| AttentionMomentum | +0.77 | n/a | under_tested | needs 1,666 bars, has 1,450 (~7 months short) |

Key point: CSMom and AltSeason are the first strategies in the whole
project to show statistically significant genuine alpha over BTC
buy-and-hold on the honest window. They were retired purely on the
multiple-testing haircut, not because the edge is fake. The audit
called them "candidate resurrections, not flips-in-fact."

Also relevant: CryptoDualMomentum passed the family-layer DSR (0.986)
but is under_tested on a 15-trades-vs-30-floor precondition.

## 4. Why "just wait" is more complicated than it sounds

The under_tested pair needs ~7–10 more months of DEV data. But dev
currently ends 2025-05-01; everything after that is holdout. Data
through 2026-06 already exists — it is just reserved. Getting those
strategies testable means another holdout regeneration (moving
dev_end forward, drawing a new later holdout), which:
- is human-only (sacred-harness change),
- carries contamination disclosure like the 2026-06-11 regen did,
- shrinks the truly-virgin forward window each time it is done.

This is the single most consequential open decision, and it is a
human call, not an agent call.

## 5. Realistic paths forward (not mutually exclusive)

**Path A — forward paper-test the alpha pair.** Paper-deploy
CrossSectionalMomentum + AltcoinSeasonRotation locally or on the
droplet. Forward data is 100% virgin, consumes no trial budget, is
reversible, and paper deploy is agent-autonomous under CLAUDE.md
(2026-05-08 expansion). 6–12 months of paper results is the cleanest
possible evidence and costs nothing statistically. Caveat: the
verdict-tree deploy gate technically requires a "keep" — deploying
retired strategies to paper as a FORWARD TEST (not as "validated")
should be explicitly framed and approved as such.

**Path B — holdout regeneration decision (human).** Decide whether
and when to move dev_end forward to make NewsSentimentMomentum and
AttentionMomentum testable. Reasonable trigger: do it once, after
enough calendar time passes that both clear MinTRL (~2026-Q1 data
would do it), not repeatedly.

**Path C — better data for the sentiment strategies.** NewsSentiment
currently uses an OHLCV-derived proxy (volume-weighted log return),
not real news. Wiring a real sentiment feed is a structurally
different, citable variation — one of the few remaining moves that
adds information rather than just re-slicing the same prices.

**Path D — accept the substrate conclusion.** If the alpha pair
fails forward paper-testing and the under_tested pair fails when
testable, the honest conclusion is that retail-accessible crypto
OHLCV does not carry provable edge at this capital/data scale.
The validation harness itself is substrate-agnostic and is the most
valuable artifact of the project (Branch C / Phase 5 pivot,
already contemplated in MASTER_PLAN).

## 6. What NOT to do

- Do not loosen gates to manufacture a pass — the whole system
  exists to prevent exactly that, and the June audit showed what
  happens when the math is accidentally lenient (everything
  "passed," none of it was real).
- Do not run new parameter variations casually — every trial
  raises the family/program null for all strategies, including the
  near-misses. The remaining trial budget is the scarcest resource
  in the project.
- Do not trust any pre-June-2026 DSR/MinTRL number — the units bug
  makes them uninterpretable (audit §1).

## 7. Current operational state (for orientation)

- Droplet paused since 2026-04-29; cron loop paused via .cron-pause.
- Paper capital $100k, reset 2026-04-26 post short-pnl fix.
- trials.log: 44 rows; ~21 finite non-superseded full_cpcv trials.
- Phase 4.B (funding-rate carry) closed 2026-06-11 with 0 passers;
  FundingRateHarvest archived with kill report.
- Working tree clean at commit 01282c6.
