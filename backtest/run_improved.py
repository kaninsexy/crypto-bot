"""
run_backtest_v2.py  —  Comprehensive backtest: original vs improved strategies

Strategies tested (pure numpy/pandas, no ta library needed):
  OLD:  DCA, MeanReversion v1, GridTrading v1, Breakout, Supertrend, TrendFollowing
  NEW:  DCA v2 (MACD filter), MeanReversion v2 (StochRSI+%B), GridTrading v2
        (trailing), VWAP, BearShort (stub)

Data: 1 year of synthetic hourly OHLCV (GBM with regime switching)
      9-month in-sample / 3-month out-of-sample split

Output: per-strategy detail blocks + ranked comparison table saved to report
"""

from __future__ import annotations
import math
import os
import sys
import numpy as np
import pandas as pd
from dataclasses import dataclass
from typing import Optional

# ─── constants ────────────────────────────────────────────────────────────────
BALANCE     = 10_000.0
SEED        = 42
SYMBOL      = "BTC/USDT"
TIMEFRAME   = "1h"
TOTAL_HOURS = 365 * 24
IS_HOURS    = 9 * 30 * 24
OOS_HOURS   = TOTAL_HOURS - IS_HOURS
WARM_UP     = 220

FEE_LIMIT   = 0.0002
FEE_MARKET  = 0.0004
SLIP_LIMIT  = 0.0002
SLIP_MARKET = 0.0005

G = "\033[92m"; R = "\033[91m"; Y = "\033[93m"
C = "\033[96m"; B = "\033[1m";  RESET = "\033[0m"


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — SYNTHETIC DATA (GBM + regime switching)
# ═══════════════════════════════════════════════════════════════════════════════

def generate_ohlcv(n_hours=TOTAL_HOURS, start_price=65_000.0,
                   annual_drift=0.30, annual_vol=0.65, seed=SEED) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dt  = 1 / (365.25 * 24)

    regimes = rng.choice(["bull", "range", "bear"], size=n_hours, p=[0.60, 0.25, 0.15])
    for i in range(1, n_hours):
        if rng.random() < 0.97:
            regimes[i] = regimes[i - 1]

    drift_map = {"bull": annual_drift,  "range": 0.02, "bear": -0.60}
    vol_map   = {"bull": annual_vol*0.9,"range": annual_vol*0.6, "bear": annual_vol*1.4}

    closes = [start_price]
    for i in range(1, n_hours):
        mu  = drift_map[regimes[i]]
        sig = vol_map[regimes[i]]
        z   = rng.standard_normal()
        closes.append(closes[-1] * math.exp((mu - 0.5*sig**2)*dt + sig*math.sqrt(dt)*z))

    closes = np.array(closes)
    range_pct = np.where(np.array(regimes)=="bear",
                         rng.uniform(0.006,0.018,n_hours),
                         rng.uniform(0.003,0.012,n_hours))
    half = closes * range_pct / 2
    opens  = closes + rng.uniform(-0.3, 0.3, n_hours) * half
    highs  = np.maximum(opens, closes) + rng.uniform(0.0, 1.0, n_hours) * half
    lows   = np.minimum(opens, closes) - rng.uniform(0.0, 1.0, n_hours) * half
    lows   = np.maximum(lows, closes * 0.01)

    base_vol  = 1000.0
    price_chg = np.abs(np.diff(closes, prepend=closes[0]) / closes)
    reg_vol   = np.where(np.array(regimes)=="bear", 2.5,
                np.where(np.array(regimes)=="range", 0.7, 1.0))
    volume    = base_vol * (1 + price_chg*10) * reg_vol * rng.uniform(0.6, 1.4, n_hours)

    from datetime import datetime, timedelta, timezone
    start_dt = datetime(2024, 1, 1, tzinfo=timezone.utc)
    ts = [start_dt + timedelta(hours=i) for i in range(n_hours)]
    return pd.DataFrame({"open":opens,"high":highs,"low":lows,"close":closes,"volume":volume},
                        index=pd.DatetimeIndex(ts, name="timestamp"))


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — INDICATOR LIBRARY (pure pandas/numpy)
# ═══════════════════════════════════════════════════════════════════════════════

def ema(s: pd.Series, p: int) -> pd.Series:
    return s.ewm(span=p, adjust=False).mean()

def sma(s: pd.Series, p: int) -> pd.Series:
    return s.rolling(p).mean()

