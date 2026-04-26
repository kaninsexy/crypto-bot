"""scripts/bearshort_pathfinder.py — Path-finder dev backtest for BearShort.

Runs BearShort end-to-end through the production PaperTrading simulator with
REGIME_ALLOCATIONS-driven gating, on the dev window only. Holdout is sealed:
no row at or after holdout_start is fed to the simulator; backtest/holdout.py
is touched only via load_manifest() (read-only); backtest/holdout_access.log
mtime is asserted unchanged at end-of-run.

Architecture (single-strategy, faithful gating):
  for each candle i in [warmup, end_of_dev):
      regime  = RegimeDetector().detect(df.iloc[i-W:i+1]).regime
      bucket_weight = REGIME_ALLOCATIONS[regime]["bearshort"]
      signal  = BearShortStrategy.generate_signal(df.iloc[:i+1])
      if signal.action == "BUY" and bucket_weight == 0.0:
          # Production gating: BUY blocked at allocation layer
          signal -> HOLD (block fresh entries in non-BEAR/non-CRASH regimes)
      sim.execute_signal(signal, fill_price)
      sim.tick_ohlcv_candle(H, L, C)            # SL / TP / trail
      sync_state on BearShort if SL/TP closed externally

This mirrors the relevant slice of PortfolioManager.run_candle for a single
strategy:
  - per-candle regime detection (RegimeDetector)
  - REGIME_ALLOCATIONS["BearShort" bucket] == 0.0 → SUSPENDED (BUY blocked)
  - SELL/HOLD/exit-tick logic always allowed (matches manager.py:813-887)
  - sync_state called after tick (matches manager.py:559-560)

We do NOT reimplement the gating rule — it reads REGIME_ALLOCATIONS directly
from portfolio.regime_detector. Kelly sizing, circuit-breaker, daily-loss
guard, correlation cap, and rebalancer are intentionally not active: with a
single strategy, no other strategy to size against, no portfolio drawdown
to police, and no other slot to balance against, those layers are ineffective
or inapplicable. The simulator's default risk-fraction sizing is used instead
(MAX_RISK_PER_TRADE = 0.02 of current balance per trade).

Hard constraints:
  - holdout sealed (no row >= holdout_start fed to engine)
  - holdout_access.log mtime asserted unchanged
  - paper_mode is True (the simulator has no live path)
  - no network calls (cache loaded directly from parquet)
  - read-only on strategies/, backtest/, portfolio/, docs/

Usage:
    python scripts/bearshort_pathfinder.py
"""

from __future__ import annotations

import math
import re
import sys
import time
from dataclasses import asdict
from pathlib import Path
from statistics import mean, median, stdev

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from loguru import logger  # noqa: E402
logger.remove()  # silence strategy + sim INFO noise

from strategies.bear_short import BearShortStrategy  # noqa: E402
from strategies.base import Signal  # noqa: E402
from paper_trading.simulator import PaperTrading  # noqa: E402
from portfolio.regime_detector import (  # noqa: E402
    RegimeDetector,
    REGIME_ALLOCATIONS,
)
from backtest.holdout import load_manifest  # noqa: E402  (read-only)
from backtest.engine import SLIPPAGE_MARKET, SLIPPAGE_LIMIT  # noqa: E402

# ── Constants ────────────────────────────────────────────────────────────────
SYMBOL          = "BTC/USDT"
TIMEFRAME       = "1h"
WARMUP          = 1000  # bars; > regime_detector.ema_slow + 10 and BearShort min_rows
INITIAL_CAPITAL = 100_000.0
BUCKET_KEY      = "bearshort"  # matches PortfolioManager.BUCKET_KEYS
CACHE_DIR       = PROJECT_ROOT / "backtest" / "cache" / "ohlcv"
LOG_DIR         = PROJECT_ROOT / "logs"
HOLDOUT_LOG     = PROJECT_ROOT / "backtest" / "holdout_access.log"
TRADES_CSV      = LOG_DIR / "bearshort_pathfinder_trades.csv"
REPORT_MD       = LOG_DIR / "bearshort_pathfinder_dev.md"
PROGRESS_EVERY  = 2000

CANDLES_PER_YEAR = 365.25 * 24  # hourly bars

# Pre-committed adequacy gate (rough MinTRL).
GATED_TRADE_GATE = 30


# ── Cache loader (mirror of backtest.holdout._load_symbol_df) ────────────────

