"""
portfolio/deposit_manager.py — Tactical Monthly Deposit Deployment

PURPOSE:
  When you add funds each month (100,000 THB month 1, ~60,000 THB thereafter),
  the money is NOT deployed immediately. Instead, it waits for favourable
  conditions per strategy bucket before being allocated.

  This prevents the common mistake of buying at peak prices just because
  new capital arrived.

HOW IT WORKS:
  ┌─────────────────────────────────────────────────────────────────┐
  │  1. deposit(amount_usdt)                                        │
  │     → Adds funds to the pending pool. Nothing is traded yet.   │
  │                                                                 │
  │  2. evaluate_deployment(market_data) → dict                     │
  │     → Checks deployment conditions per bucket:                 │
  │        DCA:       Always ready (DCA absorbs any price)          │
  │        Supertrend: Only deploy when Supertrend is bullish       │
  │        MeanRev:   Only deploy when RSI < 45 (not overbought)   │
  │        Grid:      Only deploy when ATR% < 2% (ranging market)  │
  │        Breakout:  Only deploy on confirmed breakout signal      │
  │        Reserve:   Holds 10% always (for drawdown opportunities) │
  │                                                                 │
  │  3. get_allocation(bucket) → float                             │
  │     → Returns USDT amount currently available for a bucket.    │
  │        Call this when a strategy wants to size a trade.         │
  │                                                                 │
  │  4. consume(bucket, amount)                                     │
  │     → Marks that amount as deployed. Reduces the bucket's pool. │
  └─────────────────────────────────────────────────────────────────┘

ALLOCATION BUCKETS (% of deposited capital):
  These are starting weights. The portfolio manager (Phase D) will
  override these dynamically based on market regime.

  DCA        : 30%  — always running, eats any price
  Supertrend : 25%  — trend following, deployed on bullish flip
  MeanRev    : 20%  — mean reversion, deployed on oversold dips
  Grid       : 15%  — ranging markets only
  Breakout   : 10%  — high conviction only
  Reserve    :  5%  — never auto-deployed; held for flash crash buying

  Note: Reserve is separate from the 95% above. It reduces from total.

DEPLOYMENT CONDITIONS:
  Each bucket has a deploy_condition function that receives market indicators
  and returns True (can deploy now) or False (wait).

  Conditions are intentionally conservative — it's better to wait one candle
  than to buy at the exact worst moment.

EXAMPLE MONTHLY FLOW:
  Month 1: Deposit $2,850 (100,000 THB)
    → DCA gets $855 (30%) — deployed immediately over first few candles
    → Supertrend gets $712.50 — waits until ST flips bullish
    → MeanRev gets $570 — waits until RSI dips below 45
    → Grid gets $427.50 — waits until ATR < 2%
    → Breakout gets $285 — waits for volume breakout signal
    → Reserve: $142.50 — held indefinitely (25% crash discount target)
"""

from dataclasses import dataclass, field
from typing import Callable, Optional
import pandas as pd
import ta
from loguru import logger
import config


# ── Bucket definitions ────────────────────────────────────────────────────────

BUCKETS = ["dca", "supertrend", "meanrev", "grid", "breakout", "reserve"]

# Starting weights (must sum to 1.0).
# Reserve (5%) is carved from the other buckets — Breakout is the
# smallest allocation because its OOS Phase C Sharpe was the weakest.
DEFAULT_WEIGHTS = {
    "dca":        0.30,
    "supertrend": 0.25,
    "meanrev":    0.20,
    "grid":       0.15,
    "breakout":   0.05,   # was 0.10 — reduced to make total exactly 1.0
    "reserve":    0.05,
}

# ── Deployment condition functions ────────────────────────────────────────────

def _condition_dca(indicators: dict) -> tuple[bool, str]:
    """DCA is always ready — it's designed to buy at any price."""
    return True, "DCA always deploys"


def _condition_supertrend(indicators: dict) -> tuple[bool, str]:
    """Deploy when Supertrend direction is bullish (1 = up)."""
    direction = indicators.get("supertrend_direction", 0)
    if direction == 1:
        return True, f"Supertrend bullish (dir={direction})"
    return False, f"Supertrend bearish (dir={direction}) — waiting for bullish flip"