def rsi(s: pd.Series, p=14) -> pd.Series:
    d  = s.diff()
    g  = d.clip(lower=0).ewm(alpha=1/p, adjust=False).mean()
    l  = (-d).clip(lower=0).ewm(alpha=1/p, adjust=False).mean()
    return 100 - 100 / (1 + g / l.replace(0, 1e-9))

def atr(df: pd.DataFrame, p=14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h-l, (h-c.shift(1)).abs(), (l-c.shift(1)).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/p, adjust=False).mean()

def bollinger(s: pd.Series, p=20, n=2.0):
    mid = sma(s, p)
    std = s.rolling(p).std()
    return mid + n*std, mid, mid - n*std

def macd_hist(s: pd.Series, fast=12, slow=26, sig=9) -> pd.Series:
    return (ema(s,fast) - ema(s,slow)) - ema(ema(s,fast)-ema(s,slow), sig)

def stochrsi(s: pd.Series, rsi_p=14, smooth_k=3, smooth_d=3):
    """
    StochRSI:
      1. rsi_val = RSI(close, rsi_p)
      2. stoch   = (rsi_val - min(rsi_val, rsi_p)) / (max(rsi_val, rsi_p) - min(rsi_val, rsi_p))
      3. K       = SMA(stoch * 100, smooth_k)
      4. D       = SMA(K, smooth_d)
    Returns K, D as Series (values 0–100)
    """
    rsi_val = rsi(s, rsi_p)
    lo  = rsi_val.rolling(rsi_p).min()
    hi  = rsi_val.rolling(rsi_p).max()
    raw = (rsi_val - lo) / (hi - lo + 1e-9) * 100
    K   = sma(raw, smooth_k)
    D   = sma(K,   smooth_d)
    return K, D

def rolling_vwap(df: pd.DataFrame, p=24) -> pd.Series:
    tp  = (df["high"] + df["low"] + df["close"]) / 3
    tpv = tp * df["volume"]
    return tpv.rolling(p).sum() / df["volume"].rolling(p).sum()

def supertrend_dir(df: pd.DataFrame, p=10, mult=3.0) -> pd.Series:
    atr_v = atr(df, p)
    hl2   = (df["high"] + df["low"]) / 2
    upper = hl2 + mult * atr_v
    lower = hl2 - mult * atr_v
    close = df["close"]
    direction = pd.Series(1.0, index=df.index)
    fu, fl = upper.copy(), lower.copy()
    for i in range(1, len(df)):
        fu.iloc[i] = upper.iloc[i] if upper.iloc[i] < fu.iloc[i-1] or close.iloc[i-1] > fu.iloc[i-1] else fu.iloc[i-1]
        fl.iloc[i] = lower.iloc[i] if lower.iloc[i] > fl.iloc[i-1] or close.iloc[i-1] < fl.iloc[i-1] else fl.iloc[i-1]
        pd_val = direction.iloc[i-1]
        if pd_val == 1 and close.iloc[i] < fl.iloc[i]:
            direction.iloc[i] = -1
        elif pd_val == -1 and close.iloc[i] > fu.iloc[i]:
            direction.iloc[i] = 1
        else:
            direction.iloc[i] = pd_val
    return direction


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — STRATEGY SIGNAL GENERATORS
# ═══════════════════════════════════════════════════════════════════════════════

def signals_dca_v1(df: pd.DataFrame) -> pd.Series:
    """Original DCA: 2% drop from swing high + RSI < 42 entry."""
    close   = df["close"]
    rsi_v   = rsi(close, 14)
    sigs    = pd.Series(0, index=df.index)
    avg, in_pos, swing, safety = None, False, None, 0
    MAX_S, DEV, TP = 5, 0.02, 0.04
    for i in range(WARM_UP, len(df)):
        c, r = close.iloc[i], rsi_v.iloc[i]
        if not in_pos:
            if swing is None or c > swing: swing = c
        if in_pos:
            if c >= avg*(1+TP): sigs.iloc[i]=-1; in_pos=False; avg=swing=None; safety=0
            elif safety<MAX_S and swing and c<avg*(1-DEV):
                sigs.iloc[i]=1; avg=(avg+c)/2; safety+=1
        else:
            if swing and c<swing*(1-DEV) and r<42:
                sigs.iloc[i]=1; in_pos=True; avg=c; safety=0
    return sigs


