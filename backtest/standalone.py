"""
backtest/standalone.py — Self-contained Phase C backtesting engine.

Works with ONLY numpy + pandas (no ccxt / ta / loguru needed).
When dependencies are installed in production, use runner.py instead.

WHAT IT DOES
────────────
1. Generates realistic synthetic OHLCV data using Geometric Brownian Motion
   with alternating trending + ranging + crash regimes to mimic crypto markets.

2. Implements all 6 strategy signal logics in pure pandas/numpy.

3. Simulates trades with:
   - Binance fees: 0.02% limit / 0.04% market
   - Slippage:     0.02% limit / 0.05% market
   - Position sizing from strategy metadata or risk %

4. Splits into 9-month in-sample / 3-month out-of-sample.

5. Computes: Total Return, Ann. Return, Max Drawdown, Sharpe, Calmar,
             Win Rate, Profit Factor, Avg Win/Loss, Best/Worst trade.

6. Prints a ranked comparison table.

USAGE
─────
  cd crypto_bot
  python -m backtest.standalone

  Optional overrides (env vars):
    BACKTEST_BALANCE=10000
    BACKTEST_SEED=42
    BACKTEST_SYMBOL=BTC/USDT
"""

from __future__ import annotations
import os
import sys
import math
import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime, timedelta, timezone


# ── Config ───────────────────────────────────────────────────────────────────
BALANCE     = float(os.getenv("BACKTEST_BALANCE", "10000"))
SEED        = int(os.getenv("BACKTEST_SEED", "42"))
SYMBOL      = os.getenv("BACKTEST_SYMBOL", "BTC/USDT")
TIMEFRAME   = "1h"
TOTAL_HOURS = 365 * 24          # 1 year of hourly data
IS_HOURS    = 9 * 30 * 24       # 9 months  in-sample
OOS_HOURS   = TOTAL_HOURS - IS_HOURS  # 3 months out-of-sample
WARM_UP     = 220               # Candles for indicator warm-up
FEE_LIMIT   = 0.0002            # 0.02% maker
FEE_MARKET  = 0.0004            # 0.04% taker
SLIP_LIMIT  = 0.0002
SLIP_MARKET = 0.0005


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 1 — SYNTHETIC DATA GENERATOR
# ═══════════════════════════════════════════════════════════════════════════

