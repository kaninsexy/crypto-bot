"""
portfolio/leverage_guard.py — Portfolio-level leverage safety system.

Three protections for multi-strategy futures trading:
  1. Liquidation guard  — warns when any position is <20% from force-liquidation
  2. Regime leverage caps — limits max leverage based on current market regime
  3. Correlated position risk — warns when multiple strategies hold the same symbol
     and combined potential loss exceeds 15% of total capital

All methods are pure calculations — no API calls, no side effects.
Safe to run in both paper and live mode.

Usage:
    guard = LeverageGuard(max_portfolio_loss_pct=0.30)

    # Check if any position is near liquidation
    warnings = guard.check_liquidation_safety(positions, prices, capital)

    # Check for excessive correlated exposure
    corr = guard.check_correlated_risk(positions, prices, capital)

    # Cap a strategy's requested leverage to regime limits
    safe_lev = guard.apply_leverage_cap(requested=3.0, regime="BULL", strategy_name="Breakout")
    # → 1.5 (BULL cap) with a WARNING log
"""

from loguru import logger


# ── Constants ─────────────────────────────────────────────────────────────────

# Max allowed leverage per regime.
# Conservative regimes (BEAR/CRASH/RANGE/VOLATILE) are locked at 1× because:
#   - BEAR/CRASH: trend is against longs; leverage accelerates losses
#   - RANGE/VOLATILE: no clear direction + high ATR = liquidation risk on both sides
REGIME_LEVERAGE_CAPS: dict[str, float] = {
    "STRONG_BULL": 2.0,
    "BULL":        1.5,
    "RANGE":       1.0,
    "VOLATILE":    1.0,
    "BEAR":        1.0,
    "CRASH":       1.0,
}

# Binance USDT-M maintenance margin rate for tier-1 notional (< 50 BTC equivalent).
# Used in the correct liquidation price formula.  0.4% is the conservative default
# for all symbols; larger positions have higher MMR tiers but those are rare here.
MAINTENANCE_MARGIN_RATE: float = 0.004

# Distance-to-liquidation thresholds for severity levels.
# Three bands give actionable urgency rather than a single binary warn/no-warn.
LIQ_CRITICAL_PCT: float  = 5.0    # < 5%  → CRITICAL (very close, act now)
LIQ_HIGH_PCT: float      = 10.0   # < 10% → HIGH (elevated risk)
LIQ_WARNING_PCT: float   = 20.0   # < 20% → WARNING (worth watching)

# Warn if combined potential loss across all strategies in one symbol
# exceeds this % of total capital.  15% = meaningful but not catastrophic.
CORR_LOSS_WARN_PCT: float = 15.0

# Conservative stop-loss distance to assume when a position has no SL set.
# 10% is worse than most strategies actually use — errs on the side of caution.
DEFAULT_SL_DIST_PCT: float = 0.10


# ── LeverageGuard ─────────────────────────────────────────────────────────────

