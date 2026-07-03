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
| Standard (OKX taker 0.10% + slippage 0.05%) | -4.7333 | 1.1e-22 |
| 2x fee (0.20%) | -7.7332 | 1.5e-57 |

- Gross-of-fee extrapolation (2*sr_1x - sr_2x): -1.733
- n_trades (dev CPCV): 1576
- Buy-and-hold dev Sharpe: +0.49 (the strategy massively underperforms the
  passive long; dev spans the 2022 bear, so this is not a bull-window artifact).
- Not borderline (DSR << 0.95 threshold; outside the +/-0.05 borderline margin).
- trials.log: one full_cpcv row (trial_id af7ac71b1e3b4b92b497a2ac1762a881); per-bar return series persisted.

Interpretation: Negative gross (-1.73): shallow-sweep reversals fade moves that keep going; 1576 trades of churn + realistic cost compound the loss.
