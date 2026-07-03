# LVNTraversal -- literature stub

Strategy id: `LVNTraversal`
Substrate: Binance spot 1m volume profile + taker delta (basket:
BTCUSDT primary; ETHUSDT/SOLUSDT/XRPUSDT/ADAUSDT available)
Signal timeframe: 15m (locked Variation #1 default)
Trial queue id: not yet queued (no trials this chunk -- Gate 5)
Status: PRE-REGISTRATION STUB. No trial has run. Mechanical spec
below is the locked Variation #1 hypothesis-of-record.

## Hypothesis-of-record

A low-volume node (LVN) is a thin price zone between two high-volume
nodes (HVNs): little size was transacted there, so books are thin and
price traverses it quickly rather than settling. When price enters an
LVN from below on positive aggressor delta, it tends to travel to the
next HVN above rather than stall. Long-only: only upward traversal is
tested (downside traversal -> short mirror is dropped;
`backtest.engine` is long-only spot; peer precedent
sq-013/016/018/019/020). FVGs are treated as LVN markers per the
proposal, but the mechanized signal is the LVN itself (from
`find_nodes`), not a discretionary FVG read.

## Mechanical spec (locked Variation #1)

All quantities backward-only (open-time strictly <= t).

- **Profile window.** Volume profile from Binance spot 1m over the
  prior N = 5 completed UTC days (EXCLUDING the current forming day),
  `volume_profile(n_bins=100)`.
- **Nodes.** `find_nodes(profile,"hvn",smooth_bins=5,
  min_rel_prominence=0.25)` and `find_nodes(profile,"lvn",
  smooth_bins=5,min_rel_prominence=0.25)`.
- **Target LVN.** `lvn_p` = lowest LVN price strictly above
  `close[t-1]`; `hvn_up` = lowest HVN price strictly above `lvn_p`
  (the acceptance zone the traverse targets); `hvn_dn` = highest HVN
  price strictly below `lvn_p`. Require both `hvn_up` and `hvn_dn` to
  exist; else no signal.
- **Signal bars.** 1m resampled to 15m; per-bar `delta`. ATR = 14-bar
  Wilder ATR on 15m.
- **ENTRY (BUY at 15m bar close, fills at close + slippage):** all
  true at bar t:
  1. `close[t-1] <  lvn_p`                (price was below the LVN)
  2. `close[t]  >= lvn_p`                 (price enters the LVN)
  3. `close[t]  <  hvn_up`                (not already at the target)
  4. `delta[t]  >  0`                     (buy-side traverse)
- **EXIT (SELL at 15m bar close):** first of:
  1. `close[t] >= hvn_up`                 (reached next acceptance zone)
  2. `close[t] <  lvn_p - 0.25 * ATR_entry`  (rejected back below LVN)
  3. time-stop: 8 completed 15m bars since entry (thin zones
     traverse fast -> short holding)
  where `hvn_up`, `lvn_p`, `ATR_entry` are frozen at entry.
- **Position.** One unit; no pyramiding; HOLD while open.

## Sources (reference, not authority -- per redesign proposal sec 1)

- Steidlmayer, J.P.; Koy, K. (1986). *Markets and Market Logic.*
  Low-volume-node / single-print thin-zone concept. Quality 2.
- ICT / practitioner FVG (fair-value-gap) material, mechanized here
  into the LVN rule so it becomes falsifiable. Quality 1.

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
   False`. Only upward LVN traversal is traded.
7. Entry requires BOTH a flanking HVN below (`hvn_dn`) and a target
   HVN above (`hvn_up`) to exist; an LVN with no upper acceptance
   target is not a tradeable traverse.
8. Signal timeframe 15m; N=5-day profile; smooth_bins=5;
   min_rel_prominence=0.25; time-stop 8 bars; stop 0.25*ATR. Any
   change is a variation #2 requiring a fresh written hypothesis
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
