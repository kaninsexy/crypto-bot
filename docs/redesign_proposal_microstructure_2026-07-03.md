# Redesign proposal — Microstructure / Order-Flow batch (Phase 4.E candidate)

Date: 2026-07-03. Status: PROPOSAL ONLY. Adding a new strategy
category to MASTER_PLAN.md is human-only per CLAUDE.md; this doc is
the design for Kanin to approve, amend, or reject. Nothing here has
been implemented or entered into the trial queue.

## 0. Why this direction is defensible (and why the old one stalled)

Every strategy tested so far consumed the same raw information:
1d/1h OHLCV closes, plus two weak external feeds (Google Trends, an
OHLCV-derived "sentiment" proxy). Twenty-plus trials re-sliced the
same price series in different ways. The June 2026 audit showed the
published Sharpes behind those citations were bull-window and
p-hacking artifacts — the papers didn't replicate.

The proposed concepts (volume profile, order-flow delta, liquidity
sweeps, acceptance/rejection, FVG, order blocks, VWAP) are different
in one way that actually matters statistically: **they require new
input data** — intrabar volume-at-price and aggressor (taker
buy/sell) volume. New data is new information; re-parameterizing
old data is not.

There is also a hard statistical argument FOR intraday designs.
MinTRL is calendar-time bound: years needed ≈ (1.645 / true_SR)².

| True Sharpe (net of costs) | Calendar data needed to validate at 95% |
|---|---|
| 0.7 | 5.5 years — not achievable |
| 1.0 | 2.7 years — borderline |
| 1.5 | 1.2 years — comfortable |
| 2.0 | 0.7 years — easy |

Daily-timeframe strategies realistically top out around SR 0.8–1.0,
which this project's data can barely validate (that is exactly where
the four near-misses died). Intraday microstructure strategies, IF
they work at all, tend to have higher Sharpes with more independent
bets — the only strategy class whose true SR could plausibly sit in
the validatable zone. That, not ICT theory itself, is the honest
case for this pivot.

The honest case against: rigorous evidence for ICT concepts is thin
to nonexistent (they are mostly practitioner folklore, often taught
in unfalsifiable form), and intraday trading pays taker fees +
slippage on every trade — most intraday edges die to costs. Both
concerns are testable; neither is a reason not to run a disciplined
batch.

## 1. What does NOT change

- The harness: CPCV, DSR (gate spec v2, units-correct), MinTRL,
  verdict tree, holdout single-access, trials.log accounting.
  The harness is substrate-agnostic by design (Branch C rationale).
- The discipline rules: 20-variation cap, 3-consecutive-failure
  escalation, pre-registration of every hypothesis BEFORE its trial
  runs, no grid searches.
- The written-hypothesis requirement. Note: the no-p-hacking rule as
  written already accepts "a written hypothesis documented in
  research/<strategy>-literature.md" — a peer-reviewed citation was
  never strictly mandatory. What changes is sourcing: hypotheses are
  drawn from practitioner concepts, mechanized precisely, with
  whatever citations exist (there are a few SSRN/practitioner
  studies on FVG/liquidity-sweep profitability) attached as
  reference rather than authority.
- Paper/live gates. Nothing deploys without a "keep" on holdout.

## 2. What changes

1. **New strategy family cluster: `microstructure-orderflow`.**
   Under gate spec v2's family-scaled DSR, a new family's
   multiple-testing null starts from its own trials, not the 21
   accumulated directional trials. First trials in the family face
   approximately a pure significance test (DSR ≥ 0.95 vs ~0), which
   4–5 years of data can clear at true SR ≳ 0.8. This is the single
   biggest statistical benefit of a genuinely new substrate — and it
   is legitimate only if the family really is a distinct hypothesis
   class fed by distinct data, which this is.
2. **New data layer (prerequisite, before any trial):**
   - 1m OHLCV, 5 years, for the basket majors — needed to build
     volume profiles (volume-at-price histograms) under coarser
     signal bars. ~2.6M bars/symbol; parquet cache handles it.
   - Per-candle taker buy/sell volume (aggressor split) — the
     order-flow delta substrate. Binance klines carry
     `taker_buy_base_volume` natively and Binance Vision publishes
     full history for free; OKX candles do not carry the split.
     Cross-venue provenance (Binance data, OKX execution) follows
     the precedent set by the 2026-06-11 BNB backfill, with the
     same disclosure discipline.
   - Optional later: aggTrades (tick) data for true footprint
     charts. Not required for the first batch; per-candle delta at
     1m resolution is a standard first approximation.
   - **Recon step zero:** verify current Binance Vision availability
     / format and OKX 1m history depth before committing the batch.