_MONTHS_RE = re.compile(r"_(\d+)mo\.parquet$")


def _parse_months(path: Path) -> int:
    m = _MONTHS_RE.search(path.name)
    if m is None:
        raise ValueError(f"Cache file '{path.name}' has no _{{N}}mo suffix.")
    return int(m.group(1))


def load_btc_cache() -> pd.DataFrame:
    prefix = SYMBOL.replace("/", "-")
    candidates = list(CACHE_DIR.glob(f"{prefix}_{TIMEFRAME}_*.parquet"))
    if not candidates:
        raise FileNotFoundError(f"No cache file for {SYMBOL} {TIMEFRAME} in {CACHE_DIR}")
    best = max(candidates, key=_parse_months)
    df = pd.read_parquet(best)
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    return df


def _apply_slippage(action: str, order_type: str, price: float) -> float:
    if action == "HOLD":
        return price
    slip = SLIPPAGE_MARKET if order_type == "market" else SLIPPAGE_LIMIT
    if action == "BUY":
        return price * (1.0 + slip)
    return price * (1.0 - slip)


def _classify_exit(exit_reason: str) -> str:
    """Bucket the simulator's exit_reason string into one of:
       stop_loss | take_profit | trailing_tp | time_exit |
       supertrend_flip | rsi_exit | end_of_window | other.
    """
    if exit_reason is None:
        return "other"
    r = exit_reason.lower()
    if r == "stop_loss" or r.startswith("stop_loss"):
        return "stop_loss"
    if r == "take_profit" or r.startswith("take_profit"):
        return "take_profit"
    if r == "trailing_tp" or r.startswith("trailing_tp"):
        return "trailing_tp"
    if r == "time_exit":
        return "time_exit"
    if r == "backtest_end" or "end of period" in r:
        return "end_of_window"
    if "supertrend flipped bullish" in r:
        return "supertrend_flip"
    if r.startswith("rsi=") or "shorts covering" in r:
        return "rsi_exit"
    return "other"