def signals_dca_v2(df: pd.DataFrame) -> pd.Series:
    """
    DCA v2 with MACD deal-start filter:
      Base order only opens when MACD histogram crosses from negative to positive
      in the last 3 candles (bullish momentum confirmation).
    """
    close  = df["close"]
    rsi_v  = rsi(close, 14)
    mhist  = macd_hist(close, 12, 26, 9)
    sigs   = pd.Series(0, index=df.index)
    avg, in_pos, swing, safety = None, False, None, 0
    MAX_S, DEV, TP = 5, 0.02, 0.04
    for i in range(WARM_UP, len(df)):
        c, r = close.iloc[i], rsi_v.iloc[i]
        h_curr = mhist.iloc[i]
        # MACD filter: histogram is positive now AND was negative in last 3 candles
        recent_neg = any(mhist.iloc[max(0,i-k)] < 0 for k in range(1,4))
        macd_ok = h_curr > 0 and recent_neg
        if not in_pos:
            if swing is None or c > swing: swing = c
        if in_pos:
            if c >= avg*(1+TP): sigs.iloc[i]=-1; in_pos=False; avg=swing=None; safety=0
            elif safety<MAX_S and swing and c<avg*(1-DEV):
                sigs.iloc[i]=1; avg=(avg+c)/2; safety+=1
        else:
            if swing and c<swing*(1-DEV) and r<42 and macd_ok:
                sigs.iloc[i]=1; in_pos=True; avg=c; safety=0
    return sigs


def signals_meanrev_v1(df: pd.DataFrame) -> pd.Series:
    """Original MeanReversion v1: RSI < 35 + lower BB touch."""
    close   = df["close"]
    bb_u, bb_m, bb_l = bollinger(close, 20, 2.0)
    rsi_v   = rsi(close, 14)
    ema200  = ema(close, 200)
    sigs    = pd.Series(0, index=df.index)
    in_pos, entry = False, None
    for i in range(WARM_UP, len(df)):
        c, r, lo, hi, tr = close.iloc[i], rsi_v.iloc[i], bb_l.iloc[i], bb_u.iloc[i], ema200.iloc[i]
        if not in_pos:
            if c <= lo*1.005 and r < 35 and c > tr*0.90:
                sigs.iloc[i]=1; in_pos=True; entry=c
        else:
            if c >= hi*0.995 or r > 65 or c < entry*0.97:
                sigs.iloc[i]=-1; in_pos=False; entry=None
    return sigs


def signals_meanrev_v2(df: pd.DataFrame) -> pd.Series:
    """
    MeanReversion v2: StochRSI K/D crossover + BB %B (more precise timing).

    Entry:  BB %B < 0.05  AND  StochRSI K < 25  AND  K crossed above D
    Exit:   BB %B > 0.95  OR  (StochRSI K > 75 AND K crossed below D)
    SL:     4% hard stop
    """
    close   = df["close"]
    bb_u, bb_m, bb_l = bollinger(close, 20, 2.0)
    K, D    = stochrsi(close, 14, 3, 3)
    rsi_v   = rsi(close, 14)
    ema200  = ema(close, 200)
    sigs    = pd.Series(0, index=df.index)
    in_pos, entry = False, None

    for i in range(WARM_UP, len(df)):
        c    = close.iloc[i]
        lo   = bb_l.iloc[i]; hi = bb_u.iloc[i]
        band = hi - lo
        pct_b = (c - lo) / (band + 1e-9)
        k_curr, d_curr = K.iloc[i], D.iloc[i]
        k_prev, d_prev = K.iloc[i-1], D.iloc[i-1]
        tr   = ema200.iloc[i]
        r    = rsi_v.iloc[i]

        if not in_pos:
            k_cross_up  = k_prev <= d_prev and k_curr > d_curr
            stoch_entry = k_curr < 25 and (k_cross_up or k_curr > k_prev)
            bb_entry    = pct_b < 0.05
            trend_ok    = c > tr * 0.90
            if bb_entry and stoch_entry and trend_ok:
                sigs.iloc[i]=1; in_pos=True; entry=c
        else:
            sl_hit       = c < entry * 0.96      # 4% stop loss
            k_cross_down = k_prev >= d_prev and k_curr < d_curr
            ob_exit      = k_curr > 75 and k_cross_down
            bb_exit      = pct_b > 0.95
            if sl_hit or ob_exit or (bb_exit and r > 68):
                sigs.iloc[i]=-1; in_pos=False; entry=None

    return sigs


