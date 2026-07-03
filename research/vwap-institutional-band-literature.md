# VWAPInstitutionalBand -- literature stub

Strategy id: `VWAPInstitutionalBand`
Substrate: Binance spot 1m OHLCV + taker delta (basket: BTCUSDT
primary; ETHUSDT/SOLUSDT/XRPUSDT/ADAUSDT available)
Signal timeframe: 15m (locked Variation #1 default)
Trial queue id: not yet queued (no trials this chunk -- Gate 5)
Status: PRE-REGISTRATION STUB. No trial has run. Mechanical spec
below is the locked Variation #1 hypothesis-of-record.

## Hypothesis-of-record

Session VWAP is an institutional execution anchor; price is pulled
back toward it. Variation #1 tests ONLY the mean-reversion-long side:
a stretched move to the lower 2-sigma VWAP band, confirmed by
buy-side aggressor delta, reverts toward VWAP. Long-only:
`backtest.engine` is long-only spot (peer precedent
sq-013/016/018/019/020). The continuation-long side (acceptance
BEYOND the upper band with delta confirmation, proposal sec 3) is a
SEPARATE, mutually-exclusive hypothesis and is deliberately NOT
tested in Variation #1 -- picking one side up front avoids the
discretionary "continuation or reversion?" read and keeps the trial
falsifiable. The continuation side is a variation #2 (Gate 2).

## Mechanical spec (locked Variation #1)

All quantities backward-only (open-time strictly <= t).

- **Signal bars.** 1m resampled to 15m; per-bar `delta`. ATR = 14-bar
  Wilder ATR on 15m.
- **VWAP + bands.** `session_vwap(df_15m, "1D")` (resets 00:00 UTC);
  `vwap_bands(df_15m, vwap, window=60)` giving `vwap`, `lower_1/2`,
  `upper_1/2` (sigma = rolling std of `close - vwap`, 60-bar window).
- **ENTRY (BUY at 15m bar close, fills at close + slippage):** all
  true at bar t:
  1. `close[t]   <= lower_2[t]`          (at/below the -2 sigma band)
  2. `close[t-1] >  lower_2[t-1]`        (fresh touch, not already there)
  3. `delta[t]   >  0`                   (buy-side confirmation)
- **EXIT (SELL at 15m bar close):** first of:
  1. `close[t] >= vwap[t]`                    (reverted to the anchor)
  2. `close[t] <  lower_2[t] - 0.5 * ATR_entry` (stop -- trend breaking
                                                  down through the band)
  3. time-stop: 12 completed 15m bars since entry
  where `ATR_entry` is frozen at entry; `vwap[t]`/`lower_2[t]` are
  evaluated live at bar t.
- **Position.** One unit; no pyramiding; HOLD while open.

## Sources (reference, not authority -- per redesign proposal sec 1)

- Berkowitz, S.A.; Logue, D.E.; Noser, E.A. (1988). "The Total Cost
  of Transactions on the NYSE." *Journal of Finance.* Origin of VWAP
  as the institutional execution benchmark price. Quality 3.
- Cont, R.; Kukanov, A.; Stoikov, S. (2014). "The Price Impact of
  Order Book Events." *Journal of Financial Econometrics.* Supports
  the aggressor-delta confirmation term. Quality 4.

No published crypto Sharpe is claimed; the mechanical spec is the
written hypothesis per the no-p-hacking rule.

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
   False`. Only the lower-band reversion-long is traded.
7. Variation #1 is the REVERSION side only. The continuation-beyond-
   upper-band side is NOT enabled in this file; enabling it is a
   variation #2 requiring a fresh written hypothesis.
8. Signal timeframe 15m; band window=60; -2 sigma entry; stop
   0.5*ATR; time-stop 12 bars; VWAP resets 00:00 UTC. Any change is a
   variation #2 (Gate 2).
