# research_queue.md — autonomous research loop queue (T2)

Append-only queue read by Strategist (via the `next-variation-selector`
skill) when the autonomous research loop selects the next citation
lead to test or the next retired-strategy re-test slot. Maintained by
the curator agent (hourly cron, scope extension per architecture.md
D.4 closing paragraphs). Architecture refs: D.4 closing paragraphs +
MASTER_PLAN.md Phase 4.C "0 strategies pass" reframing.

Two sections, in this exact order:

1. Active candidates — citation leads + retired-strategy re-test
   eligibility windows.
2. Retired-strategy cooldown table — per-strategy cooldown row used
   to compute re-test eligibility.

Append-only: existing rows are not deleted or reordered. Resolved
rows (strategy re-tested, citation lead pursued) get a status update
appended at the end of the row, not removed.

## Active candidates

(none yet — populated as deep-researcher surfaces leads and as
strategies retire and reach their cooldown_until date)

Schema (one bullet per candidate):

```
- type: <citation_lead | retired_retest>
  source: <citation key | strategy name>
  added: <UTC date>
  context: <one sentence — why this lead is on the queue>
  status: <pending | in_progress | resolved>
```

## Retired-strategy cooldown table

Per architecture.md D.4: initial cooldown 30d after retire; 60d
after each subsequent re-test; capped at 180d. Re-test eligibility
also requires either a new citation surfaced by deep-researcher OR
the regime context flagged as "previously-untested-by-this-strategy".

| strategy | retired_at | cooldown_until | re-tests | reason |
| --- | --- | --- | --- | --- |
| (empty — populated as strategies retire) | | | | |

Cooldown computation:
- First retirement: cooldown_until = retired_at + 30 days.
- After re-test #1: cooldown_until = re-test_date + 60 days.
- After re-test #2: cooldown_until = re-test_date + 60 days
  (and so on; cap at 180 days).
- re-tests column counts the number of completed re-tests on
  this strategy since retirement.

Curator (hourly cron) appends rows when it sees a freshly-retired
strategy in `decisions_log.jsonl` (last hour of entries). Strategist
consults this table at session start when the autonomous research
loop is active.

See also: architecture.md D.4 (workflow + closing paragraphs);
MASTER_PLAN.md Phase 4.C ("0 strategies pass" reframing); curator.md
(maintainer).
