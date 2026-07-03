# BreakoutDeltaConfirmed -- literature stub

Strategy id: `BreakoutDeltaConfirmed`
Substrate: Binance spot 1m OHLCV + taker delta (basket: BTCUSDT
primary; ETHUSDT/SOLUSDT/XRPUSDT/ADAUSDT available)
Signal timeframe: 1h (locked Variation #1 default)
Trial queue id: not yet queued (no trials this chunk -- Gate 5)
Status: PRE-REGISTRATION STUB. No trial has run. Mechanical spec
below is the locked Variation #1 hypothesis-of-record.

## Hypothesis-of-record

A range breakout is only tradeable when it is *accepted*: the
breakout bar closes beyond the range and shows top-quartile buy-side
aggressor delta (genuine initiative, not a thin-liquidity wick). This
is the acceptance-filtered redesign of the retired plain Breakout,
which failed because unfiltered breakouts are mostly noise. Long-only:
only upside breakouts are traded; `backtest.engine` is long-only spot
(peer precedent sq-013/016/018/019/020); the downside-breakdown short
is a distinct hypothesis (Gate 2).

## Mechanical spec (locked Variation #1)

All quantities backward-only (open-time strictly <= t).

- **Signal bars.** 1m resampled to 1h; per-bar `delta`. ATR = 14-bar
  Wilder ATR on 1h.
- **Range high.** `range_high` = max of `high` over the prior R = 24
  completed 1h bars (EXCLUDING bar t).
- **Delta quartile.** `delta_q75` = 75th percentile of per-bar `delta`
  over the prior W = 100 completed 1h bars (EXCLUDING bar t).
- **ENTRY (BUY at 1h bar close, fills at close + slippage):** all true
  at bar t:
  1. `close[t] >  range_high`          (closes beyond the range)
  2. `delta[t] >= delta_q75`           (top-quartile initiative)
  3. `delta[t] >  0`
- **EXIT (SELL at 1h bar close):** first of:
  1. `close[t] <  range_high`                          (fakeout, back
                                                        inside the range)
  2. `close[t] >= entry_price + 3.0 * ATR_entry`       (target, b=3.0)
  3. `close[t] <  max_close_since_entry - 2.0*ATR_entry` (chandelier
                                                         trail, a=2.0)
  4. time-stop: 48 completed 1h bars since entry
  where `range_high` (frozen at entry) and `ATR_entry` are fixed;
  `max_close_since_entry` updates each bar while in position.
- **Position.** One unit; no pyramiding; HOLD while open.

## Sources (reference, not authority -- per redesign proposal sec 1)

- Cont, R.; Kukanov, A.; Stoikov, S. (2014). "The Price Impact of
  Order Book Events." *Journal of Financial Econometrics.*
  Order-flow imbalance predicts continuation -- the empirical anchor
  for gating breakouts on delta rather than price alone. Quality 4.
- Easley, D.; Lopez de Prado, M.; O'Hara, M. (2012). "Flow Toxicity
  and Liquidity in a High-frequency World." *Review of Financial
  Studies.* Distinguishes informed (accepted) from uninformed
  (fake) flow. Quality 4.

No published crypto Sharpe is claimed; the mechanical spec is the
written hypothesis per the no-p-hacking rule. This strategy is the
delta-gated redesign of the retired unfiltered Breakout.

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
   False`. Only upside breakouts are traded.
7. The delta filter is mandatory: a breakout bar with `delta[t] <
   delta_q75` is NOT an entry, regardless of how far price closes
   beyond `range_high`. This is the whole point of the redesign.
8. Signal timeframe 1h; R=24; W=100; q75 threshold; b=3.0; trail
   a=2.0; time-stop 48 bars. Any change is a variation #2 requiring a
   fresh written hypothesis (Gate 2).


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

## Outcome (Variation #1)

**PRE-REGISTERED, NOT RUN.** The Phase 4.E batch stopped on the
3-consecutive-failure escalation (VolumeProfileAcceptance,
LiquiditySweepReversal, LVNTraversal all retired) and was closed for this
hypothesis by human decision 2026-07-03. Gross-of-fee extrapolation on the run
strategies showed two of three displacement strategies wrong-signed BEFORE fees
(VPA ~0.00, LSR -1.73, LVN -4.07), and VWAPInstitutionalBand (run as the one
distinct mean-reversion signal) was also wrong-signed gross (-1.35): the shared
long-only ICT-displacement / profile-delta-confirmation signal vocabulary shows
no positive gross expectancy on BTC spot at 15m-1h. This hypothesis shares that
vocabulary and is skipped on that evidence. NO trials.log row was written -- a
skip is not a statistical draw and must not inflate the family multiple-testing
count (2026-06-11 precedent).