def main() -> int:
    t0 = time.perf_counter()

    # ── 0. Holdout-access log mtime baseline ─────────────────────────────────
    holdout_log_existed = HOLDOUT_LOG.exists()
    holdout_log_mtime_before = HOLDOUT_LOG.stat().st_mtime_ns if holdout_log_existed else None

    # ── 1. Resolve dev window from manifest (read-only) ──────────────────────
    manifest = load_manifest()
    if "BearShort" not in manifest:
        print("ERROR: 'BearShort' missing from holdout manifest.", file=sys.stderr)
        return 1
    entry = manifest["BearShort"]
    data_start    = pd.Timestamp(entry["data_start"])
    dev_end       = pd.Timestamp(entry["dev_end"])
    holdout_start = pd.Timestamp(entry["holdout_start"])
    if data_start.tz    is None: data_start    = data_start.tz_localize("UTC")
    if dev_end.tz       is None: dev_end       = dev_end.tz_localize("UTC")
    if holdout_start.tz is None: holdout_start = holdout_start.tz_localize("UTC")

    if dev_end != holdout_start:
        # Manifest invariant: dev_end and holdout_start are the same instant.
        # If they ever drift, fail loud rather than feed holdout bars.
        print(
            f"ERROR: manifest dev_end ({dev_end}) != holdout_start ({holdout_start}). "
            f"Refusing to proceed; needs human inspection.",
            file=sys.stderr,
        )
        return 1

    # ── 2. Load cache, truncate at dev_end ───────────────────────────────────
    df_full = load_btc_cache()
    df_dev  = df_full[df_full.index < holdout_start]
    n_total = len(df_dev)
    if n_total <= WARMUP:
        print(f"ERROR: only {n_total} dev bars; need > WARMUP={WARMUP}.", file=sys.stderr)
        return 1

    # Defensive: prove truncation is correct.
    assert df_dev.index.max() < holdout_start, "Dev frame leaked a holdout bar."
    print(
        f"Loaded {SYMBOL} {TIMEFRAME} dev cache: {n_total} bars from "
        f"{df_dev.index[0].strftime('%Y-%m-%d %H:%M')} to "
        f"{df_dev.index[-1].strftime('%Y-%m-%d %H:%M')}"
    )
    print(f"Holdout boundary: {holdout_start.isoformat()} (NOT crossed)")
    print(f"Skipped first {WARMUP} bars (warmup), labeling {n_total - WARMUP}")
    print()

    # ── 3. Wire up strategy + simulator + regime detector ────────────────────
    strategy = BearShortStrategy(symbol=SYMBOL, timeframe=TIMEFRAME)
    detector = RegimeDetector()
    sim      = PaperTrading(initial_balance=INITIAL_CAPITAL, symbol=SYMBOL)

    # ── 4. Per-candle loop (mirror of BacktestEngine.run minus dual-symbol) ──
    equity_curve: dict[pd.Timestamp, float] = {}
    bar_records:  list[dict]                = []
    suspended_buys = 0

    # When the simulator transitions None → Position, capture entry context.
    pending_entry: dict | None = None
    # Aligned with sim.trade_history: same length, same order.
    trade_meta: list[dict] = []
    # Sentinel for SL/TP detection: track trade_history length + position state.
    prev_position_open = False
    prev_trade_count   = 0

    for i in range(WARMUP, n_total):
        df_slice = df_dev.iloc[: i + 1]
        ts       = df_dev.index[i]

        # Regime
        regime = detector.detect(df_slice).regime
        bucket_weight = REGIME_ALLOCATIONS[regime].get(BUCKET_KEY, 0.0)

        # Signal
        try:
            signal = strategy.generate_signal(df_slice)
        except ValueError:
            equity_curve[ts] = sim.get_equity(float(df_slice["close"].iloc[-1]))
            continue

        # Allocation gating: BUY blocked when bucket_weight == 0.
        # Mirrors PortfolioManager.run_candle:671-676 ("SUSPENDED").
        if signal.action == "BUY" and bucket_weight == 0.0:
            suspended_buys += 1
            signal = Signal(
                action="HOLD",
                strategy=signal.strategy,
                price=signal.price,
                reason=f"SUSPENDED ({regime}, bearshort=0.0%)",
            )

        # Fill price (default fill at signal candle close + slippage)
        active_close = float(df_slice["close"].iloc[-1])
        active_high  = float(df_slice["high"].iloc[-1])
        active_low   = float(df_slice["low"].iloc[-1])
        fill_price = _apply_slippage(signal.action, signal.order_type, active_close)

        if signal.action != "HOLD":
            sim.execute_signal(signal, fill_price)

        # Detect ENTRY just opened by the signal-side execute_signal.
        # (sim.position transitions None → Position on a fresh BUY.)
        position_now_open = sim.position is not None
        if not prev_position_open and position_now_open:
            pending_entry = {
                "entry_ts":     ts,
                "entry_regime": regime,
                "entry_price":  fill_price,
            }
        prev_position_open = position_now_open

        # OHLCV-accurate SL / TP / trail / time-exit tick.
        sim.tick_ohlcv_candle(high=active_high, low=active_low, close=active_close)

        # If the tick closed the position OR a SELL signal closed it just now,
        # the new TradeRecord is at sim.trade_history[-1]. Capture exit context
        # and pair it with pending_entry.
        if len(sim.trade_history) > prev_trade_count:
            # We may have advanced by 1 record (full sell or SL/TP/trail/time)
            # or by 0; partial sells don't fire here for BearShort.
            new_records = sim.trade_history[prev_trade_count:]
            for rec in new_records:
                if pending_entry is None:
                    # Should not happen — log and skip.
                    print(f"  WARN: trade closed at {ts} with no pending_entry "
                          f"(reason={rec.exit_reason}).", file=sys.stderr)
                    trade_meta.append({"entry_ts": None, "entry_regime": None,
                                       "exit_ts": ts, "exit_regime": regime})
                else:
                    trade_meta.append({
                        **pending_entry,
                        "exit_ts":     ts,
                        "exit_regime": regime,
                    })
                    pending_entry = None
            prev_trade_count = len(sim.trade_history)
            # If trades closed via tick, position is now None.
            prev_position_open = sim.position is not None

        # Strategy state sync after tick (mirrors manager.py:559-560)
        strategy.sync_state(simulator_has_position=sim.position is not None)

        # Record equity
        equity_curve[ts] = sim.get_equity(active_close)

        bar_records.append({
            "ts":     ts,
            "regime": regime,
            "bucket_weight": bucket_weight,
        })

        labeled_so_far = i - WARMUP
        if labeled_so_far > 0 and labeled_so_far % PROGRESS_EVERY == 0:
            elapsed = time.perf_counter() - t0
            total   = n_total - WARMUP
            print(
                f"  ...labeled {labeled_so_far}/{total} bars "
                f"({labeled_so_far/total:.0%}) in {elapsed:.1f}s",
                file=sys.stderr,
            )

    # Force-close any open position at the last dev candle (matches engine).
    if sim.position is not None:
        last_price = float(df_dev["close"].iloc[-1])
        last_ts    = df_dev.index[-1]
        sim._handle_full_sell(None, last_price, "backtest_end", order_type="market")
        equity_curve[last_ts] = sim.get_equity(last_price)
        if pending_entry is not None and len(sim.trade_history) > prev_trade_count:
            trade_meta.append({
                **pending_entry,
                "exit_ts":     last_ts,
                "exit_regime": bar_records[-1]["regime"] if bar_records else None,
            })
            prev_trade_count = len(sim.trade_history)

    # ── 5. Holdout-access log integrity check ────────────────────────────────
    if holdout_log_existed:
        mtime_after = HOLDOUT_LOG.stat().st_mtime_ns
        if mtime_after != holdout_log_mtime_before:
            print(
                f"FATAL: holdout_access.log mtime changed during run! "
                f"Before={holdout_log_mtime_before} After={mtime_after}",
                file=sys.stderr,
            )
            return 2

    # ── 6. Build trades CSV + aggregates ─────────────────────────────────────
    n_records = len(sim.trade_history)
    n_meta    = len(trade_meta)
    if n_records != n_meta:
        print(
            f"WARN: trade_history ({n_records}) and trade_meta ({n_meta}) "
            f"length mismatch. Pairing may be off.",
            file=sys.stderr,
        )

    rows = []
    hold_bars_list: list[int] = []
    pnl_list:       list[float] = []
    fees_total = 0.0
    exit_buckets: dict[str, int] = {}
    bear_entries = crash_entries = other_regime_entries = 0

    bar_ts_to_idx = {rec["ts"]: idx for idx, rec in enumerate(bar_records)}

    for rec, meta in zip(sim.trade_history, trade_meta):
        entry_ts     = meta.get("entry_ts")
        exit_ts      = meta.get("exit_ts")
        entry_regime = meta.get("entry_regime")
        exit_regime  = meta.get("exit_regime")
        # Hold period in bars (engine candle count).
        if entry_ts is not None and exit_ts is not None and entry_ts in bar_ts_to_idx and exit_ts in bar_ts_to_idx:
            hold_bars = bar_ts_to_idx[exit_ts] - bar_ts_to_idx[entry_ts]
        else:
            hold_bars = -1
        hold_bars_list.append(hold_bars if hold_bars >= 0 else 0)

        bucket = _classify_exit(rec.exit_reason)
        exit_buckets[bucket] = exit_buckets.get(bucket, 0) + 1

        pnl_list.append(rec.pnl)
        fees_total += rec.fees_paid

        if entry_regime == "BEAR":
            bear_entries += 1
        elif entry_regime == "CRASH":
            crash_entries += 1
        elif entry_regime is not None:
            other_regime_entries += 1

        rows.append({
            "entry_ts":     entry_ts.isoformat() if entry_ts is not None else "",
            "exit_ts":      exit_ts.isoformat()  if exit_ts  is not None else "",
            "entry_regime": entry_regime or "",
            "exit_regime":  exit_regime  or "",
            "entry_price":  round(rec.entry_price, 4),
            "exit_price":   round(rec.exit_price, 4),
            "exit_reason":  rec.exit_reason,
            "exit_bucket":  bucket,
            "pnl_gross":    round(rec.pnl + rec.fees_paid, 4),
            "pnl_net":      round(rec.pnl, 4),
            "fees":         round(rec.fees_paid, 4),
            "hold_bars":    hold_bars,
            "side":         rec.side,
            "quantity":     round(rec.quantity, 8),
        })

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(TRADES_CSV, index=False)

    # ── 7. Compute metrics (Sharpe etc.) ─────────────────────────────────────
    eq_series = pd.Series(equity_curve).sort_index()
    if len(eq_series) >= 2:
        rets = eq_series.pct_change().dropna()
        ann_vol = float(rets.std() * math.sqrt(CANDLES_PER_YEAR))
        years = (eq_series.index[-1] - eq_series.index[0]).total_seconds() / (365.25 * 24 * 3600)
        end_eq = float(eq_series.iloc[-1])
        ann_return = ((end_eq / INITIAL_CAPITAL) ** (1.0 / years) - 1.0) if years > 0 else 0.0
        sharpe = ann_return / ann_vol if ann_vol > 0 else 0.0
        total_return = (end_eq - INITIAL_CAPITAL) / INITIAL_CAPITAL
    else:
        ann_vol = ann_return = sharpe = total_return = 0.0
        end_eq = INITIAL_CAPITAL

    # Hold-period stats (only bars actually computable; -1 placeholders excluded)
    valid_holds = [h for h in hold_bars_list if h > 0]
    if valid_holds:
        hold_mean   = mean(valid_holds)
        hold_median = median(valid_holds)
        hold_p25    = float(pd.Series(valid_holds).quantile(0.25))
        hold_p75    = float(pd.Series(valid_holds).quantile(0.75))
        hold_max    = max(valid_holds)
    else:
        hold_mean = hold_median = hold_p25 = hold_p75 = hold_max = 0

    if pnl_list:
        pnl_gross_total = sum(p + 0 for p in pnl_list)  # pnl is net; gross = net + fees
        pnl_gross_total = sum(rows[k]["pnl_gross"] for k in range(len(rows)))
        pnl_net_total   = sum(pnl_list)
        pnl_mean        = mean(pnl_list)
        pnl_stdev       = stdev(pnl_list) if len(pnl_list) > 1 else 0.0
    else:
        pnl_gross_total = pnl_net_total = pnl_mean = pnl_stdev = 0.0

    # ── 8. Headline verdict ─────────────────────────────────────────────────
    n_trades = len(sim.trade_history)
    if n_trades >= GATED_TRADE_GATE:
        verdict = (
            f"DEV ADEQUATE; holdout likely adequate "
            f"(extrapolation: dev has 3608 BEAR hours, holdout has 1432 — so "
            f"holdout count should be ≈40% of dev = ~{int(n_trades * 0.40)} trades)"
        )
    else:
        verdict = (
            "DEV UNDER-POWERED; strategy-layer intervention needed before any "
            "holdout work."
        )

    # ── 9. Write report ──────────────────────────────────────────────────────
    runtime_s = time.perf_counter() - t0
    report = []
    report.append("# BearShort path-finder — dev-window backtest\n")
    report.append(f"Date: 2026-04-26  \nScript: `scripts/bearshort_pathfinder.py`  \n"
                  f"Runtime: {runtime_s:.1f}s  \n"
                  f"Trades CSV: `logs/bearshort_pathfinder_trades.csv`  \n"
                  f"Holdout: SEALED (no row >= {holdout_start.isoformat()})  \n"
                  f"`backtest/holdout_access.log` mtime: UNCHANGED  \n")
    report.append("---\n")
    report.append("## 1. Trade count\n")
    report.append(f"- Total trades opened in dev window: **{n_trades}**")
    report.append(f"- Trades opened during BEAR regime: **{bear_entries}**")
    report.append(f"- Trades opened during CRASH regime: **{crash_entries}** (expected 0; CRASH=0 hours in cache)")
    report.append(f"- Trades opened during any other regime: **{other_regime_entries}** "
                  f"(expected 0 if gating works)")
    report.append(f"- BUY signals suspended by allocation gate: **{suspended_buys}**\n")
    if other_regime_entries > 0:
        report.append("> ⚠️ Non-zero entries outside BEAR/CRASH — allocation gating "
                      "may be circumvented somewhere. Inspect the trades CSV for "
                      "rows with `entry_regime` not in {BEAR, CRASH}.\n")

    report.append("## 2. Hold-period distribution (bars)\n")
    report.append(f"- mean:   **{hold_mean:.1f}**")
    report.append(f"- median: **{hold_median:.0f}**")
    report.append(f"- p25:    **{hold_p25:.0f}**")
    report.append(f"- p75:    **{hold_p75:.0f}**")
    report.append(f"- max:    **{hold_max}**\n")
    report.append("Compare to dry-sim mean of 41.7 bars (no SL/TP). The gap is the "
                  "size of the SL/TP shortening effect.\n")

    report.append("## 3. Realized PnL (USD)\n")
    report.append(f"- Gross PnL (before fees): **${pnl_gross_total:+,.2f}**")
    report.append(f"- Net PnL  (after  fees): **${pnl_net_total:+,.2f}**")
    report.append(f"- Total fees paid:         **${fees_total:,.2f}**")
    report.append(f"- Per-trade mean (net):    **${pnl_mean:+,.2f}**")
    report.append(f"- Per-trade stdev (net):   **${pnl_stdev:,.2f}**")
    report.append(f"- Final equity:            **${end_eq:,.2f}** "
                  f"(start ${INITIAL_CAPITAL:,.0f}, return {total_return*100:+.2f}%)\n")

    report.append("## 4. Sharpe ratio\n")
    report.append(f"- Annualised return:     **{ann_return*100:+.2f}%**")
    report.append(f"- Annualised volatility: **{ann_vol*100:.2f}%**")
    report.append(f"- Sharpe (rf=0):         **{sharpe:.3f}**\n")
    report.append("Reference: prior dev_cpcv reported +1.31 (raw signal, no allocation "
                  "gating). This run is gated to BEAR/CRASH only and uses the production "
                  "simulator with SL/TP. The two are not directly comparable; the "
                  "magnitude and sign of the gap is itself informative.\n")

    report.append("## 5. Exit attribution\n")
    if n_trades > 0:
        for bucket in ("stop_loss", "take_profit", "trailing_tp", "time_exit",
                       "supertrend_flip", "rsi_exit", "end_of_window", "other"):
            count = exit_buckets.get(bucket, 0)
            pct = (count / n_trades * 100) if n_trades > 0 else 0.0
            report.append(f"- {bucket}: **{count}** ({pct:.1f}%)")
    else:
        report.append("(No closed trades.)")
    report.append("")

    report.append("## 6. Headline verdict\n")
    report.append(f"Pre-committed gate: gated dev trades ≥ 30.  ")
    report.append(f"This run produced **{n_trades}** trades.\n")
    report.append(f"**Verdict: {verdict}**\n")

    report.append("## 7. Anomalies / observations\n")
    report.append("Items observed during the run:\n")
    if other_regime_entries > 0:
        report.append(f"- ⚠️ {other_regime_entries} trades opened outside BEAR/CRASH. Inspect CSV.")
    if n_records != n_meta:
        report.append(f"- trade_history ({n_records}) vs trade_meta ({n_meta}) length mismatch — "
                      "candle-timestamp pairing may be one-off on a few rows.")
    if any(h < 0 for h in hold_bars_list):
        report.append(f"- {sum(1 for h in hold_bars_list if h < 0)} trade rows had no "
                      "candle-resolved hold period (paired-meta missing). Reflected as 0 in stats.")
    report.append("- Position sizing: simulator default risk-fraction sizing "
                  "(`MAX_RISK_PER_TRADE = 0.02` × current balance per trade). "
                  "PortfolioManager would apply Kelly here; for a single-strategy "
                  "diagnostic the Kelly profile would be self-referential, so the "
                  "default 2%-of-balance sizing is used. PnL absolute values are "
                  "therefore conservative vs. the live-bot allocation.")
    report.append("- Regime detection: full per-candle walk-forward with the same "
                  "1000-bar warmup as the regime-distribution audit; detector "
                  "instance is reused so 3-candle hysteresis evolves continuously.")
    report.append(f"- BUY-suspended count ({suspended_buys}) is the count of bar-events "
                  "where BearShort wanted to enter but the regime allocation was 0%. "
                  "This is the production gate at work.")
    report.append("")
    report.append("---\n")
    report.append(f"Constraints honoured: holdout untouched (`holdout_access.log` "
                  f"mtime unchanged), no edits to `strategies/`, `backtest/`, "
                  f"`portfolio/`, or `docs/`, no commits.\n")

    REPORT_MD.write_text("\n".join(report), encoding="utf-8")

    # ── 10. Stdout summary ───────────────────────────────────────────────────
    print()
    print("─" * 60)
    print("Headline:")
    print(f"  Total trades:            {n_trades}")
    print(f"    BEAR-entry:            {bear_entries}")
    print(f"    CRASH-entry:           {crash_entries}")
    print(f"    Other regime:          {other_regime_entries}")
    print(f"  Suspended BUY signals:   {suspended_buys}")
    print(f"  Mean hold (bars):        {hold_mean:.1f}")
    print(f"  Net PnL:                 ${pnl_net_total:+,.2f}")
    print(f"  Sharpe (annualised):     {sharpe:.3f}")
    print(f"  Final equity:            ${end_eq:,.2f}")
    print()
    print(f"  Verdict: {verdict}")
    print()
    print(f"Report:  {REPORT_MD.relative_to(PROJECT_ROOT)}")
    print(f"Trades:  {TRADES_CSV.relative_to(PROJECT_ROOT)}")
    print(f"Runtime: {runtime_s:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
