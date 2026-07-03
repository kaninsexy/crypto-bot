# LiquiditySweepReversal -- literature stub

Strategy id: `LiquiditySweepReversal`
Substrate: Binance spot 1m OHLCV + taker delta (basket: BTCUSDT
primary; ETHUSDT/SOLUSDT/XRPUSDT/ADAUSDT available)
Signal timeframe: 15m (locked Variation #1 default)
Trial queue id: not yet queued (no trials this chunk -- Gate 5)
Status: PRE-REGISTRATION STUB. No trial has run. Mechanical spec
below is the locked Variation #1 hypothesis-of-record.

## Hypothesis-of-record

A bar that takes out a prior swing low by a small margin (a stop-hunt
below obvious resting liquidity) and then closes back inside the
prior range on opposing (buy-side) aggressor delta marks a failed
breakdown; price reverts upward. Long-only: only the swing-LOW sweep
(downside stop-hunt -> long) is tested. The swing-HIGH sweep -> short
mirror is dropped because `backtest.engine` is structurally long-only
on spot (peer precedent sq-013/016/018/019/020); the short leg is a
distinct hypothesis requiring a fresh written hypothesis (Gate 2).

## Mechanical spec (locked Variation #1)

All quantities backward-only (open-time strictly <= t).

- **Signal bars.** 1m resampled to 15m via `resample_ohlcv(df,
  "15min")`; per-bar `delta` from `taker_delta`.
- **Swing low.** `swing_low` = min of `low` over the prior L = 20
  completed 15m bars (EXCLUDING bar t).
- **ATR.** 14-bar Wilder ATR on 15m bars, through bar t-1.
- **ENTRY (BUY at 15m bar close, fills at close + slippage):** all
  true at bar t:
  1. `low[t]  < swing_low`                         (sweep occurred)
  2. `swing_low - low[t] <= 0.5 * ATR`             (shallow sweep, k=0.5)
  3. `close[t] > swing_low`                        (closed back inside)
  4. `delta[t] > 0`                                (opposing buy delta)
- **EXIT (SELL at 15m bar close):** first of:
  1. `close[t] >= entry_price + 2.0 * ATR_entry`   (target, m=2.0)
  2. `close[t] <  low_sweep - 0.10 * ATR_entry`    (stop below sweep low)
  3. time-stop: 16 completed 15m bars since entry
  where `ATR_entry` and `low_sweep` are frozen at entry.
- **Position.** One unit; no pyramiding; HOLD while open.

## Sources (reference, not authority -- per redesign proposal sec 1)

- Inner Circle Trader (ICT) practitioner material on liquidity
  sweeps / stop-hunts. Unfalsifiable-as-taught; mechanized here into
  the exact rule above so it becomes testable. Quality 1.
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
   False`. Only the swing-LOW sweep is traded; no short entries.
7. Sweep must be shallow (`<= 0.5 * ATR`) AND close back inside;
   a bar that sweeps and closes below `swing_low` is NOT an entry.
8. Signal timeframe 15m; L=20; k=0.5; m=2.0; stop 0.10*ATR; time-stop
   16 bars. Any change is a variation #2 requiring a fresh written
   hypothesis (Gate 2).


## Cost model (locked -- documented before any trial per Gate 1)

Entries and exits are market (taker) orders filled at the signal bar's
close, so the taker fee and market slippage apply. The trial runner
(scripts/phase4e_trial_common.py) runs the full headline + CPCV + DSR +
verdict evaluation under BOTH cost regimes; the edge must survive both or
the verdict is retire (Gate 1).

| Run | Taker fee (per side) | Market slippage (per side) | Source |
|-----|----------------------|----------------------------|--------|
| Standard | 0.0400% (`FEE_MARKET` = 0.0004) | 0.0500% (`SLIPPAGE_MARKET` = 0.0005) | `paper_trading.simulator` / `backtest.engine` |
| 2x fees | 0.0800% (`FEE_MARKET` x2 = 0.0008) | 0.0500% (unchanged) | Gate 1 "2x fees" |

Round-trip standard cost ~= 0.18% (2 x (0.04% + 0.05%)); round-trip 2x-fee
cost ~= 0.26%. Fees are `paper_trading.simulator` module globals read at
fill time, so the 2x run is applied by doubling them around the evaluation;
slippage is unchanged per the verbatim "2x fees" gate wording.
