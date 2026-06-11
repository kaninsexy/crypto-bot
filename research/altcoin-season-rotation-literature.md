# AltcoinSeasonRotation -- Literature

<!-- Trial outcomes will be appended here by the orchestrator -->

## Trial outcomes

| variation_id | date | verdict | sr_observed | dsr | n_trades |
|---|---|---|---|---|---|
| altcoin-season-benchmark-rotation | 2026-05-08 | retire | 1.6767 | 1.0000 | 77 |
| altcoin-season-benchmark-rotation-gatev2-retest | 2026-06-11 | insufficient_data (pre-check; not run) | n/a | n/a | 0 |

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