def signals_grid_v1(df: pd.DataFrame) -> pd.Series:
    """Original grid: fixed BB range, dormant on breakout."""
    close   = df["close"]
    atr_v   = atr(df, 14)
    bb_u, _, bb_l = bollinger(close, 20, 2.0)
    sigs   = pd.Series(0, index=df.index)
    in_pos, last_level, step = False, None, None
    for i in range(WARM_UP, len(df)):
        c    = close.iloc[i]
        s    = atr_v.iloc[i] * 0.75
        lo   = bb_l.iloc[i]; hi = bb_u.iloc[i]
        if atr_v.iloc[i]/c*100 > 2.5: continue
        if not in_pos:
            if pd.notna(lo) and s > 0:
                steps_from_lo = int((c - lo) / s)
                level = lo + steps_from_lo * s
                if last_level is None or level < last_level:
                    sigs.iloc[i]=1; in_pos=True; last_level=level; step=s
        else:
            if step and c >= last_level + step:
                sigs.iloc[i]=-1; in_pos=False
            elif c > hi:
                sigs.iloc[i]=-1; in_pos=False
    return sigs


def signals_grid_v2(df: pd.DataFrame) -> pd.Series:
    """
    Grid v2 with trailing grid:
      When price breaks above BB upper, shift range up by one ATR step instead of
      going dormant. Keeps the strategy active in trending markets.
    """
    close   = df["close"]
    atr_v   = atr(df, 14)
    bb_u, _, bb_l = bollinger(close, 20, 2.0)
    sigs   = pd.Series(0, index=df.index)
    in_pos = False
    last_level = None
    step_size  = None
    # Track shifted grid bounds
    grid_lo_offset = 0.0

    for i in range(WARM_UP, len(df)):
        c  = close.iloc[i]
        s  = atr_v.iloc[i] * 0.75
        lo = bb_l.iloc[i] + grid_lo_offset
        hi = bb_u.iloc[i] + grid_lo_offset

        if pd.isna(lo) or s <= 0: continue
        atr_pct = atr_v.iloc[i] / c * 100

        if in_pos:
            if step_size and c >= last_level + step_size:
                sigs.iloc[i]=-1; in_pos=False
            elif c > hi:
                # Breakout above upper: trailing grid shifts up
                grid_lo_offset += s
                sigs.iloc[i]=-1; in_pos=False   # exit, then reposition
        else:
            if atr_pct > 3.0: continue         # pause in very high vol
            # Trailing grid: if price below shifted lo, shift down
            if c < lo:
                grid_lo_offset -= s

            lo = bb_l.iloc[i] + grid_lo_offset
            if lo <= 0: lo = bb_l.iloc[i]

            steps_from_lo = int(max(0, (c - lo)) / max(s, 1e-9))
            level = lo + steps_from_lo * s
            if last_level is None or level < last_level or abs(level - (last_level or 0)) > s*0.5:
                sigs.iloc[i]=1; in_pos=True; last_level=level; step_size=s

    return sigs


def signals_vwap(df: pd.DataFrame) -> pd.Series:
    """
    VWAP Strategy (new):
      Entry:  price > 1.5% below rolling 24-period VWAP  AND  RSI < 50
      Exit:   price returns to VWAP (0.5% above)  OR  RSI > 65  OR  3% SL
    """
    close  = df["close"]
    vwap   = rolling_vwap(df, 24)
    rsi_v  = rsi(close, 14)
    vol    = df["volume"]
    vol_ma = sma(vol, 20)
    sigs   = pd.Series(0, index=df.index)
    in_pos, entry, vwap_at_entry = False, None, None

    for i in range(WARM_UP, len(df)):
        c  = close.iloc[i]
        vw = vwap.iloc[i]
        r  = rsi_v.iloc[i]
        v  = vol.iloc[i]; vm = vol_ma.iloc[i]

        if pd.isna(vw): continue
        dev = (c - vw) / vw * 100   # negative = below VWAP

        if not in_pos:
            vol_ok = (v >= vm * 0.8) if pd.notna(vm) else True
            if dev <= -1.5 and r < 50 and vol_ok:
                sigs.iloc[i]=1; in_pos=True; entry=c; vwap_at_entry=vw
        else:
            sl_hit  = c < entry * 0.97
            vwap_rv = dev >= 0.5                   # reverted to VWAP
            ob_exit = r > 65
            if sl_hit or vwap_rv or ob_exit:
                sigs.iloc[i]=-1; in_pos=False; entry=None; vwap_at_entry=None

    return sigs


