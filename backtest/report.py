"""
backtest/report.py — Backtest results formatter and comparison printer.

Produces two outputs:

1. Per-strategy detailed report (IS + OOS side-by-side)
2. Comparison table: all 6 strategies ranked by OOS Sharpe ratio

Colour coding (ANSI terminal):
  Green  → positive / good
  Red    → negative / bad
  Yellow → neutral / warning
"""

from __future__ import annotations
from typing import Optional
from backtest.engine import BacktestResult, BacktestMetrics


# ── ANSI colours ─────────────────────────────────────────────────────────────

GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"


def _pct_colour(val: float, good_is_positive: bool = True) -> str:
    if good_is_positive:
        colour = GREEN if val > 0 else (RED if val < 0 else YELLOW)
    else:
        colour = RED if val > 0 else (GREEN if val < 0 else YELLOW)
    sign = "+" if val > 0 else ""
    return f"{colour}{sign}{val:.2f}%{RESET}"


def _val_colour(val: float, good_is_positive: bool = True) -> str:
    if good_is_positive:
        colour = GREEN if val > 0 else (RED if val < 0 else YELLOW)
    else:
        colour = RED if val > 0 else (GREEN if val < 0 else YELLOW)
    sign = "+" if val > 0 else ""
    return f"{colour}{sign}{val:.3f}{RESET}"


# ── Per-strategy detailed block ──────────────────────────────────────────────

def print_period_report(result: BacktestResult) -> None:
    """Print a single-period detailed performance report."""
    m = result.metrics

    print(f"\n  {CYAN}{BOLD}[{result.period_label.upper()}]  "
          f"{m.strategy_name} | {m.symbol}{RESET}")
    print(f"  Period  : {m.start_date} → {m.end_date}  ({m.n_candles} candles)")
    print(f"  Balance : ${10_000:,.0f} → ${10_000 * (1 + m.total_return_pct / 100):,.0f}")

    w = 28
    rows = [
        ("Total Return",    _pct_colour(m.total_return_pct)),
        ("Ann. Return",     _pct_colour(m.annualised_return_pct)),
        ("Max Drawdown",    f"{RED}-{m.max_drawdown_pct:.2f}%{RESET}"),
        ("Sharpe Ratio",    _val_colour(m.sharpe_ratio)),
        ("Calmar Ratio",    _val_colour(m.calmar_ratio)),
        ("Volatility",      f"{m.volatility_pct:.2f}%"),
        ("Total Trades",    str(m.total_trades)),
        ("Win Rate",        f"{m.win_rate_pct:.1f}%"),
        ("Profit Factor",   _val_colour(m.profit_factor)),
        ("Avg Win",         _pct_colour(m.avg_win_pct)),
        ("Avg Loss",        _pct_colour(m.avg_loss_pct)),
        ("Best Trade",      _pct_colour(m.best_trade_pct)),
        ("Worst Trade",     _pct_colour(m.worst_trade_pct)),
        ("Fees Paid",       f"${m.total_fees_usdt:,.2f}"),
    ]
    for label, value in rows:
        print(f"    {label:<{w}} {value}")


# ── Comparison table ─────────────────────────────────────────────────────────

