"""
portfolio/regime_detector.py — Market Regime Detector (Phase D)

Classifies the current market into one of 6 regimes using BTC/USDT
as the master market reference (BTC leads alt sentiment).

REGIMES
───────
  STRONG_BULL  — Sustained uptrend, rising momentum, low volatility
                 Best for: Breakout, TrendFollowing, Supertrend
  BULL         — Uptrend, normal momentum
                 Best for: Supertrend, TrendFollowing, DCA
  RANGE        — Sideways price action, low volatility
                 Best for: MeanReversion, GridTrading
  VOLATILE     — High volatility regardless of direction (e.g. news event)
                 Best for: DCA (absorbs), Grid (if ATR not too high), avoid Breakout
  BEAR         — Downtrend, weakening momentum
                 Best for: DCA (defensive), MeanReversion (carefully), hold cash
  CRASH        — Rapid drawdown, RSI extremely oversold, high fear
                 Best for: DCA only (high conviction buy), reserve deployment

DETECTION LOGIC
───────────────
  Four signal inputs, each scored:
    1. Trend      : EMA50 vs EMA200 slope
    2. Momentum   : RSI(14) level
    3. Volatility : ATR(14) as % of price
    4. Structure  : % below 50-day high (drawdown from recent peak)

  These combine into a regime score. The regime is re-detected every candle
  but only changes if it stays in the new state for 3+ consecutive candles
  (hysteresis to prevent flip-flopping on boundaries).

USAGE
─────
  from portfolio.regime_detector import RegimeDetector
  detector = RegimeDetector()
  regime, confidence, detail = detector.detect(btc_df)
  # regime → "BULL" | "STRONG_BULL" | "RANGE" | "VOLATILE" | "BEAR" | "CRASH"
  # confidence → 0.0 – 1.0
  # detail → dict of raw indicator values for debugging
"""

from __future__ import annotations
import pandas as pd
from dataclasses import dataclass
from loguru import logger


# ── Regime constants ──────────────────────────────────────────────────────────

REGIME_STRONG_BULL = "STRONG_BULL"
REGIME_BULL        = "BULL"
REGIME_RANGE       = "RANGE"
REGIME_VOLATILE    = "VOLATILE"
REGIME_BEAR        = "BEAR"
REGIME_CRASH       = "CRASH"

ALL_REGIMES = [
    REGIME_STRONG_BULL, REGIME_BULL, REGIME_RANGE,
    REGIME_VOLATILE,    REGIME_BEAR, REGIME_CRASH,
]

# ── Regime-to-allocation mappings (Phase D dynamic allocation) ────────────────
# These weights represent how much of the total ACTIVE portfolio goes to each
# strategy given the current market regime.
# Rows: regime. Cols: strategy (must sum to 1.0 per regime).
# Strategies with 0% receive no new capital until regime changes.

