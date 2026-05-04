# Skill: sentiment-source-list

Append-only source list for social-analyst sentiment queries.
WebFetch / WebSearch queries for sentiment data MUST target
an Approved source (or a sub-domain thereof). The list is
extensible without an architecture edit — extension goes
through the "Adding a source" procedure below.

Twitter and Reddit access is routed through the existing
OpenClaw Tavily gateway at `127.0.0.1:18789`; the list below
captures the upstream sources that the gateway is configured
to surface.

## Schema

Three sections, in this exact order:
1. Approved
2. Deprecated
3. Adding a source

Approved entries are append-only. Removing an Approved entry
is done by appending it to Deprecated with a one-line reason
and date.

## Approved

| Source | Access method | Initial reviewer | Date added | Quality bar |
|---|---|---|---|---|
| Crypto Twitter | Tavily search via OpenClaw gateway 127.0.0.1:18789 | Strategist | 2026-05-04 | Established sentiment proxy for crypto markets; Tavily filters spam + low-quality reposts. Scope per cycle: scoped tickers from holdout_manifest.json, last cycle window. |
| Reddit (r/BitcoinMarkets, r/CryptoCurrency, r/ethereum, r/CryptoMarkets) | Tavily search via OpenClaw gateway 127.0.0.1:18789 | Strategist | 2026-05-04 | Long-form sentiment + retail-flow proxy; complementary to Twitter's short-form signal. Subreddit list is the initial scope; additions go through the procedure below. |
| Fear-greed index | alternative.me/crypto/fear-and-greed-index/ direct WebFetch | Strategist | 2026-05-04 | Aggregated sentiment indicator; widely cited; daily numeric value + 7-day delta. Public endpoint, no key required. |

## Deprecated

(none)

## Adding a source

To add a source to Approved:

1. **Cite the quality bar.** One paragraph explaining why the
   source meets the social lane's standard. Required: filters
   spam / bots (or explain why filtering is not needed), at
   least one of {short-form sentiment, long-form sentiment,
   aggregated index, retail-flow proxy} coverage, and a
   stable per-cycle volume the analyst can rely on.

2. **Access method.** Tavily / direct WebFetch / authenticated
   API. If authenticated, key handling must NOT require
   reading a secrets file (no-secrets-in-bash blocks it).
   Document the env-var / gateway wiring.

3. **Reviewer.** Strategist sign-off; record the strategist's
   name in the Reviewer column.

4. **Date.** UTC date of addition.

5. **Append a row** to the Approved table above. The list is
   append-only; do NOT reorder existing rows. New row goes at
   the bottom.

6. **Commit via heredoc** per mandate H. Single commit,
   touching only this skill file.

## Adding a sub-source (e.g., a new subreddit)

Sub-sources of an already-Approved access method (a new
subreddit under "Reddit", a new Twitter list under "Crypto
Twitter") are append-only edits to the Quality bar column of
the existing row, not new rows. Append-only still applies:
the prior content stays, the new sub-source is added at the
end of the bar. This keeps the historical scope traceable.

## Removing (deprecating) a source

If a previously Approved source is no longer fit (Tavily
upstream change, source paywalled, fear-greed endpoint
deprecated):

1. Append a row to Deprecated with: source name, access
   method, reviewer, date deprecated, one-line reason.
2. Do NOT delete the source's row from Approved.
   social-analyst reads the union and skips sources in
   Deprecated.

## What this list does NOT cover

- News headlines — those live in `news-source-allowlist.md`.
- On-chain data — lives in `onchain-source-list.md`.
- Bot-internal market data — not part of social lane.

## Why append-only

Same reason as the news and on-chain source lists. The Phase
B gate compares flag emission rates against the underlying
source set at the time of the cycle. Silent source swaps
break that comparison. Append-only + explicit Deprecated
preserves the audit trail.

See also: architecture.md D.4 (workflow); social-analyst.md
(agent consumer); regime-flag-rules skill (flags this lane
emits); analyst-report-template skill (report shape).
