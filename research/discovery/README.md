# Discovery ledger — `research/discovery/`

**Created:** 2026-09-02 · **Authority:** `docs/research_revival_2026-09.md`
§C.2 ("Process change: discovery / confirmation split").

A **discovery screen** is exploratory analysis on the sealed
2020-01-01 → 2022-12-31 window of the Binance UM substrate. It is **not
a trial**. It writes **no** `backtest/trials.log` row — the ledger row
in this directory *is* its record. A **confirmation trial** is the
counted `full_cpcv` run on 2023-01-01 → 2025-05-01 that follows a
surviving screen; it writes exactly one `trials.log` row and carries
the ledger's `N_disc` into its pre-registration as an additional
Bonferroni-style haircut on the confirmation DSR.

> **Status (2026-09-02, updated later the same day):** the discovery /
> confirmation split is **APPROVED and IN FORCE**. The rule text landed
> verbatim in `.claude/rules/backtest.md` § "Discovery / confirmation
> split" at commit `6a564ec`, under the human pre-authorization in the
> 2026-09-02 megaloop prompt's AUTONOMY section (CLAUDE.md
> "Pre-authorization exception"). Real-data screens and ledger rows are
> therefore authorised.
>
> The paragraph this replaces said the opposite — screens `--selftest`
> only, no ledger row authorised — because it was written a few hours
> before the rule landed. It is corrected rather than left standing:
> a README that contradicts the sacred rule file is worse than either
> one alone, since the next reader cannot tell which is current.
>
> One detail the rule's wording leaves implicit. It applies to
> "substrates whose manifest entry declares a `discovery_end`". The
> three Phase 4.F entries declare it inside `notes`
> (`discovery_end=2023-01-01`), not as a top-level field, for the same
> reason they carry `substrate=binance_um` there: adding a manifest
> field is a schema change and human-only. The screens enforce the
> window from their own `DISCOVERY_END` constant and hard-assert it, so
> the seal does not depend on the manifest being parsed for it.

## Hard rules for every row

1. **Discovery never reads 2023+ data.** Every screen hard-asserts that
   the maximum timestamp it touched is `< 2023-01-01T00:00:00Z` and
   aborts otherwise. The `data range used` field must end before that
   date or the row is invalid.
2. **One row per screen run.** A screen that is re-run with different
   parameters, a different universe rule, or a different horizon is a
   *new* row, not an edit of the old one. `N_disc` for a family is the
   row count of its ledger — under-counting it is the same failure the
   multiple-testing correction exists to prevent.
3. **No `trials.log` write, ever.** The discovery scripts must not
   import `backtest.trials` or call `record_trial`. A screen that wants
   a trial row is a confirmation trial and belongs in a
   `scripts/run_*_trial.py`.
4. **Rows are append-only.** Correct an error by appending a new row
   whose `conclusion` supersedes the old one, and note the superseded
   row's date. Do not delete or silently rewrite history.
5. **Pre-registered kill test is fixed.** The "Pre-registered kill
   test" section at the top of each ledger is copied verbatim from the
   §C.4 table of `docs/research_revival_2026-09.md` and is not
   editable by a screen result. Moving the threshold after seeing the
   statistic is the p-hacking the split is designed to prevent.

## Row format

Each ledger carries one markdown table under `## Ledger`. Fields, in
column order:

| field | meaning |
|---|---|
| `date` | UTC date the screen was run (`YYYY-MM-DD`). |
| `family` | `funding_dispersion_carry`, `deleveraging_reversal`, or `listing_flow` — matches the ledger filename. |
| `signal` | The exact signal definition screened, including every parameter (e.g. `trailing 3x8h funding sum, decile 10 minus decile 1`). |
| `universe rule` | The ex-ante eligibility rule (e.g. `top-150 by trailing 30d dollar volume, listed >= 30d`). Must be computable at signal time — no survivorship. |
| `horizon` | Holding / measurement horizon (e.g. `next 1 day`, `+3 days`, `[0, +20] days`). |
| `statistic` | Name of the pre-registered statistic (e.g. `mean daily net 10-1 spread, %/day`). |
| `value` | The realised value, with units. |
| `t-stat` | The statistic's t-statistic. |
| `N` | Sample size (days, events, or listings — say which in the statistic name). |
| `data range used` | `YYYY-MM-DD → YYYY-MM-DD`; **must end < 2023-01-01**. |
| `script + git commit` | `scripts/discovery_<family>.py @ <short sha>` — the exact code that produced the value. |
| `conclusion` | `survives` / `killed` / `inconclusive`, plus one clause of why, referenced to the pre-registered threshold. |

Append rows with the screens' own `--append-ledger` flag rather than by
hand, so the script/commit provenance and the date cannot drift from
the numbers.

## Files

| file | family | screen |
|---|---|---|
| `funding_dispersion_carry.md` | 1 — funding-dispersion carry | `scripts/discovery_funding_dispersion.py` |
| `deleveraging_reversal.md` | 2 — deleveraging reversal | `scripts/discovery_deleveraging_reversal.py` |
| `listing_flow.md` | 3 — listing / delisting flow | `scripts/discovery_listing_flow.py` |

## Carrying `N_disc` forward

When a family's screen survives, the confirmation trial's
pre-registration in `research/<strategy>-literature.md` records:

- `N_disc` = the number of ledger rows for that family at the moment
  the confirmation hypothesis was frozen (quote the ledger row count
  and the commit sha);
- the surviving row's `value` as the discovery number supporting the
  expected SR (§C.2 item 3);
- the statement that the confirmation DSR is haircut by `N_disc` on
  top of the existing `trials.log` multiple-testing count.
