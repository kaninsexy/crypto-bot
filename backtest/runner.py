"""
backtest/runner.py — Orchestrates the full Phase C backtesting pipeline.

WHAT IT DOES
────────────
1. Downloads 36 months (3 years) of 1h OHLCV data for the configured symbol from OKX
   (no API key needed — public endpoint).

2. Splits data into two periods:
     In-sample  (IS) : first 9 months  → strategy development / optimisation
     Out-of-sample (OOS): last 3 months → honest performance estimate

3. For each of the 10 strategies, runs BacktestEngine on both IS and OOS.
   Each run uses a **fresh** strategy instance (no state leakage between periods).

4. Calls backtest/report.py to print a side-by-side comparison table.

USAGE
─────
  cd crypto_bot
  python -m backtest.runner

  Override symbol / timeframe / balance via env vars:
    BACKTEST_SYMBOL=ETH/USDT BACKTEST_TF=4h python -m backtest.runner

  Additional env vars:
    BACKTEST_BALANCE=10000    (default 10 000 USDT starting equity)
    BACKTEST_MONTHS=12        (total months of history to download)
    BACKTEST_SPLIT=9          (months for in-sample period; rest = OOS)
"""

import math
import os
import sys
import time
import uuid
from typing import Literal, Optional

import ccxt
import numpy as np
import pandas as pd
from datetime import datetime, timezone, timedelta
from loguru import logger
from pathlib import Path

# ── Allow running as  python -m backtest.runner  from crypto_bot/ ───────────
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from backtest.engine import BacktestEngine, BacktestResult
from backtest.cache import load_or_download_ohlcv, get_symbol_dev_cutoff
from backtest.report import print_comparison_table, print_period_report
from backtest import holdout as _holdout
from backtest import trials as _trials
from backtest.baseline import buy_and_hold_sharpe
from backtest.cpcv import CPCVConfig, run_cpcv, CPCVResult
from backtest.dsr import deflated_sharpe, min_track_record_length
from backtest.verdict import compute_verdict, VerdictResult
from rescue.policy import RESCUE_TRIAL_BUDGET

# Strategy factories — each returns a fresh, reset instance
from strategies.dca import DCAStrategy
from strategies.supertrend import SupertrendStrategy
from strategies.mean_reversion import MeanReversionStrategy
from strategies.grid_trading import GridTradingStrategy
from strategies.breakout import BreakoutStrategy
from strategies.trend_following import TrendFollowingStrategy
from strategies.bear_short import BearShortStrategy
from strategies.vwap import VWAPStrategy
from strategies.volatility_breakout import VolatilityBreakoutStrategy
from strategies.dual_momentum import DualMomentumStrategy


# ── Config from env ──────────────────────────────────────────────────────────

TIMEFRAME     = os.getenv("BACKTEST_TF", "1h")
BALANCE       = float(os.getenv("BACKTEST_BALANCE", "10000"))
TOTAL_MONTHS  = int(os.getenv("BACKTEST_MONTHS", "36"))
IS_MONTHS     = int(os.getenv("BACKTEST_SPLIT", "27"))  # in-sample months
OOS_MONTHS    = TOTAL_MONTHS - IS_MONTHS                 # out-of-sample months

# Warm-up: enough candles for the longest indicator in any strategy (~200 for SMA200)
WARM_UP = 220

# ── Universe of symbols downloaded for the backtest ─────────────────────────
# This must be a SUPERSET of every symbol referenced by:
#   • config.STRATEGY_SYMBOLS (per-strategy primary symbols)
#   • DualMomentumStrategy.DEFAULT_UNIVERSE (rotation universe)
# A startup check inside `run_all` enforces this and raises if a config
# change introduces a symbol not listed here. Add new symbols to this list
# whenever you wire a strategy to a new pair.
UNIVERSE_SYMBOLS = [
    "BTC/USDT",
    "ETH/USDT",
    "SOL/USDT",
    "AVAX/USDT",
    "BNB/USDT",
]


# ── Strategy factory ─────────────────────────────────────────────────────────

def make_strategies(timeframe: str) -> dict:
    """
    Return a dict of  name → strategy_instance  with default parameters.
    Each call returns fresh instances (no shared state).

    Each strategy's primary symbol is read from `config.STRATEGY_SYMBOLS`;
    the backtest runner no longer forces every strategy onto a single pair.
    DualMomentum still takes a primary symbol (BTC/USDT by default) but
    rotates across its internal `DEFAULT_UNIVERSE` during the run.
    """
    syms = config.STRATEGY_SYMBOLS
    return {
        "DCA": DCAStrategy(
            symbol=syms["DCA"], timeframe=timeframe,
            base_amount=200.0,
            deviation_pct=2.0,
            max_safety_orders=5,
            stop_loss_pct=0.12,
            compound=True,
        ),
        "Supertrend": SupertrendStrategy(
            symbol=syms["Supertrend"], timeframe=timeframe,
        ),
        "MeanReversion": MeanReversionStrategy(
            symbol=syms["MeanReversion"], timeframe=timeframe,
            ema_filter=True,   # Only buy dips when EMA50 > EMA200 (uptrend)
            ema_fast=50, ema_slow=200,
        ),
        "GridTrading": GridTradingStrategy(
            symbol=syms["GridTrading"], timeframe=timeframe,
            grid_levels=10,
            usdt_per_trade=200.0,
            recalibrate_every=24,
        ),
        "Breakout": BreakoutStrategy(
            symbol=syms["Breakout"], timeframe=timeframe,
            mtf_enabled=False,  # Backtest has 1h data only — no 4h feed available
        ),
        "TrendFollowing": TrendFollowingStrategy(
            symbol=syms["TrendFollowing"], timeframe=timeframe,
        ),
        "BearShort": BearShortStrategy(
            symbol=syms["BearShort"], timeframe=timeframe,
        ),
        "VWAP": VWAPStrategy(
            symbol=syms["VWAP"], timeframe=timeframe,
        ),
        "VolatilityBreakout": VolatilityBreakoutStrategy(
            symbol=syms["VolatilityBreakout"], timeframe=timeframe,
        ),
        "DualMomentum": DualMomentumStrategy(
            symbol=syms["DualMomentum"], timeframe=timeframe,
        ),
    }