REGIME_ALLOCATIONS: dict[str, dict[str, float]] = {
    # MeanReversion allocation history:
    #   BEFORE (paper-trading suspension): 0% in all regimes.
    #     Reason: OOS backtest showed -9.36% return (Sharpe -2.800) in a bear-market test period,
    #     which is an inappropriate regime for MeanReversion anyway.
    #   AFTER (re-enabled): RANGE 10%, BULL 5%, all other regimes 0%.
    #     Funding source: DCA reduced by 0.10 in RANGE (0.30→0.20) and 0.05 in BULL (0.20→0.15).
    #     DCA chosen as the donor because it is the most buffer-like strategy and is least
    #     harmed by a small trim in regimes where MeanReversion is actively earning.
    REGIME_STRONG_BULL: {
        "dca":           0.15,
        "supertrend":    0.25,  # -0.05 to fund new strategies
        "meanrev":       0.00,  # Not appropriate — trending market kills mean reversion
        "grid":          0.05,
        "breakout":      0.30,  # -0.05 to fund new strategies
        "trend":         0.10,  # -0.05 to fund dual_momentum
        "bearshort":     0.00,
        "vwap":          0.00,  # VWAP underperforms in strong trend
        "volbreakout":   0.05,  # Larry Williams VB — ideal in strong bull
        "dual_momentum": 0.10,  # Dual momentum — rotates into strongest asset
    },
    REGIME_BULL: {
        "dca":           0.15,  # -0.05 to fund MeanReversion (was 0.20)
        "supertrend":    0.25,
        "meanrev":       0.05,  # Reduced allocation — trend strategies dominate in bull
        "grid":          0.10,
        "breakout":      0.20,  # -0.05 to fund new strategies
        "trend":         0.10,
        "bearshort":     0.00,
        "vwap":          0.05,  # VWAP works in mild bull
        "volbreakout":   0.05,  # VB active in BULL regime
        "dual_momentum": 0.05,  # Small allocation in normal bull
    },
    REGIME_RANGE: {
        # VWAP, Grid, and MeanReversion best suited for ranging markets.
        "dca":           0.20,  # -0.10 to fund MeanReversion (was 0.30)
        "supertrend":    0.08,
        "meanrev":       0.10,  # Primary MeanReversion regime (StochRSI + BB %B)
        "grid":          0.32,  # -0.05 to fund new strategies
        "breakout":      0.00,  # Breakout regime-filter blocks it in RANGE anyway
        "trend":         0.05,
        "bearshort":     0.00,
        "vwap":          0.10,  # Core VWAP regime
        "volbreakout":   0.05,  # VB active in RANGE regime
        "dual_momentum": 0.10,  # Rotation can find the ranging winner
    },
    REGIME_VOLATILE: {
        "dca":           0.45,  # -0.05 to fund dual_momentum
        "supertrend":    0.10,
        "meanrev":       0.00,  # Not appropriate — BB/StochRSI signals are noise in volatile
        "grid":          0.30,
        "breakout":      0.00,
        "trend":         0.10,
        "bearshort":     0.00,
        "vwap":          0.00,  # Too risky in volatile
        "volbreakout":   0.00,  # VB not active in VOLATILE (regime filter)
        "dual_momentum": 0.05,  # Abs momentum filter usually kicks in anyway
    },
    REGIME_BEAR: {
        "dca":           0.56,  # -0.05 to fund dual_momentum
        "supertrend":    0.06,
        "meanrev":       0.00,  # Not appropriate — buying dips in a downtrend
        "grid":          0.18,
        "breakout":      0.00,
        "trend":         0.00,
        "bearshort":     0.15,
        "vwap":          0.00,  # Not useful in bear
        "volbreakout":   0.00,  # VB regime filter blocks it in BEAR
        "dual_momentum": 0.05,  # Abs momentum filter typically triggers HOLD in bear
    },
    REGIME_CRASH: {
        "dca":           0.79,
        "supertrend":    0.00,
        "meanrev":       0.00,  # Never buy dips in a crash
        "grid":          0.06,
        "breakout":      0.00,
        "trend":         0.00,
        "bearshort":     0.15,
        "vwap":          0.00,
        "volbreakout":   0.00,  # Not active in CRASH
        "dual_momentum": 0.00,  # Blocked by regime_filter (CRASH guard)
    },
}

# Cash reserve % to hold back (not deployed to any strategy) per regime.
# This is in ADDITION to the above allocations.
REGIME_CASH_RESERVE: dict[str, float] = {
    REGIME_STRONG_BULL: 0.00,
    REGIME_BULL:        0.00,
    REGIME_RANGE:       0.00,
    REGIME_VOLATILE:    0.00,
    REGIME_BEAR:        0.15,
    REGIME_CRASH:       0.15,
}


# ── Regime detector ───────────────────────────────────────────────────────────

@dataclass
class RegimeReading:
    regime:     str
    confidence: float    # 0.0 – 1.0
    # Raw indicators
    ema50:      float
    ema200:     float
    rsi:        float
    atr_pct:    float
    dd_from_high_pct: float   # % below 50-candle high (0 = at high, 30 = -30% below)
    trend_bias: str   # "up" | "down" | "flat"
    adx:        float = 0.0  # Average Directional Index (0–100; >25 = trending, <20 = ranging)


