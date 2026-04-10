"""
portfolio/kelly.py — Kelly Criterion Position Sizing (Phase E)

PURPOSE
───────
The Kelly Criterion answers: "What fraction of your capital should you
risk on each trade to maximise long-run growth?"

FORMULA
───────
    f* = (p × b − q) / b

Where:
    p  = win probability (e.g. 0.706 for 70.6% win rate)
    q  = 1 − p  (loss probability)
    b  = avg_win / avg_loss  (expressed as a ratio of absolute returns)

EXAMPLE (DCA OOS results from Phase C backtest):
    win_rate  = 70.6%  →  p = 0.706, q = 0.294
    avg_win   = +3.58%
    avg_loss  = -1.38%
    b         = 3.58 / 1.38 = 2.594

    f* = (0.706 × 2.594 − 0.294) / 2.594
       = (1.831 − 0.294) / 2.594
       = 0.592  → 59.2% of capital per trade (FULL KELLY — too aggressive!)

PRACTICAL USAGE
───────────────
Full Kelly is almost never used in practice because:
  - Backtest win rates have estimation error
  - Real markets are correlated (multiple strategies lose together)
  - Full Kelly drawdowns can be 50%+ at the trough

We use HALF-KELLY (f*/2) by default:
  - Achieves ~75% of full Kelly growth rate
  - Cuts drawdown roughly in half
  - Still aggressive vs traditional 1-2% risk sizing

For a new strategy with limited backtest data, use QUARTER-KELLY (f*/4).

MULTI-STRATEGY PORTFOLIO
────────────────────────
When running 6 strategies in parallel, the Kelly fractions are NOT additive.
If each strategy alone says "risk 30% of capital", you can't run 6 of them
simultaneously risking 180%. Instead:

  1. Compute Kelly fraction for each strategy independently.
  2. Scale down proportionally so the SUM of Kelly fractions ≤ 1.0.
     (This is the "portfolio-level Kelly" approach.)
  3. Multiply each strategy's fraction by its regime allocation weight.

USAGE
─────
    from portfolio.kelly import KellyCalculator, PHASE_C_PROFILES

    calc = KellyCalculator()

    # Use Phase C OOS backtest results
    profiles = calc.build_profiles(PHASE_C_PROFILES)

    # Get per-trade sizing for DCA with $10,000 portfolio
    fraction = profiles["DCA"].half_kelly_fraction
    trade_size = fraction × 10_000   # e.g. $2,960

    # Get regime-adjusted sizing
    regime_weight = 0.25  # DCA gets 25% of portfolio in BULL regime
    sizing = calc.regime_adjusted_size(
        strategy="DCA",
        regime_weight=regime_weight,
        portfolio_value=10_000,
        profiles=profiles,
    )
"""

from __future__ import annotations
import math
from dataclasses import dataclass
from loguru import logger


# ── Phase C OOS backtest results — used to seed initial Kelly profiles ────────
# Source: backtest/standalone.py OOS period results (synthetic GBM, seed=42).
# On production: replace with real Binance data backtest results from runner.py.

PHASE_C_PROFILES: dict[str, dict] = {
    "DCA": {
        "win_rate":  0.706,     # 70.6%
        "avg_win":   3.58,      # % per trade
        "avg_loss":  1.38,      # % per trade (absolute value)
        "n_trades":  17,        # OOS sample size
    },
    "Supertrend": {
        "win_rate":  0.383,
        "avg_win":   3.74,
        "avg_loss":  1.79,
        "n_trades":  47,
    },
    "MeanReversion": {
        "win_rate":  0.750,
        "avg_win":   2.19,
        "avg_loss":  3.08,
        "n_trades":  24,
    },
    "GridTrading": {
        "win_rate":  0.909,
        "avg_win":   0.70,
        "avg_loss":  1.68,
        "n_trades":  22,
    },
    "Breakout": {
        "win_rate":  0.273,
        "avg_win":   3.81,
        "avg_loss":  1.38,
        "n_trades":  11,
    },
    "TrendFollowing": {
        "win_rate":  0.600,
        "avg_win":   9.95,
        "avg_loss":  2.07,
        "n_trades":  5,
    },
}


# ── Dataclasses ───────────────────────────────────────────────────────────────