def generate_ohlcv(
    n_hours: int = TOTAL_HOURS,
    start_price: float = 65_000.0,
    annual_drift: float = 0.30,
    annual_vol: float   = 0.65,
    seed: int = SEED,
) -> pd.DataFrame:
    """
    Generate realistic synthetic hourly OHLCV for a BTC-like asset.

    Uses Geometric Brownian Motion with regime switching:
      - Trending up (60% weight) : higher drift, moderate vol
      - Ranging sideways (25%)   : near-zero drift, lower vol
      - Crash / drawdown (15%)   : negative drift, high vol

    High/Low derived from close using realistic intra-candle ranges.
    Volume correlated inversely with price changes (fear = higher volume).
    """
    rng = np.random.default_rng(seed)

    dt = 1 / (365.25 * 24)    # 1 hour expressed as fraction of year

    # ── Regime switching ──────────────────────────────────────────────────
    regimes = rng.choice(
        ["bull", "range", "bear"],
        size=n_hours,
        p=[0.60, 0.25, 0.15],
    )
    # Smooth regime transitions (don't flip every candle)
    for i in range(1, n_hours):
        if rng.random() < 0.97:
            regimes[i] = regimes[i - 1]

    drift_map = {"bull": annual_drift, "range": 0.02, "bear": -0.60}
    vol_map   = {"bull": annual_vol * 0.9, "range": annual_vol * 0.6, "bear": annual_vol * 1.4}

    # ── GBM price path ────────────────────────────────────────────────────
    closes = [start_price]
    for i in range(1, n_hours):
        mu    = drift_map[regimes[i]]
        sigma = vol_map[regimes[i]]
        z     = rng.standard_normal()
        ret   = math.exp((mu - 0.5 * sigma ** 2) * dt + sigma * math.sqrt(dt) * z)
        closes.append(closes[-1] * ret)

    closes = np.array(closes)

    # ── OHLC from close ───────────────────────────────────────────────────
    # Intra-candle range ≈ ATR proxy, scaled by volatility regime
    range_pct = np.where(
        np.array(regimes) == "bear",
        rng.uniform(0.006, 0.018, n_hours),
        rng.uniform(0.003, 0.012, n_hours),
    )
    half_range = closes * range_pct / 2

    opens  = closes + rng.uniform(-0.3, 0.3, n_hours) * half_range
    highs  = np.maximum(opens, closes) + rng.uniform(0.0, 1.0, n_hours) * half_range
    lows   = np.minimum(opens, closes) - rng.uniform(0.0, 1.0, n_hours) * half_range
    lows   = np.maximum(lows, closes * 0.01)    # floor at 1% of close

    # ── Volume (anti-correlated with price on crashes, higher on big moves) ─
    base_vol   = 1000.0
    price_chg  = np.abs(np.diff(closes, prepend=closes[0]) / closes)
    regime_vol = np.where(np.array(regimes) == "bear", 2.5,
                 np.where(np.array(regimes) == "range", 0.7, 1.0))
    volume     = base_vol * (1 + price_chg * 10) * regime_vol * rng.uniform(0.6, 1.4, n_hours)

    # ── Build DataFrame ───────────────────────────────────────────────────
    start_dt = datetime(2024, 1, 1, tzinfo=timezone.utc)
    timestamps = [start_dt + timedelta(hours=i) for i in range(n_hours)]

    df = pd.DataFrame({
        "open":   opens,
        "high":   highs,
        "low":    lows,
        "close":  closes,
        "volume": volume,
    }, index=pd.DatetimeIndex(timestamps, name="timestamp"))

    return df


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 2 — PURE-PANDAS INDICATOR LIBRARY
# ═══════════════════════════════════════════════════════════════════════════

def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()

def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period).mean()

def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain  = delta.clip(lower=0)
    loss  = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-9)
    return 100 - 100 / (1 + rs)

def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    prev_c = c.shift(1)
    tr = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, adjust=False).mean()

def bollinger_bands(series: pd.Series, period: int = 20, n_std: float = 2.0):
    mid   = sma(series, period)
    std   = series.rolling(period).std()
    upper = mid + n_std * std
    lower = mid - n_std * std
    return upper, mid, lower

def macd(series: pd.Series, fast=12, slow=26, signal_period=9):
    macd_line   = ema(series, fast) - ema(series, slow)
    signal_line = ema(macd_line, signal_period)
    histogram   = macd_line - signal_line
    return macd_line, signal_line, histogram

def supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0):
    """Return supertrend direction: +1 = bullish, -1 = bearish."""
    atr_val = atr(df, period)
    hl2     = (df["high"] + df["low"]) / 2
    upper   = hl2 + multiplier * atr_val
    lower   = hl2 - multiplier * atr_val
    close   = df["close"]

    direction = pd.Series(index=df.index, dtype=float)
    final_upper = upper.copy()
    final_lower = lower.copy()

    for i in range(1, len(df)):
        prev_upper = final_upper.iloc[i - 1]
        prev_lower = final_lower.iloc[i - 1]
        prev_close = close.iloc[i - 1]
        cur_close  = close.iloc[i]

        # Tighten bands only
        final_upper.iloc[i] = (upper.iloc[i] if upper.iloc[i] < prev_upper or prev_close > prev_upper
                                else prev_upper)
        final_lower.iloc[i] = (lower.iloc[i] if lower.iloc[i] > prev_lower or prev_close < prev_lower
                                else prev_lower)

        prev_dir = direction.iloc[i - 1] if i > 1 else 1
        if prev_dir == 1 and cur_close < final_lower.iloc[i]:
            direction.iloc[i] = -1
        elif prev_dir == -1 and cur_close > final_upper.iloc[i]:
            direction.iloc[i] = 1
        else:
            direction.iloc[i] = prev_dir

    return direction.fillna(1)


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 3 — STRATEGY SIGNAL GENERATORS (pure pandas)
# ═══════════════════════════════════════════════════════════════════════════

