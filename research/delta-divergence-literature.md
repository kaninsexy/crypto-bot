# DeltaDivergence -- literature stub

Strategy id: `DeltaDivergence`
Substrate: Binance spot 1m taker delta + OHLCV (basket: BTCUSDT
primary; ETHUSDT/SOLUSDT/XRPUSDT/ADAUSDT available)
Signal timeframe: 15m (locked Variation #1 default)
Trial queue id: not yet queued (no trials this chunk -- Gate 5)
Status: PRE-REGISTRATION STUB. No trial has run. Mechanical spec
below is the locked Variation #1 hypothesis-of-record.

## Hypothesis-of-record

When price makes a new low but cumulative aggressor delta is higher
(less net selling) than at the prior comparable low, selling pressure
is exhausting even as price probes lower -- a bullish divergence that
precedes a reversion up. Long-only: only the bullish
new-low / rising-delta case (the "mirror for lows" in proposal sec 3)
is tested. The bearish new-high / falling-delta exhaustion-short is
dropped (`backtest.engine` is long-only spot; peer precedent
sq-013/016/018/019/020) and is a distinct hypothesis (Gate 2).

## Mechanical spec (locked Variation #1)

All quantities backward-only (open-time strictly <= t).

- **Signal bars.** 1m resampled to 15m; per-bar `delta` and daily-
  anchored `cum_delta` from `taker_delta` (cumulative delta reset at
  00:00 UTC each day so divergences are measured within one session).
  ATR = 14-bar Wilder ATR on 15m.
- **Prior pivot low.** Over the prior P = 20 completed 15m bars
  (EXCLUDING bar t), `piv_i` = index of the bar with the minimum
  `low`; `piv_low` = that bar's `low`; `piv_cd` = `cum_delta` at
  `piv_i`.
- **ENTRY (BUY at 15m bar close, fills at close + slippage):** all
  true at bar t:
  1. `low[t]      <  piv_low`      (new price low vs the pivot)
  2. `cum_delta[t] > piv_cd`       (delta divergence: less net selling)
  3. `close[t]    >  open[t]`      (bullish reversal candle)
- **EXIT (SELL at 15m bar close):** first of:
  1. `close[t] >= entry_price + 2.0 * ATR_entry`   (target, d=2.0)
  2. `close[t] <  low[t_entry] - 0.5 * ATR_entry`  (stop below the low)
  3. time-stop: 16 completed 15m bars since entry
  where `ATR_entry` and `low[t_entry]` are frozen at entry.
- **Position.** One unit; no pyramiding; HOLD while open.

## Sources (reference, not authority -- per redesign proposal sec 1)

- Cont, R.; Kukanov, A.; Stoikov, S. (2014). "The Price Impact of
  Order Book Events." *Journal of Financial Econometrics.*
  Order-flow imbalance leads price; the direct empirical anchor for
  a delta-vs-price divergence signal. Quality 4.
- Easley, D.; Lopez de Prado, M.; O'Hara, M. (2012). "Flow Toxicity
  and Liquidity in a High-frequency World." *Review of Financial
  Studies.* Aggressor-flow imbalance as an exhaustion/informed-flow
  proxy. Quality 4.

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
   False`. Only the bullish (new-low / rising-delta) divergence is
   traded.
7. `cum_delta` is anchored/reset at 00:00 UTC daily; divergences are
   measured within a single UTC session, never across the reset.
8. Signal timeframe 15m; P=20; d=2.0; stop 0.5*ATR; time-stop 16 bars.
   Any change is a variation #2 requiring a fresh written hypothesis
   (Gate 2).


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
