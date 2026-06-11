# AltcoinSeasonRotation -- Literature

<!-- Trial outcomes will be appended here by the orchestrator -->

## Trial outcomes

| variation_id | date | verdict | sr_observed | dsr | n_trades |
|---|---|---|---|---|---|
| altcoin-season-benchmark-rotation | 2026-05-08 | retire | 1.6767 | 1.0000 | 77 |
| altcoin-season-benchmark-rotation-gatev2-retest | 2026-06-11 | insufficient_data (pre-check; not run) | n/a | n/a | 0 |
| altcoin-season-benchmark-rotation (extended-window re-test) | 2026-06-11 | retire | 0.8359 | 0.6895 | 150 |

### Extended-window re-test 2026-06-11 — MinTRL pre-check: INSUFFICIENT DATA

Gate-spec-v2 re-run work order (2026-06-11): same hypothesis + params,
regenerated substrate window (dev_end 2025-05-01). The units-correct
BLP eq.13 pre-check at target true annualised Sharpe 1.0 requires
~963 daily bars (~2.64y, actual dev-window B&H moments); the basket's
available dev intersection is 862 bars (2022-12-21 -> 2025-05-01,
2.36y), capped by BNB/USDT's OKX listing date 2022-12-21 — NOT by the
exchange archive, which reaches 2020-11 for the other basket members.
Verdict: insufficient data; the trial was NOT run (no CPCV, no
trials.log row — a skipped run is not a statistical draw and must not
inflate the family multiple-testing count). Recorded here and in
docs/bot_status.md. Unblocking requires either a longer wait or a
basket change (dropping/substituting BNB = pair substitution = human
decision per CLAUDE.md).

### Extended-window re-test 2026-06-11b — RUN after BNB cross-venue backfill

The earlier same-day MinTRL skip was unblocked by the BNB cross-venue
backfill (Binance 2021-01-01->2022-12-21 spliced ahead of OKX; seam max
close divergence 0.13%; see manifest notes + BNB-USDT_1d_66mo
provenance sidecar). Dev window now 2021-01-01 (2021-05-02 for
search-volume substrates) -> 2025-05-01. Same hypothesis + params, same
variation_id, "extended-window re-test under gate spec v2" marker in
the trials.log notes; no new variation slot.

Outcome: RETIRE on the family-scaled DSR floor (0.690 < 0.95) — alpha/IR baseline gate PASSES (NW alpha +0.88/yr p=0.0155, IR 0.512). Same shape as CrossSectionalMomentum: real but multiplicity-unclearable alpha on this window.
