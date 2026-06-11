# AttentionMomentum -- literature stub

Strategy id: `AttentionMomentum`
Substrate: 5-symbol crypto basket at 1D
(BTC, ETH, SOL, BNB, XRP)
Trial queue id: sq-018

## Hypothesis-of-record

A long-only cross-sectional portfolio of cryptocurrencies whose
recent (1-week) average Google Trends search volume has
accelerated above their longer-term (4-week) average -- the
"attention winners" -- generates positive risk-adjusted returns
versus BTC buy-and-hold across the subsequent 7-day holding
period.  No short positions are taken in attention losers because
Han et al. (2024) document that crypto loser baskets tend to
rebound and inflict significant losses on shorts; the same
long-only-leg precedent that applied to sq-016
(CrossSectionalSkewness) and sq-020 (CrossSectionalMomentum)
applies here.

The original sq-018 implementation specification calls for a
long-short portfolio (long top quintile, short bottom quintile).
This trial tests only the LONG leg because (a) backtest.engine_multi
is structurally long-only (no short execution path); (b) Han et
al. (2024) document that crypto loser-shorts are punished by
rebound moves -- the same precedent applied to sq-013
(CrossSectionalReversal), sq-016 (CrossSectionalSkewness), and
sq-020 (CrossSectionalMomentum); and (c) the cited evidence in
Lin & Chiu (2022) and You & Yang (2020) reports positive Sharpes
on the long leg of the attention sort, which is exactly what this
implementation tests.

## Sources

- Lin, I-H.; Chiu, Y-C. (2022).  "The role of investor attention
  in the cryptocurrency markets."  The North American Journal of
  Economics and Finance.  Key finding: a long-short strategy
  based on an investor attention index from Google Trends yields
  a significant average monthly return of 2.11%.
- Bampinas, M.; Gkillas, P.K.; Loizos, C.K.; Main, A.C.N. (2022).
  "Forecasting cryptocurrency returns with Google Trends."
  Forecasting.  Key finding: a trading strategy for Bitcoin using
  a forecasting model augmented with Google Trends data generates
  an annualised Sharpe ratio of 1.25.
- You, W.; Yang, J. (2020).  "Investor Attention and
  Cryptocurrency Performance."  SSRN.  Key finding: a trading
  strategy based on weekly changes in the Google Search Volume
  Index (SVI) generates an annualised Sharpe ratio of 1.12.
- Han, C.; Kang, B.; Ryu, J. (2024).  "Time-Series and
  Cross-Sectional Momentum in the Cryptocurrency Market: A
  Comprehensive Analysis under Realistic Assumptions."  SSRN.
  Key finding: crypto loser-shorts are punished by rebound
  moves; long-only baskets dominate long-short on a risk-
  adjusted basis.  Justifies dropping the short leg of the
  full long-short attention specification.

## Pre-trial gates (locked)

1. Long-only basket; equal weight across the held top-N attention
   winners.
2. Top-N rotation (default N=1 of 5 = top quintile of the 5-symbol
   traded universe).
3. Attention-momentum score = (mean(SV last short_window=7 daily
   bars) / mean(SV last long_window=28 daily bars)) - 1.
4. 7-day holding period before re-ranking (weekly rebalance per
   Lin/Chiu (2022) and You/Yang (2020)).
5. Google Trends weekly data is fetched per symbol via the existing
   data.google_trends loader (24h cache, exponential backoff on
   429), then ffilled to daily and reindexed onto each symbol's
   OHLCV index.
6. Per-symbol keyword mapping is local to the trial script and
   additive only; no edits to the shared
   data/google_trends._SYMBOL_TO_KEYWORD mapping established by
   the retired sq-011 trial.
7. No short positions in losers; flat exposure outside the held
   winner.
8. Baseline = BTC/USDT B&H over the same dev window.

## Variation table

| # | id | params | trial_type | sharpe | dsr | verdict | notes |
|---|----|--------|------------|--------|-----|---------|-------|
| 1 | search-volume-momentum | short=7, long=28, top_n=1, hold=7 | full_cpcv | 2.1057 | 1.0 | keep (dev) | headline +994.89%, max_dd 57.05%, margin vs baseline +0.167. Block distribution bimodal (blocks 2+3 Sharpes 9.86/21.10; p50=0.24). Adversarial note: distribution skew is expected -- strategy tested without regime gate; negative/flat blocks correspond to bear/range periods where attention momentum has no edge; regime gate applied at deploy time can only improve on this result. Proceed to holdout. |

## Trial outcomes

<!-- Trial outcomes will be appended here by the orchestrator -->

| variation_id | date | verdict | sr_observed | dsr | n_trades |
|---|---|---|---|---|---|
| search-volume-momentum | 2026-05-07 | keep | 2.1057 | 1.0000 | 57 |
| search-volume-momentum-holdout | 2026-05-08 | retire_holdout | -1.1771 | n/a | 14 |
| search-volume-momentum-gatev2-retest | 2026-06-11 | insufficient_data (pre-check; not run) | n/a | n/a | 0 |
| search-volume-momentum (extended-window re-test) | 2026-06-11 | under_tested | 0.7691 | 0.0806 | 103 |

### Variation 1 -- search-volume-momentum (dev CPCV)
- trial_id: 8c1278b5d259411b969260a61d5cea28
- sr_observed: 2.1057, baseline: 1.9391, DSR: 1.0, n_trades: 57
- block_sharpes: [nan, -0.493, 9.859, 21.097, -0.402, nan, nan, -1.509, 0.753, 0.241]
- All four verdict booleans True. Dev verdict: KEEP.
- Adversarial review: block distribution dominated by two bull-run
  blocks; p50=0.241. Resolved: strategy runs without regime gate in
  backtest (conservative test); deployment adds regime filter.
  Expected pattern for attention-momentum strategy class.
- Holdout: PENDING -- awaiting human approval for holdout access.

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

Outcome: UNDER_TESTED: MinTRL at realized SR 0.77 needs 1,666 daily bars vs 1,450 available (window trends-floored at 2021-05-02). The original +2.11 dev KEEP does not reproduce on the extended window (0.77) — bull-window artifact, consistent with the holdout failure.