def signals_dca(df: pd.DataFrame) -> pd.Series:
    """
    DCA: Buy on each X% deviation below the prior swing high.
    Sell when price rises 3% above avg entry. Reset after sell.

    Returns series: +1=BUY, -1=SELL, 0=HOLD
    """
    close   = df["close"]
    rsi_val = rsi(close, 14)
    signals = pd.Series(0, index=df.index)

    avg_entry   = None
    in_position = False
    swing_high  = None
    safety_count = 0
    MAX_SAFETY  = 5
    DEVIATION   = 0.02   # 2% drop triggers buy
    TP_PCT      = 0.04   # 4% profit target

    for i in range(WARM_UP, len(df)):
        c = close.iloc[i]

        # Update swing high when not in position
        if not in_position:
            if swing_high is None or c > swing_high:
                swing_high = c

        if in_position:
            # Take profit
            if c >= avg_entry * (1 + TP_PCT):
                signals.iloc[i] = -1
                in_position = False
                avg_entry   = None
                swing_high  = None
                safety_count = 0
            # Add safety order on further drops
            elif safety_count < MAX_SAFETY and swing_high and c < avg_entry * (1 - DEVIATION):
                signals.iloc[i] = 1   # DCA safety add
                avg_entry = (avg_entry + c) / 2
                safety_count += 1
        else:
            # Initial entry: price dropped >= DEVIATION from swing high AND RSI < 42
            if swing_high and c < swing_high * (1 - DEVIATION) and rsi_val.iloc[i] < 42:
                signals.iloc[i] = 1
                in_position = True
                avg_entry   = c
                safety_count = 0

    return signals


def signals_supertrend(df: pd.DataFrame) -> pd.Series:
    """
    Supertrend: BUY when direction flips +1, SELL when flips -1.
    """
    close  = df["close"]
    st_dir = supertrend(df, period=10, multiplier=3.0)

    signals = pd.Series(0, index=df.index)
    in_position = False

    for i in range(WARM_UP, len(df)):
        cur_dir  = st_dir.iloc[i]
        prev_dir = st_dir.iloc[i - 1]

        if not in_position and cur_dir == 1 and prev_dir == -1:
            signals.iloc[i] = 1
            in_position = True
        elif in_position and cur_dir == -1 and prev_dir == 1:
            signals.iloc[i] = -1
            in_position = False

    return signals


def signals_mean_reversion(df: pd.DataFrame) -> pd.Series:
    """
    MeanReversion: BUY at lower BB + RSI < 35. SELL at upper BB or RSI > 65.
    """
    close = df["close"]
    bb_up, bb_mid, bb_lo = bollinger_bands(close, 20, 2.0)
    rsi_val = rsi(close, 14)
    ema200  = ema(close, 200)

    signals     = pd.Series(0, index=df.index)
    in_position = False
    entry_price = None

    for i in range(WARM_UP, len(df)):
        c     = close.iloc[i]
        r     = rsi_val.iloc[i]
        lo    = bb_lo.iloc[i]
        hi    = bb_up.iloc[i]
        trend = ema200.iloc[i]

        if not in_position:
            # Enter: price at lower BB, oversold RSI, price above long-term trend
            if c <= lo * 1.005 and r < 35 and c > trend * 0.90:
                signals.iloc[i] = 1
                in_position = True
                entry_price = c
        else:
            # Exit: price at upper BB, or RSI overbought, or 2% stop loss
            if c >= hi * 0.995 or r > 65 or c < entry_price * 0.97:
                signals.iloc[i] = -1
                in_position = False
                entry_price = None

    return signals


