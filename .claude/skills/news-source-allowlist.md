# Skill: news-source-allowlist

Append-only source list for news-analyst. WebFetch queries
in the news lane MUST target an Approved source (or a
sub-domain thereof). The list is extensible without an
architecture edit — extension goes through the
"Adding a source" procedure below.

## Schema

Three sections, in this exact order:
1. Approved
2. Deprecated
3. Adding a source

Approved entries are append-only. Removing an Approved entry
is done by appending it to Deprecated with a one-line reason
and date — the row stays in the file as the audit trail.

## Approved

| Source | Domain | Initial reviewer | Date added | Quality bar |
|---|---|---|---|---|
| CoinDesk | coindesk.com | Strategist | 2026-05-04 | Tier-1 crypto news; established editorial standards; covers regulatory + market events. |
| The Block | theblock.co | Strategist | 2026-05-04 | Tier-1 crypto news; institutional-quality reporting; strong on derivatives + exchange events. |
| Decrypt | decrypt.co | Strategist | 2026-05-04 | Tier-1 crypto news; broad protocol + DeFi coverage. |
| Reuters crypto | reuters.com/business/finance/cryptocurrency/ | Strategist | 2026-05-04 | Tier-1 wire service; macro + regulatory cross-coverage; high reliability for FOMC/CPI/ETF stories. |

## Deprecated

(none)

## Adding a source

To add a source to Approved:

1. **Cite the quality bar.** One paragraph explaining why the
   source meets the news lane's standard. Required topics
   covered: at least one of {regulatory, exchange events,
   macro events, tier-1 protocol events}. Editorial standard:
   named editorial team, public correction policy.

2. **Reviewer.** Strategist sign-off; record the strategist's
   name in the Reviewer column.

3. **Date.** UTC date of addition.

4. **Append a row** to the Approved table above. The list is
   append-only; do NOT reorder existing rows. New row goes at
   the bottom.

5. **Commit via heredoc** per mandate H. Single commit,
   touching only this skill file. No code changes paired in
   the same commit.

## Removing (deprecating) a source

If a previously Approved source is no longer fit:

1. Append a row to Deprecated with: source name, domain,
   reviewer, date deprecated, one-line reason.
2. Do NOT delete the source's row from Approved — leave it
   in place for audit. The Deprecated entry is the live
   instruction; news-analyst reads the union and skips
   sources that appear in Deprecated.

## What this list does NOT cover

- Twitter / Reddit feeds — those live in
  `sentiment-source-list.md`.
- On-chain data — lives in `onchain-source-list.md`.
- Bot-internal data layer (OHLCV, funding rates) — that is
  the bot's own pipes; news-analyst does not WebFetch it and
  it is not on this list.

## Why append-only

The list is the audit trail of what news-analyst was told to
read on every cycle in Phase A. Phase B gate evaluation
needs to compare flag emission to source coverage at the
time of the cycle — silently swapping out sources changes
the meaning of the flag history. Append-only with explicit
Deprecated entries preserves that history.

See also: architecture.md D.4 (the analyst-overlay workflow);
news-analyst.md (the agent that consumes this list);
analyst-report-template skill (the report shape).