class LeverageGuard:
    """
    Portfolio-level leverage safety system.

    Instantiate once in PortfolioManager.__init__() and call its check methods
    inside run_candle() every 4 candles.  The summary section of the dashboard
    also calls these for display.

    Args:
        max_portfolio_loss_pct: Stored for future portfolio-wide circuit use.
                                Currently 0.30 = 30% max acceptable loss.
    """

    def __init__(self, max_portfolio_loss_pct: float = 0.30):
        self.max_portfolio_loss_pct = max_portfolio_loss_pct

    # ── 1. Liquidation safety ─────────────────────────────────────────────────

    def check_liquidation_safety(
        self,
        positions: list[dict],
        current_prices: dict,
        total_capital: float,
    ) -> list[dict]:
        """
        For each open leveraged position, compute how far the current price is
        from the forced-liquidation level and warn if that gap is dangerously small.

        Liquidation price derivation (Binance USDT-M isolated margin):

            Binance liquidates when your unrealised loss equals your margin minus
            the maintenance margin they keep as a buffer.  Rearranging their
            published formula for isolated margin:

            Long:  liq_price = entry × (1 - 1/leverage + MMR)
            Short: liq_price = entry × (1 + 1/leverage - MMR)

            where MMR = MAINTENANCE_MARGIN_RATE (0.004 = 0.4% for tier-1 BTC).

        Example — 3× long at entry $71,000:
            liq_price = 71,000 × (1 - 1/3 + 0.004)
                      = 71,000 × (1 - 0.3333 + 0.004)
                      = 71,000 × 0.6707
                      = $47,619.70
            At current price $52,000: distance = (52,000 - 47,619.70) / 52,000 × 100 = 8.4% → HIGH

        Why the old formula (entry × (1 - 0.9/leverage)) was wrong:
            That approximation treats maintenance margin as a fraction of the
            margin used, but Binance's actual formula uses MMR as a fixed rate
            on notional, not on the 1/leverage fraction.  The error is
            significant at high leverage (e.g. 3×: old gives $49,700 vs
            correct $47,620 — a $2,080 difference in the danger zone).

        At 1× leverage there is no liquidation risk (spot equivalent), so
        1× positions are skipped entirely.

        Severity levels based on distance to liquidation:
            < 5%:  CRITICAL — imminent, consider closing immediately
            < 10%: HIGH     — elevated risk, tighten stop or reduce size
            < 20%: WARNING  — worth watching; normal SL should protect

        Args:
            positions:      List of dicts from PortfolioManager._get_open_positions_summary().
                            Each dict must contain:
                              strategy, symbol, entry, qty, leverage, side, stop_loss
            current_prices: {symbol: current_price}  — prices keyed by trading pair.
            total_capital:  Total portfolio USDT (for context; not used in calculation).

        Returns:
            List of warning dicts, one per at-risk position (distance < 20%):
            {
                strategy:     str    — which strategy owns this position
                symbol:       str    — trading pair (e.g. "BTC/USDT")
                side:         str    — "long" or "short"
                severity:     str    — "CRITICAL" | "HIGH" | "WARNING"
                leverage:     float  — position leverage
                entry:        float  — average entry price
                price:        float  — current market price
                liq_price:    float  — estimated liquidation price
                distance_pct: float  — % gap between current price and liq_price
                notional:     float  — position value in USDT (qty × price)
                margin_used:  float  — margin locked = notional / leverage
            }
            Empty list if no positions are within 20% of liquidation.
        """
        result = []

        for pos in positions:
            symbol   = pos["symbol"]
            price    = current_prices.get(symbol)

            if price is None or price <= 0:
                continue

            leverage = pos["leverage"]
            if leverage <= 1:
                # 1× = no leverage = no liquidation risk
                continue

            entry = pos["entry"]
            qty   = pos["qty"]
            side  = pos["side"]

            # Notional value and isolated margin committed
            notional    = qty * price
            margin_used = notional / leverage

            # Correct Binance USDT-M isolated margin liquidation price formula.
            # Long:  entry × (1 - 1/leverage + MMR)
            # Short: entry × (1 + 1/leverage - MMR)
            mmr = MAINTENANCE_MARGIN_RATE
            if side == "long":
                liq_price    = entry * (1.0 - (1.0 / leverage) + mmr)
                distance_pct = (price - liq_price) / price * 100.0
            else:  # short
                liq_price    = entry * (1.0 + (1.0 / leverage) - mmr)
                distance_pct = (liq_price - price) / price * 100.0

            # Only report positions within the WARNING threshold
            if distance_pct >= LIQ_WARNING_PCT:
                continue

            # Assign severity band
            if distance_pct < LIQ_CRITICAL_PCT:
                severity = "CRITICAL"
            elif distance_pct < LIQ_HIGH_PCT:
                severity = "HIGH"
            else:
                severity = "WARNING"

            result.append({
                "strategy":     pos["strategy"],
                "symbol":       symbol,
                "side":         side,
                "severity":     severity,
                "leverage":     leverage,
                "entry":        round(entry,     4),
                "price":        round(price,     4),
                "liq_price":    round(liq_price, 4),
                "distance_pct": round(distance_pct, 2),
                "notional":     round(notional,    2),
                "margin_used":  round(margin_used, 2),
            })

        return result

    # ── 2. Correlated position risk ───────────────────────────────────────────

    def check_correlated_risk(
        self,
        positions: list[dict],
        current_prices: dict,
        total_capital: float,
    ) -> dict:
        """
        Group open positions by trading symbol and quantify combined exposure risk.

        The problem this solves:
            DCA strategy opens 0.3 BTC long = $9,000 notional.
            TrendFollowing ALSO opens 0.2 BTC long = $6,000 notional.
            Each strategy's stop-loss looks fine in isolation (e.g. -3% each).
            But combined: both SLs fire on the same price drop → $450 + $300
            = $750 loss hits the portfolio simultaneously. That's 7.5% of $10k.

        This check surfaces that combined worst-case loss BEFORE it happens.

        Args:
            positions:      List of dicts from _get_open_positions_summary().
            current_prices: {symbol: current_price}
            total_capital:  Total portfolio USDT capital.

        Returns:
            Dict keyed by symbol (only symbols with ≥1 open position):
            {
                symbol: {
                    strategies:             [str]   — names of exposed strategies
                    total_notional:         float   — sum of (qty × price) across strategies
                    exposure_pct:           float   — total_notional / total_capital × 100
                    max_combined_loss_pct:  float   — worst-case simultaneous SL loss / total_capital × 100
                    warning:                bool    — True if max_combined_loss_pct > 15%
                }
            }

        Note on max_combined_loss_pct:
            - If a position HAS a stop_loss: loss = qty × |entry - stop_loss|
            - If NO stop_loss is set:        loss = notional × 10% (conservative default)
            This means the number is comparable to your strategy's actual risk,
            not inflated by no-SL assumptions.
        """
        # Group positions by symbol
        by_symbol: dict[str, list[dict]] = {}
        for pos in positions:
            sym = pos["symbol"]
            by_symbol.setdefault(sym, []).append(pos)

        result: dict = {}

        for sym, sym_positions in by_symbol.items():
            price = current_prices.get(sym)
            if price is None or price <= 0 or total_capital <= 0:
                continue

            strategies        = [p["strategy"] for p in sym_positions]
            total_notional    = 0.0
            max_combined_loss = 0.0

            for pos in sym_positions:
                qty   = pos["qty"]
                entry = pos["entry"]
                sl    = pos["stop_loss"]
                side  = pos["side"]

                notional        = qty * price
                total_notional += notional

                # Loss if the stop-loss fires
                if sl is not None and sl > 0:
                    if side == "long":
                        # Loss per unit = entry - sl (we bought high, SL fires lower)
                        sl_dist = max(0.0, entry - sl)
                    else:
                        # Loss per unit = sl - entry (we sold high, SL fires higher)
                        sl_dist = max(0.0, sl - entry)
                    loss_at_sl = qty * sl_dist
                else:
                    # No SL set — assume 10% of notional as conservative worst case
                    loss_at_sl = notional * DEFAULT_SL_DIST_PCT

                max_combined_loss += loss_at_sl

            exposure_pct          = (total_notional    / total_capital) * 100.0
            max_combined_loss_pct = (max_combined_loss / total_capital) * 100.0

            result[sym] = {
                "strategies":            strategies,
                "total_notional":        round(total_notional,       2),
                "exposure_pct":          round(exposure_pct,         2),
                "max_combined_loss_pct": round(max_combined_loss_pct, 2),
                "warning":               max_combined_loss_pct > CORR_LOSS_WARN_PCT,
            }

        return result

    # ── 3. Regime leverage caps ───────────────────────────────────────────────

    def get_regime_leverage_cap(self, regime: str) -> float:
        """
        Return the maximum allowed leverage for the given market regime.

        Regime → cap logic:
            STRONG_BULL: 2.0 — clear uptrend, limited downside risk for longs
            BULL:        1.5 — moderate uptrend, some caution warranted
            RANGE:       1.0 — no directional edge; leverage is pure speculation
            VOLATILE:    1.0 — high ATR means wide swings; liquidation risk elevated
            BEAR:        1.0 — downtrend active; longs face constant headwind
            CRASH:       1.0 — survival mode; preserve capital, no leverage

        Unknown regime strings default to 1.0 (safe fallback).

        Args:
            regime: Regime string as produced by RegimeDetector
                    (e.g. "STRONG_BULL", "BULL", "RANGE", "VOLATILE", "BEAR", "CRASH")

        Returns:
            Float ≥ 1.0.
        """
        return REGIME_LEVERAGE_CAPS.get(regime, 1.0)

    def apply_leverage_cap(
        self,
        requested_leverage: float,
        regime: str,
        strategy_name: str,
    ) -> float:
        """
        Return the leverage to actually use: min(requested, regime_cap).

        Logs a WARNING if the cap reduces the strategy's requested leverage so
        that it's obvious in the log why a position was opened with less than
        expected leverage.

        Args:
            requested_leverage: Leverage the strategy wants (e.g. 3.0 from Breakout).
            regime:             Current market regime string.
            strategy_name:      Strategy name for the log message.

        Returns:
            Float in [1.0, requested_leverage].
        """
        cap    = self.get_regime_leverage_cap(regime)
        capped = min(requested_leverage, cap)

        if capped < requested_leverage:
            logger.warning(
                f"[LeverageGuard] {strategy_name} leverage capped: "
                f"{requested_leverage}x → {capped}x "
                f"(regime={regime}, cap={cap}x)"
            )

        return capped
