# Skill: analyst-report-template

The AnalystReport markdown schema emitted by the four lane
analyst agents (market-, social-, news-, fundamentals-analyst)
and parsed by research-manager during FAN-IN. The schema is
load-bearing — research-manager's parse breaks if a section is
missing or out of order. New sections are append-only at the
end and require an architecture-D.4 review before adoption.

## Required sections (exact order, exact headings)

```markdown
# AnalystReport — <lane>_<UTC YYYY-MM-DDTHH:MM:SSZ>

## Inputs read

- <file path or source URL> — <UTC timestamp> — <one-line
  description of what was read>
- ...

## Observations

<Per-substrate or per-source structured values. NO narrative.
Tables or bullet pairs only. Examples per lane below.>

## Regime view

<One paragraph synthesizing the observations against the
deterministic regime label from
`.memory/T1_episodic/_state/regime.txt`. Use one of:
"agree" / "lean-shift" / "contradict" as the lead verb. Cite
specific Observations rows.>

## Risk flags

- <flag-name>: <one-line trigger evidence citing Observations row(s)>
- ...
- (or the literal string "none" if no flags fire this cycle)

## Degraded?

degraded: <true|false>
reason: <one-sentence reason if true; omit line if false>
```

## Section semantics

- **Inputs read** — provenance for the cycle. Every source the
  agent touched (file paths, URLs, manifest entries, data-layer
  queries) gets a line. Research-manager uses this to detect
  whether all four lanes saw the same regime label and the same
  manifest substrates.

- **Observations** — raw structured values only. Indicator
  packs (market lane), source counts/polarity (social lane),
  per-story rows (news lane), funding/OI/basis/on-chain rows
  (fundamentals lane). No narrative; narrative belongs in
  Regime view.

- **Regime view** — one paragraph, one verb of (agree /
  lean-shift / contradict). The synthesis step at
  research-manager averages per-lane verbs; structured verbs
  enable that.

- **Risk flags** — binary flag NAMES that fire, one per line,
  with one-line trigger evidence. Flag names come from the
  regime-flag-rules skill (no ad-hoc names — extension goes
  through that skill's update procedure). If no flags fire,
  emit the literal string "none".

- **Degraded?** — boolean field. If true, the agent must name
  the failed source in one sentence. Research-manager's parse
  treats degraded reports as valid input (the slot exists);
  missing reports break the parse. Emit the report even on
  partial failure.

## Lane-specific Observations examples

### market-analyst

```
- BTC_USDT 4h: RSI(14)=63, slope=+1.2/bar; MACD hist=+0.0021,
  sign-flip=8 bars ago; ATR(14)=85.4 (1.18× 50bar median);
  realized vol=42% annualized; regime=BULL; bar_ts=2026-05-04T08:00:00Z
- ETH_USDT 4h: ...
```

### social-analyst

```
- Twitter (Tavily, scope=BTC, window=8h):
  posts=412, bullish=58%, bearish=27%, ambiguous=15%,
  top quotes=[<URL1 ts="..." snippet="...">, <URL2 ...>, <URL3 ...>]
- Reddit r/BitcoinMarkets (Tavily, scope=BTC, window=8h): ...
- Fear-greed index: 71 (greed), 7d delta=+8
```

### news-analyst

```
- 2026-05-04T05:12:00Z — coindesk.com/...
  headline="..." summary="..." direction=bullish
- 2026-05-04T06:30:00Z — theblock.co/...
  headline="..." summary="..." direction=ambiguous
- ...
```

### fundamentals-analyst

```
- BTC_USDT perp funding: +0.012% (8h), sign-flip=14 bars ago,
  distance from 50bar median=+0.6 sigma, regime=positive
- BTC OI: $24.1B, 24h delta=+3.2%, 30d ratio=1.12
- BTC basis (perp-spot): +18 bps, 24h delta=+4 bps,
  30d ratio=1.05
- CryptoQuant exchange netflow (24h): -1240 BTC (outflow)
- ...
```

## Path convention

Reports are written under:
`.memory/T1_episodic/episodes/<YYYY-MM-DD>/analysts/<lane>_<HH>.md`

where YYYY-MM-DD and HH are UTC date and hour. One file per
lane per cycle. Re-runs in the same hour overwrite (idempotent).

## Why a fixed template

The four lane analysts are spawned in parallel and write
independently. Research-manager's FAN-IN parse is mechanical:
header order is the contract, not Markdown rendering. A Haiku
agent improvising the schema means research-manager's grep
fails silently and the divergence audit becomes meaningless.
The cost of one paragraph of templating discipline is much
less than the cost of a synthesis cycle that reads four
unparseable reports and emits a bogus regime-context score.

See also: architecture.md D.4 (workflow); regime-flag-rules
skill (the binary flag namespace); CLAUDE.md mandate A
(read-before-respond — the Inputs read section is the audit
trail).