def signals_grid(df: pd.DataFrame) -> pd.Series:
    """
    Grid Trading: BB range + ATR step.
    BUY when price drops to a new lower grid level.
    SELL when price rises one step above entry level.
    """
    close     = df["close"]
    high_s    = df["high"]
    low_s     = df["low"]
    atr_val   = atr(df, 14)
    bb_up, _, bb_lo = bollinger_bands(close, 20, 2.0)

    signals        = pd.Series(0, index=df.index)
    in_position    = False
    last_buy_level = None
    grid_step      = None

    for i in range(WARM_UP, len(df)):
        c    = close.iloc[i]
        step = atr_val.iloc[i] * 0.75
        lo   = bb_lo.iloc[i]
        hi   = bb_up.iloc[i]
        atr_pct = atr_val.iloc[i] / c * 100

        # Trend guard: pause if ATR% > 2.5
        if atr_pct > 2.5:
            continue

        if not in_position:
            # Build grid level: nearest whole step below price relative to BB lower
            if pd.notna(lo) and pd.notna(step) and step > 0:
                steps_from_lo = int((c - lo) / step)
                level = lo + steps_from_lo * step
                if last_buy_level is None or level < last_buy_level:
                    signals.iloc[i] = 1
                    in_position = True
                    last_buy_level = level
                    grid_step = step
        else:
            # Sell target: entry level + one step
            if grid_step and c >= last_buy_level + grid_step:
                signals.iloc[i] = -1
                in_position = False
            elif c > hi:
                # Broke above BB — sell
                signals.iloc[i] = -1
                in_position = False

    return signals


def signals_breakout(df: pd.DataFrame) -> pd.Series:
    """
    Breakout: BUY when close breaks above 20-period high with volume surge.
    SELL on EMA cross-under or ATR-based stop.
    """
    close   = df["close"]
    volume  = df["volume"]
    ema50   = ema(close, 50)
    ema20   = ema(close, 20)
    atr_val = atr(df, 14)
    vol_sma = sma(volume, 20)

    high_20 = close.rolling(20).max().shift(1)   # yesterday's 20-period high

    signals     = pd.Series(0, index=df.index)
    in_position = False
    entry_price = None

    for i in range(WARM_UP, len(df)):
        c       = close.iloc[i]
        v       = volume.iloc[i]
        v_avg   = vol_sma.iloc[i]
        h20     = high_20.iloc[i]
        e50     = ema50.iloc[i]
        e20     = ema20.iloc[i]
        a_val   = atr_val.iloc[i]

        if not in_position:
            # Breakout entry: close above 20-period high with 1.5x volume surge
            if pd.notna(h20) and c > h20 and v > v_avg * 1.5 and c > e50:
                signals.iloc[i] = 1
                in_position = True
                entry_price = c
        else:
            # Exit: price drops below EMA20, or ATR trailing stop
            stop = entry_price - 2 * a_val if entry_price else c * 0.95
            if c < e20 or c < stop:
                signals.iloc[i] = -1
                in_position = False
                entry_price = None

    return signals


def signals_trend_following(df: pd.DataFrame) -> pd.Series:
    """
    TrendFollowing: Golden cross (EMA50 > EMA200) + RSI > 50 = BUY.
    Death cross or RSI < 40 = SELL.
    """
    close   = df["close"]
    ema50   = ema(close, 50)
    ema200  = ema(close, 200)
    rsi_val = rsi(close, 14)

    signals     = pd.Series(0, index=df.index)
    in_position = False

    for i in range(WARM_UP, len(df)):
        e50  = ema50.iloc[i]
        e200 = ema200.iloc[i]
        r    = rsi_val.iloc[i]
        prev_e50  = ema50.iloc[i - 1]
        prev_e200 = ema200.iloc[i - 1]

        if not in_position:
            # Golden cross entry + RSI confirmation
            if e50 > e200 and prev_e50 <= prev_e200 and r > 50:
                signals.iloc[i] = 1
                in_position = True
        else:
            # Death cross exit or RSI weakness
            if (e50 < e200 and prev_e50 >= prev_e200) or r < 40:
                signals.iloc[i] = -1
                in_position = False

    return signals


