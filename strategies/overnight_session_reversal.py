"""strategies/overnight_session_reversal.py -- sq-023 strategy.

Overnight-vs-day-session reversal on BTC/USDT 1H.

Hypothesis: returns during the US stock-market trading session
(NYSE 14:30-21:00 UTC, approximated on the 1H grid as 14:00-21:00
UTC) are negatively correlated with the preceding overnight-session
return (NYSE-closed window, ~21:00 UTC prior day to 14:00 UTC current
day).  When the overnight return is negative, the day session tends
to revert positive -- BUY at the day-session start and exit at the
day-session end.  When the overnight return is positive, the
specification calls for a short-leg position; the engine is
structurally long-only on spot, so that branch HOLDs (long-only-leg
precedent from sq-013, sq-016, sq-018, sq-019, sq-020, sq-024).

Sources:
  - Hyuna Ham, Doojin Ryu, Robert I. Webb (2022). International
    Review of Financial Analysis. "The effects of overnight events
    on daytime trading sessions."  Cryptocurrency-specific.
    Quality 4.
  - Dong Lou, Christopher Polk, Spyros Skouras (2019). Journal of
    Financial Economics. "A tug of war: Overnight versus intraday
    expected returns."  Equities; cited as theoretical
    foundation.  Quality 5.
  - Adam Zaremba et al. (2021). International Review of Financial
    Analysis. "Up or down? Short-term reversal, momentum, and
    liquidity effects in cryptocurrency markets."  Cryptocurrency-
    specific.  Quality 4.
  - Han, C.; Kang, B.; Ryu, J. (2024). SSRN. "Time-Series and
    Cross-Sectional Momentum in the Cryptocurrency Market."
    Justifies dropping the short leg: crypto loser-shorts are
    punished by rebound moves.

Algorithm per 1H candle:

  1. At the candle indexed `entry_hour` UTC (default 14, which is
     the bar whose close = 15:00 UTC, approximating NYSE
     14:30 UTC open) compute the overnight return:

         prev_close = close of yesterday's `exit_hour` bar
                      (= price at exit_hour+1:00 UTC yesterday,
                      end of prior day session)
         today_open = open of today's `entry_hour` bar
                      (= price at entry_hour:00 UTC today,
                      start of current day session)
         overnight_return = (today_open - prev_close) / prev_close

     This window covers ~21:00 UTC yesterday through ~14:00 UTC
     today, i.e., the NYSE-closed period.

  2. Apply the long-only reversal rule:

         overnight_return < 0  AND not _position_open  -> BUY
         otherwise                                     -> HOLD

  3. At the candle indexed `exit_hour` UTC (default 20, whose close
     = 21:00 UTC, approximating NYSE close), if a position is open
     emit SELL.

  4. Long-only by construction: never emit Signal.SELL when
     `_position_open` is False.

CPCV note: `_position_open` is per-instance state.  The trial
script's `make_strategy` factory must create a FRESH instance for
each CPCV block so no boundary state leaks across blocks.  The
default 50-candle engine warmup plus the strategy's own internal
HOLD-on-missing-prior-bar guard (returns HOLD until both the
yesterday `exit_hour` bar and today `entry_hour` bar are present)
keep entries deterministic block-to-block.
"""

from __future__ import annotations

import math
from typing import Optional

import pandas as pd

from strategies.base import BaseStrategy, Signal


# CITATION: overnight-session-reversal-literature
# NYSE day session is 09:30-16:00 ET.  Without DST that maps to
# 14:30-21:00 UTC; with EDT it maps to 13:30-20:00 UTC.  On the 1H
# grid we approximate by entering at the close of the bar indexed
# 14:00 UTC (fill price = 15:00 UTC ~ 30 min after NYSE open) and
# exiting at the close of the bar indexed 20:00 UTC (fill price =
# 21:00 UTC ~ NYSE close).  Holding window = 6 hours, ~6.5-hour
# day session.
_DEFAULT_ENTRY_HOUR: int = 14
# CITATION: overnight-session-reversal-literature
# exit_hour = 20: SELL signal at the bar whose close coincides
# with NYSE close (21:00 UTC, no-DST baseline).
_DEFAULT_EXIT_HOUR: int = 20


