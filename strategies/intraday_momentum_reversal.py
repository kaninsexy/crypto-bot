"""strategies/intraday_momentum_reversal.py -- sq-019 strategy.

Conditional intraday momentum/reversal on BTC/USDT 1H.

Hypothesis: the return of the first hour of a fixed UTC trading
session predicts the return of the last hour. The direction is
conditional on the realized-volatility regime: low-vol days exhibit
momentum (the closing-hour return follows the opening hour), high-vol
days exhibit reversal (the closing-hour return inverts the opening
hour). Source: Wen et al. (2022) NAJEF, Shen et al. (2022) Financial
Review, Zaremba et al. (2021) IRFA.

Algorithm per 1H candle:

  1. At the candle indexed `entry_hour` UTC (the bar whose close
     coincides with the open of the closing-hour candle), look up
     today's `opening_hour` candle and compute the opening return:

         opening_return = (close_open - open_open) / open_open

     where `close_open` and `open_open` are the close and open of
     the `opening_hour` 1H bar.

  2. Compute the realized-volatility regime from daily resampled
     close-to-close returns:

         vol_short = std(daily_returns_last_vol_lookback_short_days)
         vol_long  = std(daily_returns_last_vol_lookback_long_days)
         high_vol  = vol_short > vol_long

  3. Apply the conditional rule (long-only):

         high_vol AND opening_return < 0  -> BUY (reversal)
         low_vol  AND opening_return > 0  -> BUY (momentum)
         otherwise                        -> HOLD

  4. At the candle indexed `exit_hour` UTC (one bar after entry,
     i.e., the closing-hour candle itself), if a position is open
     emit SELL.

  5. Long-only: never emit SELL when `_position_open` is False. Han
     et al. (2024) document that crypto loser-shorts are punished by
     rebound moves; the same precedent applied to sq-013, sq-016,
     sq-018, sq-020 applies here.

CPCV note: `_position_open` is per-instance state. The trial
script's `make_strategy` factory must create a FRESH instance for
each CPCV block so no boundary state leaks across blocks. The
default 50-candle warm-up window plus the strategy's own
vol_lookback_long-day daily-vol warmup (returns HOLD until enough
history accumulates) keep entries deterministic block-to-block.
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal


# CITATION: intraday-momentum-reversal-literature
# Wen et al. (2022) and Shen et al. (2022) define the trading session
# as a fixed UTC window with the first-hour return predicting the
# last-hour return.  The 08:00-16:00 UTC window covers the European
# session and the early US overlap, the highest-volume liquidity
# window for BTC perpetual futures.
_DEFAULT_OPENING_HOUR: int = 8
# CITATION: intraday-momentum-reversal-literature
# entry_hour = session_end_hour - 2: the candle whose close coincides
# with the open of the closing-hour bar.  With session_end_hour=16
# and 1H candles, entry_hour=14 means the BUY fill on this candle's
# close (= 15:00 open) and SELL on the next candle's close (= 16:00
# open), so the position is held for exactly the closing 1H bar.
_DEFAULT_ENTRY_HOUR: int = 14
# CITATION: intraday-momentum-reversal-literature
# exit_hour = entry_hour + 1: the closing-hour candle whose close
# ends the session.
_DEFAULT_EXIT_HOUR: int = 15
# CITATION: intraday-momentum-reversal-literature
# Zaremba et al. (2021) IRFA: large/liquid cryptos exhibit momentum,
# small/illiquid cryptos exhibit reversal.  The realized-volatility
# regime (vol_short > vol_long) is the BTC-specific proxy for that
# liquidity/dispersion factor — high-vol regimes correspond to
# stressed/illiquid markets where reversal dominates.
_DEFAULT_VOL_LOOKBACK_SHORT_DAYS: int = 7
# CITATION: intraday-momentum-reversal-literature
# Shen et al. (2022) use a ~30-day baseline for the volatility
# reference window in their intraday-momentum tests.
_DEFAULT_VOL_LOOKBACK_LONG_DAYS: int = 30


class IntradayMomentumReversalStrategy(BaseStrategy):
    """Conditional intraday momentum/reversal on a fixed UTC session.

    Long-only by construction: the long-only-leg precedent that
    applied to sq-013, sq-016, sq-018, and sq-020 carries over here
    because (a) backtest.engine is structurally long-only on spot,
    and (b) Han et al. (2024) document that crypto loser-shorts are
    punished by rebound moves.

    CPCV note: `_position_open` resets to False on each fresh
    instantiation.  The strategy_factory in the trial script must
    create a new instance per CPCV block so block-boundary state
    does not leak.
    """

    def __init__(
        self,
        symbol: str = "BTC/USDT",
        timeframe: str = "1h",
        opening_hour: int = _DEFAULT_OPENING_HOUR,
        entry_hour: int = _DEFAULT_ENTRY_HOUR,
        exit_hour: int = _DEFAULT_EXIT_HOUR,
        vol_lookback_short_days: int = _DEFAULT_VOL_LOOKBACK_SHORT_DAYS,
        vol_lookback_long_days: int = _DEFAULT_VOL_LOOKBACK_LONG_DAYS,
    ):
        super().__init__(
            name="IntradayMomentumReversal",
            symbol=symbol,
            timeframe=timeframe,
        )
        if vol_lookback_short_days < 2:
            raise ValueError("vol_lookback_short_days must be >= 2")
        if vol_lookback_long_days <= vol_lookback_short_days:
            raise ValueError(
                "vol_lookback_long_days must be > vol_lookback_short_days"
            )
        if not (0 <= opening_hour <= 23):
            raise ValueError("opening_hour must be in [0, 23]")
        if not (0 <= entry_hour <= 23):
            raise ValueError("entry_hour must be in [0, 23]")
        if not (0 <= exit_hour <= 23):
            raise ValueError("exit_hour must be in [0, 23]")

        self.opening_hour = int(opening_hour)
        self.entry_hour = int(entry_hour)
        self.exit_hour = int(exit_hour)
        self.vol_lookback_short_days = int(vol_lookback_short_days)
        self.vol_lookback_long_days = int(vol_lookback_long_days)

        # Per-instance long-only state.  False on every fresh instance
        # for CPCV block-boundary correctness.
        self._position_open: bool = False

    def _compute_opening_return(
        self, df: pd.DataFrame, last_ts: pd.Timestamp,
    ) -> Optional[float]:
        """Return the close-to-open return of today's opening_hour bar.

        Returns None if the opening bar is not present in df (e.g.,
        the strategy was instantiated mid-day in a CPCV block whose
        first candle is after `opening_hour` UTC on day 0).
        """
        opening_ts = last_ts.normalize() + pd.Timedelta(
            hours=self.opening_hour
        )
        if opening_ts not in df.index:
            return None
        row = df.loc[opening_ts]
        open_px = float(row["open"])
        close_px = float(row["close"])
        if open_px <= 0 or not math.isfinite(open_px):
            return None
        ret = (close_px - open_px) / open_px
        if not math.isfinite(ret):
            return None
        return ret

    def _compute_vol_regime(
        self, df: pd.DataFrame,
    ) -> Optional[tuple[float, float, bool]]:
        """Return (vol_short, vol_long, high_vol) or None if insufficient
        history.

        Daily realized vol is std of daily close-to-close returns over
        the last `vol_lookback_short_days` and `vol_lookback_long_days`
        days respectively.  high_vol = vol_short > vol_long.
        """
        daily_close = df["close"].resample("1D").last().dropna()
        # Need at least vol_lookback_long_days + 1 daily closes to
        # produce vol_lookback_long_days daily returns.
        if len(daily_close) < self.vol_lookback_long_days + 1:
            return None
        daily_returns = daily_close.pct_change().dropna()
        if len(daily_returns) < self.vol_lookback_long_days:
            return None
        recent_short = daily_returns.iloc[-self.vol_lookback_short_days:]
        recent_long = daily_returns.iloc[-self.vol_lookback_long_days:]
        vol_short = float(recent_short.std(ddof=0))
        vol_long = float(recent_long.std(ddof=0))
        if not (math.isfinite(vol_short) and math.isfinite(vol_long)):
            return None
        if vol_long <= 0:
            return None
        return vol_short, vol_long, vol_short > vol_long

    def generate_signal(self, df: Optional[pd.DataFrame]) -> Signal:
        if df is None or df.empty:
            return self.hold(price=0.0, reason="empty df")

        last_ts = df.index[-1]
        hour = int(last_ts.hour)
        price = float(df["close"].iloc[-1])

        # Exit branch.  Long-only guard: SELL only fires when a prior
        # BUY in this instance flipped _position_open to True.
        if hour == self.exit_hour and self._position_open:
            self._position_open = False
            return self.sell(
                price=price,
                reason=f"intraday exit | hour={hour}UTC",
                order_type="market",
            )

        # Entry branch.
        if hour == self.entry_hour and not self._position_open:
            opening_return = self._compute_opening_return(df, last_ts)
            if opening_return is None:
                return self.hold(
                    price=price,
                    reason=f"no opening bar | hour={hour}UTC",
                )

            vol_regime = self._compute_vol_regime(df)
            if vol_regime is None:
                return self.hold(
                    price=price,
                    reason=(
                        f"vol warmup | need "
                        f"{self.vol_lookback_long_days + 1} daily closes"
                    ),
                )
            vol_short, vol_long, high_vol = vol_regime

            if high_vol:
                # Reversal regime: long when opening was negative.
                if opening_return < 0:
                    self._position_open = True
                    return self.buy(
                        price=price,
                        reason=(
                            f"reversal-long | high_vol "
                            f"vol_s={vol_short:.5f} > "
                            f"vol_l={vol_long:.5f} | "
                            f"open_ret={opening_return:+.5f}"
                        ),
                        order_type="market",
                    )
                return self.hold(
                    price=price,
                    reason=(
                        f"reversal-skip | high_vol | "
                        f"open_ret={opening_return:+.5f} >= 0"
                    ),
                )

            # Low-vol regime: momentum (long when opening was positive).
            if opening_return > 0:
                self._position_open = True
                return self.buy(
                    price=price,
                    reason=(
                        f"momentum-long | low_vol "
                        f"vol_s={vol_short:.5f} <= "
                        f"vol_l={vol_long:.5f} | "
                        f"open_ret={opening_return:+.5f}"
                    ),
                    order_type="market",
                )
            return self.hold(
                price=price,
                reason=(
                    f"momentum-skip | low_vol | "
                    f"open_ret={opening_return:+.5f} <= 0"
                ),
            )

        return self.hold(
            price=price,
            reason=(
                f"hour={hour}UTC | position_open={self._position_open}"
            ),
        )