STRATEGY_FUNCS = {
    "DCA":           signals_dca,
    "Supertrend":    signals_supertrend,
    "MeanReversion": signals_mean_reversion,
    "GridTrading":   signals_grid,
    "Breakout":      signals_breakout,
    "TrendFollowing":signals_trend_following,
}

# Per-trade USDT sizing (strategies with explicit sizing)
STRATEGY_USDT = {
    "DCA":       200.0,
    "GridTrading": 200.0,
}
DEFAULT_RISK_PCT = 0.02   # 2% of balance for other strategies


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 4 — TRADE SIMULATOR
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Trade:
    entry_price: float
    exit_price:  float
    quantity:    float
    cost:        float
    pnl:         float
    pnl_pct:     float
    fees_paid:   float
    entry_ts:    object
    exit_ts:     object
    exit_reason: str


@dataclass
class SimResult:
    trades:        list
    equity_curve:  pd.Series
    final_balance: float
    total_fees:    float


def simulate(
    df: pd.DataFrame,
    signals: pd.Series,
    initial_balance: float = BALANCE,
    usdt_per_trade: Optional[float] = None,
    risk_pct: float = DEFAULT_RISK_PCT,
) -> SimResult:
    """
    Simulate trading based on a signal series (+1=BUY, -1=SELL, 0=HOLD).

    Position sizing:
      - If usdt_per_trade is set: use fixed USDT per trade
      - Otherwise: use risk_pct of current balance

    Fees & slippage applied on entry/exit.
    """
    balance   = initial_balance
    position  = None   # dict when in a position
    trades    = []
    equity    = {}
    total_fees = 0.0

    for i in range(len(df)):
        ts    = df.index[i]
        close = float(df["close"].iloc[i])
        sig   = int(signals.iloc[i])

        if sig == 1 and position is None:
            # ── BUY ────────────────────────────────────────────────────
            fill = close * (1 + SLIP_LIMIT)
            cost = min(usdt_per_trade or balance * risk_pct, balance)
            if cost < 1:
                equity[ts] = balance
                continue
            fee      = cost * FEE_LIMIT
            cost_net = cost - fee     # what we actually spend on the asset
            qty      = cost_net / fill
            balance -= cost
            total_fees += fee

            position = {
                "entry":   fill,
                "qty":     qty,
                "cost":    cost_net,
                "ts_in":   ts,
            }

        elif sig == -1 and position is not None:
            # ── SELL ───────────────────────────────────────────────────
            fill     = close * (1 - SLIP_LIMIT)
            proceeds = fill * position["qty"]
            fee      = proceeds * FEE_LIMIT
            net      = proceeds - fee
            pnl      = net - position["cost"]
            pnl_pct  = pnl / position["cost"] * 100
            total_fees += fee

            balance += net

            trades.append(Trade(
                entry_price=position["entry"],
                exit_price=fill,
                quantity=position["qty"],
                cost=position["cost"],
                pnl=pnl,
                pnl_pct=pnl_pct,
                fees_paid=fee,
                entry_ts=position["ts_in"],
                exit_ts=ts,
                exit_reason="signal",
            ))
            position = None

        # Current equity = cash + mark-to-market of open position
        if position is not None:
            market_val = close * position["qty"]
            equity[ts] = balance + market_val
        else:
            equity[ts] = balance

    # Force close open position at period end
    if position is not None:
        last_close = float(df["close"].iloc[-1])
        fill  = last_close * (1 - SLIP_MARKET)
        proceeds = fill * position["qty"]
        fee  = proceeds * FEE_MARKET
        net  = proceeds - fee
        pnl  = net - position["cost"]
        pnl_pct = pnl / position["cost"] * 100
        total_fees += fee
        balance += net
        trades.append(Trade(
            entry_price=position["entry"],
            exit_price=fill,
            quantity=position["qty"],
            cost=position["cost"],
            pnl=pnl,
            pnl_pct=pnl_pct,
            fees_paid=fee,
            entry_ts=position["ts_in"],
            exit_ts=df.index[-1],
            exit_reason="period_end",
        ))
        # Update last equity
        if equity:
            last_ts = list(equity.keys())[-1]
            equity[last_ts] = balance

    return SimResult(
        trades=trades,
        equity_curve=pd.Series(equity),
        final_balance=balance,
        total_fees=total_fees,
    )


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 5 — METRICS ENGINE
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Metrics:
    strategy:          str
    period:            str
    start:             str
    end:               str
    n_candles:         int
    total_return_pct:  float
    ann_return_pct:    float
    max_drawdown_pct:  float
    sharpe:            float
    calmar:            float
    volatility_pct:    float
    total_trades:      int
    win_rate_pct:      float
    profit_factor:     float
    avg_win_pct:       float
    avg_loss_pct:      float
    best_trade_pct:    float
    worst_trade_pct:   float
    total_fees:        float
    final_equity:      float