# ── Historical data download ─────────────────────────────────────────────────

def download_history(
    symbol: str,
    timeframe: str = "1h",
    months: int = 12,
) -> pd.DataFrame:
    """
    Download `months` of OHLCV data from OKX (public, no API key needed).

    OKX limits each fetch to 300 candles per request. For 1h data over 12 months that
    is ~8 760 candles, so we page backwards in batches of 300.

    Returns a DataFrame indexed by UTC timestamp, columns: open/high/low/close/volume.
    """
    logger.info(f"Downloading {months}mo of {timeframe} data for {symbol} from OKX...")

    exchange = ccxt.okx({"enableRateLimit": True})

    # Calculate start timestamp
    since_dt = datetime.now(timezone.utc) - timedelta(days=int(months * 30.44))
    since_ms  = int(since_dt.timestamp() * 1000)

    all_candles = []
    batch_size  = 300

    while True:
        try:
            raw = exchange.fetch_ohlcv(
                symbol,
                timeframe=timeframe,
                since=since_ms,
                limit=batch_size,
            )
        except (ccxt.NetworkError, ccxt.ExchangeError) as e:
            logger.error(f"Download error: {e}")
            raise

        if not raw:
            break

        all_candles.extend(raw)
        last_ts = raw[-1][0]

        logger.debug(
            f"  Fetched {len(raw)} candles | "
            f"Total: {len(all_candles)} | "
            f"Up to: {datetime.fromtimestamp(last_ts/1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')}"
        )

        if len(raw) < batch_size:
            break  # Reached present

        since_ms = last_ts + 1  # next batch starts after last candle
        time.sleep(exchange.rateLimit / 1000)

    if not all_candles:
        raise ValueError(f"No data returned for {symbol} {timeframe}")

    df = pd.DataFrame(
        all_candles,
        columns=["timestamp", "open", "high", "low", "close", "volume"]
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df.set_index("timestamp", inplace=True)
    df = df.drop_duplicates().sort_index()

    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)

    logger.info(
        f"Downloaded {len(df)} candles | "
        f"{df.index[0].strftime('%Y-%m-%d')} → {df.index[-1].strftime('%Y-%m-%d')}"
    )
    return df


# ── Split helper ─────────────────────────────────────────────────────────────

