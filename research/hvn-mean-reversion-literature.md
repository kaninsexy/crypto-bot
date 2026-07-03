# HVNMeanReversion -- literature stub

Strategy id: `HVNMeanReversion`
Substrate: Binance spot 1m volume profile + taker delta (basket:
BTCUSDT primary; ETHUSDT/SOLUSDT/XRPUSDT/ADAUSDT available)
Signal timeframe: 1h (locked Variation #1 default)
Trial queue id: not yet queued (no trials this chunk -- Gate 5)
Status: PRE-REGISTRATION STUB. No trial has run. Mechanical spec
below is the locked Variation #1 hypothesis-of-record.

## Hypothesis-of-record

A high-volume node (HVN) is a price where large size was transacted;
positions are defended there. Price falling INTO an HVN from above
tends to decelerate and revert upward (the node acts as support /
order-block-adjacent demand). Long-only: only the buy-at-HVN-support
case is tested; the sell-at-HVN-resistance mirror is dropped
(`backtest.engine` is long-only spot; peer precedent
sq-013/016/018/019/020) and is a distinct hypothesis (Gate 2).

## Mechanical spec (locked Variation #1)

All quantities backward-only (open-time strictly <= t).

- **Profile window.** Volume profile from Binance spot 1m over the
  prior N = 5 completed UTC days (EXCLUDING the current forming day),
  `volume_profile(n_bins=100)`.
- **Nodes.** `find_nodes(profile,"hvn",smooth_bins=5,
  min_rel_prominence=0.25)`.
- **Support HVN.** `hvn_s` = highest HVN price <= `close[t-1]`
  (nearest HVN below prior close). Require it to exist; else no signal.
- **Signal bars.** 1m resampled to 1h; per-bar `delta`. ATR = 14-bar
  Wilder ATR on 1h.
- **ENTRY (BUY at 1h bar close, fills at close + slippage):** all true
  at bar t:
  1. `low[t]   <= hvn_s * (1 + 0.001)`     (bar touches HVN from above,
                                             tol = 0.1%)
  2. `close[t] >  hvn_s`                    (holds above the node)
  3. `delta[t] >  0`                        (buy-side defense)
- **EXIT (SELL at 1h bar close):** first of:
  1. `close[t] >= hvn_s + 1.5 * ATR_entry`  (reversion target, r=1.5)
  2. `close[t] <  hvn_s - 0.75 * ATR_entry` (stop -- HVN broke, s=0.75)
  3. time-stop: 24 completed 1h bars since entry
  where `hvn_s` and `ATR_entry` are frozen at entry.
- **Position.** One unit; no pyramiding; HOLD while open.

## Sources (reference, not authority -- per redesign proposal sec 1)

- Steidlmayer, J.P.; Koy, K. (1986). *Markets and Market Logic.*
  High-volume-node / value acceptance concept. Quality 2.
- Easley, D.; Lopez de Prado, M.; O'Hara, M. (2012). "Flow Toxicity
  and Liquidity in a High-frequency World." *Review of Financial
  Studies.* Supports the idea that price defends zones of heavy
  informed/uninformed transaction. Quality 4.

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
   False`. Only buy-at-HVN-support is traded.
7. Approach must be from ABOVE: entry requires `close[t] > hvn_s`
   with the bar's low touching the node; a bar closing below the HVN
   is a break, not a defense, and is NOT an entry.
8. Signal timeframe 1h; N=5-day profile; tol=0.1%; r=1.5; s=0.75;
   time-stop 24 bars. Any change is a variation #2 requiring a fresh
   written hypothesis (Gate 2).


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