class OvernightSessionReversalStrategy(BaseStrategy):
    """Long-only reversal of the NYSE overnight-session return.

    Long-only by construction: the long-only-leg precedent from
    sq-013 (CrossSectionalReversal), sq-016 (CrossSectionalSkewness),
    sq-018 (AttentionMomentum), sq-019 (IntradayMomentumReversal),
    sq-020 (CrossSectionalMomentum), and sq-024
    (ShortTermCrossSectionalMomentum) carries over here because
    (a) backtest.engine is structurally long-only on spot, and
    (b) Han et al. (2024) document that crypto loser-shorts are
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
        entry_hour: int = _DEFAULT_ENTRY_HOUR,
        exit_hour: int = _DEFAULT_EXIT_HOUR,
    ):
        super().__init__(
            name="OvernightSessionReversal",
            symbol=symbol,
            timeframe=timeframe,
        )
        if not (0 <= entry_hour <= 23):
            raise ValueError("entry_hour must be in [0, 23]")
        if not (0 <= exit_hour <= 23):
            raise ValueError("exit_hour must be in [0, 23]")
        if entry_hour == exit_hour:
            raise ValueError("entry_hour must differ from exit_hour")

        self.entry_hour = int(entry_hour)
        self.exit_hour = int(exit_hour)

        # Per-instance long-only state.  False on every fresh
        # instance for CPCV block-boundary correctness.
        self._position_open: bool = False

    def _compute_overnight_return(
        self, df: pd.DataFrame, last_ts: pd.Timestamp,
    ) -> Optional[float]:
        """Return the overnight-session return, or None if either
        boundary bar is missing.

        prev_close = close of yesterday's `exit_hour` bar.
        today_open = open of today's `entry_hour` bar (= last_ts).

        Both timestamps must be present in `df`; if either is
        missing (CPCV block boundary or first day after warmup),
        return None and the caller HOLDs.
        """
        today_entry_ts = last_ts.normalize() + pd.Timedelta(
            hours=self.entry_hour
        )
        prev_exit_ts = (
            last_ts.normalize()
            - pd.Timedelta(days=1)
            + pd.Timedelta(hours=self.exit_hour)
        )
        if today_entry_ts not in df.index:
            return None
        if prev_exit_ts not in df.index:
            return None

        today_open = float(df.loc[today_entry_ts, "open"])
        prev_close = float(df.loc[prev_exit_ts, "close"])
        if prev_close <= 0 or not math.isfinite(prev_close):
            return None
        if not math.isfinite(today_open):
            return None
        ret = (today_open - prev_close) / prev_close
        if not math.isfinite(ret):
            return None
        return ret

    def generate_signal(self, df: Optional[pd.DataFrame]) -> Signal:
        if df is None or df.empty:
            return self.hold(price=0.0, reason="empty df")

        last_ts = df.index[-1]
        hour = int(last_ts.hour)
        price = float(df["close"].iloc[-1])

        # Exit branch.  Long-only guard: SELL only fires when a
        # prior BUY in this instance flipped _position_open to True.
        if hour == self.exit_hour and self._position_open:
            self._position_open = False
            return self.sell(
                price=price,
                reason=f"day-session exit | hour={hour}UTC",
                order_type="market",
            )

        # Entry branch.
        if hour == self.entry_hour and not self._position_open:
            overnight_return = self._compute_overnight_return(df, last_ts)
            if overnight_return is None:
                return self.hold(
                    price=price,
                    reason=(
                        f"warmup | missing prior exit bar or current "
                        f"entry bar | hour={hour}UTC"
                    ),
                )

            # Long-only reversal: BUY when overnight was negative.
            # The short leg (overnight > 0 -> short) is dropped per
            # the long-only engine constraint.
            if overnight_return < 0:
                self._position_open = True
                return self.buy(
                    price=price,
                    reason=(
                        f"overnight-reversal-long | "
                        f"overnight_ret={overnight_return:+.5f} < 0 | "
                        f"hour={hour}UTC"
                    ),
                    order_type="market",
                )
            return self.hold(
                price=price,
                reason=(
                    f"reversal-skip | overnight_ret="
                    f"{overnight_return:+.5f} >= 0 (would be short, "
                    f"long-only engine) | hour={hour}UTC"
                ),
            )

        return self.hold(
            price=price,
            reason=(
                f"hour={hour}UTC | position_open={self._position_open}"
            ),
        )