3. **Fee realism gate (new, batch-specific):** every intraday trial
   is run twice — once at standard taker fees + slippage model, once
   at 2× fees. A hypothesis whose edge survives only the optimistic
   fee case is recorded as retire, not keep. This is cheap insurance
   against the classic intraday failure mode.

## 3. The pre-registered hypothesis batch (max 7 slots)

Each concept below must be mechanized into exact, parameter-explicit
rules in its research/<name>-literature.md BEFORE its trial runs.
"The market likely goes up" is not a rule; "enter long at bar close
when X, exit when Y/Z" is. Drafting these files is the first
implementation task after approval. Starting points:

| # | Strategy | Core mechanized hypothesis | Data needed |
|---|---|---|---|
| 1 | VolumeProfileAcceptance | Price closing ≥2 consecutive signal bars above the prior N-day value-area high (volume-at-price 70% zone), on above-median delta, continues upward (acceptance = initiative buying at new prices) | 1m volume profile + taker delta |
| 2 | LiquiditySweepReversal | Bar takes out a prior swing high/low by ≤ k·ATR then closes back inside the range on opposing delta → fade the sweep (stop-hunt reversal) | OHLCV + taker delta |
| 3 | LVNTraversal (FVG-adjacent) | Price entering a low-volume node between two HVNs traverses it fast in the entry direction (thin books don't hold price); FVGs used as LVN markers | 1m volume profile |
| 4 | HVNMeanReversion (order-block-adjacent) | Price reaching a high-volume node from outside decelerates/reverts (positions defended where size was transacted) | 1m volume profile |
| 5 | DeltaDivergence | New price high on materially weaker cumulative taker delta than the prior high → exhaustion fade; and the mirror for lows | taker delta |
| 6 | VWAPInstitutionalBand | Session/anchored VWAP ± k·σ band: trend continuation on acceptance beyond band with delta confirmation; reversion to VWAP otherwise (institutional execution anchor) | OHLCV + delta |
| 7 | BreakoutDeltaConfirmed | Range breakout taken ONLY when breakout bar shows top-quartile delta and closes beyond the level (acceptance-filtered breakout — the redesign of the retired Breakout) | OHLCV + delta |

Batch rules: these 7 are the enumerated starting hypotheses.
3 consecutive failures stop the batch for re-assessment (existing
rule). No variation #2 for any of them without a fresh written
hypothesis. Timeframe is per-strategy (15m–1h signal bars built on
1m profile data is the expected default), per the
timeframe-per-strategy principle.

## 4. Sizing the multiple-testing cost honestly

7 trials in a fresh family: by trial 7, the family-layer null will
have grown from ~0 to whatever the realized cross-trial Sharpe
variance implies (the cs-momentum family hit sr_zero 0.61 after 5).
Budgeting realistically: a survivor needs net SR ≈ 1.2–1.5 on
4–5 years of data to clear both DSR and MinTRL by trial 5+. That is
a demanding but not absurd bar for an intraday edge — and if none
of the 7 gets close, that is a real answer about this substrate
too, obtained at a known, capped statistical price.

## 5. Sequencing

1. Kanin approves/amends this proposal (chat). MASTER_PLAN.md gains
   the Phase 4.E category (human-authorized edit).
2. Data recon: verify Binance Vision 1m + taker-volume history and
   OKX 1m depth; pick basket (default: BTC, ETH, SOL, XRP, ADA —
   avoid BNB seam complexity for v1).
3. Data layer build: 1m ingestion, volume-profile builder
   (volume-at-price histogram with value area / HVN / LVN
   extraction), taker-delta series, session/anchored VWAP. Unit
   tests. Manifest entries (new substrate rows; dev/holdout
   boundary decision rides on the existing regeneration question in
   docs/project_diagnosis_2026-07-02.md §4).
4. Literature files: mechanize all 7 hypotheses, lock pre-trial
   gates, including the 2× fee gate.
5. Run the batch through the unchanged harness.

## 6. Parked, not rejected

- The four near-misses (CSMom, AltSeason, NewsSent, AttentionMom)
  stay parked exactly as documented in project_diagnosis §5 —
  the under-tested two become testable with calendar time. Do not
  spend their trial budget while this batch runs.
- Paper trading: deferred per Kanin 2026-07-03. For the record,
  paper mode itself risks no money (simulated fills); the only cost
  is the droplet (~$6–12/mo) or $0 run locally. Revisit only after
  a backtest survivor exists.
