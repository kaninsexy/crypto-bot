"""
backtest/runner.py — Orchestrates the full Phase C backtesting pipeline.

WHAT IT DOES
────────────
1. Downloads 12 months of 1h OHLCV data for the configured symbol from Binance
   (no API key needed — public endpoint).

2. Splits data into two periods:
     In-sample  (IS) : first 9 months  → strategy development / optimisation
     Out-of-sample (OOS): last 3 months → honest performance estimate

3. For each of the 6 strategies, runs BacktestEngine on both IS and OOS.
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

from backtest.engine import BacktestEngine, BacktestResult
from backtest.report import print_comparison_table, print_period_report

# Strategy factories — each returns a fresh, reset instance
from strategies.dca import DCAStrategy
from strategies.supertrend import SupertrendStrategy
from strategies.mean_reversion import MeanReversionStrategy
from strategies.grid_trading import GridTradingStrategy
from strategies.breakout import BreakoutStrategy
from strategies.trend_following import TrendFollowingStrategy


# ── Config from env ──────────────────────────────────────────────────────────

SYMBOL        = os.getenv("BACKTEST_SYMBOL", "BTC/USDT")
TIMEFRAME     = os.getenv("BACKTEST_TF", "1h")
BALANCE       = float(os.getenv("BACKTEST_BALANCE", "10000"))
TOTAL_MONTHS  = int(os.getenv("BACKTEST_MONTHS", "12"))
IS_MONTHS     = int(os.getenv("BACKTEST_SPLIT", "9"))   # in-sample months
OOS_MONTHS    = TOTAL_MONTHS - IS_MONTHS                 # out-of-sample months

# Warm-up: enough candles for the longest indicator in any strategy (~200 for SMA200)
WARM_UP = 220


# ── Strategy factory ─────────────────────────────────────────────────────────

def make_strategies(symbol: str, timeframe: str) -> dict:
    """
    Return a dict of  name → strategy_instance  with default parameters.
    Each call returns fresh instances (no shared state).
    """
    return {
        "DCA": DCAStrategy(
            symbol=symbol, timeframe=timeframe,
            base_amount=200.0,
            deviation_pct=2.0,
            max_safety_orders=5,
            stop_loss_pct=0.12,
            compound=True,
        ),
        "Supertrend": SupertrendStrategy(
            symbol=symbol, timeframe=timeframe,
        ),
        "MeanReversion": MeanReversionStrategy(
            symbol=symbol, timeframe=timeframe,
            ema_filter=True,   # Only buy dips when EMA50 > EMA200 (uptrend)
            ema_fast=50, ema_slow=200,
        ),
        "GridTrading": GridTradingStrategy(
            symbol=symbol, timeframe=timeframe,
            grid_levels=10,
            usdt_per_trade=200.0,
            recalibrate_every=24,
        ),
        "Breakout": BreakoutStrategy(
            symbol=symbol, timeframe=timeframe,
            mtf_enabled=False,  # Backtest has 1h data only — no 4h feed available
        ),
        "TrendFollowing": TrendFollowingStrategy(
            symbol=symbol, timeframe=timeframe,
        ),
    }


# ── Historical data download ─────────────────────────────────────────────────

def download_history(
    symbol: str,
    timeframe: str = "1h",
    months: int = 12,
) -> pd.DataFrame:
    """
    Download `months` of OHLCV data from Binance (public, no API key needed).

    Binance limits each fetch to 1000 candles. For 1h data over 12 months that
    is ~8 760 candles, so we page backwards in batches of 1000.

    Returns a DataFrame indexed by UTC timestamp, columns: open/high/low/close/volume.
    """
    logger.info(f"Downloading {months}mo of {timeframe} data for {symbol} from Binance...")

    exchange = ccxt.binance({"enableRateLimit": True})

    # Calculate start timestamp
    since_dt = datetime.now(timezone.utc) - timedelta(days=int(months * 30.44))
    since_ms  = int(since_dt.timestamp() * 1000)

    all_candles = []
    batch_size  = 1000

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
    symbol: str = SYMBOL,
    timeframe: str = TIMEFRAME,
    balance: float = BALANCE,
    total_months: int = TOTAL_MONTHS,
    is_months: int = IS_MONTHS,
) -> dict:
    """
    Run the full Phase C backtesting pipeline.

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

    print("\n" + "═" * 70)
    print(f"  PHASE C — BACKTESTING ENGINE")
    print(f"  Symbol : {symbol}  |  Timeframe : {timeframe}")
    print(f"  Balance: ${balance:,.0f}  |  Period: {total_months}mo  (IS:{is_months}mo / OOS:{total_months-is_months}mo)")
    print("═" * 70 + "\n")

    # 1. Download data
    df_full = download_history(symbol, timeframe, months=total_months)

    # 2. Split
    df_is, df_oos = split_df(df_full, is_months)

    engine = BacktestEngine(
        initial_balance=balance,
        warm_up_candles=WARM_UP,
        verbose=False,
    )

    results = {}

    # 3. Run each strategy on both periods
    for name, _ in make_strategies(symbol, timeframe).items():
        print(f"\n{'─'*70}")
        print(f"  Running: {name}")
        print(f"{'─'*70}")

        # Fresh instances for each period to avoid state bleed
        strat_is  = make_strategies(symbol, timeframe)[name]
        strat_oos = make_strategies(symbol, timeframe)[name]

        try:
            r_is = engine.run(df_is, strat_is, period_label="in-sample")
        except Exception as e:
            logger.error(f"{name} IS failed: {e}")
            r_is = None

        try:
            r_oos = engine.run(df_oos, strat_oos, period_label="out-of-sample")
        except Exception as e:
            logger.error(f"{name} OOS failed: {e}")
            r_oos = None

        results[name] = {"is": r_is, "oos": r_oos}

    # 4. Print reports
    print_comparison_table(results, symbol=symbol, timeframe=timeframe)

    # 5. Save results to JSON for the dashboard
    _save_results_json(results, symbol=symbol, timeframe=timeframe)

    # 6. Save CSV + text report for easy review
    _save_report_files(results, symbol=symbol, timeframe=timeframe)

    return results


