"""
paper_trading/base_simulator.py — Structural protocol for trading simulators.

Defines the public surface that `BacktestEngine.run`,
`portfolio.manager.PortfolioManager`, and other harness consumers
rely on, so multiple simulator backends (the existing single-leg
`PaperTrading` and the two-leg `PerpSimulator` introduced in Phase
4.B) can be substituted without changing call sites.

The protocol is `runtime_checkable` so simple `isinstance` assertions
in tests still work.  It is structural, not nominal: there is no
inheritance requirement on `PaperTrading` or `PerpSimulator` — they
satisfy the protocol by exposing the listed attributes and methods.

The "position" attribute is intentionally typed as `Optional[Any]`.
Call sites read it for two purposes only:

1. None-check (`sim.position is None` / `is not None`).
2. Read shallow attributes — `position.symbol`, `position.quantity`,
   `position.total_cost` — when a position exists.

A `BasePosition` protocol could be added later if more call sites
start coupling to position internals; today the structural
protocol on the simulator surface is sufficient.
"""

from __future__ import annotations

from typing import Any, Optional, Protocol, runtime_checkable

from strategies.base import Signal


@runtime_checkable
class BaseSimulator(Protocol):
    """Common surface every trading simulator must expose.

    Attributes:
        balance:           Cash balance (USDT).  Mutated by trade
                           execution, deposits, and withdrawals.
        initial_balance:   Capital with which the simulator was
                           constructed.  Used by the portfolio layer
                           to compute earned-profit and to seed
                           rebalancing math.
        position:          The simulator's currently open position,
                           or None when flat.  Implementations choose
                           the concrete type; consumers read shallow
                           attributes (`symbol`, `quantity`,
                           `total_cost`) on the returned object.
        trade_history:     Append-only list of completed (or partial)
                           trade records.  Concrete element type is
                           implementation-defined; consumers iterate
                           via `len(...)`, `[-1]`, and shallow
                           attribute access.
        symbol:            Default symbol the simulator trades.  The
                           engine mutates this attribute in-place for
                           multi-symbol rotation strategies.  Two-leg
                           simulators expose a synthetic
                           "<spot>+<perp>" string here for log
                           consistency; per-leg symbols live on the
                           simulator's two-leg internals.
        total_fees_paid:   Cumulative trading fees in USDT.

    Methods:
        execute_signal:        Process a strategy Signal and update
                               state.  Implementations are responsible
                               for fee handling, position
                               construction, and side semantics.
        tick:                  Per-candle tick using the close price
                               only.  Used by the engine when only
                               close-priced ticks are available.
        tick_ohlcv_candle:     Per-candle tick using OHLC for
                               accurate SL/TP wick detection.
                               Engine calls this in normal operation;
                               two-leg simulators use the OHLC values
                               on the perp leg's mark series.
        get_balance:           Cash balance (USDT) — convenience
                               accessor mirroring the `balance`
                               attribute.
        get_equity:            Current equity (cash + open-position
                               MTM at the supplied price).  Two-leg
                               simulators compute combined-leg
                               equity here.
        get_checkpoint:        Serialize state for crash recovery
                               (paper deploy persistence).
        restore_checkpoint:    Reverse of get_checkpoint.
    """

    balance: float
    initial_balance: float
    position: Optional[Any]
    trade_history: list
    symbol: str
    total_fees_paid: float

    def execute_signal(self, signal: Signal, current_price: float) -> None: ...

    def tick(self, current_price: float) -> None: ...

    def tick_ohlcv_candle(
        self, high: float, low: float, close: float
    ) -> None: ...

    def get_balance(self) -> float: ...

    def get_equity(self, current_price: float) -> float: ...

    def get_checkpoint(self) -> dict: ...

    def restore_checkpoint(self, data: dict) -> None: ...
