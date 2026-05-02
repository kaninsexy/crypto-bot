"""
strategies/funding_rate_harvest.py — Phase 4.B Variation #1.

Delta-neutral cash-and-carry: long spot + short perp at equal
notional, held continuously while funding rate is positive.  Income
is collected as cash on the perp leg at every 8h funding settlement
on OKX USDT-M.

Hypothesis-of-record + parameter citations live in
`research/funding-rate-literature.md` § "Variation #1 —
phase4b-delta-neutral-singlepair-btc-v1".  Risk-model design (per-
leg margin, liquidation, funding payment math, exit triggers) lives
in `research/funding-rate-risk-model.md`.

Module surface
──────────────
    FundingRateHarvestStrategy
        BaseStrategy subclass that drives the open side of the
        delta-neutral position.  Emits BUY every call.

    make_funding_settlement_counter(funding_cadence_hours: int)
        Factory returning a `count_signal_events_per_block`
        callback for `CPCVConfig`.  The callback counts funding
        settlements per block (= block_hours // cadence), which
        is the structural signal cadence for this strategy and
        the right input to the per-block validity gate per Track 2
        of the harness extension (CPCVConfig field added 2026-05-02).
"""

from __future__ import annotations

from typing import Any, Callable

import pandas as pd

from strategies.base import BaseStrategy, Signal


class FundingRateHarvestStrategy(BaseStrategy):
    """Open-side strategy for the Phase 4.B delta-neutral funding
    harvest variation.

    Contract: emit BUY on every call to `generate_signal`.  Two-leg
    construction (open both legs at equal notional) lives inside
    `paper_trading.perp_simulator.PerpSimulator.execute_signal`;
    this class does not track per-leg state.

    Re-entry semantics: PerpSimulator silently ignores BUY when a
    position is already open, so emitting BUY every candle is the
    correct contract — after any exit (funding-flip or maintenance-
    margin breach) the next candle's BUY automatically re-opens
    the position once the simulator is flat.  No internal "have I
    opened yet" state needed.

    The engine's warm-up window (`engine_perp.run_perp` skips the
    first `warm_up_candles` bars before invoking the strategy)
    handles "skip until enough data" upstream; the strategy itself
    has no warm-up branch.
    """

    def __init__(
        self,
        symbol: str = "BTC/USDT",
        timeframe: str = "1h",
    ):
        super().__init__(
            name="FundingRateHarvest",
            symbol=symbol,
            timeframe=timeframe,
        )

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        price = float(df["close"].iloc[-1])
        return Signal(
            action="BUY",
            strategy=self.name,
            price=price,
            reason="funding_harvest_open",
            order_type="market",
            quantity_pct=1.0,
        )


# ── Funding-settlement counter factory ───────────────────────────────────────

def make_funding_settlement_counter(
    funding_cadence_hours: int,
) -> Callable[[Any, dict], int]:
    """Return a per-block signal-event counter for `CPCVConfig`.

    The returned callable matches the `count_signal_events_per_block`
    contract introduced by Track 2 (2026-05-02): it accepts
    `(result, blocks)` where `blocks` is the runner-supplied block
    descriptor (a `dict[str, pd.DataFrame]` for `legs`-typed
    entries; the spot frame is used because both legs share the
    same time index by construction).  It returns the number of
    funding settlements that fall inside the block — the structural
    signal cadence for this strategy, decoupled from the
    open/close trade count that the default callback would observe.

    Args:
        funding_cadence_hours: settlement cadence in hours (8 on
            OKX USDT-M majors as of 2026-04-29; manifest field
            `funding_cadence_hours` is the canonical source).

    Returns:
        A `(result, blocks) -> int` callable.  Closure over
        `funding_cadence_hours` so multiple instances coexist
        (e.g. variations testing different cadences).

    Implementation note: the callback computes block span from
    `index[-1] - index[0]` of the spot frame and integer-divides
    by the cadence.  Returns 0 for empty or singleton frames so
    the per-block validity gate cleanly NaN's the block (matches
    the trade-count fallback's behaviour for under-populated
    blocks).
    """
    if funding_cadence_hours <= 0:
        raise ValueError(
            f"funding_cadence_hours must be positive; got {funding_cadence_hours}"
        )

    def _count(result: Any, blocks: dict) -> int:
        # Prefer the spot frame; fall back to perp if spot is
        # absent (shouldn't happen for legs-typed entries but a
        # belt-and-braces guard keeps the callback resilient).
        # Explicit `is None` / membership checks per CLAUDE.md —
        # `or` on a DataFrame raises on ambiguous truthiness.
        if isinstance(blocks, dict):
            if "spot" in blocks and blocks["spot"] is not None:
                block_df = blocks["spot"]
            elif "perp" in blocks and blocks["perp"] is not None:
                block_df = blocks["perp"]
            else:
                block_df = None
        else:
            block_df = blocks
        if block_df is None or len(block_df) < 2:
            return 0
        span: pd.Timedelta = block_df.index[-1] - block_df.index[0]
        block_hours = span.total_seconds() / 3600.0
        return int(block_hours // funding_cadence_hours)

    return _count