@dataclass
class KellyProfile:
    """
    Kelly analysis for a single strategy.

    Attributes:
        strategy:            Strategy name.
        win_rate:            Fraction of trades that are winners (0.0 – 1.0).
        avg_win_pct:         Average return on winning trades (%).
        avg_loss_pct:        Average loss on losing trades (%, absolute value).
        n_trades:            Number of OOS trades used to compute this profile.
        b_ratio:             avg_win / avg_loss (reward-to-risk ratio).
        full_kelly:          Full Kelly fraction (can be > 1.0 if very edge-positive).
        half_kelly:          f*/2  — recommended for production use.
        quarter_kelly:       f*/4  — recommended for strategies with few OOS trades.
        recommended_kelly:   half or quarter depending on n_trades confidence.
        edge:                Expected value per trade (p*b - q). Positive = profitable.
        confidence_discount: Multiplier applied for low sample size (<30 trades).
    """
    strategy:          str
    win_rate:          float
    avg_win_pct:       float
    avg_loss_pct:      float
    n_trades:          int
    b_ratio:           float
    full_kelly:        float
    half_kelly:        float
    quarter_kelly:     float
    recommended_kelly: float
    edge:              float
    confidence_discount: float


# ── Kelly Calculator ──────────────────────────────────────────────────────────