def print_comparison_table(
    results: dict,
    symbol: str,
    timeframe: str,
) -> None:
    """
    Print a side-by-side comparison of all strategies for both IS and OOS.

    results format:
        {strategy_name: {"is": BacktestResult | None, "oos": BacktestResult | None}}
    """

    # ── Per-strategy detail blocks ────────────────────────────────────────
    print("\n" + "═" * 70)
    print(f"  {BOLD}DETAILED RESULTS — {symbol}  [{timeframe}]{RESET}")
    print("═" * 70)

    for name, r in results.items():
        for label, period in [("is", "in-sample"), ("oos", "out-of-sample")]:
            if r.get(label):
                r[label].period_label = period
                print_period_report(r[label])

    # ── Ranked comparison table ───────────────────────────────────────────
    print("\n\n" + "═" * 70)
    print(f"  {BOLD}STRATEGY COMPARISON TABLE — OOS (Out-of-Sample){RESET}")
    print("═" * 70)

    # Collect OOS metrics for ranking
    rows = []
    for name, r in results.items():
        oos = r.get("oos")
        ins = r.get("is")
        m_oos: Optional[BacktestMetrics] = oos.metrics if oos else None
        m_is:  Optional[BacktestMetrics] = ins.metrics  if ins  else None

        if m_oos:
            rows.append((name, m_is, m_oos))

    # Sort by OOS Sharpe (descending)
    rows.sort(key=lambda x: x[2].sharpe_ratio if x[2] else -999, reverse=True)

    # Print header
    col_w = 15
    header = (
        f"  {'Strategy':<16}"
        f"{'IS Ret%':>10}  {'OOS Ret%':>10}"
        f"{'MaxDD%':>9}  {'Sharpe':>8}"
        f"{'WinRate':>9}  {'Trades':>8}"
        f"{'PF':>7}  {'Overfitting':>12}"
    )
    print(header)
    print("  " + "─" * 88)

    for name, m_is, m_oos in rows:
        # Overfitting score: IS return - OOS return (large gap = overfit)
        if m_is:
            overfit_gap = m_is.total_return_pct - m_oos.total_return_pct
            if overfit_gap > 15:
                overfit_str = f"{RED}⚠ -{overfit_gap:.1f}pp{RESET}"
            elif overfit_gap > 5:
                overfit_str = f"{YELLOW}△ -{overfit_gap:.1f}pp{RESET}"
            else:
                overfit_str = f"{GREEN}✓ OK{RESET}"
        else:
            overfit_str = "N/A"

        is_ret_str  = _pct_colour(m_is.total_return_pct)  if m_is  else "N/A"
        oos_ret_str = _pct_colour(m_oos.total_return_pct)
        dd_str      = f"{RED}-{m_oos.max_drawdown_pct:.1f}%{RESET}"
        sharpe_str  = _val_colour(m_oos.sharpe_ratio)
        wr_str      = f"{m_oos.win_rate_pct:.1f}%"
        pf_str      = _val_colour(m_oos.profit_factor) if m_oos.profit_factor < 9999 else "∞"

        print(
            f"  {name:<16}"
            f"{is_ret_str:>18}  {oos_ret_str:>18}"
            f"{dd_str:>18}  {sharpe_str:>17}"
            f"  {wr_str:>7}  {m_oos.total_trades:>6}"
            f"  {pf_str:>16}  {overfit_str}"
        )

    print("\n" + "═" * 70)

    # ── Winner callout ────────────────────────────────────────────────────
    if rows:
        best_name, _, best_oos = rows[0]
        print(f"\n  {BOLD}Best OOS Sharpe:{RESET} {GREEN}{best_name}{RESET} "
              f"({_val_colour(best_oos.sharpe_ratio)} Sharpe, "
              f"{_pct_colour(best_oos.total_return_pct)} return, "
              f"max DD {RED}-{best_oos.max_drawdown_pct:.1f}%{RESET})")

    # ── Risk-adjusted ranking note ────────────────────────────────────────
    print(f"""
  {BOLD}HOW TO READ THIS TABLE{RESET}
  ─────────────────────
  IS Ret%   — In-sample return (training period)
  OOS Ret%  — Out-of-sample return (honest forward test)
  MaxDD%    — Largest peak-to-trough drawdown in OOS period
  Sharpe    — Annualised Sharpe ratio (higher is better; >1 is good)
  WinRate   — % of closed trades that were profitable
  Trades    — Total closed trades in OOS period
  PF        — Profit Factor = Gross profit / Gross loss (>1.5 is good)
  Overfitting — IS vs OOS return gap (>15pp gap = possible overfit)
""")