def signals_supertrend(df: pd.DataFrame) -> pd.Series:
    """Supertrend direction flip."""
    close = df["close"]
    st    = supertrend_dir(df, 10, 3.0)
    sigs  = pd.Series(0, index=df.index)
    in_pos = False
    for i in range(WARM_UP, len(df)):
        cur, prev = st.iloc[i], st.iloc[i-1]
        if not in_pos and cur==1 and prev==-1: sigs.iloc[i]=1; in_pos=True
        elif in_pos and cur==-1 and prev==1:   sigs.iloc[i]=-1; in_pos=False
    return sigs


def signals_trend_following(df: pd.DataFrame) -> pd.Series:
    """Golden cross + RSI confirmation."""
    close  = df["close"]
    e50    = ema(close, 50); e200 = ema(close, 200)
    rsi_v  = rsi(close, 14)
    sigs   = pd.Series(0, index=df.index)
    in_pos = False
    for i in range(WARM_UP, len(df)):
        e5, e2, r = e50.iloc[i], e200.iloc[i], rsi_v.iloc[i]
        p5, p2    = e50.iloc[i-1], e200.iloc[i-1]
        if not in_pos and e5>e2 and p5<=p2 and r>50: sigs.iloc[i]=1;  in_pos=True
        elif in_pos and ((e5<e2 and p5>=p2) or r<40): sigs.iloc[i]=-1; in_pos=False
    return sigs


def signals_breakout(df: pd.DataFrame) -> pd.Series:
    """20-period high breakout with 1.5× volume surge."""
    close  = df["close"]
    vol    = df["volume"]
    e50    = ema(close, 50); e20 = ema(close, 20)
    atr_v  = atr(df, 14)
    vol_ma = sma(vol, 20)
    h20    = close.rolling(20).max().shift(1)
    sigs   = pd.Series(0, index=df.index)
    in_pos, entry = False, None
    for i in range(WARM_UP, len(df)):
        c, v, vm = close.iloc[i], vol.iloc[i], vol_ma.iloc[i]
        h, e5, e2, a = h20.iloc[i], e50.iloc[i], e20.iloc[i], atr_v.iloc[i]
        if not in_pos:
            if pd.notna(h) and c>h and v>vm*1.5 and c>e5: sigs.iloc[i]=1; in_pos=True; entry=c
        else:
            stop = entry - 2*a if entry else c*0.95
            if c<e2 or c<stop: sigs.iloc[i]=-1; in_pos=False; entry=None
    return sigs


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 4 — SIMULATOR
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class Trade:
    entry_price: float; exit_price: float; quantity: float
    cost: float; pnl: float; pnl_pct: float; fees_paid: float

@dataclass
class SimResult:
    trades: list; equity_curve: pd.Series
    final_balance: float; total_fees: float

def simulate(df, signals, initial_balance=BALANCE,
             usdt_per_trade=None, risk_pct=0.02) -> SimResult:
    balance, position, trades, equity, total_fees = initial_balance, None, [], {}, 0.0
    for i in range(len(df)):
        ts    = df.index[i]
        close = float(df["close"].iloc[i])
        sig   = int(signals.iloc[i])
        if sig == 1 and position is None:
            fill = close * (1 + SLIP_LIMIT)
            cost = min(usdt_per_trade or balance*risk_pct, balance)
            if cost < 1: equity[ts] = balance; continue
            fee = cost * FEE_LIMIT; cost_net = cost - fee
            qty = cost_net / fill; balance -= cost; total_fees += fee
            position = {"entry": fill, "qty": qty, "cost": cost_net}
        elif sig == -1 and position is not None:
            fill     = close * (1 - SLIP_LIMIT)
            proceeds = fill * position["qty"]
            fee      = proceeds * FEE_LIMIT; net = proceeds - fee
            pnl      = net - position["cost"]
            pnl_pct  = pnl / position["cost"] * 100
            total_fees += fee; balance += net
            trades.append(Trade(position["entry"],fill,position["qty"],
                                position["cost"],pnl,pnl_pct,fee))
            position = None
        equity[ts] = (balance + close*position["qty"]) if position else balance

    if position:
        lc   = float(df["close"].iloc[-1])
        fill = lc * (1 - SLIP_MARKET); proceeds = fill*position["qty"]
        fee  = proceeds*FEE_MARKET; net = proceeds-fee
        pnl  = net-position["cost"]; pnl_pct=pnl/position["cost"]*100
        total_fees += fee; balance += net
        trades.append(Trade(position["entry"],fill,position["qty"],
                            position["cost"],pnl,pnl_pct,fee))
        if equity: equity[list(equity.keys())[-1]] = balance

    return SimResult(trades, pd.Series(equity), balance, total_fees)


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 5 — METRICS
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class Metrics:
    strategy: str; period: str; start: str; end: str; n_candles: int
    total_return_pct: float; ann_return_pct: float; max_drawdown_pct: float
    sharpe: float; calmar: float; volatility_pct: float; total_trades: int
    win_rate_pct: float; profit_factor: float; avg_win_pct: float
    avg_loss_pct: float; best_trade_pct: float; worst_trade_pct: float
    total_fees: float; final_equity: float

