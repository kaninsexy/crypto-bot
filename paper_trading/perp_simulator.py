"""
paper_trading/perp_simulator.py — Two-leg delta-neutral simulator (Phase 4.B).

Sibling of `paper_trading.simulator.PaperTrading`.  Implements the
risk model specified in `research/funding-rate-risk-model.md`:

  - Long spot + short perp at equal notional (delta-neutral entry).
  - Cross-margin default (per § 1).
  - Liquidation distance per § 2 (cross-margin extension).
  - Funding payment accrual per § 3 (positive funding → bot receives).
  - Exit triggers per § 4: funding-flip (N consecutive negative
    settlements) and maintenance-margin-cushion breach.
  - Combined-position sanity at exit per § 5.

Paper-mode invariant
────────────────────
The simulator MUST NOT issue any real OKX API calls.  All OKX data
is supplied externally by the engine layer, which reads cached
parquets in `data/okx_perp.py` and `data/okx_funding.py`.  This
module performs no I/O.

Surface
───────
Satisfies `paper_trading.base_simulator.BaseSimulator` so it can be
substituted into `portfolio.manager.StrategySlot.simulator` without
changes to the portfolio layer.  The protocol-required surface
(`balance`, `position`, `trade_history`, `tick`, `tick_ohlcv_candle`,
`execute_signal`, `get_balance`, `get_equity`, `get_checkpoint`,
`restore_checkpoint`) carries the obvious two-leg semantics:

  - balance is the simulator's idle cash (USDT).  Funding cash and
    realized PnL sweep into balance on exit; while a position is
    open, accrued funding stays on the position so equity =
    balance + position MTM + accrued funding.
  - position is the open `PerpPosition` (or None when flat).
  - tick_ohlcv_candle interprets (high, low, close) as perp-mark
    OHLC for cushion / liquidation checks; the spot leg is updated
    by the engine via update_spot_close before any tick.

Two additional public methods exposed for the engine layer:

  - update_spot_close(price): stash the latest spot close; required
    before execute_signal(BUY) and consumed by get_equity.
  - apply_funding_settlement(rate, mark_price): credit/debit the
    accrued-funding ledger and tick the funding-flip exit counter.

These are not part of BaseSimulator because PaperTrading does not
need them; engine_perp uses them through the concrete type.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from loguru import logger

from paper_trading.simulator import TradeRecord
from strategies.base import Signal


# ── Defaults (binding per research/funding-rate-risk-model.md) ───────────────

# Maintenance margin ratio for the lowest BTC/ETH USDT-M tier on OKX.
# Source: § 2 of research/funding-rate-risk-model.md.
DEFAULT_MAINTENANCE_MARGIN_RATIO: float = 0.005

# Default leverage for variation #1.  § 2.2 example uses L = 5x.
DEFAULT_LEVERAGE: float = 5.0

# Funding-flip exit defaults from § 4.1.
DEFAULT_FLIP_EXIT_N: int = 3
DEFAULT_FLIP_EXIT_THRESHOLD: float = 0.0

# Maintenance-margin cushion exit threshold from § 4.2.
DEFAULT_CUSHION_THRESHOLD: float = 0.5

# Combined-position sanity tolerance at exit (§ 5).  ±5 % of entry
# notional after the 2026-05-02 chat-side recalibration: the original
# ±1 % over-fired on regime-transition closes where basis legitimately
# widens, without indicating a leg-construction bug.  See
# `research/funding-rate-risk-model.md` § 5 "Tolerance calibration
# history (chat 2026-05-02)" for the gate-2 audit numbers
# (max 2.67 %, p95 1.32 %, funding_cash_share 93.22 %) and rationale.
DEFAULT_COMBINED_POSITION_TOLERANCE: float = 0.05

# Per-leg taker fee.  OKX USDT-M default; mirrors the FEE_MARKET
# value in paper_trading.simulator for behavioural parity.
DEFAULT_TAKER_FEE: float = 0.0004


# ── PerpPosition ─────────────────────────────────────────────────────────────

@dataclass
class PerpPosition:
    """One open delta-neutral two-leg position.

    Attributes:
        spot_symbol:      Manifest notation, e.g. "BTC/USDT".
        perp_symbol:      Manifest notation matching the spot pair.
        spot_quantity:    Spot units bought (positive, magnitude).
        perp_quantity:    Perp units sold short (positive, magnitude;
                          the short side is implicit in the sign of
                          the perp PnL formula).
        spot_entry_price: Spot fill at entry.
        perp_entry_price: Perp fill at entry.
        spot_cost:        USDT paid for the spot leg (full notional;
                          spot does not borrow).
        perp_margin:      USDT posted as initial margin on the
                          short-perp leg.
        leverage:         Perp leg leverage (notional / IM).
        entry_time:       Wall-clock timestamp at entry.
        entry_notional:   Per-leg notional at entry (equal across
                          legs by construction).
        accrued_funding:  Running funding cash credited (or debited)
                          to the perp leg since entry; flushed to
                          balance on exit.
        symbol:           Synthetic logging label, e.g.
                          "BTC/USDT (perp+spot)".  Required by the
                          BaseSimulator protocol's downstream
                          consumers that read `position.symbol`.
        side:             Always "delta_neutral" — distinguishes
                          two-leg positions from PaperTrading's
                          single-leg "long" / "short".
        strategy:         Strategy name from the opening signal.
        total_cost:       USDT consumed at entry (spot_cost +
                          perp_margin + entry_fees).  Used by the
                          portfolio layer's earned-profit math.
    """
    spot_symbol: str
    perp_symbol: str
    spot_quantity: float
    perp_quantity: float
    spot_entry_price: float
    perp_entry_price: float
    spot_cost: float
    perp_margin: float
    leverage: float
    entry_time: datetime
    entry_notional: float
    total_cost: float
    strategy: str
    accrued_funding: float = 0.0
    side: str = "delta_neutral"
    symbol: str = ""

    def __post_init__(self):
        if not self.symbol:
            if self.spot_symbol == self.perp_symbol:
                self.symbol = f"{self.spot_symbol} (perp+spot)"
            else:
                self.symbol = f"{self.perp_symbol}@perp+{self.spot_symbol}@spot"


# ── PerpSimulator ────────────────────────────────────────────────────────────

class PerpSimulator:
    """Two-leg delta-neutral cash-and-carry paper simulator.

    Constructor:
        initial_balance:  Starting USDT capital (combined across
                          both legs' margin/cost requirements).
        spot_symbol:      Manifest notation for the spot leg, e.g.
                          "BTC/USDT".
        perp_symbol:      Manifest notation for the perp leg.  For
                          variation #1 (single-pair) this matches
                          spot_symbol.
        leverage:         Perp leg leverage.  Defaults to 5x (the
                          example from § 2.2 of the risk model).
        maintenance_margin_ratio: OKX tier-0 mr; default 0.005.
        flip_exit_n:      Consecutive-negative-funding settlements
                          before funding-flip exit fires.  Default 3.
        flip_exit_threshold: Funding-rate threshold below which a
                          settlement counts as "negative" toward
                          flip exit.  Default 0.0.
        cushion_threshold: Maintenance-margin cushion (multiple of
                          MM) below which the position is closed
                          voluntarily.  Mutually exclusive with
                          `exit_mr_ratio_threshold`.  When neither
                          kwarg is set, defaults to
                          DEFAULT_CUSHION_THRESHOLD (legacy path).
        exit_mr_ratio_threshold: Account margin ratio (equity /
                          position_notional) below which the position
                          is closed with reason='margin_breach'.
                          Mutually exclusive with `cushion_threshold`.
                          Path (a) of the cushion-threshold semantic
                          mismatch fix (chat 2026-05-02): the
                          literature's
                          `exit_margin_breach_threshold` is documented
                          as account margin ratio, NOT
                          (equity-MM)/MM multiplier; this kwarg lets
                          callers pass that literal value verbatim
                          without translation.  Default None
                          (cushion path active).
        taker_fee:        Per-leg taker fee rate.  Default 0.04 %.
        margin_mode:      "cross" (default) or "isolated".  Variation
                          #2+ may toggle to isolated; variation #1
                          uses cross.
    """

    def __init__(
        self,
        initial_balance: float,
        spot_symbol: str,
        perp_symbol: str,
        *,
        leverage: float = DEFAULT_LEVERAGE,
        maintenance_margin_ratio: float = DEFAULT_MAINTENANCE_MARGIN_RATIO,
        flip_exit_n: int = DEFAULT_FLIP_EXIT_N,
        flip_exit_threshold: float = DEFAULT_FLIP_EXIT_THRESHOLD,
        cushion_threshold: Optional[float] = None,
        exit_mr_ratio_threshold: Optional[float] = None,
        taker_fee: float = DEFAULT_TAKER_FEE,
        margin_mode: str = "cross",
    ):
        if margin_mode not in ("cross", "isolated"):
            raise ValueError(
                f"margin_mode must be 'cross' or 'isolated'; got {margin_mode!r}"
            )
        if leverage <= 0:
            raise ValueError(f"leverage must be positive; got {leverage}")
        # Path (a) cushion-threshold fix: cushion_threshold and
        # exit_mr_ratio_threshold are alternative exit-rule semantics
        # for the same downside-event question.  Setting both creates
        # an ambiguous resolution order and silently broadens the exit
        # surface; force the caller to pick one.
        if (
            cushion_threshold is not None
            and exit_mr_ratio_threshold is not None
        ):
            raise ValueError(
                "cushion_threshold and exit_mr_ratio_threshold are "
                "mutually exclusive; set exactly one (or neither for "
                "the default cushion_threshold path)."
            )

        self.initial_balance: float = initial_balance
        self.balance: float = initial_balance
        self.position: Optional[PerpPosition] = None
        self.trade_history: list = []
        self.symbol: str = (
            f"{spot_symbol} (perp+spot)" if spot_symbol == perp_symbol
            else f"{perp_symbol}@perp+{spot_symbol}@spot"
        )
        self.total_fees_paid: float = 0.0

        self.spot_symbol = spot_symbol
        self.perp_symbol = perp_symbol
        self.leverage = leverage
        self.maintenance_margin_ratio = maintenance_margin_ratio
        self.flip_exit_n = flip_exit_n
        self.flip_exit_threshold = flip_exit_threshold
        # Effective threshold dispatch: when either is set, store the
        # caller's value and disable the other path; when neither is
        # set, fall back to DEFAULT_CUSHION_THRESHOLD (preserves
        # pre-Path-(a) behaviour for legacy callers).
        self.exit_mr_ratio_threshold: Optional[float] = exit_mr_ratio_threshold
        if exit_mr_ratio_threshold is not None:
            self.cushion_threshold: Optional[float] = None
        elif cushion_threshold is None:
            self.cushion_threshold = DEFAULT_CUSHION_THRESHOLD
        else:
            self.cushion_threshold = cushion_threshold
        self.taker_fee = taker_fee
        self.margin_mode = margin_mode

        self._latest_spot_close: Optional[float] = None
        self._latest_perp_close: Optional[float] = None
        self._neg_funding_streak: int = 0
        # Combined-position-sanity flag set on exit per § 5; consumed
        # by trial-row forensics if non-empty.
        self.combined_position_sanity_violations: list[dict] = []
        # Per-exit forensic ledger (Track Gate-2, 2026-05-02): every
        # close appends one entry covering basis-at-exit, per-leg
        # PnL split, accrued funding, and total realised PnL.
        # Mirrors `combined_position_sanity_violations` as a
        # read-only inspection surface.  Non-empty even when no
        # sanity violation fires; consumers filter by exit_reason.
        self.exit_forensics: list[dict] = []

    # ── Engine integration helpers (not in BaseSimulator) ─────────────────────

    def update_spot_close(self, price: float) -> None:
        """Stash the latest spot close.  Required before
        execute_signal(BUY) and used by get_equity for spot-leg MTM."""
        self._latest_spot_close = float(price)

    def apply_funding_settlement(
        self, funding_rate: float, mark_price: float,
    ) -> None:
        """Apply one funding settlement to the open perp leg.

        Sign convention per § 3.1 of the risk model:
          funding_rate > 0 → longs pay shorts → bot RECEIVES.
          funding_rate < 0 → shorts pay longs → bot PAYS.

        Side effects:
          - Updates accrued_funding on the open position (no-op when
            flat).
          - Advances the funding-flip exit counter and fires the
            close if the streak reaches `flip_exit_n`.
        """
        # Reset streak when flat or when funding is non-negative.
        if self.position is None:
            self._neg_funding_streak = 0
            return

        funding_cash = funding_rate * mark_price * self.position.perp_quantity
        # Bot is short perp; positive funding accrues positive cash to
        # the short side.
        self.position.accrued_funding += funding_cash

        if funding_rate < self.flip_exit_threshold:
            self._neg_funding_streak += 1
        else:
            self._neg_funding_streak = 0

        if self._neg_funding_streak >= self.flip_exit_n:
            logger.info(
                f"[PERP] Funding-flip exit: {self._neg_funding_streak} "
                f"consecutive settlements below {self.flip_exit_threshold:.6f}"
            )
            self._close_two_leg(
                perp_exit_price=mark_price,
                spot_exit_price=self._latest_spot_close or mark_price,
                reason="funding_flip",
            )

    # ── BaseSimulator surface ─────────────────────────────────────────────────

    def execute_signal(self, signal: Signal, current_price: float) -> None:
        """Process a Signal at the supplied perp price.

        BUY  → opens both legs at equal notional.
        SELL → closes both legs.
        HOLD → no-op.
        """
        if signal.action == "BUY":
            if self.position is not None:
                logger.debug(
                    "[PERP] BUY received but position already open; ignoring."
                )
                return
            self._open_two_leg(perp_entry_price=current_price, signal=signal)
        elif signal.action == "SELL":
            if self.position is None:
                logger.debug("[PERP] SELL received with no position; ignoring.")
                return
            self._close_two_leg(
                perp_exit_price=current_price,
                spot_exit_price=self._latest_spot_close or current_price,
                reason=signal.reason or "signal_close",
            )
        # HOLD intentionally falls through.

    def tick(self, current_price: float) -> None:
        """Per-candle close-only tick.  Routes to tick_ohlcv_candle
        with HLC all equal so cushion logic uses a single price."""
        self._latest_perp_close = float(current_price)
        self.tick_ohlcv_candle(
            high=current_price, low=current_price, close=current_price,
        )

    def tick_ohlcv_candle(
        self, high: float, low: float, close: float,
    ) -> None:
        """Per-candle perp-mark OHLC tick.

        The cushion check is evaluated against `high` because the
        worst-case adverse move for a short-perp leg is an upside
        spike: a high mark print compresses the cushion most.
        Liquidation triggers on the same path with the same ordering
        as PaperTrading's SL check.
        """
        self._latest_perp_close = float(close)
        if self.position is None:
            return

        # Path-(a) dispatch (chat 2026-05-02 cushion-threshold fix):
        # when `exit_mr_ratio_threshold` is set, evaluate the account
        # margin ratio (equity / notional) — the literature value's
        # native unit — and short-circuit before the legacy cushion
        # path runs.  When unset, fall back to the legacy cushion
        # check.  __init__ enforces mutual exclusion so the two
        # branches never both fire.
        # Explicit `is not None` per CLAUDE.md.
        spot_exit = (
            self._latest_spot_close
            if self._latest_spot_close is not None
            else high
        )
        if self.exit_mr_ratio_threshold is not None:
            mr_ratio = self._compute_account_margin_ratio(mark_price=high)
            if mr_ratio < self.exit_mr_ratio_threshold:
                logger.warning(
                    f"[PERP] Account margin ratio {mr_ratio:.4f} "
                    f"< threshold {self.exit_mr_ratio_threshold:.4f} "
                    f"at mark {high:.4f}; closing position."
                )
                self._close_two_leg(
                    perp_exit_price=high,
                    spot_exit_price=spot_exit,
                    reason="margin_breach",
                )
            return

        # Cushion check at the worst-case mark within the bar.
        if self.cushion_threshold is None:
            return
        cushion = self._compute_cushion(mark_price=high)
        if cushion < self.cushion_threshold:
            logger.warning(
                f"[PERP] Maintenance-margin cushion {cushion:.3f} "
                f"< threshold {self.cushion_threshold:.3f} at mark {high:.4f}; "
                "closing position."
            )
            self._close_two_leg(
                perp_exit_price=high,
                spot_exit_price=spot_exit,
                reason="margin_breach",
            )

    def get_balance(self) -> float:
        return self.balance

    def get_equity(self, current_price: float) -> float:
        """Combined two-leg equity at a given perp price.

        Uses the latest stored spot close for the spot leg's MTM.
        Accrued funding on the open position is included; on exit
        funding is swept into balance, so equity numbers are
        continuous across the close event.
        """
        if self.position is None:
            return self.balance

        pos = self.position
        spot_price = (
            self._latest_spot_close
            if self._latest_spot_close is not None
            else pos.spot_entry_price
        )
        spot_pnl = (spot_price - pos.spot_entry_price) * pos.spot_quantity
        # Short perp PnL: positive when price falls.
        perp_pnl = (pos.perp_entry_price - current_price) * pos.perp_quantity
        # Position cost (spot full notional + perp initial margin) is
        # money the simulator has already deducted from `balance`; add
        # it back on top of MTM PnL to recover equity.
        equity = (
            self.balance
            + pos.spot_cost + pos.perp_margin
            + spot_pnl + perp_pnl
            + pos.accrued_funding
        )
        return equity

    def get_checkpoint(self) -> dict:
        """Serialize state for crash recovery."""
        pos_data: Optional[dict] = None
        if self.position is not None:
            p = self.position
            pos_data = {
                "spot_symbol":      p.spot_symbol,
                "perp_symbol":      p.perp_symbol,
                "spot_quantity":    p.spot_quantity,
                "perp_quantity":    p.perp_quantity,
                "spot_entry_price": p.spot_entry_price,
                "perp_entry_price": p.perp_entry_price,
                "spot_cost":        p.spot_cost,
                "perp_margin":      p.perp_margin,
                "leverage":         p.leverage,
                "entry_time":       p.entry_time.isoformat(),
                "entry_notional":   p.entry_notional,
                "total_cost":       p.total_cost,
                "strategy":         p.strategy,
                "accrued_funding":  p.accrued_funding,
                "side":             p.side,
                "symbol":           p.symbol,
            }
        return {
            "balance":               self.balance,
            "total_fees_paid":       self.total_fees_paid,
            "neg_funding_streak":    self._neg_funding_streak,
            "latest_spot_close":     self._latest_spot_close,
            "latest_perp_close":     self._latest_perp_close,
            "position":              pos_data,
        }

    def restore_checkpoint(self, data: dict) -> None:
        self.balance = data.get("balance", self.balance)
        self.total_fees_paid = data.get("total_fees_paid", 0.0)
        self._neg_funding_streak = data.get("neg_funding_streak", 0)
        self._latest_spot_close = data.get("latest_spot_close")
        self._latest_perp_close = data.get("latest_perp_close")
        pos_data = data.get("position")
        if pos_data is None:
            self.position = None
            return
        self.position = PerpPosition(
            spot_symbol=pos_data["spot_symbol"],
            perp_symbol=pos_data["perp_symbol"],
            spot_quantity=pos_data["spot_quantity"],
            perp_quantity=pos_data["perp_quantity"],
            spot_entry_price=pos_data["spot_entry_price"],
            perp_entry_price=pos_data["perp_entry_price"],
            spot_cost=pos_data["spot_cost"],
            perp_margin=pos_data["perp_margin"],
            leverage=pos_data["leverage"],
            entry_time=datetime.fromisoformat(pos_data["entry_time"]),
            entry_notional=pos_data["entry_notional"],
            total_cost=pos_data["total_cost"],
            strategy=pos_data.get("strategy", ""),
            accrued_funding=pos_data.get("accrued_funding", 0.0),
            side=pos_data.get("side", "delta_neutral"),
            symbol=pos_data.get("symbol", ""),
        )

    # ── Internal: leg construction ────────────────────────────────────────────

    def _open_two_leg(
        self, perp_entry_price: float, signal: Signal,
    ) -> None:
        """Open spot+perp legs at equal notional from `self.balance`.

        Per § 5 of the risk model, equal-notional means
        `position_perp_notional == position_spot_notional` at entry,
        producing a zero net delta by construction.

        Capital usage per unit of notional:
          - spot leg pays full notional (no leverage on spot).
          - perp leg pays notional/leverage as initial margin.
          - both legs pay taker_fee × notional in fees.

        Total balance consumed for one unit of per-leg notional is
        `1 + 1/leverage + 2*taker_fee`.  We divide the available
        balance by that factor to derive per-leg notional, then size
        each leg from its own price.
        """
        if self._latest_spot_close is None:
            raise ValueError(
                "PerpSimulator.execute_signal(BUY) requires update_spot_close() "
                "to have supplied a spot price before the signal fires."
            )
        spot_entry_price = self._latest_spot_close

        # Solve for per-leg notional that consumes exactly `balance`:
        # notional + notional/leverage + 2*taker_fee*notional = balance
        denom = 1.0 + (1.0 / self.leverage) + 2.0 * self.taker_fee
        per_leg_notional = self.balance / denom
        if per_leg_notional <= 0:
            logger.warning(
                f"[PERP] Cannot open: per-leg notional {per_leg_notional} ≤ 0 "
                f"with balance {self.balance}"
            )
            return

        spot_quantity = per_leg_notional / spot_entry_price
        perp_quantity = per_leg_notional / perp_entry_price
        spot_cost = spot_quantity * spot_entry_price
        perp_margin = (perp_quantity * perp_entry_price) / self.leverage
        spot_fee = spot_cost * self.taker_fee
        perp_fee = (perp_quantity * perp_entry_price) * self.taker_fee
        entry_fees = spot_fee + perp_fee

        deducted = spot_cost + perp_margin + entry_fees
        # Floating-point slop guard: clamp to balance if denom rounding
        # left a tiny over-spend.  Treat clamping as the new floor for
        # subsequent capital math.
        if deducted > self.balance:
            deducted = self.balance

        self.balance -= deducted
        self.total_fees_paid += entry_fees

        self.position = PerpPosition(
            spot_symbol=self.spot_symbol,
            perp_symbol=self.perp_symbol,
            spot_quantity=spot_quantity,
            perp_quantity=perp_quantity,
            spot_entry_price=spot_entry_price,
            perp_entry_price=perp_entry_price,
            spot_cost=spot_cost,
            perp_margin=perp_margin,
            leverage=self.leverage,
            entry_time=datetime.now(timezone.utc),
            entry_notional=per_leg_notional,
            total_cost=deducted,
            strategy=signal.strategy,
            accrued_funding=0.0,
        )
        # Reset the funding streak — a new position starts fresh.
        self._neg_funding_streak = 0

        logger.info(
            f"[PERP] OPEN delta-neutral | spot {spot_quantity:.6f} @ "
            f"{spot_entry_price:.4f} | perp {perp_quantity:.6f} @ "
            f"{perp_entry_price:.4f} | per-leg notional ${per_leg_notional:,.2f} "
            f"| margin ${perp_margin:,.2f} | fees ${entry_fees:.4f} | "
            f"balance after open ${self.balance:,.2f}"
        )

    def _close_two_leg(
        self,
        perp_exit_price: float,
        spot_exit_price: float,
        reason: str,
    ) -> None:
        """Close both legs at the supplied prices.

        Realized PnL composition matches § 3.2 of the risk model:
          total = spot_pnl + perp_pnl + Σ funding − fees

        Combined-position sanity per § 5: at exit
        `abs(spot_qty * spot_exit + perp_qty * perp_exit)` should be
        ≈ 2 × entry_notional (the long-spot value plus the
        perp-buyback value, both as positive magnitudes).  A
        deviation greater than `combined_position_tolerance` × entry
        notional records a violation entry on
        `combined_position_sanity_violations` for forensic review.
        """
        if self.position is None:
            return
        pos = self.position

        spot_proceeds = pos.spot_quantity * spot_exit_price
        perp_buyback_value = pos.perp_quantity * perp_exit_price
        spot_pnl = spot_proceeds - pos.spot_cost
        # Short perp realized: positive when price drops.
        perp_pnl = (
            (pos.perp_entry_price - perp_exit_price) * pos.perp_quantity
        )
        spot_exit_fee = spot_proceeds * self.taker_fee
        perp_exit_fee = perp_buyback_value * self.taker_fee
        exit_fees = spot_exit_fee + perp_exit_fee

        # Combined-position sanity check.
        combined = spot_proceeds + perp_buyback_value
        expected = 2.0 * pos.entry_notional
        deviation = (
            abs(combined - expected) / expected if expected > 0 else 0.0
        )
        if deviation > DEFAULT_COMBINED_POSITION_TOLERANCE:
            self.combined_position_sanity_violations.append({
                "ts":              datetime.now(timezone.utc).isoformat(),
                "spot_exit":       spot_exit_price,
                "perp_exit":       perp_exit_price,
                "combined":        combined,
                "expected":        expected,
                "deviation_pct":   deviation * 100.0,
                "reason":          reason,
            })

        # Sweep funds into balance.
        # The simulator originally deducted (spot_cost + perp_margin +
        # entry_fees).  On exit it returns:
        #   spot_proceeds (= spot_cost + spot_pnl)
        #   perp_margin + perp_pnl       (margin + leg PnL)
        #   accrued_funding              (funding cash)
        # minus exit fees.
        gross_in = spot_proceeds + (pos.perp_margin + perp_pnl) + pos.accrued_funding
        self.balance += gross_in - exit_fees
        self.total_fees_paid += exit_fees

        total_pnl = (
            spot_pnl + perp_pnl + pos.accrued_funding - exit_fees
        )
        cost_basis = pos.spot_cost + pos.perp_margin
        pnl_pct = (total_pnl / cost_basis * 100.0) if cost_basis > 0 else 0.0

        record = TradeRecord(
            symbol=pos.symbol,
            strategy=pos.strategy,
            side=pos.side,
            entry_price=pos.perp_entry_price,
            exit_price=perp_exit_price,
            quantity=pos.perp_quantity,
            cost=cost_basis,
            entry_time=pos.entry_time,
            exit_time=datetime.now(timezone.utc),
            pnl=total_pnl,
            pnl_pct=pnl_pct,
            fees_paid=exit_fees,
            exit_reason=reason,
            is_partial=False,
            order_type="market",
            compounded=False,
        )
        self.trade_history.append(record)

        # Per-exit forensic ledger (Track Gate-2): one entry per
        # close, regardless of whether the combined-position sanity
        # check tripped its tolerance.  Consumers filter by
        # `exit_reason` to scope diagnostics.  Schema below mirrors
        # the audit script's expectations
        # (`scripts/phase_4b_gate2_audit.py`): `perp_qty` is signed
        # (negative for short, matching the leg direction); the
        # absolute value is used in
        # `perp_notional_at_exit`/`basis_at_exit_abs_pct` so the
        # numbers stay legible.
        spot_notional_at_exit = pos.spot_quantity * spot_exit_price
        perp_notional_at_exit = abs(pos.perp_quantity) * perp_exit_price
        if pos.entry_notional > 0:
            basis_at_exit_abs_pct = (
                abs(spot_notional_at_exit - perp_notional_at_exit)
                / pos.entry_notional
            )
        else:
            basis_at_exit_abs_pct = 0.0
        self.exit_forensics.append({
            "exit_time":             datetime.now(timezone.utc),
            "exit_reason":           reason,
            "spot_qty":              pos.spot_quantity,
            "spot_exit_price":       spot_exit_price,
            "perp_qty":              -pos.perp_quantity,  # signed: short → neg
            "perp_exit_price":       perp_exit_price,
            "entry_notional":        pos.entry_notional,
            "spot_notional_at_exit": spot_notional_at_exit,
            "perp_notional_at_exit": perp_notional_at_exit,
            "basis_at_exit_abs_pct": basis_at_exit_abs_pct,
            "spot_pnl":              spot_pnl,
            "perp_pnl":              perp_pnl,
            "funding_cash":          pos.accrued_funding,
            "total_pnl":             total_pnl,
        })

        logger.info(
            f"[PERP] CLOSE delta-neutral | reason={reason} | "
            f"spot pnl ${spot_pnl:+.2f} | perp pnl ${perp_pnl:+.2f} | "
            f"funding ${pos.accrued_funding:+.2f} | exit fees ${exit_fees:.4f} "
            f"| total ${total_pnl:+.2f} ({pnl_pct:+.2f}%) | "
            f"balance ${self.balance:,.2f}"
        )

        self.position = None
        self._neg_funding_streak = 0

    # ── Internal: cushion / liquidation math ──────────────────────────────────

    def _compute_account_margin_ratio(self, mark_price: float) -> float:
        """Account margin ratio = current_equity / position_notional.

        cross:    equity = perp_margin + perp_pnl + spot_pnl + accrued_funding
        isolated: equity = perp_margin + perp_pnl + accrued_funding
        notional = perp_qty × mark_price (the perp leg defines the
                  position notional that the margin ratio is
                  measured against; matches OKX's published
                  margin-ratio definition for USDT-M perps).

        Mirrors `_compute_cushion`'s `margin_mode` dispatch but
        returns the ratio directly rather than the
        cushion-multiple-of-MM transform.

        Returns inf when no position is open or the perp leg's
        notional is zero (degenerate state; no exit decision can be
        derived).
        """
        if self.position is None:
            return float("inf")
        pos = self.position
        notional = pos.perp_quantity * mark_price
        if notional <= 0:
            return float("inf")
        perp_pnl = (pos.perp_entry_price - mark_price) * pos.perp_quantity
        if self.margin_mode == "cross":
            spot_price = (
                self._latest_spot_close
                if self._latest_spot_close is not None
                else pos.spot_entry_price
            )
            spot_pnl = (spot_price - pos.spot_entry_price) * pos.spot_quantity
            equity = (
                pos.perp_margin + perp_pnl + spot_pnl + pos.accrued_funding
            )
        else:
            equity = pos.perp_margin + perp_pnl + pos.accrued_funding
        return equity / notional

    def _compute_cushion(self, mark_price: float) -> float:
        """Maintenance-margin cushion at a given mark, expressed as
        a multiple of MM (per § 4.2 of the risk model).

        cushion = (current_equity − maintenance_margin) / maintenance_margin

        Equity here uses the cross-margin pool (spot equity offsets
        the short-perp loss when price rises) when `margin_mode ==
        "cross"`.  For isolated mode, only the perp leg's IM + PnL +
        accrued funding count as equity for cushion purposes.
        """
        if self.position is None:
            return float("inf")
        pos = self.position
        perp_pnl = (pos.perp_entry_price - mark_price) * pos.perp_quantity
        maintenance_margin = pos.perp_quantity * mark_price * self.maintenance_margin_ratio
        if maintenance_margin <= 0:
            return float("inf")

        if self.margin_mode == "cross":
            spot_price = (
                self._latest_spot_close
                if self._latest_spot_close is not None
                else pos.spot_entry_price
            )
            spot_pnl = (spot_price - pos.spot_entry_price) * pos.spot_quantity
            equity_for_cushion = (
                pos.perp_margin + perp_pnl + spot_pnl + pos.accrued_funding
            )
        else:
            equity_for_cushion = (
                pos.perp_margin + perp_pnl + pos.accrued_funding
            )

        return (equity_for_cushion - maintenance_margin) / maintenance_margin

    def liquidation_price(self) -> Optional[float]:
        """Price at which the short-perp leg liquidates, per § 2.

        Cross-margin (default): S0 × (1 + 2/L) / (1 + mr).
        Isolated:                S0 × (1 + 1/L) / (1 + mr).

        Returns None when no position is open.
        """
        if self.position is None:
            return None
        s0 = self.position.perp_entry_price
        l = self.leverage
        mr = self.maintenance_margin_ratio
        if self.margin_mode == "cross":
            return s0 * (1.0 + 2.0 / l) / (1.0 + mr)
        return s0 * (1.0 + 1.0 / l) / (1.0 + mr)