class KellyCalculator:
    """
    Computes Kelly fractions for individual strategies and portfolio-level sizing.
    """

    def __init__(
        self,
        kelly_fraction: float = 0.5,      # Default: half Kelly
        min_trades_full: int  = 30,       # Minimum trades for full half-Kelly confidence
        min_trades_any:  int  = 10,       # Below this: use quarter Kelly regardless
        max_kelly_cap:   float = 0.35,    # Never risk more than 35% of portfolio on one strategy
    ):
        """
        Args:
            kelly_fraction:  Fraction of full Kelly to use (0.5 = half Kelly).
            min_trades_full: OOS sample size needed for full confidence.
            min_trades_any:  Below this, apply extra discount to Kelly fraction.
            max_kelly_cap:   Hard cap on per-strategy Kelly fraction.
        """
        self.kelly_fraction  = kelly_fraction
        self.min_trades_full = min_trades_full
        self.min_trades_any  = min_trades_any
        self.max_kelly_cap   = max_kelly_cap

    def compute_kelly(
        self,
        win_rate: float,
        avg_win_pct: float,
        avg_loss_pct: float,
        n_trades: int = 30,
    ) -> KellyProfile:
        """
        Compute Kelly fractions for a single strategy.

        Args:
            win_rate:     Fraction of winning trades (e.g. 0.706).
            avg_win_pct:  Average winning trade return in % (e.g. 3.58).
            avg_loss_pct: Average losing trade loss in % (absolute, e.g. 1.38).
            n_trades:     OOS sample size (affects confidence discount).

        Returns:
            KellyProfile with all sizing fractions.
        """
        if avg_loss_pct <= 0:
            raise ValueError("avg_loss_pct must be positive (it's the absolute loss %).")

        p = win_rate
        q = 1 - p
        b = avg_win_pct / avg_loss_pct   # reward-to-risk ratio

        # Full Kelly: f* = (p*b - q) / b
        edge       = p * b - q            # Expected return per unit risked
        full_kelly = edge / b             # Kelly fraction

        # If edge is negative: strategy is unprofitable — Kelly says "don't trade"
        if edge <= 0 or full_kelly <= 0:
            logger.warning(
                f"Negative edge ({edge:.4f}) for strategy. "
                f"Full Kelly = {full_kelly:.4f} (don't trade!)"
            )
            return KellyProfile(
                strategy="unknown",
                win_rate=win_rate, avg_win_pct=avg_win_pct,
                avg_loss_pct=avg_loss_pct, n_trades=n_trades,
                b_ratio=round(b, 4), full_kelly=0.0,
                half_kelly=0.0, quarter_kelly=0.0,
                recommended_kelly=0.0, edge=round(edge, 4),
                confidence_discount=0.0,
            )

        # Confidence discount based on sample size
        if n_trades >= self.min_trades_full:
            confidence_discount = 1.0
        elif n_trades >= self.min_trades_any:
            # Linear interpolation between 0.5 and 1.0
            confidence_discount = 0.5 + 0.5 * (n_trades - self.min_trades_any) / (self.min_trades_full - self.min_trades_any)
        else:
            confidence_discount = 0.5   # Minimum trust factor

        half_kelly    = min(full_kelly * 0.5 * confidence_discount, self.max_kelly_cap)
        quarter_kelly = min(full_kelly * 0.25 * confidence_discount, self.max_kelly_cap / 2)

        # Recommendation: half Kelly if n_trades ≥ 20, else quarter Kelly
        recommended_kelly = half_kelly if n_trades >= 20 else quarter_kelly

        return KellyProfile(
            strategy="unknown",
            win_rate=win_rate, avg_win_pct=avg_win_pct,
            avg_loss_pct=avg_loss_pct, n_trades=n_trades,
            b_ratio=round(b, 4),
            full_kelly=round(full_kelly, 4),
            half_kelly=round(half_kelly, 4),
            quarter_kelly=round(quarter_kelly, 4),
            recommended_kelly=round(recommended_kelly, 4),
            edge=round(edge, 4),
            confidence_discount=round(confidence_discount, 3),
        )

    def build_profiles(
        self,
        backtest_results: dict[str, dict],
    ) -> dict[str, KellyProfile]:
        """
        Build Kelly profiles for all strategies from Phase C backtest results.

        Args:
            backtest_results: Dict like PHASE_C_PROFILES above.
                Keys: strategy name.
                Values: dict with win_rate, avg_win, avg_loss, n_trades.

        Returns:
            Dict of {strategy_name: KellyProfile}.
        """
        profiles = {}
        for name, data in backtest_results.items():
            profile = self.compute_kelly(
                win_rate=data["win_rate"],
                avg_win_pct=data["avg_win"],
                avg_loss_pct=data["avg_loss"],
                n_trades=data.get("n_trades", 20),
            )
            profile.strategy = name
            profiles[name] = profile
            logger.info(
                f"[Kelly] {name:>15} | "
                f"WR={data['win_rate']*100:.1f}% | "
                f"b={profile.b_ratio:.2f} | "
                f"edge={profile.edge:+.4f} | "
                f"½K={profile.half_kelly:.3f} "
                f"({'⚠ no-trade' if profile.half_kelly == 0 else ''})"
            )

        return profiles

    def portfolio_kelly_sizes(
        self,
        profiles: dict[str, KellyProfile],
        regime_allocations: dict[str, float],
        portfolio_value: float,
    ) -> dict[str, float]:
        """
        Compute per-trade USDT sizing for all strategies, combining:
          Kelly fraction × regime allocation weight × portfolio value

        The portfolio-level Kelly check ensures the sum of all concurrent
        risk exposures doesn't exceed a safe fraction of the total portfolio.

        Args:
            profiles:           Dict of {strategy: KellyProfile}.
            regime_allocations: Dict of {strategy: weight} summing to 1.0.
                                From regime_detector.get_allocations().
            portfolio_value:    Total portfolio equity in USDT.

        Returns:
            Dict of {strategy_name: usdt_per_trade}
        """
        sizes = {}

        for name, profile in profiles.items():
            regime_weight = regime_allocations.get(name.lower(), 0.0)
            if regime_weight == 0.0 or profile.recommended_kelly == 0.0:
                sizes[name] = 0.0
                continue

            # Strategy's share of total portfolio (by regime weight)
            strategy_capital = portfolio_value * regime_weight

            # Kelly fraction of strategy capital = per-trade size
            usdt_per_trade = strategy_capital * profile.recommended_kelly

            # Hard floor and ceiling
            usdt_per_trade = max(10.0, min(usdt_per_trade, strategy_capital * self.max_kelly_cap))

            sizes[name] = round(usdt_per_trade, 2)

        logger.info(
            f"[Kelly] Portfolio sizing (${portfolio_value:,.0f}) | "
            + " | ".join(f"{k}=${v:.0f}" for k, v in sizes.items() if v > 0)
        )
        return sizes

    # ── Reporting ─────────────────────────────────────────────────────────

    def summary(self, profiles: dict[str, KellyProfile]) -> str:
        lines = [
            "═" * 72,
            "  KELLY CRITERION — POSITION SIZING SUMMARY",
            "═" * 72,
            f"  {'Strategy':<16} {'WinRate':>8} {'b-ratio':>8} {'Edge':>8} "
            f"{'Full K':>8} {'½ Kelly':>8} {'¼ Kelly':>8} {'Recommended':>12}",
            "  " + "─" * 68,
        ]
        for name, p in profiles.items():
            lines.append(
                f"  {name:<16} {p.win_rate*100:>7.1f}% {p.b_ratio:>8.2f} "
                f"{p.edge:>+8.4f} {p.full_kelly:>8.3f} "
                f"{p.half_kelly:>8.3f} {p.quarter_kelly:>8.3f} "
                f"{p.recommended_kelly:>12.3f}"
            )

        # Portfolio totals
        total_half = sum(p.half_kelly for p in profiles.values())
        lines.extend([
            "  " + "─" * 68,
            f"  {'Sum of ½ Kelly fracs':16} {'':>8} {'':>8} {'':>8} "
            f"{'':>8} {total_half:>8.3f}",
            "",
            "  Note: ½ Kelly > 1.0 means over-diversified (strategies compete for capital).",
            "  Regime allocations proportionally scale each strategy's actual capital.",
            "═" * 72,
        ])
        return "\n".join(lines)
