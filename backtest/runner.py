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

import os
import sys
import time
import ccxt
import pandas as pd
from datetime import datetime, timezone, timedelta
from loguru import logger
from pathlib import Path

# ── Allow running as  python -m backtest.runner  from crypto_bot/ ───────────
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from backtest.engine import BacktestEngine, BacktestResult
from backtest.cache import load_or_download_ohlcv
from backtest.report import print_comparison_table, print_period_report

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
) -> dict:
    """
    Run the full Phase C backtesting pipeline across the multi-symbol universe.

    Each strategy runs on its assigned pair from `config.STRATEGY_SYMBOLS`.
    DualMomentum rotates across its internal universe (BTC/ETH/BNB) using
    per-symbol OHLCV pulled from the download set.

    Returns a dict:
        {
          strategy_name: {
            "is":  BacktestResult,
            "oos": BacktestResult,
          }
        }
    """
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


if __name__ == "__main__":
    run_all()