def _condition_meanrev(indicators: dict) -> tuple[bool, str]:
    """Deploy when RSI is below 45 — not overbought, price is relatively cheap."""
    rsi = indicators.get("rsi", 50)
    threshold = 45
    if rsi < threshold:
        return True, f"RSI={rsi:.1f} < {threshold} — mean reversion window open"
    return False, f"RSI={rsi:.1f} ≥ {threshold} — waiting for dip"


def _condition_grid(indicators: dict) -> tuple[bool, str]:
    """Deploy when ATR% is low — market is ranging, not trending."""
    atr_pct = indicators.get("atr_pct", 3.0)
    threshold = 2.0
    if atr_pct < threshold:
        return True, f"ATR%={atr_pct:.2f}% < {threshold}% — ranging market, grid suitable"
    return False, f"ATR%={atr_pct:.2f}% ≥ {threshold}% — trending market, grid waiting"


def _condition_breakout(indicators: dict) -> tuple[bool, str]:
    """
    Deploy when there's a confirmed volume breakout on the current candle.
    This is more restrictive — only deploy when a signal has actually fired.
    """
    breakout_confirmed = indicators.get("breakout_confirmed", False)
    if breakout_confirmed:
        return True, "Breakout signal confirmed — deploying capital"
    return False, "No confirmed breakout — holding capital until breakout fires"


def _condition_reserve(indicators: dict) -> tuple[bool, str]:
    """
    Reserve is NEVER auto-deployed. It's held for flash crash opportunities.
    The user or portfolio manager must explicitly release it via release_reserve().
    """
    return False, "Reserve held for flash crash / drawdown buying opportunities"


DEPLOY_CONDITIONS: dict[str, Callable] = {
    "dca":        _condition_dca,
    "supertrend": _condition_supertrend,
    "meanrev":    _condition_meanrev,
    "grid":       _condition_grid,
    "breakout":   _condition_breakout,
    "reserve":    _condition_reserve,
}


# ── Deposit Manager ───────────────────────────────────────────────────────────

@dataclass
class BucketState:
    name: str
    weight: float
    pending: float = 0.0    # USDT waiting to be deployed
    deployed: float = 0.0   # USDT already put to work
    total_received: float = 0.0  # Cumulative deposits into this bucket


