# Skill: onchain-source-list

Append-only source list for fundamentals-analyst on-chain
queries. WebFetch queries for on-chain metrics MUST target an
Approved source. The list is extensible without an
architecture edit — extension goes through the
"Adding a source" procedure below.

This list covers ON-CHAIN data only (exchange netflows,
stablecoin supply, miner flows, large-holder concentration,
chain-level metrics). Funding rate / OI / basis are pulled
through the bot's own data layer (the funding-harvest pipeline
already reads them) and are NOT on this list.

## Schema

Three sections, in this exact order:
1. Approved
2. Deprecated
3. Adding a source

Approved entries are append-only. Removing an Approved entry
is done by appending it to Deprecated with a one-line reason
and date.

## Approved

| Source | Endpoint type | Initial reviewer | Date added | Quality bar |
|---|---|---|---|---|
| CryptoQuant public dashboards | cryptoquant.com (public-tier dashboards via WebFetch; no API key) | Strategist | 2026-05-04 | Tier-1 on-chain provider; covers exchange netflows, stablecoin supply on exchanges, miner-flow, large-holder concentration. Public-tier coverage is sufficient for the Phase A flag-emission ruleset; no key needed in the request. |

## Deprecated

(none)

## Adding a source

To add a source to Approved:

1. **Cite the quality bar.** One paragraph explaining why the
   source meets the on-chain lane's standard. Required:
   verifiable methodology (the source's data-collection method
   is publicly documented), reproducibility (a third party
   could check the numbers), and at least one of {exchange
   flows, stablecoin supply, large-holder concentration,
   chain-level metric} coverage.

2. **Endpoint type.** Public dashboard / authenticated API /
   raw-chain query. If authenticated, the API key handling
   must NOT require fundamentals-analyst to read a secrets
   file (no-secrets-in-bash hook will block). Document how
   the key flows in (env var + framework wiring).

3. **Reviewer.** Strategist sign-off; record the strategist's
   name in the Reviewer column.

4. **Date.** UTC date of addition.

5. **Append a row** to the Approved table above. The list is
   append-only; do NOT reorder existing rows. New row goes at
   the bottom.

6. **Commit via heredoc** per mandate H. Single commit,
   touching only this skill file.

## Removing (deprecating) a source

If a previously Approved source is no longer fit (rate-limit
tightened beyond cycle budget, methodology changed, paywall
on previously-public dashboard):

1. Append a row to Deprecated with: source name, endpoint
   type, reviewer, date deprecated, one-line reason.
2. Do NOT delete the source's row from Approved.
   fundamentals-analyst reads the union and skips sources
   that appear in Deprecated.

## What this list does NOT cover

- Funding rate / OI / basis — bot's own data layer.
- News headlines about on-chain events — news-source-allowlist.
- Twitter discussion of on-chain events — sentiment-source-list.

## Why append-only

The flag history is meaningful only against a stable source
set. If exchange-flow-anomaly fired in 65% of cycles in
month 1 and 12% in month 2, was the underlying signal change
or was the source swap? Append-only + explicit Deprecated
preserves the audit trail required for the Phase B gate
decision.

See also: architecture.md D.4 (workflow); fundamentals-analyst.md
(agent consumer); regime-flag-rules skill (flags this lane
emits); analyst-report-template skill (report shape).