def compute_metrics(
    result: SimResult,
    df: pd.DataFrame,
    strategy: str,
    period: str,
    initial_balance: float = BALANCE,
) -> Metrics:
    eq = result.equity_curve
    trades = result.trades

    n = len(eq)
    hours_per_year = 365.25 * 24
    n_hours = n
    years   = n_hours / hours_per_year

    start_eq = initial_balance
    end_eq   = result.final_balance

    total_return = (end_eq - start_eq) / start_eq * 100
    ann_return   = ((end_eq / max(start_eq, 1e-9)) ** (1 / max(years, 1e-9)) - 1) * 100 if years > 0 else 0

    # Max drawdown
    running_max = eq.cummax()
    drawdowns   = (eq - running_max) / running_max * 100
    max_dd      = float(drawdowns.min())   # negative

    # Sharpe
    ret_series  = eq.pct_change().dropna()
    ann_vol     = float(ret_series.std() * math.sqrt(hours_per_year)) * 100
    sharpe      = ann_return / ann_vol if ann_vol > 0 else 0

    # Calmar
    calmar = ann_return / abs(max_dd) if max_dd != 0 else 0

    # Trade stats
    n_trades = len(trades)
    wins   = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]

    win_rate = len(wins) / n_trades * 100 if n_trades else 0
    gross_profit = sum(t.pnl for t in wins)
    gross_loss   = abs(sum(t.pnl for t in losses))
    pf = gross_profit / gross_loss if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0)

    pnl_pcts = [t.pnl_pct for t in trades] if trades else [0]
    avg_win  = float(np.mean([t.pnl_pct for t in wins]))  if wins   else 0
    avg_loss = float(np.mean([t.pnl_pct for t in losses])) if losses else 0
    best     = max(pnl_pcts)
    worst    = min(pnl_pcts)

    return Metrics(
        strategy=strategy,
        period=period,
        start=str(df.index[0])[:10],
        end=str(df.index[-1])[:10],
        n_candles=n,
        total_return_pct=round(total_return, 3),
        ann_return_pct=round(ann_return, 3),
        max_drawdown_pct=round(abs(max_dd), 3),
        sharpe=round(sharpe, 4),
        calmar=round(calmar, 4),
        volatility_pct=round(ann_vol, 3),
        total_trades=n_trades,
        win_rate_pct=round(win_rate, 2),
        profit_factor=round(min(pf, 9999.0), 4),
        avg_win_pct=round(avg_win, 3),
        avg_loss_pct=round(avg_loss, 3),
        best_trade_pct=round(best, 3),
        worst_trade_pct=round(worst, 3),
        total_fees=round(result.total_fees, 4),
        final_equity=round(end_eq, 2),
    )


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 6 — REPORTER
# ═══════════════════════════════════════════════════════════════════════════

G = "\033[92m"; R = "\033[91m"; Y = "\033[93m"; C = "\033[96m"
B = "\033[1m";  RESET = "\033[0m"

def _pct(v: float, pos_good: bool = True) -> str:
    col = (G if v > 0 else R) if pos_good else (R if v > 0 else G)
    sign = "+" if v > 0 else ""
    return f"{col}{sign}{v:.2f}%{RESET}"