def compute_metrics(result, df, strategy, period, initial_balance=BALANCE) -> Metrics:
    eq = result.equity_curve
    trades = result.trades
    HPY = 365.25 * 24
    years = len(eq) / HPY
    total_ret = (result.final_balance - initial_balance) / initial_balance * 100
    ann_ret   = ((result.final_balance / max(initial_balance,1e-9))**(1/max(years,1e-9))-1)*100
    rm = eq.cummax(); dd = (eq-rm)/rm*100; max_dd = float(dd.min())
    ret_s  = eq.pct_change().dropna()
    ann_vol = float(ret_s.std() * math.sqrt(HPY)) * 100
    sharpe  = ann_ret/ann_vol if ann_vol>0 else 0
    calmar  = ann_ret/abs(max_dd) if max_dd!=0 else 0
    n = len(trades)
    wins   = [t for t in trades if t.pnl>0]
    losses = [t for t in trades if t.pnl<=0]
    wr     = len(wins)/n*100 if n else 0
    gp     = sum(t.pnl for t in wins); gl = abs(sum(t.pnl for t in losses))
    pf     = gp/gl if gl>0 else (float("inf") if gp>0 else 0)
    pcts   = [t.pnl_pct for t in trades] if trades else [0]
    return Metrics(
        strategy=strategy, period=period,
        start=str(df.index[0])[:10], end=str(df.index[-1])[:10],
        n_candles=len(eq),
        total_return_pct=round(total_ret,3), ann_return_pct=round(ann_ret,3),
        max_drawdown_pct=round(abs(max_dd),3),
        sharpe=round(sharpe,4), calmar=round(calmar,4),
        volatility_pct=round(ann_vol,3), total_trades=n,
        win_rate_pct=round(wr,2), profit_factor=round(min(pf,9999),4),
        avg_win_pct=round(float(np.mean([t.pnl_pct for t in wins])) if wins else 0, 3),
        avg_loss_pct=round(float(np.mean([t.pnl_pct for t in losses])) if losses else 0, 3),
        best_trade_pct=round(max(pcts),3), worst_trade_pct=round(min(pcts),3),
        total_fees=round(result.total_fees,4), final_equity=round(result.final_balance,2),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 6 — REPORTER
# ═══════════════════════════════════════════════════════════════════════════════

def _pct(v, pos_good=True):
    col = (G if v>0 else R) if pos_good else (R if v>0 else G)
    return f"{col}{'+' if v>0 else ''}{v:.2f}%{RESET}"

def _val(v, pos_good=True):
    col = (G if v>0 else R) if pos_good else (R if v>0 else G)
    return f"{col}{'+' if v>0 else ''}{v:.3f}{RESET}"

def print_detail(m: Metrics, show_header=True):
    col = G if "in-sample" in m.period else C
    if show_header:
        print(f"\n  {col}{B}[{m.period.upper()}]  {m.strategy}{RESET}")
    print(f"  Period  : {m.start} → {m.end}  ({m.n_candles:,} candles)")
    print(f"  Balance : ${BALANCE:,.0f} → ${m.final_equity:,.0f}")
    for label, val in [
        ("Total Return",  _pct(m.total_return_pct)),
        ("Ann. Return",   _pct(m.ann_return_pct)),
        ("Max Drawdown",  f"{R}-{m.max_drawdown_pct:.2f}%{RESET}"),
        ("Sharpe",        _val(m.sharpe)),
        ("Calmar",        _val(m.calmar)),
        ("Trades",        str(m.total_trades)),
        ("Win Rate",      f"{m.win_rate_pct:.1f}%"),
        ("Profit Factor", _val(m.profit_factor)),
        ("Avg Win",       _pct(m.avg_win_pct)),
        ("Avg Loss",      _pct(m.avg_loss_pct)),
        ("Fees Paid",     f"${m.total_fees:,.2f}"),
    ]:
        print(f"    {label:<22} {val}")


def print_comparison_table(results: dict, label: str = "OOS"):
    print(f"\n\n{'═'*90}")
    print(f"  {B}{label} STRATEGY COMPARISON  —  {SYMBOL}  [synthetic 1h | seed={SEED}]{RESET}")
    print(f"{'═'*90}")

    rows = []
    for name, r in results.items():
        oos = r.get("oos"); ins = r.get("is")
        if oos: rows.append((name, ins, oos))
    rows.sort(key=lambda x: x[2].sharpe if x[2] else -999, reverse=True)

    print(f"  {'Strategy':<22} {'IS Ret%':>10}  {'OOS Ret%':>10}  "
          f"{'MaxDD%':>8}  {'Sharpe':>8}  {'Calmar':>8}  "
          f"{'WinRate':>8}  {'Trades':>7}  {'PF':>7}  {'Overfit':>12}")
    print("  " + "─"*88)

    for name, ins, oos in rows:
        gap = (ins.total_return_pct - oos.total_return_pct) if ins else 0
        ov  = f"{R}⚠ -{gap:.1f}pp{RESET}" if gap>15 else (f"{Y}△ -{gap:.1f}pp{RESET}" if gap>5 else f"{G}✓ OK{RESET}")
        is_s = _pct(ins.total_return_pct) if ins else "N/A"
        pf_s = _val(oos.profit_factor) if oos.profit_factor<9999 else f"{G}∞{RESET}"
        print(f"  {name:<22}"
              f"{is_s:>19}  {_pct(oos.total_return_pct):>19}"
              f"  {R}-{oos.max_drawdown_pct:.1f}%{RESET:>5}"
              f"  {_val(oos.sharpe):>17}  {_val(oos.calmar):>17}"
              f"  {oos.win_rate_pct:>6.1f}%  {oos.total_trades:>6}"
              f"  {pf_s:>14}  {ov}")

    print(f"\n{'═'*90}")
    if rows:
        best, _, best_oos = rows[0]
        print(f"\n  {B}Best OOS Sharpe:{RESET} {G}{best}{RESET} "
              f"({_val(best_oos.sharpe)} Sharpe, "
              f"{_pct(best_oos.total_return_pct)} return, "
              f"max DD {R}-{best_oos.max_drawdown_pct:.1f}%{RESET})")


def print_v1_vs_v2_comparison(results: dict):
    """Side-by-side OOS comparison of v1 vs v2 strategies."""
    pairs = [
        ("DCA (v1)",            "DCA v2 (MACD filter)"),
        ("MeanReversion (v1)",  "MeanReversion v2 (StochRSI+%B)"),
        ("GridTrading (v1)",    "GridTrading v2 (Trailing)"),
    ]
    print(f"\n\n{'═'*90}")
    print(f"  {B}v1 vs v2 IMPROVEMENT COMPARISON  —  OOS period{RESET}")
    print(f"{'═'*90}")
    print(f"  {'Strategy':<26} {'Return%':>10}  {'MaxDD%':>8}  {'Sharpe':>8}  {'WinRate':>9}  {'PF':>8}  {'Trades':>7}")
    print("  " + "─"*78)
    for old_name, new_name in pairs:
        for tag, name in [("v1", old_name), ("v2", new_name)]:
            r = results.get(name, {})
            oos = r.get("oos")
            if oos:
                marker = f"  {C}{tag}{RESET}" if tag=="v2" else f"  {Y}{tag}{RESET}"
                pf_s = _val(oos.profit_factor) if oos.profit_factor<9999 else f"{G}∞{RESET}"
                print(f"  {name:<26}"
                      f"{_pct(oos.total_return_pct):>19}"
                      f"  {R}-{oos.max_drawdown_pct:.1f}%{RESET:>5}"
                      f"  {_val(oos.sharpe):>17}"
                      f"  {oos.win_rate_pct:>6.1f}%"
                      f"  {pf_s:>15}"
                      f"  {oos.total_trades:>6}{marker}")
        print("  " + "─"*78)


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 7 — MAIN
# ═══════════════════════════════════════════════════════════════════════════════

STRATEGY_FUNCS = {
    "DCA (v1)":                signals_dca_v1,
    "DCA v2 (MACD filter)":    signals_dca_v2,
    "MeanReversion (v1)":      signals_meanrev_v1,
    "MeanReversion v2 (StochRSI+%B)": signals_meanrev_v2,
    "GridTrading (v1)":        signals_grid_v1,
    "GridTrading v2 (Trailing)": signals_grid_v2,
    "VWAP (new)":              signals_vwap,
    "Supertrend":              signals_supertrend,
    "TrendFollowing":          signals_trend_following,
    "Breakout":                signals_breakout,
}

STRATEGY_USDT = {"DCA (v1)": 200.0, "DCA v2 (MACD filter)": 200.0,
                 "GridTrading (v1)": 200.0, "GridTrading v2 (Trailing)": 200.0}

def run_all():
    print(f"\n{'═'*90}")
    print(f"  {B}CRYPTO BOT — COMPREHENSIVE BACKTEST  v1 vs v2{RESET}")
    print(f"  Symbol : {SYMBOL}  |  Timeframe : {TIMEFRAME}")
    print(f"  Balance: ${BALANCE:,.0f}  |  Seed: {SEED}")
    print(f"  Period : {TOTAL_HOURS//24} days  "
          f"(IS: {IS_HOURS//24}d / OOS: {OOS_HOURS//24}d)")
    print(f"{'═'*90}\n")

    print("  Generating synthetic OHLCV data...")
    df_full = generate_ohlcv()
    df_is   = df_full.iloc[:IS_HOURS].copy()
    df_oos  = df_full.iloc[IS_HOURS - WARM_UP:].copy()
    print(f"  IS  : {len(df_is):,} candles  {df_is.index[0].date()} → {df_is.index[-1].date()}")
    print(f"  OOS : {len(df_oos):,} candles  {df_oos.index[0].date()} → {df_oos.index[-1].date()}\n")

    results = {}
    for name, func in STRATEGY_FUNCS.items():
        usdt = STRATEGY_USDT.get(name)
        print(f"  Running {name}...", end="", flush=True)
        try:
            sig_is  = func(df_is);  sim_is  = simulate(df_is,  sig_is,  BALANCE, usdt)
            sig_oos = func(df_oos); sim_oos = simulate(df_oos, sig_oos, BALANCE, usdt)
            m_is    = compute_metrics(sim_is,  df_is,  name, "in-sample")
            m_oos   = compute_metrics(sim_oos, df_oos, name, "out-of-sample")
            results[name] = {"is": m_is, "oos": m_oos}
            print(f"  IS={_pct(m_is.total_return_pct)}  "
                  f"OOS={_pct(m_oos.total_return_pct)}  "
                  f"Sharpe={_val(m_oos.sharpe)}")
        except Exception as e:
            import traceback
            print(f"  ERROR: {e}")
            traceback.print_exc()
            results[name] = {"is": None, "oos": None}

    # Detailed per-strategy blocks
    print(f"\n\n{'═'*90}")
    print(f"  {B}DETAILED RESULTS  [out-of-sample period]{RESET}")
    print(f"{'═'*90}")
    for name, r in results.items():
        if r.get("oos"):
            print(f"\n  {C}{B}{name}{RESET}")
            print_detail(r["oos"], show_header=False)

    # v1 vs v2 comparison
    print_v1_vs_v2_comparison(results)

    # Full ranked comparison
    print_comparison_table(results)

    # Key legend
    print(f"""
  {B}HOW TO READ{RESET}
  IS / OOS    — In-sample (9m training) / Out-of-sample (3m forward test)
  MaxDD%      — Largest peak-to-trough drawdown
  Sharpe      — Annualised (>1 = good, >2 = excellent)
  PF          — Profit Factor: gross profit / gross loss (>1.5 is good)
  Overfit     — IS vs OOS gap (>15pp = possible overfitting)
  {Y}Note:{RESET} Synthetic GBM data. Real-market runner uses actual Binance candles.
""")

    return results


if __name__ == "__main__":
    results = run_all()
