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
import copy
import math
from collections import defaultdict
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


# ── ALL_REGIME_FALLBACK ───────────────────────────────────────────────────────
# Flat {strategy: stats} dict used as the regime-agnostic fallback whenever a
# (regime, strategy) cell is missing from REGIME_PRIORS below.
#
# The first 6 entries are seeded from PHASE_C_PROFILES VERBATIM so existing
# callers that happen to route through the fallback see identical numbers.
# The 4 trailing entries cover strategies currently absent from
# PHASE_C_PROFILES — today they receive 0 Kelly sizing, and this fix removes
# that silent zero.  n_trades=5 is intentionally below KellyCalculator's
# `min_trades_any=10` threshold so compute_kelly applies the maximum
# confidence discount (0.5) until Phase 2b backtest data replaces the
# placeholder values.

ALL_REGIME_FALLBACK: dict[str, dict] = {
    "DCA": {
        "win_rate":  0.706,
        "avg_win":   3.58,
        "avg_loss":  1.38,
        "n_trades":  17,
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
    "BearShort": {
        "win_rate":  0.45,
        "avg_win":   2.5,
        "avg_loss":  2.0,
        "n_trades":  5,    # TODO: replace with Phase 2b backtest (2022-2025 OKX)
    },
    "VWAP": {
        "win_rate":  0.55,
        "avg_win":   1.8,
        "avg_loss":  1.5,
        "n_trades":  5,    # TODO: replace with Phase 2b backtest (2022-2025 OKX)
    },
    "VolatilityBreakout": {
        "win_rate":  0.40,
        "avg_win":   4.0,
        "avg_loss":  2.0,
        "n_trades":  5,    # TODO: replace with Phase 2b backtest (2022-2025 OKX)
    },
    "DualMomentum": {
        "win_rate":  0.50,
        "avg_win":   3.5,
        "avg_loss":  2.5,
        "n_trades":  5,    # TODO: replace with Phase 2b backtest (2022-2025 OKX)
    },
}


# ── REGIME_PRIORS ─────────────────────────────────────────────────────────────
# Per-regime Bayesian priors for Kelly sizing.
# Shape: REGIME_PRIORS[regime][strategy] -> {win_rate, avg_win, avg_loss, n_trades}
#
# Intentionally empty — cells are populated from the Phase 2b backtest output
# (2022-2025 OKX, regime-labeled) in a follow-up task.  Missing cells fall
# back to ALL_REGIME_FALLBACK via
# RegimeAwareKellyCalculator.build_regime_profiles(), which preserves the
# current safe, regime-agnostic behaviour.
#
# To populate a cell:
#   1. Run backtest/runner.py for the strategy with regime labels enabled.
#   2. Extract per-regime {win_rate, avg_win, avg_loss, n_trades}.
#   3. Insert under REGIME_PRIORS[<REGIME_NAME>][<strategy>] = {...}.
#
# The regime name strings below MUST match the REGIME_* constants defined in
# portfolio/regime_detector.py exactly.

REGIME_PRIORS: dict[str, dict[str, dict]] = {
    "STRONG_BULL": {},
    "BULL":        {},
    "RANGE":       {},
    "BEAR":        {},
    "CRASH":       {},
    "VOLATILE":    {},
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

    def blend_with_live(
        self,
        prior: dict,        # {win_rate, avg_win, avg_loss, n_trades}
        live_stats: dict,   # same shape; may have n_trades == 0 (cold start)
    ) -> dict:
        """
        Bayesian blend of a backtest prior with live trading stats.

            blended = (prior * n_prior + live * n_live) / (n_prior + n_live)

        Cold-start and edge cases:
          - If live["n_trades"] == 0: return a COPY of prior unchanged.
          - If prior["n_trades"] == 0 and live["n_trades"] > 0: return a
            COPY of live_stats.
          - If both n_trades are 0: return a COPY of prior (safe default;
            blending zero data produces no information).

        Blending weights win_rate, avg_win, and avg_loss as a weighted
        average by n_trades. The returned n_trades is n_prior + n_live.

        Behaviour over time:
          n_live = 0    -> 100% prior
          n_live = 10   -> roughly 60-80% prior depending on n_prior
          n_live = 50   -> live dominates; prior acts as regularization
          prior persists as a floor — it never fully disappears.

        NOT a staticmethod: future versions may use instance state (e.g.
        per-strategy blend weights).
        """
        n_prior = prior.get("n_trades", 0)
        n_live  = live_stats.get("n_trades", 0)

        # Cold-start: no live trades yet (covers both-zero case too).
        if n_live == 0:
            return copy.deepcopy(prior)

        # No prior but we have live data — switch to the live sample verbatim.
        if n_prior == 0:
            return copy.deepcopy(live_stats)

        # Both populated — weighted average by sample size.
        n_total  = n_prior + n_live
        wr       = (prior["win_rate"] * n_prior + live_stats["win_rate"] * n_live) / n_total
        avg_win  = (prior["avg_win"]  * n_prior + live_stats["avg_win"]  * n_live) / n_total
        avg_loss = (prior["avg_loss"] * n_prior + live_stats["avg_loss"] * n_live) / n_total

        logger.debug(
            f"blend: n_prior={n_prior}, n_live={n_live} -> n_total={n_total}, wr={wr:.3f}"
        )

        return {
            "win_rate": wr,
            "avg_win":  avg_win,
            "avg_loss": avg_loss,
            "n_trades": n_total,
        }

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


# ── Live-stats computation (mode-agnostic) ────────────────────────────────────

def compute_live_stats(
    closed_trades: list[dict],
    regime_filter: str | None = None,
) -> dict[str, dict]:
    """
    Compute per-strategy Kelly-relevant stats from closed trades.

    MODE-AGNOSTIC: the caller loads trades from whichever store is
    appropriate (paper_state.json, SQLite trade log, in-memory buffer).
    This function performs no I/O.

    Expected trade dict shape (subset of flush_new_trades output):
        {
            "strategy": str,              # required
            "pnl_pct":  float,            # required; decimal % e.g. 3.58
            "regime":   str | None,       # optional; used by regime_filter
            ...other fields ignored
        }

    Args:
        closed_trades: list of closed trade dicts (any order).
        regime_filter: if set, include only trades where
                       trade.get("regime") == regime_filter. Trades
                       missing the "regime" key are EXCLUDED from
                       filtered output — prefer under-counting to
                       mis-attributing.

    Returns:
        {strategy_name: {"win_rate", "avg_win", "avg_loss", "n_trades"}}
        Strategies with zero trades after filtering are OMITTED from
        the returned dict (not included as zero-stat entries).

    Statistic definitions:
        win_rate = count(pnl_pct > 0) / n_trades
        avg_win  = mean(pnl_pct where pnl_pct > 0) or 0.0 if no wins
        avg_loss = mean(abs(pnl_pct) where pnl_pct < 0) or 0.0 if none
        n_trades = count of trades included
        pnl_pct == 0.0 is treated as a LOSS (defensive; rare edge case).

    Empty input list returns {}.
    """
    if not closed_trades:
        return {}

    # Group pnl_pct values per strategy after filtering.
    grouped: dict[str, list[float]] = defaultdict(list)
    for trade in closed_trades:
        if regime_filter is not None:
            trade_regime = trade.get("regime")
            # Missing or mismatched regime → exclude from filtered output.
            if trade_regime is None:
                continue
            if trade_regime != regime_filter:
                continue

        strategy = trade.get("strategy")
        if strategy is None:
            continue

        pnl_pct = trade.get("pnl_pct")
        if pnl_pct is None:
            continue

        grouped[strategy].append(float(pnl_pct))

    result: dict[str, dict] = {}
    for strategy, pnls in grouped.items():
        n_trades = len(pnls)
        # Defensive: defaultdict should never give us an empty list here,
        # but still omit zero-trade strategies per spec.
        if n_trades == 0:
            continue

        wins   = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]   # 0.0 counted as a loss per spec

        win_rate = len(wins) / n_trades
        avg_win  = (sum(wins) / len(wins)) if len(wins)   > 0 else 0.0
        avg_loss = (sum(abs(p) for p in losses) / len(losses)) if len(losses) > 0 else 0.0

        result[strategy] = {
            "win_rate": win_rate,
            "avg_win":  avg_win,
            "avg_loss": avg_loss,
            "n_trades": n_trades,
        }

    return result


# ── Regime-aware Kelly calculator ─────────────────────────────────────────────

class RegimeAwareKellyCalculator(KellyCalculator):
    """
    Kelly calculator that builds per-regime profiles by blending
    REGIME_PRIORS (or ALL_REGIME_FALLBACK when a regime cell is missing)
    with optional live trade stats.

    Inherits all existing KellyCalculator behaviour — existing callers can
    continue to use compute_kelly / build_profiles / portfolio_kelly_sizes
    unchanged.  Until REGIME_PRIORS cells are populated, this class
    produces results identical to using ALL_REGIME_FALLBACK as the prior
    for every strategy, so swapping a RegimeAwareKellyCalculator in for a
    plain KellyCalculator is behaviour-preserving.
    """

    def build_regime_profiles(
        self,
        regime: str,
        live_stats: dict[str, dict] | None = None,
    ) -> dict[str, KellyProfile]:
        """
        Build KellyProfile objects for every strategy in ALL_REGIME_FALLBACK
        under the given regime.

        For each strategy:
          1. Look up REGIME_PRIORS[regime][strategy] (the regime-specific
             prior). If absent, fall back to ALL_REGIME_FALLBACK[strategy]
             and tag the source as "FALLBACK".
          2. Look up live_stats[strategy] (cold-start safe: uses an all-
             zero live dict when absent or when live_stats is None).
          3. Blend prior with live via self.blend_with_live().
          4. Feed blended stats into self.compute_kelly() to produce the
             KellyProfile, set profile.strategy, and log the source tag.

        The `src=` field in the log is critical operational visibility:
        it shows at a glance which regime/strategy cells still need real
        backtest data (source=FALLBACK) vs which are populated
        (source=REGIME_PRIORS).

        Returns:
            {strategy: KellyProfile} for every strategy in
            ALL_REGIME_FALLBACK (all 10 active strategies).

        Raises:
            ValueError: if `regime` is not a key in REGIME_PRIORS.
        """
        if regime not in REGIME_PRIORS:
            raise ValueError(
                f"Unknown regime '{regime}'. "
                f"Expected one of: {sorted(REGIME_PRIORS.keys())}"
            )

        regime_cell = REGIME_PRIORS[regime]
        profiles: dict[str, KellyProfile] = {}

        for strategy in ALL_REGIME_FALLBACK.keys():
            # 1. Prior — explicit `is None`, never `or`.
            prior = regime_cell.get(strategy)
            if prior is None:
                prior  = ALL_REGIME_FALLBACK[strategy]
                source = "FALLBACK"
            else:
                source = "REGIME_PRIORS"

            # 2. Live stats — cold-start safe zero dict when missing.
            live = live_stats.get(strategy) if live_stats is not None else None
            if live is None:
                live = {
                    "win_rate": 0.0,
                    "avg_win":  0.0,
                    "avg_loss": 0.0,
                    "n_trades": 0,
                }

            # 3. Blend (blend_with_live returns a fresh dict; prior/live
            #    are never mutated).
            blended = self.blend_with_live(prior, live)

            # 4. Produce KellyProfile via the existing compute_kelly path.
            profile = self.compute_kelly(
                win_rate     = blended["win_rate"],
                avg_win_pct  = blended["avg_win"],
                avg_loss_pct = blended["avg_loss"],
                n_trades     = blended["n_trades"],
            )
            profile.strategy = strategy

            logger.info(
                f"[Kelly-{regime}] {strategy:>18} | src={source:<15} | "
                f"n_prior={prior['n_trades']:>3} n_live={live['n_trades']:>3} "
                f"-> ½K={profile.half_kelly:.3f}"
            )

            profiles[strategy] = profile

        return profiles


# ── Smoke test (runnable via `python -m portfolio.kelly`) ─────────────────────

if __name__ == "__main__":
    import sys

    try:
        calc = RegimeAwareKellyCalculator()

        # 1. Build profiles for each of the 6 regimes. live_stats=None
        #    exercises the cold-start path end-to-end.
        for regime in ("STRONG_BULL", "BULL", "RANGE", "BEAR", "CRASH", "VOLATILE"):
            profiles = calc.build_regime_profiles(regime, live_stats=None)
            assert len(profiles) == 10, (
                f"expected 10 strategies for {regime}, got {len(profiles)}"
            )
            summary_pairs = ", ".join(
                f"{name}=½K{p.half_kelly:.3f}" for name, p in profiles.items()
            )
            print(f"[{regime}] {summary_pairs}")

        # 2. blend_with_live — canonical 50/50 blend.
        prior = {"win_rate": 0.70, "avg_win": 3.0, "avg_loss": 1.5, "n_trades": 20}
        live  = {"win_rate": 0.50, "avg_win": 4.0, "avg_loss": 2.0, "n_trades": 20}
        blended = calc.blend_with_live(prior, live)
        assert blended["n_trades"] == 40, (
            f"expected n_trades=40, got {blended['n_trades']}"
        )
        assert abs(blended["win_rate"] - 0.60) < 1e-9, (
            f"expected win_rate≈0.60, got {blended['win_rate']}"
        )
        assert abs(blended["avg_win"] - 3.5) < 1e-9, (
            f"expected avg_win≈3.5, got {blended['avg_win']}"
        )
        assert abs(blended["avg_loss"] - 1.75) < 1e-9, (
            f"expected avg_loss≈1.75, got {blended['avg_loss']}"
        )

        # 3. Cold-start blend — live n_trades == 0 must return a fresh
        #    copy of the prior, unchanged.
        cold_live = {"win_rate": 0, "avg_win": 0, "avg_loss": 0, "n_trades": 0}
        blended = calc.blend_with_live(prior, cold_live)
        assert blended == prior, (
            f"cold-start blend should equal prior; got {blended}"
        )
        assert blended is not prior, (
            "cold-start blend must return a fresh copy, not the prior itself"
        )

        # 4. compute_live_stats — 3-strategy, 2-regime synthetic dataset.
        trades = [
            {"strategy": "DCA",         "pnl_pct":  3.0, "regime": "RANGE"},
            {"strategy": "DCA",         "pnl_pct": -1.5, "regime": "RANGE"},
            {"strategy": "DCA",         "pnl_pct":  4.0, "regime": "BULL"},
            {"strategy": "GridTrading", "pnl_pct":  0.7, "regime": "RANGE"},
            {"strategy": "Supertrend",  "pnl_pct":  2.0},  # no regime key
        ]

        unfiltered = compute_live_stats(trades)
        assert set(unfiltered) == {"DCA", "GridTrading", "Supertrend"}, (
            f"unfiltered keys mismatch: {sorted(unfiltered)}"
        )
        assert unfiltered["DCA"]["n_trades"] == 3, (
            f"DCA unfiltered n_trades should be 3, got {unfiltered['DCA']['n_trades']}"
        )

        range_only = compute_live_stats(trades, regime_filter="RANGE")
        assert "Supertrend" not in range_only, (
            "Supertrend has no regime key — must be excluded from RANGE filter"
        )
        assert range_only["DCA"]["n_trades"] == 2, (
            f"DCA RANGE n_trades should be 2, got {range_only['DCA']['n_trades']}"
        )
        assert abs(range_only["DCA"]["win_rate"] - 0.5) < 1e-9, (
            f"DCA RANGE win_rate should be 0.5, got {range_only['DCA']['win_rate']}"
        )

        empty = compute_live_stats([])
        assert empty == {}, f"empty trade list must return {{}}; got {empty}"

        print("All Phase 2c smoke tests passed.")
        sys.exit(0)

    except AssertionError as err:
        print(f"SMOKE TEST FAILED: {err}")
        sys.exit(1)
    except Exception as err:
        print(f"SMOKE TEST ERRORED: {type(err).__name__}: {err}")
        sys.exit(1)
