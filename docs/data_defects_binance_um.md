# Data-defects registry — Binance USDT-M archive

**Created 2026-09-02 (megaloop run 3, I5).** Counts measured on the cache as of
that date: 182 symbols with metrics, 458 with klines, 433 with funding.

## Why this exists

Run 2's deleveraging screen was about to count **feed gaps as liquidation
events**. The defect was already documented — `docs/recon_binance_um_2026-09.md`
§4 described it in full — and it was caught only because someone happened to
remember reading it.

**Memory is not a control.** This file makes the knowledge mechanical: every
defect has a detection query, a measured count, and a one-line guard. The rule
in `.claude/rules/backtest.md` § "Discovery / confirmation split" makes
consulting it mandatory:

> Any screen or trial reading a substrate must first consult that substrate's
> data-defects registry and apply its guards; a defect list that exists but is
> not applied is the same failure as no list.

**Code:** `data.binance_vision_um.defect_report(df, kind)` counts them;
`clean_metrics(df)` and `fetch_metrics(..., clean=True)` apply the metrics
guards. Tests: `data/tests/test_um_data_defects.py`.

---

## D1 — `sum_open_interest` drops to a hard zero *(the expensive one)*

| | |
|---|---|
| **What** | The open-interest feed reports exactly `0` for a handful of 5-minute bars per symbol. BTC open interest does not go to zero and recover within the hour: it is a feed gap, not an event. |
| **Scale** | **41,956 rows across 169 symbols** (0.241 % of 17.4 M cached 5-minute rows). Worst: FTTUSDT 6,916. |
| **Detect** | `defect_report(df, "metrics")["zero_open_interest"]` |
| **Guard** | `clean_metrics(df)` — masks non-positive OI to `NaN`. Or `fetch_metrics(..., clean=True)`. |
| **Why it bites** | `resample_metrics(df, "1D")` takes the day's LAST value. A zero at day-end makes that day's OI zero, so the next day's 24 h change reads **−100 %** — five times past a −20 % event threshold. 45 such daily bars appeared in the first 40 symbols alone. Unguarded, a deleveraging screen measures the reversal of feed gaps. |
| **Excluded, not repaired** | `NaN` lets the daily `last` fall back to the day's last VALID reading, and a day with none yields `NaN`, which produces no event. Interpolating would invent open interest that was never observed. |

## D2 — alt metrics only begin 2021-12

| | |
|---|---|
| **What** | The 5-minute metrics archive covers **BTCUSDT from 2020-09-01**; every other symbol starts **2021-12-01**. |
| **Scale** | 181 of 182 cached symbols. |
| **Detect** | `df.index.min()` per symbol. |
| **Guard** | Request metrics from `2021-12-01`, not from the kline start. Any CROSS-SECTIONAL open-interest study has ~13 usable months, however much is downloaded. |
| **Why it bites** | Two ways. Statistically, it caps the deleveraging cross-section at 220 events — below what its kill test needed. Operationally, requesting from 2020-01 makes the loader serially download ~700 days of 404s per symbol at ~1.5 req/s: hours of wall clock to discover nothing. Both cost run 2 real time. |

## D3 — `funding_interval_hours` is not always 8

| | |
|---|---|
| **What** | Funding settles every 8 h for most symbols, but 7 switch to 4 h or 2 h, clustered around **Nov 2022** (adjacent to the FTX collapse). |
| **Scale** | **7 symbols**: FTTUSDT, FTTBUSD (2 h), HNTUSDT, PHBBUSD (4 h), OMGUSDT (2 h), SOLBUSD (2 h and 4 h), and one more. |
| **Detect** | `defect_report(df, "funding")["distinct_funding_intervals"]` |
| **Guard** | Read the interval **per settlement**; never assume 3 settlements/day. `holdout._load_binance_um_df` carries the column through rather than dropping it. |
| **Why it bites** | A carry design that assumes 8 h under-counts a 2 h symbol's funding by **4×** — in the exact window where funding was most extreme, so the error is largest where it matters most. |

## D4 — BUSD duplicates and `_`-suffixed delivery contracts in the raw universe

| | |
|---|---|
| **What** | The raw symbol list mixes BUSD-margined duplicates (`BTCBUSD`, `ETHBUSD`), dated delivery contracts (`BTCUSDT_220325`), and `..._SETTLED` markers in with the USDT perps. |
| **Scale** | **154 of 986** raw symbols (986 → 832 after filtering). |
| **Detect** | Compare `universe_table(perp_only=False)` with `universe_table()`. |
| **Guard** | Always use `universe_table()` (perp-only is the default since `04eaaed`). Intersect any cache-derived symbol list with it. |
| **Why it bites** | BUSD pairs duplicate the same underlying, so a cross-sectional sort double-counts one asset; delivery contracts have expiry dynamics that are not perp dynamics. Run 3 hit this: a top-110 fetch by volume spent 16 slots on BUSD/delivery names and delivered only 79 usable perps. |

## D5 — zero-volume kline days

| | |
|---|---|
| **What** | Daily bars with `volume == 0`, typically around listing or delisting. |
| **Scale** | **1,766 rows across 19 symbols.** |
| **Detect** | `defect_report(df, "klines")["zero_volume_bars"]` |
| **Guard** | Exclude zero-volume bars from any volume-ranked universe rule and from any return computed across them (the close is stale, so the return is fictional). |
| **Why it bites** | A stale close produces a zero return followed by a large catch-up move — an artificial event for any 2σ-style trigger. |

## D6 — the `delisted` flag is publish-lag, not truth

| | |
|---|---|
| **What** | `delisted` is derived from whether a symbol's `last_month` is near the current month. The archive publishes with a lag, and the cached universe table was itself built at a point in time. |
| **Scale** | **832 of 832** perp symbols are currently flagged delisted — including BTCUSDT. The flag is presently useless as a liveness signal. |
| **Detect** | `universe_table()["delisted"].mean()` — a value near 1.0 means the table is stale, not that the market closed. |
| **Guard** | Never use `delisted` to mean "not trading now". Use it only for *relative* history within the cached window (`last_month < 2023-01` genuinely means it stopped before 2023), and regenerate the table before trusting it near the present. |
| **Why it bites** | A universe rule reading "exclude delisted" against this table excludes **everything**, silently. |

---

## Adding a defect

1. Measure it — a count over the cache, not an impression.
2. Add a row here: what, scale, detect, guard, why it bites.
3. Add the count to `defect_report` if it is countable from a frame.
4. Add a guard to `clean_metrics` (or the relevant loader) if it is fixable.
5. Add a test that **fails without the guard** — `test_um_data_defects.py`
   pins D1 that way, reproducing the exact −100 % fabrication.

A row with no detection query is an anecdote. A guard with no failing test is
a hope.