class RegimeDetector:
    """
    Detects BTC market regime from OHLCV data.

    Uses EMA trend, RSI momentum, ATR volatility, and structural drawdown.
    Applies 3-candle hysteresis to prevent rapid flip-flopping.
    """

    def __init__(
        self,
        ema_fast: int   = 50,
        ema_slow: int   = 200,
        rsi_period: int = 14,
        atr_period: int = 14,
        high_lookback: int = 50,      # Candles to look back for recent high
        hysteresis: int = 3,          # Consecutive candles before regime change confirmed
    ):
        self.ema_fast    = ema_fast
        self.ema_slow    = ema_slow
        self.rsi_period  = rsi_period
        self.atr_period  = atr_period
        self.high_lookback = high_lookback
        self.hysteresis  = hysteresis

        self._current_regime: str   = REGIME_RANGE
        self._candidate:      str   = REGIME_RANGE
        self._candidate_count: int  = 0
        self._history: list[RegimeReading] = []

        logger.info(f"RegimeDetector initialized | EMA{ema_fast}/{ema_slow} | RSI{rsi_period} | hysteresis={hysteresis}")

    # ── Public API ─────────────────────────────────────────────────────────

    def detect(self, df: pd.DataFrame) -> RegimeReading:
        """
        Detect regime from OHLCV DataFrame.

        Args:
            df: BTC/USDT OHLCV data (at least ema_slow + 10 rows needed).

        Returns:
            RegimeReading with regime label, confidence, and raw indicators.
        """
        if len(df) < self.ema_slow + 10:
            logger.warning(
                f"[Regime] Not enough data ({len(df)} rows, need {self.ema_slow + 10}). "
                f"Defaulting to RANGE."
            )
            return RegimeReading(
                regime=REGIME_RANGE, confidence=0.0,
                ema50=0, ema200=0, rsi=50, atr_pct=2.0,
                dd_from_high_pct=0, trend_bias="flat",
            )

        close = df["close"]
        high  = df["high"]
        low   = df["low"]

        # ── Indicators ─────────────────────────────────────────────────
        ema50  = float(close.ewm(span=self.ema_fast, adjust=False).mean().iloc[-1])
        ema200 = float(close.ewm(span=self.ema_slow, adjust=False).mean().iloc[-1])

        # RSI
        delta    = close.diff()
        gain     = delta.clip(lower=0)
        loss     = (-delta).clip(lower=0)
        avg_gain = gain.ewm(alpha=1/self.rsi_period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1/self.rsi_period, adjust=False).mean()
        rs       = avg_gain / avg_loss.replace(0, 1e-9)
        rsi_val  = float((100 - 100 / (1 + rs)).iloc[-1])

        # ATR %
        prev_close = close.shift(1)
        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ], axis=1).max(axis=1)
        atr_val = float(tr.ewm(alpha=1/self.atr_period, adjust=False).mean().iloc[-1])
        atr_pct = atr_val / float(close.iloc[-1]) * 100

        # Structural drawdown from recent high
        lookback = min(self.high_lookback, len(df))
        recent_high = float(high.iloc[-lookback:].max())
        current_price = float(close.iloc[-1])
        dd_pct = (recent_high - current_price) / recent_high * 100  # positive = below high

        # ADX (Average Directional Index) — measures TREND STRENGTH, not direction
        # ADX > 25: market is trending (up OR down). ADX < 20: ranging/choppy.
        # Uses Wilder's smoothing (equivalent to EWM alpha = 1/period).
        adx_val = self._compute_adx(high, low, close)

        # ── Trend bias ──────────────────────────────────────────────────
        ema50_prev  = float(close.ewm(span=self.ema_fast, adjust=False).mean().iloc[-5])
        ema200_prev = float(close.ewm(span=self.ema_slow, adjust=False).mean().iloc[-5])
        ema50_rising  = ema50 > ema50_prev
        ema200_rising = ema200 > ema200_prev

        if ema50 > ema200:
            trend_bias = "up"
        elif ema50 < ema200:
            trend_bias = "down"
        else:
            trend_bias = "flat"

        # ── Regime classification ───────────────────────────────────────
        raw_regime, confidence = self._classify(
            ema50=ema50, ema200=ema200,
            rsi=rsi_val, atr_pct=atr_pct,
            dd_pct=dd_pct,
            ema50_rising=ema50_rising,
            adx=adx_val,
        )

        # ── Hysteresis: only commit to new regime after N consecutive reads ──
        if raw_regime == self._current_regime:
            self._candidate       = raw_regime
            self._candidate_count = 0
        else:
            if raw_regime == self._candidate:
                self._candidate_count += 1
            else:
                self._candidate       = raw_regime
                self._candidate_count = 1

            if self._candidate_count >= self.hysteresis:
                old = self._current_regime
                self._current_regime  = self._candidate
                self._candidate_count = 0
                logger.info(
                    f"[Regime] ⇄ Regime change: {old} → {self._current_regime} "
                    f"(RSI={rsi_val:.1f} | ATR%={atr_pct:.2f}% | "
                    f"EMA50={ema50:.0f} vs EMA200={ema200:.0f})"
                )

        reading = RegimeReading(
            regime=self._current_regime,
            confidence=confidence,
            ema50=round(ema50, 2),
            ema200=round(ema200, 2),
            rsi=round(rsi_val, 2),
            atr_pct=round(atr_pct, 3),
            dd_from_high_pct=round(dd_pct, 2),
            trend_bias=trend_bias,
            adx=round(adx_val, 1),
        )
        self._history.append(reading)
        if len(self._history) > 1000:
            self._history = self._history[-500:]

        return reading

    def get_allocations(self) -> dict[str, float]:
        """Return target strategy allocations for the current regime."""
        return REGIME_ALLOCATIONS.get(self._current_regime, REGIME_ALLOCATIONS[REGIME_RANGE])

    def get_cash_reserve(self) -> float:
        """Return the % of portfolio to hold as cash in the current regime."""
        return REGIME_CASH_RESERVE.get(self._current_regime, 0.0)

    @property
    def current_regime(self) -> str:
        return self._current_regime

    # ── Classification logic ────────────────────────────────────────────

    def _classify(
        self,
        ema50: float, ema200: float,
        rsi: float, atr_pct: float, dd_pct: float,
        ema50_rising: bool,
        adx: float = 0.0,
    ) -> tuple[str, float]:
        """
        Map indicator values to a regime + confidence score.

        ADX integration:
          ADX > 25  → market is TRENDING (up or down), boosts Bull/Bear confidence.
          ADX < 20  → market is RANGING, promotes RANGE regime over BEAR/BULL.
          ADX 20-25 → transitional — use other indicators as tiebreaker.

        Returns (regime_label, confidence 0.0–1.0).
        """
        is_trending = adx > 25       # Strong directional move
        is_ranging  = adx < 20       # Indecisive / choppy

        # CRASH: extreme drawdown + RSI in deep oversold
        if dd_pct >= 20 and rsi < 30:
            confidence = min(1.0, (dd_pct - 20) / 10 + (30 - rsi) / 20)
            return REGIME_CRASH, round(min(confidence, 1.0), 2)

        # VOLATILE: ATR spiking (market in shock — direction unclear)
        if atr_pct >= 3.5:
            confidence = min(1.0, (atr_pct - 3.5) / 2.0)
            return REGIME_VOLATILE, round(confidence, 2)

        # BEAR: EMA50 below EMA200, RSI weak, AND trend confirmed by ADX
        # ADX improvement: without ADX the bot called BEAR even in choppy sideways.
        # Now we require either (a) real trend strength (ADX > 20) or (b) deep RSI weakness.
        if ema50 < ema200 and rsi < 50:
            if is_ranging and rsi >= 40:
                # EMA50 < EMA200 but no real trend — classify as RANGE, not BEAR
                confidence = max(0.1, 1.0 - atr_pct / 3.5) * 0.7
                return REGIME_RANGE, round(confidence, 2)
            gap_pct    = (ema200 - ema50) / ema200 * 100
            # ADX boosts confidence if trend is genuinely strong
            adx_boost  = min(0.3, (adx - 20) / 50) if is_trending else 0.0
            confidence = min(1.0, gap_pct / 5 + (50 - rsi) / 30 + adx_boost)
            return REGIME_BEAR, round(min(confidence, 1.0), 2)

        # STRONG_BULL: EMA50 well above EMA200, strong momentum, ADX confirms trend
        if (
            ema50 > ema200 * 1.03        # EMA50 > 3% above EMA200
            and rsi > 60
            and atr_pct < 2.5
            and ema50_rising
            and (adx > 20 or rsi > 70)   # Either trend or extreme momentum
        ):
            confidence = min(1.0, (ema50 / ema200 - 1.03) / 0.05 + (rsi - 60) / 20)
            if is_trending:
                confidence = min(1.0, confidence + 0.15)  # ADX boost
            return REGIME_STRONG_BULL, round(min(confidence, 1.0), 2)

        # BULL: EMA50 above EMA200, decent RSI
        if ema50 > ema200 and rsi >= 45:
            gap_pct    = (ema50 - ema200) / ema200 * 100
            adx_boost  = min(0.2, (adx - 20) / 50) if is_trending else 0.0
            confidence = min(1.0, gap_pct / 5 + (rsi - 45) / 30 + adx_boost)
            return REGIME_BULL, round(min(confidence, 1.0), 2)

        # RANGE: everything else (choppy, indecisive)
        # ADX improvement: low ADX explicitly confirms ranging — higher confidence.
        base_conf  = max(0.1, 1.0 - atr_pct / 3.5)
        adx_factor = max(0.5, 1.0 - adx / 40) if adx > 0 else 1.0  # low ADX → higher range conf
        confidence = min(1.0, base_conf * adx_factor + (0.3 if is_ranging else 0.0))
        return REGIME_RANGE, round(min(confidence, 1.0), 2)

    def _compute_adx(
        self, high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14
    ) -> float:
        """
        Compute ADX (Average Directional Index) using Wilder's smoothing.

        ADX measures trend STRENGTH (not direction):
          > 25: trending market
          < 20: ranging / choppy market
          20-25: transitional

        Returns the latest ADX value (0–100).
        """
        try:
            alpha = 1.0 / period

            # True Range
            prev_close = close.shift(1)
            tr = pd.concat([
                high - low,
                (high - prev_close).abs(),
                (low  - prev_close).abs(),
            ], axis=1).max(axis=1)

            # Directional Movement
            up_move   = high - high.shift(1)
            down_move = low.shift(1) - low

            plus_dm  = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
            minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

            # Wilder smoothing (EWM with alpha = 1/period)
            atr_s      = tr.ewm(alpha=alpha, adjust=False).mean()
            plus_di    = 100 * plus_dm.ewm(alpha=alpha, adjust=False).mean() / atr_s.replace(0, 1e-9)
            minus_di   = 100 * minus_dm.ewm(alpha=alpha, adjust=False).mean() / atr_s.replace(0, 1e-9)

            dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, 1e-9)
            adx = float(dx.ewm(alpha=alpha, adjust=False).mean().iloc[-1])
            return round(adx, 2)
        except Exception:
            return 0.0

    # ── Reporting ────────────────────────────────────────────────────────

    def summary(self, reading: RegimeReading) -> str:
        allocs = self.get_allocations()
        cash   = self.get_cash_reserve()
        adx_label = "trending" if reading.adx > 25 else ("ranging" if reading.adx < 20 else "neutral")
        lines = [
            "─" * 52,
            f"  MARKET REGIME DETECTOR",
            "─" * 52,
            f"  Regime     : {reading.regime:<14} (confidence: {reading.confidence:.0%})",
            f"  Trend      : EMA50={reading.ema50:,.0f} {'>' if reading.ema50 > reading.ema200 else '<'} EMA200={reading.ema200:,.0f}",
            f"  Momentum   : RSI={reading.rsi:.1f}",
            f"  Volatility : ATR%={reading.atr_pct:.2f}%",
            f"  ADX        : {reading.adx:.1f} ({adx_label})",
            f"  Structure  : {reading.dd_from_high_pct:.1f}% below 50-candle high",
            "  Allocations (active capital):",
        ]
        for k, v in allocs.items():
            lines.append(f"    {k:<14} {v*100:>5.0f}%")
        if cash > 0:
            lines.append(f"    {'cash reserve':<14} {cash*100:>5.0f}%")
        lines.append("─" * 52)
        return "\n".join(lines)