def split_df(df: pd.DataFrame, is_months: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Split df into in-sample and out-of-sample DataFrames.

    The IS portion starts from the beginning of the data.
    The OOS portion starts immediately after.

    We keep WARM_UP candles of overlap at the OOS boundary so the strategy
    can still compute indicators for the first real OOS candle.
    """
    total = len(df)
    # Estimate number of candles in is_months
    is_frac  = is_months / TOTAL_MONTHS
    is_end   = int(total * is_frac)

    df_is  = df.iloc[:is_end].copy()
    # OOS starts WARM_UP candles before is_end so indicators can warm up
    oos_start = max(0, is_end - WARM_UP)
    df_oos = df.iloc[oos_start:].copy()

    logger.info(
        f"Split: IS={len(df_is)} candles "
        f"({df_is.index[0].strftime('%Y-%m-%d')} → {df_is.index[-1].strftime('%Y-%m-%d')}) | "
        f"OOS={len(df_oos)} candles "
        f"({df_oos.index[0].strftime('%Y-%m-%d')} → {df_oos.index[-1].strftime('%Y-%m-%d')})"
    )
    return df_is, df_oos


# ── Main ─────────────────────────────────────────────────────────────────────

def run_all(
    timeframe: str = TIMEFRAME,
    balance: float = BALANCE,
    total_months: int = TOTAL_MONTHS,
    is_months: int = IS_MONTHS,
    mode: Literal["dev", "final_gate", "dev_cpcv"] = "dev",
) -> dict:
    """
    Run the full Phase C backtesting pipeline across the multi-symbol universe.

    Each strategy runs on its assigned pair from `config.STRATEGY_SYMBOLS`.
    DualMomentum rotates across its internal universe (BTC/ETH/BNB) using
    per-symbol OHLCV pulled from the download set.

    Args:
      mode:
        "dev"        — default, preserves all prior behaviour exactly:
                       universe-wide download, IS/OOS split, engine
                       run on both periods, comparison report.  No
                       holdout access, no trials.log write.
        "final_gate" — Phase 3c deploy-gate path.  For each strategy:
                       loads its holdout window via
                       `holdout.load_holdout(...)`, runs the engine
                       once on it, computes the keep/retire verdict
                       via `compute_verdict`, and appends a schema-v2
                       final_gate row to `trials.log`.  No universe
                       download (data flows through the holdout
                       accessor's bypass context).  The
                       `FinalGateAlreadyRecorded` guard in trials.py
                       enforces single-access-per-split-epoch.
        "dev_cpcv"   — Phase 3c rescue-iteration path.  For each
                       strategy: runs CPCV on the dev window only
                       (holdout sealed), computes the buy-and-hold
                       baseline + DSR (n_trials = `RESCUE_TRIAL_BUDGET`,
                       NOT `count_trials_for_dsr`) + MinTRL + verdict,
                       and appends a v1-schema full_cpcv row to
                       trials.log.  This is the row that final_gate's
                       prior-full_cpcv guard reads downstream.  No
                       holdout access, no live API.

    Returns a dict:
        mode == "dev":
          { strategy_name: {"is": BacktestResult, "oos": BacktestResult} }
        mode == "final_gate":
          { strategy_name: {"holdout": BacktestResult, "verdict": VerdictResult} }
        mode == "dev_cpcv":
          { strategy_name: DevCpcvResult }
    """
    if mode == "final_gate":
        return _run_all_final_gate(timeframe=timeframe, balance=balance)
    if mode == "dev_cpcv":
        return _run_all_dev_cpcv(timeframe=timeframe, balance=balance)

    logger.remove()
    logger.add(
        sys.stderr,
        level="INFO",
        format="<green>{time:HH:mm:ss}</green> | <level>{level:7}</level> | {message}",
        colorize=True,
    )

    # ── 0. Validate UNIVERSE_SYMBOLS covers everything we need ───────────────
    # Fail fast before any downloads if config drifted and a new pair was
    # added to STRATEGY_SYMBOLS or DualMomentum's universe without being
    # added here. Downloading the wrong set is wasteful and silently
    # corrupting.
    strategy_syms = set(config.STRATEGY_SYMBOLS.values())
    dm_universe   = set(DualMomentumStrategy.DEFAULT_UNIVERSE)
    required      = strategy_syms | dm_universe
    missing       = required - set(UNIVERSE_SYMBOLS)
    if missing:
        raise ValueError(
            f"UNIVERSE_SYMBOLS is missing symbols required by config: {sorted(missing)}. "
            f"Current UNIVERSE_SYMBOLS = {UNIVERSE_SYMBOLS}. "
            f"Add the missing symbols so they get downloaded."
        )

    print("\n" + "═" * 70)
    print(f"  PHASE C — BACKTESTING ENGINE (multi-symbol)")
    print(f"  Universe : {UNIVERSE_SYMBOLS}")
    print(f"  Timeframe: {timeframe}")
    print(f"  Balance  : ${balance:,.0f}  |  Period: {total_months}mo  "
          f"(IS:{is_months}mo / OOS:{total_months-is_months}mo)")
    print("═" * 70 + "\n")

    # ── 1. Download (or load from cache) every universe symbol ───────────────
    # `load_or_download_ohlcv` wraps the OKX downloader with a 24 h parquet
    # cache. Pass `download_history` as the download callable so this module
    # still owns the network behaviour.
    dfs_full: dict[str, pd.DataFrame] = {}
    for sym in UNIVERSE_SYMBOLS:
        dfs_full[sym] = load_or_download_ohlcv(
            symbol=sym,
            timeframe=timeframe,
            months=total_months,
            download_fn=download_history,
            until_ts=get_symbol_dev_cutoff(sym),
        )

    # ── 2. Split each symbol into IS / OOS ───────────────────────────────────
    dfs_is:  dict[str, pd.DataFrame] = {}
    dfs_oos: dict[str, pd.DataFrame] = {}
    for sym, df_full in dfs_full.items():
        logger.info(f"[Split] {sym}")
        df_is, df_oos = split_df(df_full, is_months)
        dfs_is[sym]  = df_is
        dfs_oos[sym] = df_oos

    engine = BacktestEngine(
        initial_balance=balance,
        warm_up_candles=WARM_UP,
        verbose=False,
    )

    results = {}

    # ── 3. Run each strategy on both periods with the correct symbol(s) ──────
    for name in make_strategies(timeframe).keys():
        print(f"\n{'─'*70}")
        print(f"  Running: {name}")
        print(f"{'─'*70}")

        # Fresh instances for each period to avoid state bleed
        strat_is  = make_strategies(timeframe)[name]
        strat_oos = make_strategies(timeframe)[name]

        strat_symbol_is  = strat_is.symbol
        strat_symbol_oos = strat_oos.symbol

        # Primary per-strategy OHLCV frames. These always come from the
        # universe dict; config ↔ UNIVERSE_SYMBOLS coverage was validated at
        # the top of this function.
        df_is_strat  = dfs_is[strat_symbol_is]
        df_oos_strat = dfs_oos[strat_symbol_oos]

        # Multi-symbol strategies (DualMomentum) need the full universe
        # keyed by the symbols THEY rank. Filter the master dict down to
        # `strategy.universe_symbols` so the engine's defensive check
        # doesn't trip on unrelated pairs.
        if hasattr(strat_is, "update_universe"):
            universe_syms_is = getattr(strat_is, "universe_symbols", None) or []
            universe_dfs_is = {
                s: dfs_is[s] for s in universe_syms_is if s in dfs_is
            }
        else:
            universe_dfs_is = None

        if hasattr(strat_oos, "update_universe"):
            universe_syms_oos = getattr(strat_oos, "universe_symbols", None) or []
            universe_dfs_oos = {
                s: dfs_oos[s] for s in universe_syms_oos if s in dfs_oos
            }
        else:
            universe_dfs_oos = None

        try:
            r_is = engine.run(
                df_is_strat,
                strat_is,
                period_label="in-sample",
                universe_dfs=universe_dfs_is,
            )
        except Exception as e:
            logger.error(f"{name} IS failed: {e}")
            r_is = None

        try:
            r_oos = engine.run(
                df_oos_strat,
                strat_oos,
                period_label="out-of-sample",
                universe_dfs=universe_dfs_oos,
            )
        except Exception as e:
            logger.error(f"{name} OOS failed: {e}")
            r_oos = None

        results[name] = {"is": r_is, "oos": r_oos}

    # ── 4. Print reports ─────────────────────────────────────────────────────
    # print_comparison_table expects a `symbol` arg for the header. Since each
    # strategy now runs on its own pair, pass the sentinel "MULTI-SYMBOL" so
    # the header doesn't misleadingly claim BTC/USDT.
    print_comparison_table(
        results, symbol="MULTI-SYMBOL", timeframe=timeframe
    )

    # ── 5. Save results to JSON for the dashboard ────────────────────────────
    _save_results_json(results, timeframe=timeframe)

    # ── 6. Save CSV + text report for easy review ────────────────────────────
    _save_report_files(results, timeframe=timeframe)

    return results


def _symbol_label(name: str) -> str:
    """
    Return the display symbol for a strategy in reports.

    Single-symbol strategies show their assigned pair from
    `config.STRATEGY_SYMBOLS`. DualMomentum shows a compact summary of its
    rotation universe so at-a-glance readers know the row reflects multiple
    pairs, not one.
    """
    if name == "DualMomentum":
        uni = "/".join(s.split("/")[0] for s in DualMomentumStrategy.DEFAULT_UNIVERSE)
        return f"multi ({uni})"
    return config.STRATEGY_SYMBOLS.get(name, "multi")


def _save_results_json(results: dict, timeframe: str) -> None:
    """Serialize backtest results to dashboard/data/backtest_results.json."""
    try:
        from dashboard.state import write_backtest_results
        from backtest.engine import BacktestMetrics

        def _metrics_to_dict(m: BacktestMetrics) -> dict:
            return {
                "strategy_name":       m.strategy_name,
                "symbol":              m.symbol,
                "start_date":          str(m.start_date),
                "end_date":            str(m.end_date),
                "n_candles":           m.n_candles,
                "total_return_pct":    round(m.total_return_pct, 2),
                "annualised_return_pct": round(m.annualised_return_pct, 2),
                "max_drawdown_pct":    round(m.max_drawdown_pct, 2),
                "sharpe_ratio":        round(m.sharpe_ratio, 3),
                "calmar_ratio":        round(m.calmar_ratio, 3),
                "volatility_pct":      round(m.volatility_pct, 2),
                "total_trades":        m.total_trades,
                "win_rate_pct":        round(m.win_rate_pct, 1),
                "profit_factor":       round(m.profit_factor, 3) if m.profit_factor < 9999 else None,
                "avg_win_pct":         round(m.avg_win_pct, 2),
                "avg_loss_pct":        round(m.avg_loss_pct, 2),
                "best_trade_pct":      round(m.best_trade_pct, 2),
                "worst_trade_pct":     round(m.worst_trade_pct, 2),
                "total_fees_usdt":     round(m.total_fees_usdt, 2),
            }

        serialized = {}
        for name, r in results.items():
            entry = {"display_symbol": _symbol_label(name)}
            if r.get("is") and r["is"].metrics:
                entry["is"] = _metrics_to_dict(r["is"].metrics)
            if r.get("oos") and r["oos"].metrics:
                entry["oos"] = _metrics_to_dict(r["oos"].metrics)
            serialized[name] = entry

        # Top-level `symbol` is now a sentinel — per-strategy symbols are on
        # each entry's metrics (`strategy_name -> is/oos -> symbol`) and in
        # `display_symbol`. Downstream consumers that want a single ticker
        # should read from the per-strategy entries instead.
        write_backtest_results({
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "symbol":     "multi",
            "timeframe":  timeframe,
            "strategies": serialized,
        })
        logger.info("Backtest results saved to dashboard/data/backtest_results.json")
    except Exception as e:
        logger.warning(f"Could not save backtest results to dashboard: {e}")


def _save_report_files(results: dict, timeframe: str) -> None:
    """
    Save two files to backtest/reports/ after each run:
      - YYYY-MM-DD_HHMMSS_summary.csv   — comparison table (open in Excel/Numbers)
      - YYYY-MM-DD_HHMMSS_full.txt      — full text report (same as terminal output)

    Each row's "Symbol" column reflects the strategy's actual pair (not a
    single runner-level symbol), so readers can see at a glance that, say,
    MeanReversion was tested on ETH/USDT and GridTrading on SOL/USDT.
    """
    try:
        reports_dir = Path(__file__).parent / "reports"
        reports_dir.mkdir(exist_ok=True)

        ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")

        # ── CSV summary ───────────────────────────────────────────────────────
        csv_path = reports_dir / f"{ts}_summary.csv"
        rows = []
        for name, r in results.items():
            row = {
                "Strategy":  name,
                "Symbol":    _symbol_label(name),
                "Timeframe": timeframe,
            }
            for period, key in [("IS", "is"), ("OOS", "oos")]:
                m = r.get(key) and r[key].metrics
                if m:
                    row.update({
                        f"{period} Return%":       round(m.total_return_pct, 2),
                        f"{period} Ann Return%":   round(m.annualised_return_pct, 2),
                        f"{period} MaxDD%":        round(m.max_drawdown_pct, 2),
                        f"{period} Sharpe":        round(m.sharpe_ratio, 3),
                        f"{period} Calmar":        round(m.calmar_ratio, 3),
                        f"{period} WinRate%":      round(m.win_rate_pct, 1),
                        f"{period} Trades":        m.total_trades,
                        f"{period} ProfitFactor":  round(m.profit_factor, 3) if m.profit_factor < 9999 else 999,
                        f"{period} AvgWin%":       round(m.avg_win_pct, 2),
                        f"{period} AvgLoss%":      round(m.avg_loss_pct, 2),
                        f"{period} Fees$":         round(m.total_fees_usdt, 2),
                    })
                else:
                    for col in ["Return%", "Ann Return%", "MaxDD%", "Sharpe", "Calmar",
                                "WinRate%", "Trades", "ProfitFactor", "AvgWin%", "AvgLoss%", "Fees$"]:
                        row[f"{period} {col}"] = "N/A"
            rows.append(row)

        df_out = pd.DataFrame(rows)
        df_out.to_csv(csv_path, index=False)

        # ── Plain text summary ────────────────────────────────────────────────
        txt_path = reports_dir / f"{ts}_full.txt"
        lines = [
            f"BACKTEST REPORT — MULTI-SYMBOL [{timeframe}]",
            f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "=" * 78,
            "",
            f"{'Strategy':<16} {'Symbol':<22} {'IS Ret%':>9} {'OOS Ret%':>9} "
            f"{'MaxDD%':>8} {'Sharpe':>8} {'WinRate':>8} {'Trades':>7} {'PF':>7}",
            "-" * 78,
        ]
        for name, r in sorted(results.items(),
                               key=lambda x: (x[1].get("oos") and x[1]["oos"].metrics.sharpe_ratio) or -999,
                               reverse=True):
            oos = r.get("oos") and r["oos"].metrics
            ins = r.get("is")  and r["is"].metrics
            sym_label = _symbol_label(name)
            if oos:
                lines.append(
                    f"{name:<16} {sym_label:<22}"
                    f" {ins.total_return_pct if ins else 0:>+8.2f}%"
                    f" {oos.total_return_pct:>+8.2f}%"
                    f" {-oos.max_drawdown_pct:>+7.2f}%"
                    f" {oos.sharpe_ratio:>8.3f}"
                    f" {oos.win_rate_pct:>7.1f}%"
                    f" {oos.total_trades:>7}"
                    + (f" {oos.profit_factor:>7.3f}" if oos.profit_factor < 9999 else f" {'inf':>7}")
                )
        lines += ["", f"CSV saved to: {csv_path.name}"]
        txt_path.write_text("\n".join(lines))

        logger.info(f"Reports saved to backtest/reports/")
        logger.info(f"  CSV : {csv_path.name}")
        logger.info(f"  Text: {txt_path.name}")

    except Exception as e:
        logger.warning(f"Could not save report files: {e}")


# ── Phase 3c final_gate orchestration (chunk 11) ─────────────────────────────
#
# `mode="final_gate"` runs the deploy-gate machinery: each strategy's
# holdout window is read via the audited accessor, the engine is run on
# it, the keep/retire verdict is computed via `compute_verdict`, and a
# schema-v2 final_gate row is appended to trials.log.  The runner
# orchestrates calls — the actual gate logic lives in
# `backtest/verdict.py` and the accessor enforcement lives in
# `backtest/holdout.py`.


def _latest_full_cpcv_event(strategy_id: str) -> dict | None:
    """Return the most recent full_cpcv trial row for `strategy_id`,
    or None.  Used to populate the cpcv block + dsr_validation on the
    final_gate row — those fields are forensic context (the dev-window
    CPCV results that justified running the gate at all)."""
    latest: dict | None = None
    for ev in _trials.read_trials(
        strategy_id=strategy_id, trial_type="full_cpcv",
    ):
        ts = ev.get("ts", "")
        if latest is None or (
            isinstance(ts, str) and ts > latest.get("ts", "")
        ):
            latest = ev
    return latest


def _split_holdout_multi_symbol(
    holdout_df: pd.DataFrame,
    primary_symbol: str,
) -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    """Split a multi-symbol holdout frame into (primary, universe_dfs).

    `holdout.load_holdout` returns multi-symbol data as a single frame
    with a 'symbol' column (rows for each symbol stacked + sorted by
    timestamp).  `engine.run` for multi-symbol strategies wants
    per-symbol frames keyed by symbol, with the strategy's primary
    frame as `df`.  This helper does that split.
    """
    symbols = sorted(holdout_df["symbol"].unique().tolist())
    universe_dfs: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        sub = holdout_df[holdout_df["symbol"] == sym].drop(columns=["symbol"])
        sub = sub.sort_index()
        universe_dfs[sym] = sub
    if primary_symbol not in universe_dfs:
        raise ValueError(
            f"primary_symbol={primary_symbol!r} not in holdout symbols "
            f"{sorted(universe_dfs.keys())}"
        )
    return universe_dfs[primary_symbol], universe_dfs


def _run_strategy_final_gate(
    strategy_id: str,
    timeframe: str,
    balance: float,
) -> tuple[BacktestResult, VerdictResult]:
    """Execute the final_gate flow for a single strategy.

    Steps:
      1. `holdout.load_holdout(...)` — single-access-audited read.
      2. `engine.run(...)` on the holdout window (multi-symbol split
         where applicable).
      3. `compute_verdict(...)` to derive keep / retire / under_tested.
      4. `trials.record_trial(...)` with a schema-v2 final_gate event.

    The trial event's `cpcv` block + `dsr_validation` are populated
    from the most recent prior `full_cpcv` row for this strategy in
    trials.log (forensic context — the dev-window CPCV results that
    justified putting the strategy in front of the gate).  If no
    prior full_cpcv row exists, raises RuntimeError; final_gate is
    not the right path for an un-validated strategy.

    Returns (BacktestResult, VerdictResult).  The trials.log row is
    written as a side-effect; the existing `FinalGateAlreadyRecorded`
    guard in trials.py will raise on a second invocation in the same
    split epoch.
    """
    sample = make_strategies(timeframe)[strategy_id]
    primary_symbol = sample.symbol

    prior = _latest_full_cpcv_event(strategy_id)
    if prior is None:
        raise RuntimeError(
            f"final_gate for {strategy_id!r}: no prior full_cpcv row "
            "in trials.log.  Run a dev-window CPCV pass first; "
            "final_gate is not the entry point for un-validated "
            "strategies."
        )

    caller = f"phase3c.{strategy_id}.final_dsr"
    holdout_df = _holdout.load_holdout(
        strategy_id=strategy_id,
        caller=caller,
        reason="phase3c final_gate",
    )

    is_multi_symbol = "symbol" in holdout_df.columns
    if is_multi_symbol:
        primary_df, universe_dfs = _split_holdout_multi_symbol(
            holdout_df, primary_symbol,
        )
    else:
        primary_df = holdout_df
        universe_dfs = None

    engine = BacktestEngine(
        initial_balance=balance,
        warm_up_candles=WARM_UP,
        verbose=False,
    )
    fresh = make_strategies(timeframe)[strategy_id]
    result = engine.run(
        primary_df,
        fresh,
        period_label=f"final_gate-{strategy_id}",
        universe_dfs=universe_dfs,
    )

    # Returns + verdict.  primary_df is the OHLCV frame for the
    # baseline comparison (strategy's primary symbol over the same
    # holdout window).
    returns = (
        result.equity_curve.pct_change().dropna().values.astype(float)
    )
    n_trials = _trials.count_trials_for_dsr(strategy_id)

    verdict = compute_verdict(
        strategy_id=strategy_id,
        sr_candidate=float(result.metrics.sharpe_ratio),
        returns=returns,
        total_trades=int(result.metrics.total_trades),
        baseline_df=primary_df,
        n_trials=n_trials,
    )

    # dsr_holdout: for keep/retire branches the verdict already
    # carries it (verdict.dsr).  For under_tested rows the gate
    # explicitly did not compute DSR — the SR is acknowledged-
    # untrustworthy at this T or trade-count, so recording a number
    # off it would just look like data.  Leave NaN.
    if not math.isnan(verdict.dsr):
        dsr_holdout_value = float(verdict.dsr)
    else:
        dsr_holdout_value = float("nan")

    # Build schema-v2 final_gate event.  cpcv block + dsr_validation
    # are inherited from the prior dev-window full_cpcv run.
    event = {
        "strategy_id": strategy_id,
        "variation_id": prior.get("variation_id", "final_gate"),
        "trial_type": "final_gate",
        "params": prior.get("params", {}),
        "hypothesis": prior.get(
            "hypothesis", "phase3c final_gate run",
        ),
        "split_holdout_start": prior.get(
            "split_holdout_start",
            _holdout.load_manifest()[strategy_id]["holdout_start"],
        ),
        "symbols": prior.get("symbols", [primary_symbol]),
        "n_trades": int(result.metrics.total_trades),
        "sharpe": float(result.metrics.sharpe_ratio),
        "cpcv": prior["cpcv"],
        "dsr_validation": prior["dsr_validation"],
        "dsr_holdout": dsr_holdout_value,
        # ── v2 fields ───────────────────────────────────────────────
        "verdict": verdict.verdict,
        "trade_count_pass": verdict.trade_count_pass,
        "mintrl_pass": verdict.mintrl_pass,
        "mt_mean_pass": verdict.mt_mean_pass,
        "baseline_pass": verdict.baseline_pass,
        "sr_zero_expected_at_eval": verdict.sr_zero_expected_at_eval,
        "mintrl_required_at_eval": verdict.mintrl_required_at_eval,
        "baseline_sharpe_at_eval": verdict.baseline_sharpe_at_eval,
        "total_trades": int(result.metrics.total_trades),
    }
    _trials.record_trial(event)

    return result, verdict


def _run_all_final_gate(
    timeframe: str,
    balance: float,
) -> dict:
    """Iterate every strategy in `make_strategies` through the
    final_gate flow.  A `FinalGateAlreadyRecorded` raise is a hard
    integrity violation (single-access guard tripped) — re-raised
    immediately rather than absorbed into the per-strategy error
    dict.  Generic exceptions are logged and recorded per-strategy
    so one bad strategy doesn't kill the run."""
    results: dict = {}
    for name in make_strategies(timeframe).keys():
        logger.info(f"[FinalGate] Running: {name}")
        try:
            r, v = _run_strategy_final_gate(
                strategy_id=name, timeframe=timeframe, balance=balance,
            )
            results[name] = {"holdout": r, "verdict": v}
        except _trials.FinalGateAlreadyRecorded:
            raise
        except Exception as e:
            logger.error(f"[FinalGate] {name} failed: {e}")
            results[name] = {"holdout": None, "verdict": None, "error": str(e)}
    return results


# ── Phase 3c dev_cpcv orchestration ──────────────────────────────────────────
#
# `mode="dev_cpcv"` is the rescue-iteration path: runs CPCV + DSR +
# MinTRL + baseline + verdict on the dev window, writes one v1
# full_cpcv row per strategy to trials.log.  The row is the input
# `final_gate`'s prior-full_cpcv guard reads downstream.
#
# Critically, n_trials for DSR is `RESCUE_TRIAL_BUDGET` (=20), NOT
# `count_trials_for_dsr(strategy_id)`.  See `rescue/policy.py` for
# the rationale; the short version is that gating against a fixed
# Phase-3c iteration budget keeps the threshold symmetric across
# variations and removes the incentive to gate-shop early.
#
# This mode does NOT touch holdout data.  All reads route through
# `holdout.load_dev`, which is freely accessible per the
# validation_framework spec.


from dataclasses import dataclass as _dataclass


@_dataclass(frozen=True)
class DevCpcvResult:
    """Outcome of one dev_cpcv strategy run.

    Attributes:
      strategy_id:        Manifest key.
      observed_sharpe:    Engine-reported Sharpe over the full dev
                          window (the headline number being judged).
      sr_zero_expected:   BLP eq.7 Gumbel haircut at
                          n_trials=`RESCUE_TRIAL_BUDGET`.
      dsr_pvalue:         Φ((SR−sr_zero)/sr_std) — the deflated
                          probability that the observed Sharpe is
                          non-spurious given the multiple-testing
                          context.  Recorded as `dsr_validation` in
                          the trials.log row.
      mintrl:             BLP eq.13 minimum sample size in bars.
      baseline_sharpe:    Buy-and-hold Sharpe over the same dev
                          window (strategy's primary symbol).
      verdict:            VerdictResult — the keep/retire/under_tested
                          decision and its component bools.
      trial_row:          The exact dict appended to trials.log
                          (post-validation: schema_version, trial_id,
                          ts, params_hash, git_commit are all filled
                          by `trials.record_trial`).
    """
    strategy_id: str
    observed_sharpe: float
    sr_zero_expected: float
    dsr_pvalue: float
    mintrl: float
    baseline_sharpe: float
    verdict: VerdictResult
    trial_row: dict


def _print_dev_cpcv_block(res: DevCpcvResult) -> None:
    """Human-readable verdict block, verdict prominent."""
    sep = "═" * 70
    sub = "─" * 70
    print()
    print(sep)
    print(f"  DEV_CPCV — {res.strategy_id}")
    print(sep)
    verdict_label = res.verdict.verdict.upper()
    print(f"  VERDICT: {verdict_label}")
    print(sub)
    print(f"  observed_sharpe         {res.observed_sharpe:>+10.4f}")
    print(f"  sr_zero_expected (N=20) {res.sr_zero_expected:>+10.4f}")
    print(f"  dsr_pvalue              {res.dsr_pvalue:>10.4f}")
    print(f"  mintrl (bars)           {res.mintrl:>10.2f}")
    print(f"  baseline_sharpe         {res.baseline_sharpe:>+10.4f}")
    print(sub)
    print(f"  trade_count_pass        {res.verdict.trade_count_pass}")
    print(f"  mintrl_pass             {res.verdict.mintrl_pass}")
    print(f"  mt_mean_pass            {res.verdict.mt_mean_pass}")
    print(f"  baseline_pass           {res.verdict.baseline_pass}")
    print(f"  total_trades            {res.verdict.total_trades}")
    print(f"  t_observed              {res.verdict.t_observed}")
    print(sep)


def _split_dev_multi_symbol(
    dev_df: pd.DataFrame,
    primary_symbol: str,
) -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    """Split a multi-symbol dev frame into (primary, universe_dfs).

    Mirrors `_split_holdout_multi_symbol` but for the dev window.
    Multi-symbol `holdout.load_dev` returns a stacked frame with a
    'symbol' column; the engine wants per-symbol frames.
    """
    symbols = sorted(dev_df["symbol"].unique().tolist())
    universe_dfs: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        sub = dev_df[dev_df["symbol"] == sym].drop(columns=["symbol"])
        sub = sub.sort_index()
        universe_dfs[sym] = sub
    if primary_symbol not in universe_dfs:
        raise ValueError(
            f"primary_symbol={primary_symbol!r} not in dev symbols "
            f"{sorted(universe_dfs.keys())}"
        )
    return universe_dfs[primary_symbol], universe_dfs


def _concat_per_block_returns(cpcv_result: CPCVResult) -> np.ndarray:
    """Concatenate non-empty `per_block_returns` arrays into a single
    series for DSR / MinTRL / verdict consumption.

    Mirrors the contract documented on `deflated_sharpe`'s `returns`
    arg: "For dsr_validation: concatenate CPCVResult.per_block_returns,
    skipping empty arrays."  Empty arrays are blocks whose trade
    count fell below `_MIN_TRADES_PER_BLOCK`; skipping them lines up
    with how `dsr_from_cpcv_result` and `mintrl_from_cpcv_result`
    handle the same input.

    Raises RuntimeError (not a numpy message) if every block is
    empty — that's a CPCV that should not have produced a result
    we'd feed to DSR.
    """
    valid = [r for r in cpcv_result.per_block_returns if len(r) > 0]
    if not valid:
        raise RuntimeError(
            "dev_cpcv: CPCVResult has no non-empty per_block_returns "
            "arrays; cannot compute dsr_validation. Every block fell "
            "below the trade-count floor, or the CPCV run produced "
            "empty equity curves throughout.  Refusing to feed an "
            "empty series to DSR."
        )
    return np.concatenate(valid).astype(float)


def _build_full_cpcv_row(
    *,
    strategy_id: str,
    primary_symbol: str,
    cpcv_result: CPCVResult,
    cpcv_config: CPCVConfig,
    headline_result: BacktestResult,
    dsr_validation_value: float,
    n_trials: int,
) -> dict:
    """Assemble the v1 full_cpcv row dict.  Caller passes to
    `trials.record_trial` which fills schema_version, trial_id, ts,
    git_commit, params_hash.

    Schema-v1 required (full_cpcv): strategy_id, variation_id,
    trial_type, params, hypothesis, split_holdout_start, symbols,
    n_trades, sharpe, cpcv (block), dsr_validation.

    The cpcv block's `n_blocks`, `k_held_out`, `purge_periods` and
    `embargo_periods` come from the supplied `CPCVConfig` — never
    hardcoded.  `n_paths` comes from the CPCVResult (it's the
    realised count, which equals `n_blocks` in block-Sharpe mode but
    stays a separate field for forward compatibility with future
    path-CPCV).

    Extras:
      - n_trials (recorded explicitly per the rescue/policy.py budget;
        downstream consumers can audit which budget was used without
        recomputing).
    """
    manifest = _holdout.load_manifest()
    entry = manifest[strategy_id]
    is_multi_symbol = "symbols" in entry
    symbols = (
        list(entry["symbols"]) if is_multi_symbol else [entry["symbol"]]
    )

    return {
        "strategy_id": strategy_id,
        "variation_id": "rescue-default",
        "trial_type": "full_cpcv",
        "params": {},  # default-config rescue run; per-strategy
                       # parameter sweeps add their own variation_id
                       # and params.
        "hypothesis": (
            f"phase3c rescue: default {strategy_id} configuration on "
            "the dev window with CPCV block-Sharpe distribution + DSR "
            "(n_trials=RESCUE_TRIAL_BUDGET) + MinTRL + buy-and-hold "
            "baseline.  Tests whether the strategy clears the verdict "
            "tree without parameter tuning."
        ),
        "split_holdout_start": entry["holdout_start"],
        "symbols": symbols,
        "n_trades": int(headline_result.metrics.total_trades),
        "sharpe": float(headline_result.metrics.sharpe_ratio),
        "cpcv": {
            "n_paths": int(cpcv_result.n_paths),
            "n_blocks": int(cpcv_config.n_blocks),
            "k_held_out": int(cpcv_config.k_held_out),
            "purge_periods": int(cpcv_config.purge_periods),
            "embargo_periods": int(cpcv_config.embargo_periods),
            "sharpe_distribution": cpcv_result.sharpe_distribution,
        },
        "dsr_validation": float(dsr_validation_value),
        # ── Forensic extras (not v1 required, but recorded so the row
        #    is self-describing for audit).
        "n_trials": int(n_trials),
    }


def _run_strategy_dev_cpcv(
    strategy_id: str,
    timeframe: str,
    balance: float,
) -> DevCpcvResult:
    """Execute the dev_cpcv flow for a single strategy.

    Sequence:
      1. CPCV on dev window (run_cpcv).
      2. Engine.run on the full dev window — produces the headline
         observed Sharpe and the trade count.  Per-bar engine returns
         are NOT used downstream; DSR / MinTRL / verdict consume the
         per-block CPCV concat instead (dsr_validation contract).
      3. Buy-and-hold baseline on the strategy's primary-symbol dev
         frame.
      4. Concatenate non-empty `cpcv_result.per_block_returns` —
         this single series feeds DSR, MinTRL, and verdict.
      5. DSR via `deflated_sharpe` with n_trials=RESCUE_TRIAL_BUDGET
         (20), explicitly NOT `count_trials_for_dsr`.
      6. MinTRL via `min_track_record_length`.
      7. Verdict via `compute_verdict` (which re-runs deflated_sharpe
         internally with the same n_trials — the duplication is
         deliberate: it lets us record the standalone DSR value
         without coupling the verdict surface to a precomputed
         result, and the cost is microseconds).
      8. Build the v1 full_cpcv row.  ALL prior steps must succeed
         before `trials.record_trial` is called — that's the
         atomicity guarantee.
      9. Append to trials.log.
     10. Print the verdict block.
     11. Return DevCpcvResult.

    Atomicity: trials.record_trial is the only mutation, and it is
    the last step before return.  Any earlier raise leaves trials.log
    unmodified.  `record_trial` itself validates the row dict before
    the append_jsonl call, so a malformed row also leaves no trace.
    """
    factory = lambda: make_strategies(timeframe)[strategy_id]
    sample = factory()
    primary_symbol = sample.symbol

    # 1. CPCV.
    cpcv_config = CPCVConfig()
    cpcv_result = run_cpcv(
        strategy_id=strategy_id,
        params={},
        config=cpcv_config,
        strategy_factory=factory,
    )

    # 2. Headline engine.run on the full dev window.
    dev_df = _holdout.load_dev(strategy_id)
    is_multi_symbol = "symbol" in dev_df.columns
    if is_multi_symbol:
        primary_dev_df, universe_dfs = _split_dev_multi_symbol(
            dev_df, primary_symbol,
        )
    else:
        primary_dev_df = dev_df
        universe_dfs = None

    engine = BacktestEngine(
        initial_balance=balance,
        warm_up_candles=WARM_UP,
        verbose=False,
    )
    headline_result = engine.run(
        primary_dev_df,
        factory(),
        period_label=f"dev_cpcv-{strategy_id}",
        universe_dfs=universe_dfs,
    )

    observed_sharpe = float(headline_result.metrics.sharpe_ratio)
    total_trades = int(headline_result.metrics.total_trades)

    # 3. Baseline.
    baseline_result = buy_and_hold_sharpe(primary_dev_df)
    baseline_sharpe = float(baseline_result.sharpe)

    # 4. DSR / MinTRL / verdict input series.
    #    Per `dsr.py`'s contract for `dsr_validation`: concatenate
    #    `CPCVResult.per_block_returns`, skipping empty arrays.
    #    `min_track_record_length` documents the same input
    #    convention; `compute_verdict` plumbs `returns` straight
    #    through to both, so the same series feeds all three.
    #    Engine-bar returns (equity_curve.pct_change()) are NOT used
    #    here — that's the dsr_holdout convention, which dev_cpcv
    #    isn't.
    returns_for_dsr = _concat_per_block_returns(cpcv_result)

    # 5. DSR — n_trials=RESCUE_TRIAL_BUDGET, explicit.
    n_trials = RESCUE_TRIAL_BUDGET
    dsr_result = deflated_sharpe(
        sr_candidate=observed_sharpe,
        returns=returns_for_dsr,
        n_trials=n_trials,
    )

    # 6. MinTRL.
    mintrl_result = min_track_record_length(
        sr_candidate=observed_sharpe,
        returns=returns_for_dsr,
    )

    # 7. Verdict.  Pass the same n_trials so the verdict's internal
    #    DSR call agrees with our standalone one.
    verdict = compute_verdict(
        strategy_id=strategy_id,
        sr_candidate=observed_sharpe,
        returns=returns_for_dsr,
        total_trades=total_trades,
        baseline_df=primary_dev_df,
        n_trials=n_trials,
    )

    # 8. Build the row.
    row = _build_full_cpcv_row(
        strategy_id=strategy_id,
        primary_symbol=primary_symbol,
        cpcv_result=cpcv_result,
        cpcv_config=cpcv_config,
        headline_result=headline_result,
        dsr_validation_value=float(dsr_result.dsr),
        n_trials=n_trials,
    )

    # 9. Append (atomic — last side-effect).
    _trials.record_trial(row)

    res = DevCpcvResult(
        strategy_id=strategy_id,
        observed_sharpe=observed_sharpe,
        sr_zero_expected=float(dsr_result.sr_zero_expected),
        dsr_pvalue=float(dsr_result.dsr),
        mintrl=float(mintrl_result.min_trl),
        baseline_sharpe=baseline_sharpe,
        verdict=verdict,
        trial_row=row,
    )

    # 10. Print.
    _print_dev_cpcv_block(res)

    return res


def _run_all_dev_cpcv(
    timeframe: str,
    balance: float,
) -> dict:
    """Iterate every strategy in `make_strategies` through the
    dev_cpcv flow.  Per-strategy failures are logged and the result
    entry is `None` for that strategy; one bad strategy doesn't kill
    the run."""
    results: dict = {}
    for name in make_strategies(timeframe).keys():
        logger.info(f"[DevCpcv] Running: {name}")
        try:
            results[name] = _run_strategy_dev_cpcv(
                strategy_id=name, timeframe=timeframe, balance=balance,
            )
        except Exception as e:
            logger.error(f"[DevCpcv] {name} failed: {e}")
            results[name] = None
    return results


if __name__ == "__main__":
    run_all()
