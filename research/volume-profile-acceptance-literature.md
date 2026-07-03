# VolumeProfileAcceptance -- literature stub

Strategy id: `VolumeProfileAcceptance`
Substrate: Binance spot 1m volume profile + taker delta (basket:
BTCUSDT primary; ETHUSDT/SOLUSDT/XRPUSDT/ADAUSDT available)
Signal timeframe: 1h (locked Variation #1 default)
Trial queue id: not yet queued (no trials this chunk -- Gate 5)
Status: PRE-REGISTRATION STUB. No trial has run. Mechanical spec
below is the locked Variation #1 hypothesis-of-record.

## Hypothesis-of-record

Price that is *accepted* above a prior value area -- i.e. closes and
holds above the prior N-day value-area high (VAH) on above-median
buy-side aggressor delta -- exhibits initiative buying at new prices
and continues upward. Acceptance (not just a wick through) is the
signal; rejection back into value is the invalidation. Long-only:
`backtest.engine` is structurally long-only on spot (BUY opens, SELL
closes), matching the peer precedent of sq-013/016/018/019/020. The
short/rejection mirror is a distinct hypothesis and is NOT tested
here (would require a fresh written hypothesis per Gate 2).

## Mechanical spec (locked Variation #1)

All quantities computed from bars with open-time strictly <= t
(backward-only; `data.microstructure` is peek-free by construction).

- **Profile window.** Volume profile built from Binance spot 1m bars
  over the prior N = 5 completed UTC days (rolling, EXCLUDING the
  current forming day), `volume_profile(n_bins=100)`.
- **Value area.** `value_area(profile, pct=0.70)`; VAH = `vah`.
- **Signal bars.** 1m resampled to 1h via `resample_ohlcv(df,"1h")`;
  per-bar `delta` from `taker_delta`.
- **Delta baseline.** `median_delta` = rolling median of per-bar
  `delta` over the last 30 completed 1h bars.
- **ENTRY (BUY at 1h bar close, fills at close + slippage):** all true
  at bar t:
  1. `close[t]  > VAH`
  2. `close[t-1] > VAH`   (two consecutive 1h closes above VAH)
  3. `delta[t]  > median_delta`
  4. `delta[t]  > 0`
- **EXIT (SELL at 1h bar close):** first of:
  1. `close[t] < VAH`                        (acceptance failed)
  2. time-stop: 24 completed 1h bars since entry
- **Position.** One unit; no pyramiding; HOLD while open; SELL never
  emitted unless `_in_position == True`.

## Sources (reference, not authority -- per redesign proposal sec 1)

- Steidlmayer, J.P.; Koy, K. (1986). *Markets and Market Logic.*
  Origin of Market Profile / value-area / acceptance-rejection.
  Practitioner framework, not an empirical Sharpe claim. Quality 2.
- Cont, R.; Kukanov, A.; Stoikov, S. (2014). "The Price Impact of
  Order Book Events." *Journal of Financial Econometrics.* Order-flow
  imbalance predicts short-horizon returns -- the empirical anchor
  for using taker delta as the confirmation term. Quality 4.

Direct peer-reviewed evidence for value-area acceptance in crypto is
thin; these support the *mechanism* (order-flow predictivity), not a
published crypto Sharpe. The written mechanical spec above is the
hypothesis per the no-p-hacking rule's written-hypothesis path.

## Pre-trial gates (locked)

Gates 1-5 are copied verbatim into every Phase 4.E literature file.

1. Every trial runs at standard taker fees + slippage AND at 2x fees;
   edge must survive both or the verdict is retire.
2. These 7 are the enumerated starting hypotheses; no variation #2
   without a fresh written hypothesis; 20-variation cap;
   3-consecutive-failure batch stop.
3. Substrate is Binance spot 1m (data/binance_vision.py); execution
   venue remains OKX; cross-venue provenance disclosure required in
   any verdict, per the 2026-06-11 BNB-backfill precedent.
4. Timeframe is per-strategy (expected default 15m-1h signal bars on
   1m profile data), never global.
5. NO trials run in this chunk. Manifest entries for the new substrate
   require a dev/holdout split decision that is human-only (see
   docs/project_diagnosis_2026-07-02.md section 4). trials.log is
   untouched.

Strategy-specific locked gates:

6. Long-only. Strategy MUST NEVER emit SELL when `_in_position ==
   False`. No short entries under any condition.
7. Profile window EXCLUDES the current forming UTC day (no
   same-day look-ahead into the value area being traded).
8. Signal timeframe 1h; N=5-day profile; pct=0.70; delta median
   lookback 30 bars; time-stop 24 bars. Any change to these is a
   variation #2 requiring a fresh written hypothesis (Gate 2).


## Cost model (locked -- documented before any trial per Gate 1)

Entries and exits are market (taker) orders filled at the signal bar's
close, so the taker fee and market slippage apply. The trial runner
(scripts/phase4e_trial_common.py) runs the full headline + CPCV + DSR +
verdict evaluation under BOTH cost regimes; the edge must survive both or
the verdict is retire (Gate 1).

| Run | Taker fee (per side) | Market slippage (per side) | Source |
|-----|----------------------|----------------------------|--------|
| Standard | 0.1000% (0.0010) | 0.0500% (`SLIPPAGE_MARKET` = 0.0005) | OKX spot regular-user taker, verified 2026-07-03 |
| 2x fees | 0.2000% (0.0020) | 0.0500% (unchanged) | Gate 1 "2x fees" stress |

Round-trip standard cost ~= 0.30% (2 x (0.10% + 0.05%)); round-trip 2x-fee
cost ~= 0.50%. The taker fee is OKX spot regular-user 0.10% (verified
2026-07-03); `paper_trading.simulator`'s module default (0.04%) understates
it 2.5x, so scripts/phase4e_trial_common.py overrides the base rate
explicitly to the OKX rate rather than mutating the simulator globals.
Slippage is unchanged across the two runs per the verbatim "2x fees" gate
wording.

## Outcome (Variation #1 -- dev CPCV, 2026-07-03)

**Verdict: RETIRE** -- fails the fee-realism gate (Gate 1); the edge survives
neither fee run and is wrong-signed / flat gross-of-fee.

| Fee run | Net dev Sharpe | DSR |
|---|---|---|
| Standard (OKX taker 0.10% + slippage 0.05%) | -0.8733 | 0.0363 |
| 2x fee (0.20%) | -1.7416 | 1.8e-04 |

- Gross-of-fee extrapolation (2*sr_1x - sr_2x): -0.005
- n_trades (dev CPCV): 607
- Buy-and-hold dev Sharpe: +0.49 (the strategy massively underperforms the
  passive long; dev spans the 2022 bear, so this is not a bull-window artifact).
- Not borderline (DSR << 0.95 threshold; outside the +/-0.05 borderline margin).
- trials.log: one full_cpcv row (trial_id fcbd3dc729a14ad19fc922f389fd19a6); per-bar return series persisted.

Interpretation: Flat gross expectancy (~0.00): acceptance above the value area shows no continuation edge on BTC 1h; cost drag then turns it negative.
