"""scripts/bearshort_signal_audit.py — One-off diagnostic.

Re-runs BearShort's entry/exit signal logic over the full BTC/USDT 1h cache in
dry-simulation mode (no orders, no portfolio, no broker). For every bar it
records the regime label active at that bar and asks the strategy whether a
SHORT entry, exit, or HOLD would fire. Then it summarises:

  - signal-event counts per regime
  - mean holding period (in bars) using strategy-driven exits only
  - closed-trade counts in dev vs holdout windows, and within BEAR hours
  - trades-per-BEAR-month density in dev vs holdout
  - MinTRL adequacy gate on the holdout closed-trade count

Critical caveat: in this dry-sim mode there is no simulator, so SL/TP never
fire. Exits are driven only by Supertrend flipping bullish or RSI > rsi_exit.
This biases holding periods upward and trade counts downward relative to the
real backtest engine. The numbers below are signal-fire density, not realised
trade density. That is the right diagnostic for "does the filter stack
trigger often enough in BEAR hours?", which is the question this script is
answering.

Read-only against the parquet cache and the holdout manifest:
  - cache loaded via parquet glob pattern (mirror of holdout._load_symbol_df,
    duplicated to avoid importing the sacred-harness module surface beyond
    load_manifest())
  - manifest loaded via backtest.holdout.load_manifest() — no side effects
  - does NOT call load_dev / load_holdout / cache.load_or_download_ohlcv, so
    backtest/holdout_access.log is untouched

Usage:
    python scripts/bearshort_signal_audit.py
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

# Silence loguru BEFORE importing detector + strategy. Both emit INFO logs
# on every state change which would otherwise drown the audit output.
from loguru import logger  # noqa: E402
logger.remove()

from portfolio.regime_detector import RegimeDetector, ALL_REGIMES  # noqa: E402
from strategies.bear_short import BearShortStrategy  # noqa: E402
from backtest.holdout import load_manifest  # noqa: E402  (read-only)


SYMBOL          = "BTC/USDT"
TIMEFRAME       = "1h"
WARMUP          = 1000
CACHE_DIR       = PROJECT_ROOT / "backtest" / "cache" / "ohlcv"
LOG_DIR         = PROJECT_ROOT / "logs"
CSV_OUT_PATH    = LOG_DIR / "bearshort_signal_audit.csv"
PROGRESS_EVERY  = 2000
HOURS_PER_MONTH = 24 * 30
MIN_TRL_TRADES  = 30   # rough adequacy threshold for DSR power at Sharpe~1.0


# ── Cache loader (mirror of backtest.holdout._load_symbol_df) ────────────────

_MONTHS_RE = re.compile(r"_(\d+)mo\.parquet$")


def _parse_months(path: Path) -> int:
    m = _MONTHS_RE.search(path.name)
    if m is None:
        raise ValueError(
            f"Cache file '{path.name}' does not match the expected "
            "naming convention '{symbol}_{timeframe}_{N}mo.parquet'."
        )
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


def main() -> int:
    t0 = time.perf_counter()

    manifest = load_manifest()
    if "BearShort" not in manifest:
        print("ERROR: 'BearShort' missing from holdout manifest.", file=sys.stderr)
        return 1
    holdout_start = pd.Timestamp(manifest["BearShort"]["holdout_start"])
    if holdout_start.tz is None:
        holdout_start = holdout_start.tz_localize("UTC")

    df = load_btc_cache()
    n_total = len(df)
    if n_total <= WARMUP:
        print(f"ERROR: only {n_total} bars in cache; need > WARMUP={WARMUP}.", file=sys.stderr)
        return 1

    print(
        f"Loaded {SYMBOL} {TIMEFRAME} cache: {n_total} bars from "
        f"{df.index[0].strftime('%Y-%m-%d %H:%M')} to "
        f"{df.index[-1].strftime('%Y-%m-%d %H:%M')}"
    )
    print(f"Skipped first {WARMUP} bars (warmup), labeling {n_total - WARMUP}")
    print(f"Holdout boundary: {holdout_start.isoformat()}")
    print()

    detector = RegimeDetector()
    strategy = BearShortStrategy(symbol=SYMBOL, timeframe=TIMEFRAME)

    # Per-bar label rows for regime substrate accounting.
    bar_records: list[dict] = []
    # Per-event rows for the CSV (one per BUY or SELL signal).
    event_rows:  list[dict] = []
    # Per-trade rows (entry + exit pair) for closed-trade accounting.
    trades:      list[dict] = []
    open_trade:  dict | None = None

    for i in range(WARMUP, n_total):
        window  = df.iloc[i - WARMUP : i + 1]
        ts      = df.index[i]

        reading = detector.detect(window)
        regime  = reading.regime

        signal  = strategy.generate_signal(window)

        # Snapshot indicator values used by the strategy at this bar.
        # Recomputing here keeps the CSV columns explicit; values mirror what
        # the strategy itself just used inside generate_signal().
        close    = window["close"]
        ema20    = float(close.ewm(span=strategy.ema_fast, adjust=False).mean().iloc[-1])
        ema50    = float(close.ewm(span=strategy.ema_slow, adjust=False).mean().iloc[-1])
        # Recompute supertrend direction the same way the strategy does.
        # (Strategy returns only BUY/SELL/HOLD, not the underlying values.)
        st_line, st_dir = strategy._compute_supertrend(window)
        st_direction    = int(st_dir.iloc[-1])

        import ta as _ta
        rsi_val   = float(
            _ta.momentum.RSIIndicator(close, window=strategy.rsi_period).rsi().iloc[-1]
        )
        macd_ind  = _ta.trend.MACD(
            close,
            window_fast=strategy.macd_fast,
            window_slow=strategy.macd_slow,
            window_sign=strategy.macd_signal,
        )
        macd_hist = float(macd_ind.macd_diff().iloc[-1])
        atr_val   = float(
            _ta.volatility.AverageTrueRange(
                window["high"], window["low"], close, window=strategy.atr_period,
            ).average_true_range().iloc[-1]
        )
        price     = float(close.iloc[-1])

        is_in_holdout = ts >= holdout_start

        bar_records.append({
            "ts":       ts,
            "regime":   regime,
            "in_holdout": is_in_holdout,
        })

        if signal.action in ("BUY", "SELL"):
            event = "entry" if signal.action == "BUY" else "exit"
            event_rows.append({
                "timestamp":     ts.isoformat(),
                "event":         event,
                "regime":        regime,
                "is_in_holdout": is_in_holdout,
                "price":         round(price, 4),
                "st_direction":  st_direction,
                "ema20":         round(ema20, 4),
                "ema50":         round(ema50, 4),
                "rsi":           round(rsi_val, 4),
                "macd_hist":     round(macd_hist, 6),
                "atr":           round(atr_val, 4),
            })

            if event == "entry":
                # Strategy guards _in_short flag, so a BUY here means a fresh
                # short entry; if open_trade is non-None something drifted.
                if open_trade is not None:
                    print(
                        f"  WARNING: BUY at {ts} while open_trade exists "
                        f"(entry={open_trade['entry_ts']}). Overwriting.",
                        file=sys.stderr,
                    )
                open_trade = {
                    "entry_ts":     ts,
                    "entry_regime": regime,
                    "entry_idx":    i,
                }
            else:  # exit
                if open_trade is None:
                    print(
                        f"  WARNING: SELL at {ts} with no open_trade.",
                        file=sys.stderr,
                    )
                else:
                    trades.append({
                        "entry_ts":     open_trade["entry_ts"],
                        "exit_ts":      ts,
                        "entry_regime": open_trade["entry_regime"],
                        "exit_regime":  regime,
                        "hold_bars":    i - open_trade["entry_idx"],
                    })
                    open_trade = None

        labeled_so_far = i - WARMUP
        if labeled_so_far > 0 and labeled_so_far % PROGRESS_EVERY == 0:
            elapsed = time.perf_counter() - t0
            total   = n_total - WARMUP
            print(
                f"  ...labeled {labeled_so_far}/{total} bars "
                f"({labeled_so_far / total:.0%}) in {elapsed:.1f}s",
                file=sys.stderr,
            )

    # ── CSV output ───────────────────────────────────────────────────────────
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    events_df = pd.DataFrame(event_rows)
    events_df.to_csv(CSV_OUT_PATH, index=False)

    # ── Aggregates ───────────────────────────────────────────────────────────
    bars_df  = pd.DataFrame(bar_records)
    n_labeled = len(bars_df)

    entries = events_df[events_df["event"] == "entry"] if not events_df.empty else events_df
    exits   = events_df[events_df["event"] == "exit"]  if not events_df.empty else events_df

    entry_regime_counts = entries["regime"].value_counts() if not entries.empty else pd.Series(dtype=int)

    # Hold-period stats (bars).
    if trades:
        hold_bars = [t["hold_bars"] for t in trades]
        mean_hold = sum(hold_bars) / len(hold_bars)
    else:
        mean_hold = 0.0

    # Closed-trade window classification.
    dev_closed     = [t for t in trades if t["entry_ts"] <  holdout_start and t["exit_ts"] <  holdout_start]
    holdout_closed = [t for t in trades if t["entry_ts"] >= holdout_start and t["exit_ts"] >= holdout_start]
    dev_closed_bear     = [t for t in dev_closed     if t["entry_regime"] == "BEAR"]
    holdout_closed_bear = [t for t in holdout_closed if t["entry_regime"] == "BEAR"]

    # BEAR-substrate hour counts per window.
    dev_bear_hours     = int(((bars_df["regime"] == "BEAR") & (~bars_df["in_holdout"])).sum())
    holdout_bear_hours = int(((bars_df["regime"] == "BEAR") &  (bars_df["in_holdout"])).sum())
    dev_bear_months    = dev_bear_hours / HOURS_PER_MONTH if dev_bear_hours > 0 else 0.0
    holdout_bear_months= holdout_bear_hours / HOURS_PER_MONTH if holdout_bear_hours > 0 else 0.0

    def _per_month(n: int, months: float) -> str:
        if months <= 0:
            return "n/a (zero BEAR-months)"
        return f"{n / months:.2f}"

    # ── Stdout summary ───────────────────────────────────────────────────────
    print()
    print(f"Total bars labeled: {n_labeled}")
    if not entries.empty:
        regime_breakdown = ", ".join(
            f"{r}={int(entry_regime_counts.get(r, 0))}" for r in ALL_REGIMES
        )
    else:
        regime_breakdown = ", ".join(f"{r}=0" for r in ALL_REGIMES)
    print(f"Total BearShort entries: {len(entries)} (regime breakdown: {regime_breakdown})")
    print(f"Total BearShort exits: {len(exits)}")
    print(f"Mean holding period: {mean_hold:.1f} bars")

    print(f"Trades fully closed in dev window: {len(dev_closed)}")
    print(f"  └─ in dev BEAR hours: {len(dev_closed_bear)}")
    print(f"Trades fully closed in holdout window: {len(holdout_closed)}")
    print(f"  └─ in holdout BEAR hours: {len(holdout_closed_bear)}")

    print(f"Mean trades per BEAR-month in dev: {_per_month(len(dev_closed_bear), dev_bear_months)}")
    print(f"Mean trades per BEAR-month in holdout: {_per_month(len(holdout_closed_bear), holdout_bear_months)}")
    print()
    print("MinTRL gate (rough): for an observed Sharpe of 1.0 to be statistically")
    print("distinguishable from zero at confidence 0.95, ~30 trades are needed.")
    verdict = "ADEQUATE" if len(holdout_closed) >= MIN_TRL_TRADES else "UNDER-POWERED"
    print(f"Holdout closed-trade count: {len(holdout_closed)} → {verdict}")
    print()
    print(f"BEAR substrate (hours): dev={dev_bear_hours}, holdout={holdout_bear_hours} "
          f"(dev≈{dev_bear_months:.2f} months, holdout≈{holdout_bear_months:.2f} months)")
    print(f"Wrote {CSV_OUT_PATH.relative_to(PROJECT_ROOT)} ({len(events_df)} rows)")
    print()
    print("CAVEAT: dry-sim has no simulator, so SL/TP never fire. Exits are")
    print("strategy-signal-only (Supertrend flip OR RSI > rsi_exit). Real-engine")
    print("backtests will close trades earlier on SL/TP, lowering hold-period and")
    print("raising trade count vs the numbers above.")
    print()
    print(f"Runtime: {time.perf_counter() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
