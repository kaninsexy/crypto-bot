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

from typing import Any, Callable, Optional

import pandas as pd

from strategies.base import BaseStrategy, Signal


# 8h funding settlements per year; an 8h rate × this constant is
# the annualised rate used by the V2 entry/exit gate.  See
# research/funding-rate-literature.md § "Theoretical/design baseline".
_FUNDING_ANNUALISATION = 1095  # CITATION: funding-rate-literature


class FundingRateHarvestStrategy(BaseStrategy):
    """Open-side strategy for the Phase 4.B delta-neutral funding
    harvest variation.

    V1 contract: emit BUY on every call to `generate_signal`.  Two-leg
    construction (open both legs at equal notional) lives inside
    `paper_trading.perp_simulator.PerpSimulator.execute_signal`;
    this class does not track per-leg state.

    Re-entry semantics: PerpSimulator silently ignores BUY when a
    position is already open, so emitting BUY every candle is the
    correct contract — after any exit (funding-flip or maintenance-
    margin breach) the next candle's BUY automatically re-opens
    the position once the simulator is flat.  No internal "have I
    opened yet" state needed.

    V2 extension: the optional `min_funding_rate_entry` and
    `exit_funding_rate_threshold` parameters add a regime-aware
    threshold gate.  When either is non-zero, the strategy looks up
    the most recent funding rate against an internally-held funding
    history (set via `set_funding_history`) and either suppresses
    the BUY (rate below entry threshold) or emits a SELL (rate
    below exit threshold while a position is open).  Both default
    to 0.0 so V1 behaviour is preserved bit-for-bit.

    V2b extension (added 2026-05-08): the optional
    `vol_regime_threshold` parameter activates a volatility-regime
    gate sourced from Almeida, Grith, Miftachov, Wang (2024) arXiv
    2410.15195v2.  When non-zero, the strategy looks up the most
    recent realized-vol value against an internally-held vol
    history (set via `set_vol_history`) and either emits BUY when
    the latest vol is below the threshold (LV regime, harvest) or
    SELL when the latest vol is at or above the threshold (HV
    regime, flat).  Default 0.0 preserves V1 behaviour.  The V2
    funding-rate gate and the V2b vol-regime gate compose
    cleanly: when both are active the position must clear BOTH
    gates to enter, and either gate alone is sufficient to exit.

    The engine's warm-up window (`engine_perp.run_perp` skips the
    first `warm_up_candles` bars before invoking the strategy)
    handles "skip until enough data" upstream; the strategy itself
    has no warm-up branch.
    """

    def __init__(
        self,
        symbol: str = "BTC/USDT",
        timeframe: str = "1h",
        min_funding_rate_entry: float = 0.0,
        exit_funding_rate_threshold: float = 0.0,
        vol_regime_threshold: float = 0.0,
    ):
        super().__init__(
            name="FundingRateHarvest",
            symbol=symbol,
            timeframe=timeframe,
        )
        self._min_funding_rate_entry = float(min_funding_rate_entry)
        self._exit_funding_rate_threshold = float(exit_funding_rate_threshold)
        self._vol_regime_threshold = float(vol_regime_threshold)
        self._funding_history: Optional[pd.DataFrame] = None
        self._vol_history: Optional[pd.DataFrame] = None

    def set_funding_history(self, funding_history: pd.DataFrame) -> None:
        """Provide the strategy with the funding-rate series it should
        consult when applying V2 threshold gates.

        Required only when `min_funding_rate_entry` or
        `exit_funding_rate_threshold` is non-zero; in V1 mode (both
        zero) the lookup is skipped and the funding history is
        irrelevant.

        Expected schema: DatetimeIndex (UTC), at least one column
        named `funding_rate` carrying the per-8h rate (NOT
        annualised).  Mirrors the format produced by
        `data.okx_funding.load_or_fetch_funding_history`.
        """
        self._funding_history = funding_history

    def _latest_funding_rate_per_8h(
        self, ts: pd.Timestamp,
    ) -> Optional[float]:
        """Return the most recent 8h funding rate at-or-before `ts`,
        or None when no settlement has yet been observed.
        """
        if self._funding_history is None or self._funding_history.empty:
            return None
        idx = self._funding_history.index.searchsorted(ts, side="right") - 1
        if idx < 0:
            return None
        return float(self._funding_history.iloc[idx]["funding_rate"])

    def set_vol_history(self, vol_history: pd.DataFrame) -> None:
        """Provide the strategy with the realized-vol time series it
        should consult when applying the V2b regime gate.

        Required only when `vol_regime_threshold` is non-zero; in V1
        and V2 modes (vol_regime_threshold == 0.0) the lookup is
        skipped and the vol history is irrelevant.

        Expected schema: DatetimeIndex (UTC), at least one column
        named `realized_vol_annualized` carrying the annualised
        realized volatility (NOT the per-bar standard deviation).
        Mirrors the format produced by
        `scripts/phase_4b_v2b_volregime_probe.py`.
        """
        self._vol_history = vol_history

    def _latest_realized_vol(
        self, ts: pd.Timestamp,
    ) -> Optional[float]:
        """Return the most recent annualized-realized-vol value at-or-
        before `ts`, or None when no vol observation has yet been
        recorded.
        """
        if self._vol_history is None or self._vol_history.empty:
            return None
        idx = self._vol_history.index.searchsorted(ts, side="right") - 1
        if idx < 0:
            return None
        v = float(self._vol_history.iloc[idx]["realized_vol_annualized"])
        if v != v:  # NaN guard (rolling-window leading NaNs)
            return None
        return v

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        price = float(df["close"].iloc[-1])

        v2_mode = (
            self._min_funding_rate_entry > 0.0
            or self._exit_funding_rate_threshold > 0.0
        )
        v2b_mode = self._vol_regime_threshold > 0.0
        if not (v2_mode or v2b_mode):
            return Signal(
                action="BUY",
                strategy=self.name,
                price=price,
                reason="funding_harvest_open",
                order_type="market",
                quantity_pct=1.0,
            )

        ts = df.index[-1]

        # V2b vol-regime gate runs first when active.  HV regime
        # (vol >= threshold) is a hard exit (force SELL even if a
        # position is already open); LV regime falls through to the
        # rest of the gate stack.  Pre-first-vol-observation HOLDs
        # rather than opening blind, mirroring V2's pre-first-funding
        # branch.
        if v2b_mode:
            vol = self._latest_realized_vol(ts)
            if vol is None:
                return Signal(
                    action="HOLD",
                    strategy=self.name,
                    price=price,
                    reason="v2b_no_vol_observed_yet",
                )
            if vol >= self._vol_regime_threshold:
                return Signal(
                    action="SELL",
                    strategy=self.name,
                    price=price,
                    reason="vol_regime_hv_flat",
                    order_type="market",
                    quantity_pct=1.0,
                )

        # V2 funding-rate gate.
        if v2_mode:
            rate_per_8h = self._latest_funding_rate_per_8h(ts)
            rate_annual = (
                rate_per_8h * _FUNDING_ANNUALISATION
                if rate_per_8h is not None else None
            )

            # Pre-first-settlement: V2 holds rather than opening blind.
            if rate_annual is None:
                return Signal(
                    action="HOLD",
                    strategy=self.name,
                    price=price,
                    reason="v2_no_funding_observed_yet",
                )

            # Hysteresis exit: rate fell below exit threshold.
            # Strategy emits SELL unconditionally; PerpSimulator
            # silently ignores SELL when flat (mirrors the BUY-
            # already-open pattern).
            if (
                self._exit_funding_rate_threshold > 0.0
                and rate_annual < self._exit_funding_rate_threshold
            ):
                return Signal(
                    action="SELL",
                    strategy=self.name,
                    price=price,
                    reason="rate_below_exit_threshold",
                    order_type="market",
                    quantity_pct=1.0,
                )

            # Entry gate: rate below entry threshold blocks new
            # positions but does not force-close an open one (the
            # dead band between exit and entry thresholds is the
            # hysteresis hold zone).
            if (
                self._min_funding_rate_entry > 0.0
                and rate_annual < self._min_funding_rate_entry
            ):
                return Signal(
                    action="HOLD",
                    strategy=self.name,
                    price=price,
                    reason="rate_below_entry_threshold",
                )

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
