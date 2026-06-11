# CrossSectionalMomentum -- literature stub

Strategy id: `CrossSectionalMomentum`
Substrate: 10-symbol crypto basket at 1D
(BTC, ETH, SOL, BNB, XRP, ADA, AVAX, DOT, LINK, LTC)
Trial queue id: sq-020

## Hypothesis-of-record

A long-only cross-sectional portfolio of cryptocurrencies that
performed best over the prior 30-day formation window (the "winner"
tail) generates positive risk-adjusted returns versus BTC
buy-and-hold across the subsequent 7-day holding period.  No short
positions are taken in losers because Han et al. (2024) document
that crypto losers tend to rebound and inflict significant losses
on shorts, making the long-only winner portfolio the dominant
configuration.

## Sources

- Drogen, L.; Hoffstein, C.; Otte, K. (2023). "Cross-sectional
  Momentum in Cryptocurrency Markets." SSRN. Key finding: a
  long-only strategy buying the top quintile of crypto assets
  ranked on 30-day prior returns and holding for 7 days
  consistently delivered excess returns relative to Bitcoin from
  2018-2022.
- Han, C.; Kang, B.; Ryu, J. (2024). "Time-Series and
  Cross-Sectional Momentum in the Cryptocurrency Market: A
  Comprehensive Analysis under Realistic Assumptions." SSRN. Key
  finding: the momentum effect in cryptocurrencies is concentrated
  among winners; losers often rebound and inflict significant
  losses on shorts, so a long-only winner portfolio dominates a
  traditional long-short portfolio.
- Borgards, O. (2021). "Dynamic time series momentum of
  cryptocurrencies." North American Journal of Economics and
  Finance. Key finding: a dynamic time-series momentum strategy
  significantly outperforms buy-and-hold for cryptocurrencies,
  yielding higher risk-adjusted returns and lower downside risk.

## Pre-trial gates (locked)

1. Long-only basket; equal weight across the held top-N winners.
2. Top-N rotation (default N=2 of 10 = top quintile).
3. 30-day prior-return lookback for the cross-sectional ranking.
4. 7-day holding period before re-ranking (weekly rebalance).
5. No short positions in losers; flat exposure outside the winners.
6. Baseline = BTC/USDT B&H over the same dev window.

## Variation table

| # | id | params | trial_type | sharpe | dsr | verdict | notes |
|---|----|--------|------------|--------|-----|---------|-------|
| 1 | cs-momentum-long-winners | lookback=30, top_n=2, hold=7 | full_cpcv | TBD | TBD | TBD | initial dev_cpcv trial |

## Trial outcomes

<!-- Trial outcomes will be appended here by the orchestrator -->

| variation_id | date | verdict | sr_observed | dsr | n_trades |
|---|---|---|---|---|---|
| cs-momentum-long-winners | 2026-05-07 | retire | 1.6423 | 1.0000 | 73 |
| cs-momentum-long-winners-gatev2-retest | 2026-06-11 | insufficient_data (pre-check; not run) | n/a | n/a | 0 |
| cs-momentum-long-winners (extended-window re-test) | 2026-06-11 | retire | 0.9443 | 0.7870 | 144 |

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

Outcome: RETIRE on the family-scaled DSR floor (0.787 < 0.95; cs-momentum family sr_zero 0.61) — but BOTH legs of the new directional baseline gate PASS for the first time: NW alpha +0.89/yr (p=0.0153) and IR 0.525 vs BTC B&H. The audit's COND-FLIP is half-confirmed: genuine alpha exists over the 2021-2025 window; it does not clear the multiplicity haircut.
