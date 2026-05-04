"""strategies/intraday_seasonality.py — Pure time-of-day filter (sq-003).

Long-only intraday seasonality strategy: enter at `entry_hour` UTC, exit at
`exit_hour` UTC.  No indicators, no external data, no `ta` imports — the
signal is solely a function of the candle's UTC hour.

Pre-trial gates (locked, source: scripts/run_intraday_seasonality_effects_trial.py
hypothesis text and research/intraday-seasonality-effects-literature.md):

    1. Single-pair: BTC/USDT (manifest notation) on 1H timeframe only.
    2. Long-only: never emit Signal.SELL when self._position_open is False.
    3. Pure time filter: no indicators, no external data, no `ta` imports.
    4. Entry at candle timestamped hour==entry_hour UTC,
       exit at candle timestamped hour==exit_hour UTC.

CPCV note: `_position_open` is per-instance state.  The trial script's
`make_strategy` factory must create a FRESH instance for each CPCV block so
no boundary state leaks across blocks.  The default 50-candle warm-up window
(~2 days at 1H) may skip the first entry window of each block; this affects
at most one of ~73 trade opportunities per block and is documented as an
acceptable artefact.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

from strategies.base import BaseStrategy, Signal


# entry_hour=21 / exit_hour=23 are the prompt-locked starting variation
# for sq-003 (variation_id=intraday-hourly-long-21-23utc) — initial
# hypothesis on the trial-queue item.  Sensitivity to neighbouring
# hours is reserved for variation #2+ per the literature stub.
# CITATION: intraday-seasonality-effects-literature
_DEFAULT_ENTRY_HOUR: int = 21
# CITATION: intraday-seasonality-effects-literature
_DEFAULT_EXIT_HOUR: int = 23


class IntradaySeasonalityEffects(BaseStrategy):
    """Pure time-of-day filter: long at `entry_hour` UTC, exit at `exit_hour`.

    No indicators required.  Long-only — `generate_signal` will never emit
    SELL unless `_position_open` is True (set when this instance issued a
    BUY in a prior call).

    CPCV note: `_position_open` resets to False on each fresh instantiation.
    The strategy_factory in the trial script must create a new instance per
    CPCV block so block-boundary state does not leak.  The warm-up window
    (50 candles = ~2 days) may skip the first entry window of each block;
    this affects at most 1 of ~73 trade opportunities per block and is
    documented as an acceptable artefact.
    """

    def __init__(
        self,
        symbol: str = "BTC/USDT",
        timeframe: str = "1h",
        entry_hour: int = _DEFAULT_ENTRY_HOUR,
        exit_hour: int = _DEFAULT_EXIT_HOUR,
    ):
        super().__init__(
            name="IntradaySeasonalityEffects",
            symbol=symbol,
            timeframe=timeframe,
        )
        self.entry_hour = int(entry_hour)
        self.exit_hour = int(exit_hour)
        # Per-instance long-only state.  False on every fresh instance —
        # critical for CPCV block-boundary correctness (gate #7).
        self._position_open: bool = False

    def generate_signal(self, df: Optional[pd.DataFrame]) -> Signal:
        if df is None or df.empty:
            return self.hold(price=0.0, reason="empty df")

        last_ts = df.index[-1]
        # `last_ts` is the candle's index value.  Pandas DatetimeIndex
        # exposes `.hour` directly; for tz-aware indices the hour reflects
        # the index's tz (UTC for the OHLCV cache by construction).
        hour = int(last_ts.hour)
        price = float(df["close"].iloc[-1])

        if hour == self.entry_hour and not self._position_open:
            self._position_open = True
            return self.buy(
                price=price,
                reason=f"intraday entry | hour={hour}UTC",
            )

        if hour == self.exit_hour and self._position_open:
            # Long-only guard (gate #2): SELL only fires when a prior BUY
            # in this instance flipped _position_open to True.  Without
            # the open-position check the strategy would emit SELL at
            # exit_hour every day, including blocks where no entry was
            # ever taken — that path is forbidden per the pre-trial gate.
            self._position_open = False
            return self.sell(
                price=price,
                reason=f"intraday exit | hour={hour}UTC",
            )

        return self.hold(
            price=price,
            reason=f"hour={hour}UTC | position_open={self._position_open}",
        )