def _val(v: float, pos_good: bool = True) -> str:
    col = (G if v > 0 else R) if pos_good else (R if v > 0 else G)
    sign = "+" if v > 0 else ""
    return f"{col}{sign}{v:.3f}{RESET}"


def print_detail(m: Metrics) -> None:
    col = G if m.period == "in-sample" else C
    print(f"\n  {col}{B}[{m.period.upper()}]  {m.strategy}{RESET}")
    print(f"  Period  : {m.start} → {m.end}  ({m.n_candles:,} candles)")
    print(f"  Balance : ${BALANCE:,.0f} → ${m.final_equity:,.0f}")

    rows = [
        ("Total Return",   _pct(m.total_return_pct)),
        ("Ann. Return",    _pct(m.ann_return_pct)),
        ("Max Drawdown",   f"{R}-{m.max_drawdown_pct:.2f}%{RESET}"),
        ("Sharpe Ratio",   _val(m.sharpe)),
        ("Calmar Ratio",   _val(m.calmar)),
        ("Volatility",     f"{m.volatility_pct:.2f}%"),
        ("Total Trades",   str(m.total_trades)),
        ("Win Rate",       f"{m.win_rate_pct:.1f}%"),
        ("Profit Factor",  _val(m.profit_factor)),
        ("Avg Win",        _pct(m.avg_win_pct)),
        ("Avg Loss",       _pct(m.avg_loss_pct)),
        ("Best Trade",     _pct(m.best_trade_pct)),
        ("Worst Trade",    _pct(m.worst_trade_pct)),
        ("Fees Paid",      f"${m.total_fees:,.2f}"),
    ]
    for label, val in rows:
        print(f"    {label:<24} {val}")


def print_comparison(results: dict) -> None:
    """Print detailed per-strategy blocks then summary comparison table."""

    # Per-strategy detail
    print("\n" + "═" * 72)
    print(f"  {B}DETAILED RESULTS — {SYMBOL}  [synthetic 1h | seed={SEED}]{RESET}")
    print("═" * 72)
    for name, r in results.items():
        if r.get("is"):
            print_detail(r["is"])
        if r.get("oos"):
            print_detail(r["oos"])

    # OOS comparison table
    print("\n\n" + "═" * 72)
    print(f"  {B}OOS STRATEGY COMPARISON TABLE{RESET}")
    print("═" * 72)

    rows = []
    for name, r in results.items():
        oos = r.get("oos")
        ins = r.get("is")
        if oos:
            rows.append((name, ins, oos))

    rows.sort(key=lambda x: x[2].sharpe if x[2] else -999, reverse=True)

    header = (
        f"  {'Strategy':<16}"
        f"{'IS Ret%':>10}  {'OOS Ret%':>10}"
        f"{'MaxDD%':>9}  {'Sharpe':>8}"
        f"{'WinRate':>9}  {'Trades':>7}"
        f"{'PF':>8}  {'Overfit':>10}"
    )
    print(header)
    print("  " + "─" * 86)

    for name, ins, oos in rows:
        overfit_gap = (ins.total_return_pct - oos.total_return_pct) if ins else 0
        if overfit_gap > 15:
            ov_str = f"{R}⚠ -{overfit_gap:.1f}pp{RESET}"
        elif overfit_gap > 5:
            ov_str = f"{Y}△ -{overfit_gap:.1f}pp{RESET}"
        else:
            ov_str = f"{G}✓ OK{RESET}"

        is_str  = _pct(ins.total_return_pct) if ins else "N/A"
        oos_str = _pct(oos.total_return_pct)
        dd_str  = f"{R}-{oos.max_drawdown_pct:.1f}%{RESET}"
        sh_str  = _val(oos.sharpe)
        pf_str  = _val(oos.profit_factor) if oos.profit_factor < 9999 else "∞"

        print(
            f"  {name:<16}"
            f"{is_str:>18}  {oos_str:>18}"
            f"{dd_str:>18}  {sh_str:>17}"
            f"  {oos.win_rate_pct:>6.1f}%  {oos.total_trades:>6}"
            f"  {pf_str:>15}  {ov_str}"
        )

    print("\n" + "═" * 72)

    if rows:
        best_name, _, best_oos = rows[0]
        print(
            f"\n  {B}Best OOS Sharpe:{RESET} {G}{best_name}{RESET} "
            f"({_val(best_oos.sharpe)} Sharpe, "
            f"{_pct(best_oos.total_return_pct)} return, "
            f"max DD {R}-{best_oos.max_drawdown_pct:.1f}%{RESET})\n"
        )

    print(f"""  {B}HOW TO READ THIS TABLE{RESET}
  IS Ret%   — In-sample return (9-month training period)
  OOS Ret%  — Out-of-sample return (3-month honest forward test)
  MaxDD%    — Largest peak-to-trough drawdown in OOS period
  Sharpe    — Annualised Sharpe (>1 = good, >2 = excellent)
  WinRate   — % of closed trades profitable
  PF        — Profit Factor: gross profit / gross loss (>1.5 is good)
  Overfit   — IS vs OOS return gap (>15pp gap = possible overfitting)

  {Y}Note:{RESET} Results use synthetic GBM data (seed={SEED}). Run runner.py
         on your machine with real Binance data for live-grade results.
""")


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 7 — MAIN
# ═══════════════════════════════════════════════════════════════════════════