class DepositManager:
    """
    Manages monthly capital deposits and tactical deployment per strategy bucket.

    Usage:
        dm = DepositManager()

        # When you wire money in:
        dm.deposit(2850.0)

        # Each candle, check what's ready:
        indicators = dm.compute_indicators(df)
        ready = dm.evaluate_deployment(indicators)
        # ready = {"dca": 855.0, "supertrend": 0.0, "meanrev": 712.5, ...}

        # When a strategy wants to trade:
        available = dm.get_allocation("dca")
        dm.consume("dca", trade_cost)
    """

    def __init__(
        self,
        weights: dict[str, float] = None,
        thb_to_usdt: float = 0.028,     # Approximate THB→USDT rate (update monthly)
    ):
        """
        Args:
            weights:       Custom bucket weights. Must sum to 1.0.
                           Defaults to DEFAULT_WEIGHTS if not provided.
            thb_to_usdt:   Conversion rate for convenience. 1 THB ≈ 0.028 USDT.
        """
        weights = weights or DEFAULT_WEIGHTS
        total = sum(weights.values())
        if abs(total - 1.0) > 0.001:
            raise ValueError(f"Bucket weights must sum to 1.0, got {total:.3f}")

        self.thb_to_usdt = thb_to_usdt
        self.buckets: dict[str, BucketState] = {
            name: BucketState(name=name, weight=weights[name])
            for name in BUCKETS
        }
        self.total_deposited: float = 0.0
        self.total_deployed: float = 0.0
        self._deposit_log: list[dict] = []

        logger.info(
            f"DepositManager initialized | Buckets: "
            + " | ".join(f"{k}={v*100:.0f}%" for k, v in weights.items())
        )

    # ── Deposit ───────────────────────────────────────────────────────────────

    def deposit(self, amount_usdt: float, note: str = "") -> dict[str, float]:
        """
        Accept a new deposit and split it across buckets by weight.

        Args:
            amount_usdt: New capital to deposit (in USDT).
            note:        Optional label (e.g. "Month 2 deposit").

        Returns:
            Dict of {bucket_name: amount_allocated}.
        """
        if amount_usdt <= 0:
            raise ValueError(f"Deposit amount must be positive, got {amount_usdt}")

        self.total_deposited += amount_usdt
        allocation = {}

        for name, bucket in self.buckets.items():
            share = round(amount_usdt * bucket.weight, 2)
            bucket.pending += share
            bucket.total_received += share
            allocation[name] = share

        self._deposit_log.append({
            "amount_usdt": amount_usdt,
            "allocation": allocation.copy(),
            "note": note,
        })

        logger.info(
            f"💰 Deposit received: ${amount_usdt:,.2f} USDT | "
            + " | ".join(f"{k}=+${v:.0f}" for k, v in allocation.items())
            + (f" | {note}" if note else "")
        )
        return allocation

    def deposit_thb(self, amount_thb: float, actual_usdt: float = None, note: str = "") -> dict[str, float]:
        """
        Deposit Thai Baht monthly contribution.

        IMPORTANT: THB/USDT exchange rates change daily.
        - If you already know how much USDT Binance credited you, pass actual_usdt.
          This is always preferred for real money.
        - If you leave actual_usdt=None, the manager estimates using thb_to_usdt rate
          (only suitable for rough planning — not for live use).

        Args:
            amount_thb:  The THB amount you sent (for record keeping).
            actual_usdt: The USDT you actually received after conversion.
                         Check this on Binance after the wire clears.
                         If None, estimates from thb_to_usdt rate.
            note:        Optional label for this deposit.
        """
        if actual_usdt is not None:
            usdt = round(actual_usdt, 2)
            effective_rate = usdt / amount_thb
            logger.info(
                f"Deposit: {amount_thb:,.0f} THB → ${usdt:,.2f} USDT "
                f"(actual rate: {effective_rate:.5f} THB/USDT)"
            )
        else:
            usdt = round(amount_thb * self.thb_to_usdt, 2)
            logger.warning(
                f"Estimated conversion: {amount_thb:,.0f} THB → ${usdt:,.2f} USDT "
                f"@ {self.thb_to_usdt} (approximate). "
                f"For live trading, pass actual_usdt= from your Binance statement."
            )
        return self.deposit(usdt, note=note or f"{amount_thb:,.0f} THB deposit")

    # ── Market indicators ─────────────────────────────────────────────────────

    def compute_indicators(self, df: pd.DataFrame) -> dict:
        """
        Compute the indicator values needed by deployment conditions.

        Args:
            df: OHLCV DataFrame (at least 30 rows).

        Returns:
            Dict with keys: rsi, atr_pct, supertrend_direction, breakout_confirmed.
        """
        close = df["close"]
        high  = df["high"]
        low   = df["low"]

        # RSI
        rsi = float(
            ta.momentum.RSIIndicator(close, window=14).rsi().iloc[-1]
        )

        # ATR %
        atr = ta.volatility.AverageTrueRange(
            high=high, low=low, close=close, window=14
        ).average_true_range()
        current_price = float(close.iloc[-1])
        atr_pct = float(atr.iloc[-1]) / current_price * 100

        # Supertrend direction (simplified — same calc as SupertrendStrategy)
        hl2 = (high + low) / 2
        atr_val = atr
        upper = (hl2 + 3.5 * atr_val).ffill()
        lower = (hl2 - 3.5 * atr_val).ffill()
        mid = (upper + lower) / 2
        supertrend_direction = 1 if current_price > float(mid.iloc[-1]) else -1

        # Breakout: price outside recent 20-bar range + volume spike
        lookback = df.iloc[-21:-1]
        resistance = float(lookback["high"].max())
        support = float(lookback["low"].min())
        avg_vol = float(df["volume"].iloc[-21:-1].mean())
        current_vol = float(df["volume"].iloc[-1])
        broke_up   = current_price > resistance
        broke_down = current_price < support
        vol_spike  = current_vol >= avg_vol * 1.5
        breakout_confirmed = (broke_up or broke_down) and vol_spike

        return {
            "rsi":                   rsi,
            "atr_pct":               atr_pct,
            "supertrend_direction":  supertrend_direction,
            "breakout_confirmed":    breakout_confirmed,
            "current_price":         current_price,
        }

    # ── Deployment evaluation ─────────────────────────────────────────────────

    def evaluate_deployment(self, indicators: dict) -> dict[str, float]:
        """
        Check deployment conditions for each bucket and return how much
        capital is ready to be deployed right now.

        Args:
            indicators: Output of compute_indicators().

        Returns:
            Dict {bucket_name: usdt_ready_to_deploy}. Zero if condition not met.
        """
        ready = {}
        for name, bucket in self.buckets.items():
            if bucket.pending <= 0:
                ready[name] = 0.0
                continue

            condition_fn = DEPLOY_CONDITIONS[name]
            can_deploy, reason = condition_fn(indicators)

            if can_deploy:
                ready[name] = bucket.pending
                logger.debug(f"[DepositMgr] {name.upper()} READY ${bucket.pending:.2f} — {reason}")
            else:
                ready[name] = 0.0
                logger.debug(f"[DepositMgr] {name.upper()} WAITING ${bucket.pending:.2f} — {reason}")

        deployable = sum(ready.values())
        if deployable > 0:
            logger.info(
                f"[DepositMgr] ${deployable:.2f} ready to deploy | "
                + " | ".join(f"{k}=${v:.0f}" for k, v in ready.items() if v > 0)
            )

        return ready

    # ── Allocation access ─────────────────────────────────────────────────────

    def get_allocation(self, bucket: str) -> float:
        """
        Return the pending USDT available for a given bucket.

        Use this to check how much capital a strategy can use for its next trade.
        """
        if bucket not in self.buckets:
            raise ValueError(f"Unknown bucket '{bucket}'. Must be one of: {BUCKETS}")
        return self.buckets[bucket].pending

    def consume(self, bucket: str, amount_usdt: float) -> None:
        """
        Mark capital as deployed. Call this when a trade is executed.

        Args:
            bucket:       Which bucket the trade came from.
            amount_usdt:  USDT amount actually spent.
        """
        if bucket not in self.buckets:
            raise ValueError(f"Unknown bucket '{bucket}'. Must be one of: {BUCKETS}")
        b = self.buckets[bucket]
        if amount_usdt > b.pending + 0.01:
            logger.warning(
                f"[DepositMgr] {bucket.upper()} consumed ${amount_usdt:.2f} "
                f"but only ${b.pending:.2f} was pending — capping at pending."
            )
            amount_usdt = b.pending
        b.pending  -= amount_usdt
        b.deployed += amount_usdt
        self.total_deployed += amount_usdt
        logger.info(
            f"[DepositMgr] {bucket.upper()} deployed ${amount_usdt:.2f} | "
            f"Remaining pending: ${b.pending:.2f} | "
            f"Total deployed from bucket: ${b.deployed:.2f}"
        )

    def release_reserve(self, amount_usdt: float) -> None:
        """
        Manually release reserve funds for deployment (e.g. on a flash crash).
        Moves funds from reserve → the bucket with the most pending capital.
        """
        reserve = self.buckets["reserve"]
        to_release = min(amount_usdt, reserve.pending)
        if to_release <= 0:
            logger.warning("[DepositMgr] No reserve funds available to release.")
            return

        reserve.pending -= to_release

        # Put released funds into DCA (best absorbs any price)
        self.buckets["dca"].pending += to_release
        logger.info(
            f"[DepositMgr] 🔓 Reserve released: ${to_release:.2f} → DCA bucket | "
            f"Reserve remaining: ${reserve.pending:.2f}"
        )

    # ── Reporting ─────────────────────────────────────────────────────────────

    def summary(self) -> str:
        lines = [
            "=" * 56,
            "         DEPOSIT MANAGER SUMMARY",
            "=" * 56,
            f"  Total deposited : ${self.total_deposited:>10,.2f}",
            f"  Total deployed  : ${self.total_deployed:>10,.2f}",
            f"  Total pending   : ${self.total_deposited - self.total_deployed:>10,.2f}",
            "",
            f"  {'Bucket':<12} {'Weight':>6}  {'Pending':>10}  {'Deployed':>10}  {'Received':>10}",
            f"  {'-'*12} {'-'*6}  {'-'*10}  {'-'*10}  {'-'*10}",
        ]
        for name, b in self.buckets.items():
            lines.append(
                f"  {name:<12} {b.weight*100:>5.0f}%  "
                f"${b.pending:>9,.2f}  ${b.deployed:>9,.2f}  ${b.total_received:>9,.2f}"
            )
        lines.append("=" * 56)
        return "\n".join(lines)

    @property
    def pending_total(self) -> float:
        return sum(b.pending for b in self.buckets.values())

    @property
    def deployment_ratio(self) -> float:
        """What fraction of total deposits has been deployed (0.0 – 1.0)."""
        if self.total_deposited == 0:
            return 0.0
        return self.total_deployed / self.total_deposited
