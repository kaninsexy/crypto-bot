# Skill: regime-flag-rules

The binary-flag namespace + emission rules used by the four
lane analyst agents (Risk flags section of AnalystReport) and
by research-manager during synthesis (final adopted flags
slot). Flags are binary by design — no scalar severity, no
priority levels — because Phase A shadow-mode evaluation
needs simple paired observations against verdict outcomes,
and Phase C live wiring (post-gate) drives CapGuard's
`excluded_strategies` list which is itself binary.

## Namespace governance

Flag names below are the canonical set for Phase 4.D Phase A.
Adding a new flag requires:
1. Architecture.md D.4 review (the synthesis contract changes
   when a new flag joins the namespace).
2. Update of this skill file with the new flag's name +
   trigger + default + evidence-required fields.
3. A note in `.memory/T2_semantic/decisions_log.jsonl` about
   the flag addition.

Mid-cycle ad-hoc flag names are a parse violation —
research-manager's synthesis ignores unknown names and the
agent's slot effectively contributes nothing.

## Flag rules (per lane)

Each rule has four fields:
- **name**: the canonical flag string (use this verbatim in
  AnalystReport Risk flags section).
- **trigger**: the evidence pattern that fires the flag.
- **default state**: NOT-EMITTED unless the trigger fires.
- **evidence required**: which Observations rows must support
  the flag — research-manager grep-checks these.

### Market lane (market-analyst)

```
name: vol-regime-shift
trigger: ATR(14) ratio to 50-bar median crosses 1.50 OR drops
  below 0.65 within last 8 bars on the active substrate.
default: NOT-EMITTED
evidence: ATR ratio row in Observations + bar_ts in Inputs read.
```

```
name: rsi-extreme
trigger: RSI(14) >= 80 (overbought) OR <= 20 (oversold) on the
  active substrate, with 5-bar slope confirming direction.
default: NOT-EMITTED
evidence: RSI value + slope rows in Observations.
```

```
name: regime-boundary-proximity
trigger: deterministic regime label has been at the boundary
  (distance < 0.5 sigma to next class) for >= 50% of the
  cycle window.
default: NOT-EMITTED
evidence: regime label + distance-to-boundary in Observations.
```

### Social lane (social-analyst)

```
name: sentiment-extreme-greed
trigger: fear-greed index >= 80 AND polarity skew bullish on
  >= 2 of the Approved sources.
default: NOT-EMITTED
evidence: fear-greed value + per-source polarity rows.
```

```
name: sentiment-extreme-fear
trigger: fear-greed index <= 20 AND polarity skew bearish on
  >= 2 of the Approved sources.
default: NOT-EMITTED
evidence: fear-greed value + per-source polarity rows.
```

```
name: social-volume-spike
trigger: aggregate post volume in cycle window >= 3× the
  trailing 7-day per-cycle median on >= 1 Approved source.
default: NOT-EMITTED
evidence: per-source volume row + 7-day median in Observations.
```

### News lane (news-analyst)

```
name: macro-event-imminent
trigger: a scheduled FOMC / CPI / NFP event lies within the
  next 24h AND a story tagged it in the cycle window.
default: NOT-EMITTED
evidence: per-story row with event tag + UTC timestamp.
```

```
name: regulatory-action-imminent
trigger: SEC / CFTC / equivalent regulator headline appeared
  in the cycle window with direction != ambiguous.
default: NOT-EMITTED
evidence: per-story row with regulator tag + direction tag.
```

```
name: exchange-incident-active
trigger: tier-1 exchange (OKX / Binance / Coinbase / Kraken)
  hack / suspension / listing-event headline in cycle window.
default: NOT-EMITTED
evidence: per-story row with exchange tag.
```

### Fundamentals lane (fundamentals-analyst)

```
name: funding-regime-flip
trigger: funding rate sign-flip on >= 1 active substrate in
  the last 16 bars, with current value >= 1 sigma magnitude
  on the new side.
default: NOT-EMITTED
evidence: funding rate row (value + sign-flip-bars-ago + sigma).
```

```
name: basis-dislocation-widening
trigger: basis (perp-spot or quarterly-spot) magnitude has
  grown by >= 30% over the last 24h AND current ratio to
  30-day median >= 1.30.
default: NOT-EMITTED
evidence: basis row (current bps + 24h delta + 30d ratio).
```

```
name: exchange-flow-anomaly
trigger: 24h exchange netflow magnitude on a covered chain >=
  2× trailing 30-day median.
default: NOT-EMITTED
evidence: exchange netflow row + 30-day median in Observations.
```

```
name: oi-spike
trigger: open interest 24h delta >= +25% OR <= -25% on >= 1
  active substrate.
default: NOT-EMITTED
evidence: OI row (level + 24h delta).
```

### Synthesis-level (research-manager only)

```
name: cross-lane-divergence
trigger: >= 2 of the 4 lanes' Regime view verbs disagree with
  the deterministic regime label.
default: NOT-EMITTED
evidence: per-lane Regime view paragraph cited from each
  AnalystReport.
```

```
name: cross-model-divergence
trigger: Sonnet primary and Gemini secondary disagree on >= 1
  binary flag OR |score_sonnet - score_gemini| > 0.30.
default: NOT-EMITTED
evidence: divergence audit section in synthesis.
```

## What the flags do NOT do (Phase A)

- They do NOT size positions.
- They do NOT pause live trading (paper or otherwise).
- They are NOT read by `portfolio/manager.py` or CapGuard.
- They are NOT promoted to T2 facts.md without Strategist
  review.

Phase A is shadow-only by architecture.md D.4. Phase B gate
(chat-side, after >= 30 paired observations) decides whether
flags drive CapGuard's `excluded_strategies` list and a new
risk-flag check in portfolio.manager.py rebalance step
(Phase C wiring).

## Why binary, not scalar

The Phase B gate metric is a paired comparison: did the flag
fire on a cycle that paired with a verdict outcome? Binary is
the cleanest signal for that comparison. Scalar severity
introduces a second knob (threshold for "high") that we'd
need to calibrate before we know whether the flag is worth
having. Cost discipline: validate the signal before adding
machinery.

See also: analyst-report-template skill (the AnalystReport
shape that emits these flags); architecture.md D.4 (workflow
+ Phase A→B→C gating); architecture.md C.1 (research-manager
escalation triggers).