def _save_results_json(results: dict, symbol: str, timeframe: str) -> None:
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
            entry = {}
            if r.get("is") and r["is"].metrics:
                entry["is"] = _metrics_to_dict(r["is"].metrics)
            if r.get("oos") and r["oos"].metrics:
                entry["oos"] = _metrics_to_dict(r["oos"].metrics)
            serialized[name] = entry

        write_backtest_results({
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "symbol":     symbol,
            "timeframe":  timeframe,
            "strategies": serialized,
        })
        logger.info("Backtest results saved to dashboard/data/backtest_results.json")
    except Exception as e:
        logger.warning(f"Could not save backtest results to dashboard: {e}")


def _save_report_files(results: dict, symbol: str, timeframe: str) -> None:
    """
    Save two files to backtest/reports/ after each run:
      - YYYY-MM-DD_HHMMSS_summary.csv   — comparison table (open in Excel/Numbers)
      - YYYY-MM-DD_HHMMSS_full.txt      — full text report (same as terminal output)
    """
    try:
        reports_dir = Path(__file__).parent / "reports"
        reports_dir.mkdir(exist_ok=True)

        ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")

        # ── CSV summary ───────────────────────────────────────────────────────
        csv_path = reports_dir / f"{ts}_summary.csv"
        rows = []
        for name, r in results.items():
            row = {"Strategy": name, "Symbol": symbol, "Timeframe": timeframe}
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
            f"BACKTEST REPORT — {symbol} [{timeframe}]",
            f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "=" * 70,
            "",
            f"{'Strategy':<16} {'IS Ret%':>9} {'OOS Ret%':>9} {'MaxDD%':>8} "
            f"{'Sharpe':>8} {'WinRate':>8} {'Trades':>7} {'PF':>7}",
            "-" * 70,
        ]
        for name, r in sorted(results.items(),
                               key=lambda x: (x[1].get("oos") and x[1]["oos"].metrics.sharpe_ratio) or -999,
                               reverse=True):
            oos = r.get("oos") and r["oos"].metrics
            ins = r.get("is")  and r["is"].metrics
            if oos:
                lines.append(
                    f"{name:<16} {ins.total_return_pct if ins else 0:>+8.2f}%"
                    f" {oos.total_return_pct:>+8.2f}%"
                    f" {-oos.max_drawdown_pct:>+7.2f}%"
                    f" {oos.sharpe_ratio:>8.3f}"
                    f" {oos.win_rate_pct:>7.1f}%"
                    f" {oos.total_trades:>7}"
                    f" {oos.profit_factor:>7.3f}" if oos.profit_factor < 9999 else f" {'inf':>7}"
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