def run_all() -> dict:
    print("\n" + "═" * 72)
    print(f"  {B}PHASE C — BACKTESTING ENGINE (standalone){RESET}")
    print(f"  Symbol : {SYMBOL}  |  Timeframe : {TIMEFRAME}")
    print(f"  Balance: ${BALANCE:,.0f}  |  Seed: {SEED}")
    print(f"  Period : {TOTAL_HOURS//24} days  "
          f"(IS: {IS_HOURS//24}d / OOS: {OOS_HOURS//24}d)")
    print("═" * 72 + "\n")

    # 1. Generate synthetic data
    print("  Generating synthetic OHLCV data...")
    df_full = generate_ohlcv(n_hours=TOTAL_HOURS, seed=SEED)
    df_is   = df_full.iloc[:IS_HOURS].copy()
    df_oos  = df_full.iloc[IS_HOURS - WARM_UP:].copy()   # overlap for warm-up

    print(f"  Full : {len(df_full):,} candles | "
          f"{df_full.index[0].strftime('%Y-%m-%d')} → {df_full.index[-1].strftime('%Y-%m-%d')}")
    print(f"  IS   : {len(df_is):,} candles | "
          f"{df_is.index[0].strftime('%Y-%m-%d')} → {df_is.index[-1].strftime('%Y-%m-%d')}")
    print(f"  OOS  : {len(df_oos):,} candles (incl. {WARM_UP} warm-up) | "
          f"{df_oos.index[0].strftime('%Y-%m-%d')} → {df_oos.index[-1].strftime('%Y-%m-%d')}\n")

    results = {}

    # 2. Run each strategy
    for name, func in STRATEGY_FUNCS.items():
        usdt = STRATEGY_USDT.get(name)
        print(f"  Running {name}...", end="", flush=True)

        try:
            # In-sample
            sig_is = func(df_is)
            sim_is = simulate(df_is, sig_is, BALANCE, usdt)
            m_is   = compute_metrics(sim_is, df_is, name, "in-sample")

            # Out-of-sample
            sig_oos = func(df_oos)
            sim_oos = simulate(df_oos, sig_oos, BALANCE, usdt)
            m_oos   = compute_metrics(sim_oos, df_oos, name, "out-of-sample")

            results[name] = {"is": m_is, "oos": m_oos}
            print(f" IS={_pct(m_is.total_return_pct)}  OOS={_pct(m_oos.total_return_pct)}  "
                  f"Sharpe={_val(m_oos.sharpe)}")

        except Exception as e:
            print(f" ERROR: {e}")
            results[name] = {"is": None, "oos": None}

    # 3. Print full report
    print_comparison(results)
    return results


if __name__ == "__main__":
    run_all()
