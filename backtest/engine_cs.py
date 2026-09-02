"""
backtest/engine_cs.py — Cross-sectional long-short perpetual book engine.

Third sibling of `backtest.engine.BacktestEngine.run` (single-symbol
spot), `backtest.engine_multi.run_engine_multi` (long-only multi-asset
basket) and `backtest.engine_perp.run_perp` (two-leg perp+spot).  This
one replays a **dollar-neutral-capable, long-short USDT-M perpetual
book** over a dynamic universe: per-bar target weights from a
cross-sectional strategy, funding accrued at 8h settlements, per-leg
maintenance-margin / liquidation checks, taker fee + slippage, and
forced closure on delisting (universe-mask False).

Substrate: Binance UM archive (`data/binance_vision_um.py`), execution
venue OKX perps.  Motivation and scope: `docs/research_revival_2026-09.md`
§C.3 ("Engine: new `backtest/engine_cs.py` ... Do **not** modify
`engine_multi.py` (its long-only contract underlies 21 recorded
trials)").

Per-bar return shape contract
─────────────────────────────
Identical to `engine_perp`: the returned result's `equity_curve` is a
`pd.Series` whose `.pct_change().dropna()` is directly consumable by
`backtest.cpcv_common._sharpe_from_returns` and by
`backtest.per_bar_store.persist_per_bar_returns`.  No harness module is
edited by this file — `cpcv.py`, `cpcv_common.py`, `cpcv_multi.py`,
`dsr.py`, `verdict.py`, `engine.py`, `engine_multi.py` and
`engine_perp.py` are untouched.

`CrossSectionalResult` SUBCLASSES `backtest.engine.BacktestResult`, so
every consumer that reads `.metrics` / `.equity_curve` /
`.trade_history` / `.period_label` (that is: the whole CPCV → DSR →
verdict chain) accepts it unchanged.  The extra fields (per-bar return
series, turnover, funding-vs-price PnL decomposition, liquidation
events, realised weights) are additive diagnostics.

CPCV adapter shim
─────────────────
`run_engine_cs(data, strategy, *, period_label=..., initial_balance=...)`
mirrors `run_engine_multi`'s call shape EXACTLY for those four
arguments, which is the shape `backtest.cpcv_multi.run_cpcv_multi`
invokes per block.  A future `cpcv_cs.py` is therefore a copy of
`cpcv_multi.py` with the engine import swapped — no edit to any
existing runner.  The two extra inputs a perp book needs (funding
settlements and the eligibility mask) can be supplied EITHER as
explicit `funding=` / `universe_mask=` keywords OR as extra columns on
the per-symbol frames (`funding_rate`, `eligible`).  The column form is
what a block-slicing CPCV runner wants: `data[sym].loc[ts_range]`
slices funding and eligibility along with the prices, so no
out-of-band state has to be re-sliced.

Timeline convention (differs from engine_multi — deliberately)
──────────────────────────────────────────────────────────────
`engine_multi` synchronises on the timestamp INTERSECTION of the
basket.  A cross-sectional perp universe has listings and delistings by
construction, so intersecting would silently discard the universe.
`engine_cs` synchronises on the sorted UNION and treats a symbol with
no bar (or a False mask) at `t` as ineligible at `t` — which is what
force-closes a delisted leg.

Funding-timestamp jitter
────────────────────────
Binance UM `fundingRate` archive rows carry millisecond jitter on the
settlement stamp (e.g. `00:00:00.002`).  `align_funding_to_bars` floors
every settlement timestamp to the hour BEFORE bucketing it into a bar,
so an 00:00:00.002 settlement lands in the 00:00 bar rather than
leaking into the next one.

Paper-mode invariant
────────────────────
Like every other engine module, this one performs no I/O.  All prices,
funding and universe data are supplied by the caller.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional, Union

import numpy as np
import pandas as pd
from loguru import logger

from backtest.engine import BacktestMetrics, BacktestResult

# ── Cost / risk constants ────────────────────────────────────────────────────

#: OKX USDT-M perpetual taker fee, per leg, per side.
FEE_TAKER_PERP: float = 0.0005

#: Adverse fill applied to every market execution (bar close ± this).
SLIPPAGE: float = 0.0005

#: Maintenance-margin ratio.  `research/funding-rate-risk-model.md` §2
#: uses 0.005 for the lowest BTC/ETH tier; that is optimistic for the
#: alt names a cross-sectional universe holds, so the default here is
#: 1 % and the value is a parameter.
DEFAULT_MAINTENANCE_MARGIN_RATIO: float = 0.01

#: Per-leg leverage (notional ÷ initial margin posted).  Gross book
#: exposure is capped at 1× equity by the weight constraint, so this
#: only sets how far an individual leg can run before its own margin
#: breaches.
DEFAULT_LEVERAGE: float = 3.0

#: Extra cost charged on a forced (liquidation) close, on top of
#: slippage and the taker fee.
LIQUIDATION_PENALTY: float = 0.005

#: Gross weight cap enforced on the STRATEGY's own target book
#: (Σ|w| ≤ 1.0).  An engine-computed beta hedge is added on top and is
#: deliberately not renormalised — see `_apply_beta_hedge`.
DEFAULT_MAX_GROSS_WEIGHT: float = 1.0

DEFAULT_INITIAL_BALANCE: float = 10_000.0
DEFAULT_BETA_LOOKBACK: int = 30
DEFAULT_HEDGE_SYMBOL: str = "BTCUSDT"

#: Trades below this notional are no-ops (float dust).
_MIN_TRADE_NOTIONAL: float = 1e-9


# ── Records ──────────────────────────────────────────────────────────────────

@dataclass
class CSLegTrade:
    """One realised (fully or partially closed) leg.

    Diagnostics only: the equity curve is marked-to-market bar by bar
    and is the authoritative accounting.  These records exist so
    `BacktestMetrics` win-rate / profit-factor fields and the
    verdict-tree forensics have per-leg granularity.
    """
    symbol: str
    side: str                     # "long" | "short"
    quantity: float               # magnitude of the closed units
    entry_price: float
    exit_price: float
    entry_time: Optional[pd.Timestamp]
    exit_time: Optional[pd.Timestamp]
    pnl: float                    # net of the closing fee, incl. funding
    pnl_pct: float
    funding: float                # funding attributed to the closed units
    fees: float
    reason: str                   # "rebalance"|"liquidation"|"ineligible"|"end"


@dataclass
class CSLiquidationEvent:
    """One per-leg maintenance-margin breach, force-closed at the bar
    close with `liquidation_penalty` charged on top."""
    timestamp: pd.Timestamp
    symbol: str
    side: str
    entry_price: float
    liquidation_price: float
    trigger_price: float
    exit_price: float
    notional: float
    penalty: float


@dataclass
class CSForcedCloseEvent:
    """One leg closed because its symbol left the eligible universe
    (delisting, mask False, or no bar at this timestamp)."""
    timestamp: pd.Timestamp
    symbol: str
    side: str
    exit_price: float
    notional: float
    reason: str


@dataclass
class _Leg:
    """Mutable per-symbol book state."""
    symbol: str
    qty: float = 0.0              # signed: > 0 long, < 0 short
    entry_price: float = 0.0      # magnitude-weighted average entry
    entry_time: Optional[pd.Timestamp] = None
    funding: float = 0.0          # funding accrued on the open units
    last_price: float = float("nan")


@dataclass
class CrossSectionalResult(BacktestResult):
    """`BacktestResult` + cross-sectional perp-book diagnostics.

    Inherited (and therefore CPCV/DSR/verdict-compatible) fields:
    `metrics`, `equity_curve`, `trade_history`, `period_label`.
    """
    returns: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    turnover: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    funding_pnl_series: pd.Series = field(
        default_factory=lambda: pd.Series(dtype=float))
    price_pnl_series: pd.Series = field(
        default_factory=lambda: pd.Series(dtype=float))
    fee_series: pd.Series = field(
        default_factory=lambda: pd.Series(dtype=float))
    gross_exposure: pd.Series = field(
        default_factory=lambda: pd.Series(dtype=float))
    funding_pnl: float = 0.0
    price_pnl: float = 0.0
    total_fees: float = 0.0
    total_penalties: float = 0.0
    total_turnover: float = 0.0
    n_trades: int = 0             # count of LEG OPENS
    liquidation_events: list = field(default_factory=list)
    forced_close_events: list = field(default_factory=list)
    weights_history: dict = field(default_factory=dict)
    symbols: list = field(default_factory=list)

    @property
    def n_liquidations(self) -> int:
        return len(self.liquidation_events)


# ── Funding alignment ────────────────────────────────────────────────────────

def align_funding_to_bars(
    funding: dict,
    index: pd.DatetimeIndex,
    symbols: list,
    *,
    rate_column: str = "last_funding_rate",
    floor_freq: str = "h",
) -> pd.DataFrame:
    """Bucket 8h funding settlements into per-bar accrued rates.

    Every settlement timestamp is FLOORED to `floor_freq` first — the
    Binance UM archive stamps settlements with millisecond jitter
    (`00:00:00.002`), which without flooring pushes a settlement into
    the following bar whenever a bar boundary is the settlement hour.

    A settlement at floored time `s` is accrued into the FIRST bar
    whose timestamp is ≥ `s`; i.e. bar `t` accrues everything in
    `(t_prev, t]`, matching the mark-to-market convention of the
    engine loop.  Settlements after the last bar are dropped.

    Args:
      funding:     `{symbol: Series | DataFrame}` of settlement rates.
                   A DataFrame must carry `rate_column` (the
                   `data.binance_vision_um.fetch_funding` shape) or a
                   single column.
      index:       The bar timeline (sorted, UTC).
      symbols:     Columns to produce, in order.
      rate_column: Column holding the rate when a DataFrame is passed.
      floor_freq:  Rounding applied to settlement stamps ("h").

    Returns:
      Wide float DataFrame `[index × symbols]`, 0.0 where nothing
      settled.
    """
    out = pd.DataFrame(0.0, index=index, columns=list(symbols), dtype=float)
    if not funding or len(index) == 0:
        return out
    idx_vals = index.values
    for sym in symbols:
        raw = funding.get(sym)
        if raw is None or len(raw) == 0:
            continue
        if isinstance(raw, pd.DataFrame):
            if rate_column in raw.columns:
                series = raw[rate_column]
            elif raw.shape[1] == 1:
                series = raw.iloc[:, 0]
            else:
                raise ValueError(
                    f"funding[{sym!r}] DataFrame has no {rate_column!r} "
                    f"column and is not single-column: "
                    f"{list(raw.columns)!r}"
                )
        else:
            series = pd.Series(raw)
        ts = pd.DatetimeIndex(series.index)
        if ts.tz is None and index.tz is not None:
            ts = ts.tz_localize(index.tz)
        elif ts.tz is not None and index.tz is not None:
            ts = ts.tz_convert(index.tz)
        # Millisecond jitter guard — floor BEFORE bucketing.
        ts = ts.floor(floor_freq)
        pos = np.searchsorted(idx_vals, ts.values, side="left")
        vals = pd.to_numeric(series.values, errors="coerce").astype(float)
        keep = (pos < len(index)) & np.isfinite(vals)
        if not keep.any():
            continue
        acc = np.zeros(len(index), dtype=float)
        np.add.at(acc, pos[keep], vals[keep])
        out[sym] = acc
    return out


# ── Panel normalisation ──────────────────────────────────────────────────────

def _normalise_panel(
    data: Union[dict, pd.DataFrame],
    symbols: Optional[list] = None,
):
    """Return `(index, close, high, low, funding_col, eligible_col, symbols)`.

    Accepts either `{symbol: DataFrame}` (columns: `close` required;
    `high`, `low`, `funding_rate`, `eligible` optional) or a wide
    close-price panel `DataFrame` whose columns are symbols.
    """
    if isinstance(data, pd.DataFrame):
        wide = data.sort_index()
        syms = [s for s in wide.columns] if symbols is None else [
            s for s in symbols if s in wide.columns
        ]
        if len(syms) == 0:
            raise ValueError(
                f"wide panel has no usable symbol columns "
                f"(requested={symbols!r}, available={list(wide.columns)!r})"
            )
        index = pd.DatetimeIndex(wide.index)
        close = wide[syms].astype(float)
        return index, close, None, None, None, None, syms

    if not isinstance(data, dict) or len(data) == 0:
        raise ValueError(
            "data must be a non-empty {symbol: DataFrame} dict or a wide "
            f"close panel DataFrame; got {type(data).__name__}"
        )
    syms = list(data.keys()) if symbols is None else [
        s for s in symbols if s in data
    ]
    if len(syms) == 0:
        raise ValueError(
            f"No overlap between requested symbols={symbols!r} and data "
            f"keys {sorted(data.keys())!r}."
        )

    index: Optional[pd.DatetimeIndex] = None
    for sym in syms:
        idx = pd.DatetimeIndex(data[sym].sort_index().index)
        index = idx if index is None else index.union(idx)
    assert index is not None
    index = index.sort_values()
    if len(index) == 0:
        raise ValueError("panel timeline is empty across all symbols")

    close = pd.DataFrame(index=index, columns=syms, dtype=float)
    high = pd.DataFrame(index=index, columns=syms, dtype=float)
    low = pd.DataFrame(index=index, columns=syms, dtype=float)
    fund = pd.DataFrame(0.0, index=index, columns=syms, dtype=float)
    elig = pd.DataFrame(True, index=index, columns=syms, dtype=bool)
    has_fund = False
    has_elig = False

    for sym in syms:
        df = data[sym].sort_index()
        if "close" not in df.columns:
            raise ValueError(
                f"data[{sym!r}] has no 'close' column; got "
                f"{list(df.columns)!r}"
            )
        df = df[~df.index.duplicated(keep="last")]
        close[sym] = pd.to_numeric(df["close"], errors="coerce").reindex(index)
        high[sym] = (
            pd.to_numeric(df["high"], errors="coerce").reindex(index)
            if "high" in df.columns else close[sym]
        )
        low[sym] = (
            pd.to_numeric(df["low"], errors="coerce").reindex(index)
            if "low" in df.columns else close[sym]
        )
        if "funding_rate" in df.columns:
            has_fund = True
            fund[sym] = (
                pd.to_numeric(df["funding_rate"], errors="coerce")
                .reindex(index).fillna(0.0)
            )
        if "eligible" in df.columns:
            has_elig = True
            elig[sym] = (
                df["eligible"].reindex(index).fillna(False).astype(bool)
            )

    return (
        index, close, high, low,
        fund if has_fund else None,
        elig if has_elig else None,
        syms,
    )


# ── Liquidation math (research/funding-rate-risk-model.md §2.2) ──────────────

def liquidation_price(
    entry_price: float,
    is_long: bool,
    leverage: float,
    maintenance_margin_ratio: float,
) -> float:
    """Per-leg liquidation price, isolated-margin convention.

    Short (risk-model §2.2, verbatim):
        S_liq = S0 × (1 + 1/L) / (1 + mr)

    Long (the symmetric derivation: IM + Q(S − S0) ≤ Q·S·mr):
        S_liq = S0 × (1 − 1/L) / (1 − mr)

    Each leg of a cross-sectional book is margined on its own notional
    — the cross-margin ×2 extension in §2.3 is specific to the
    equal-notional spot+perp construction and is deliberately NOT
    applied here (a long-short book's legs are not the two sides of one
    delta-neutral pair).  That makes this the conservative choice.
    """
    if leverage <= 0:
        raise ValueError(f"leverage must be > 0; got {leverage}")
    mr = maintenance_margin_ratio
    if is_long:
        denom = 1.0 - mr
        if denom <= 0:
            return 0.0
        return entry_price * (1.0 - 1.0 / leverage) / denom
    return entry_price * (1.0 + 1.0 / leverage) / (1.0 + mr)


# ── Public API ───────────────────────────────────────────────────────────────

def run_engine_cs(
    data: Union[dict, pd.DataFrame],
    strategy: Any,
    *,
    period_label: str = "full",
    initial_balance: float = DEFAULT_INITIAL_BALANCE,
    funding: Optional[dict] = None,
    universe_mask: Optional[pd.DataFrame] = None,
    fee_taker: float = FEE_TAKER_PERP,
    fee_multiplier: float = 1.0,
    slippage: float = SLIPPAGE,
    maintenance_margin_ratio: float = DEFAULT_MAINTENANCE_MARGIN_RATIO,
    leverage: float = DEFAULT_LEVERAGE,
    liquidation_penalty: float = LIQUIDATION_PENALTY,
    rebalance_every: int = 1,
    beta_lookback: int = DEFAULT_BETA_LOOKBACK,
    max_gross_weight: float = DEFAULT_MAX_GROSS_WEIGHT,
    close_at_end: bool = True,
    funding_rate_column: str = "last_funding_rate",
) -> CrossSectionalResult:
    """Replay a cross-sectional long-short perp book.

    Args:
      data:              `{symbol: DataFrame}` (UTC index; `close`
                         required; `high`/`low`/`funding_rate`/
                         `eligible` optional) or a wide close panel
                         whose columns are symbols.
      strategy:          Any object exposing
                         `target_weights(t, panel_upto_t) -> {symbol: w}`
                         with `Σ|w| ≤ max_gross_weight`, positive =
                         long, negative = short.  Optional attributes:
                         `symbols` (universe restriction),
                         `beta_hedge` (bool), `hedge_symbol`
                         (default "BTCUSDT"), `name`.
                         `panel_upto_t` is the wide close panel sliced
                         to rows ≤ t (no lookahead).
      period_label:      Provenance string copied onto the result.
      initial_balance:   Starting USDT equity.
      funding:           `{symbol: Series|DataFrame}` of 8h settlement
                         rates.  Aligned via `align_funding_to_bars`
                         (settlement stamps floored to the hour).
                         Takes precedence over any `funding_rate`
                         column on the frames.
      universe_mask:     Bool `[timestamp × symbol]` — True = eligible
                         to hold.  ANDed with any per-frame `eligible`
                         column and with "close is finite".
      fee_taker:         Per-side taker fee (OKX perp default 0.05 %).
      fee_multiplier:    Stress multiplier on every fee (2.0 = the
                         2× fee stress of §C.3).
      slippage:          Adverse fill fraction on every execution.
      maintenance_margin_ratio / leverage / liquidation_penalty:
                         Per-leg risk model, see `liquidation_price`.
      rebalance_every:   Rebalance on bars where `i % n == 0`.
      beta_lookback:     Trailing bars used for the BTC beta hedge.
      max_gross_weight:  Σ|w| cap enforced on the strategy's weights.
      close_at_end:      Force-close the book on the final bar so exit
                         costs and trade records are complete.
      funding_rate_column: Rate column name when `funding` values are
                         DataFrames (Binance UM: `last_funding_rate`).

    Returns:
      `CrossSectionalResult` (a `BacktestResult` subclass).

    Raises:
      ValueError: unusable panel, or a strategy returning weights whose
                  Σ|w| exceeds `max_gross_weight`.
    """
    req_symbols = getattr(strategy, "symbols", None)
    (index, close, high, low, fund_col, elig_col,
     symbols) = _normalise_panel(data, req_symbols)

    n = len(index)
    if n == 0:
        raise ValueError("empty panel timeline")

    # ── Funding: explicit settlements win over an embedded column ──
    if funding is not None:
        fund_panel = align_funding_to_bars(
            funding, index, symbols, rate_column=funding_rate_column,
        )
    elif fund_col is not None:
        fund_panel = fund_col
    else:
        fund_panel = pd.DataFrame(
            0.0, index=index, columns=symbols, dtype=float)

    # ── Eligibility: explicit mask AND frame column AND finite close ──
    mask = pd.DataFrame(True, index=index, columns=symbols, dtype=bool)
    if universe_mask is not None:
        um = universe_mask.reindex(index=index, columns=symbols)
        mask &= um.fillna(False).astype(bool)
    if elig_col is not None:
        mask &= elig_col.reindex(index=index, columns=symbols).fillna(
            False).astype(bool)
    mask &= np.isfinite(close.to_numpy(dtype=float))

    close_v = close.to_numpy(dtype=float)
    high_v = (
        high.to_numpy(dtype=float) if high is not None else close_v)
    low_v = low.to_numpy(dtype=float) if low is not None else close_v
    fund_v = fund_panel.to_numpy(dtype=float)
    mask_v = mask.to_numpy(dtype=bool)
    col = {s: j for j, s in enumerate(symbols)}

    beta_hedge = bool(getattr(strategy, "beta_hedge", False))
    hedge_symbol = getattr(strategy, "hedge_symbol", DEFAULT_HEDGE_SYMBOL)
    strategy_name = getattr(strategy, "name", strategy.__class__.__name__)
    fee_rate = float(fee_taker) * float(fee_multiplier)

    equity = float(initial_balance)
    legs: dict = {}
    trades: list = []
    liquidations: list = []
    forced_closes: list = []
    weights_history: dict = {}

    eq_arr = np.zeros(n, dtype=float)
    price_arr = np.zeros(n, dtype=float)
    fund_arr = np.zeros(n, dtype=float)
    fee_arr = np.zeros(n, dtype=float)
    turn_arr = np.zeros(n, dtype=float)
    gross_arr = np.zeros(n, dtype=float)

    n_leg_opens = 0
    total_penalties = 0.0

    # ── Closing helper (shared by rebalance / liquidation / delist) ──
    def _close_leg(sym: str, ts, price: float, reason: str,
                   penalty_rate: float = 0.0) -> float:
        """Fully close `sym` at `price` (pre-slippage).  Returns the
        cash impact (fees + penalty, negative)."""
        nonlocal equity
        leg = legs.get(sym)
        if leg is None or leg.qty == 0.0:
            return 0.0
        is_long = leg.qty > 0
        fill = price * (1.0 - slippage) if is_long else price * (1.0 + slippage)
        qty_mag = abs(leg.qty)
        notional = qty_mag * fill
        fee = fee_rate * notional
        penalty = penalty_rate * notional
        realised = qty_mag * (fill - leg.entry_price) * (1.0 if is_long else -1.0)
        trades.append(CSLegTrade(
            symbol=sym,
            side="long" if is_long else "short",
            quantity=qty_mag,
            entry_price=leg.entry_price,
            exit_price=fill,
            entry_time=leg.entry_time,
            exit_time=ts,
            pnl=realised + leg.funding - fee - penalty,
            pnl_pct=(
                (realised + leg.funding - fee - penalty)
                / (qty_mag * leg.entry_price) * 100.0
                if qty_mag * leg.entry_price > 0 else 0.0
            ),
            funding=leg.funding,
            fees=fee + penalty,
            reason=reason,
        ))
        equity -= (fee + penalty)
        legs.pop(sym, None)
        return notional

    for i in range(n):
        ts = index[i]
        bar_price_pnl = 0.0
        bar_funding = 0.0
        bar_fees = 0.0
        bar_penalty = 0.0
        bar_turnover = 0.0

        # ── 1. Mark-to-market + funding accrual over (t-1, t] ──
        if i > 0 and legs:
            for sym, leg in legs.items():
                j = col[sym]
                p_now = close_v[i, j]
                p_prev = close_v[i - 1, j]
                if math.isfinite(p_now) and math.isfinite(p_prev):
                    bar_price_pnl += leg.qty * (p_now - p_prev)
                mark = p_now if math.isfinite(p_now) else leg.last_price
                rate = fund_v[i, j]
                if rate != 0.0 and math.isfinite(mark):
                    # Long pays when rate > 0; short receives.
                    cash = -leg.qty * mark * rate
                    bar_funding += cash
                    leg.funding += cash
                if math.isfinite(p_now):
                    leg.last_price = p_now
        equity += bar_price_pnl + bar_funding

        # ── 2. Per-leg maintenance-margin breach → force close ──
        for sym in list(legs.keys()):
            leg = legs[sym]
            j = col[sym]
            is_long = leg.qty > 0
            adverse = low_v[i, j] if is_long else high_v[i, j]
            if not math.isfinite(adverse):
                adverse = close_v[i, j]
            if not math.isfinite(adverse):
                continue
            s_liq = liquidation_price(
                leg.entry_price, is_long, leverage, maintenance_margin_ratio)
            breached = adverse <= s_liq if is_long else adverse >= s_liq
            if not breached:
                continue
            exit_px = close_v[i, j]
            if not math.isfinite(exit_px):
                exit_px = leg.last_price
            if not math.isfinite(exit_px):
                continue
            side = "long" if is_long else "short"
            entry_px = leg.entry_price
            before = equity
            notional = _close_leg(
                sym, ts, exit_px, "liquidation",
                penalty_rate=liquidation_penalty,
            )
            charged = before - equity
            bar_fees += fee_rate * notional
            bar_penalty += liquidation_penalty * notional
            total_penalties += liquidation_penalty * notional
            bar_turnover += notional
            liquidations.append(CSLiquidationEvent(
                timestamp=ts, symbol=sym, side=side, entry_price=entry_px,
                liquidation_price=s_liq, trigger_price=float(adverse),
                exit_price=exit_px, notional=notional,
                penalty=liquidation_penalty * notional,
            ))
            logger.debug(
                f"[EngineCS] liquidation {sym} {side} @ {ts} "
                f"trigger={adverse:.6g} s_liq={s_liq:.6g} "
                f"charged={charged:.4f}"
            )

        # ── 3. Ineligible (delisted / mask False / no bar) → close ──
        for sym in list(legs.keys()):
            j = col[sym]
            if mask_v[i, j]:
                continue
            leg = legs[sym]
            px = close_v[i, j]
            if not math.isfinite(px):
                px = leg.last_price          # last available close
            if not math.isfinite(px):
                # Never had a price: drop the leg without a fill.
                legs.pop(sym, None)
                continue
            side = "long" if leg.qty > 0 else "short"
            notional = _close_leg(sym, ts, px, "ineligible")
            bar_fees += fee_rate * notional
            bar_turnover += notional
            forced_closes.append(CSForcedCloseEvent(
                timestamp=ts, symbol=sym, side=side, exit_price=px,
                notional=notional, reason="ineligible",
            ))

        # ── 4. Rebalance ──
        is_last = (i == n - 1)
        do_rebalance = (i % max(1, int(rebalance_every)) == 0)
        if do_rebalance and not (is_last and close_at_end):
            panel_upto = close.iloc[: i + 1]
            raw = strategy.target_weights(ts, panel_upto)
            targets = dict(raw) if raw else {}
            gross = sum(abs(float(w)) for w in targets.values())
            if gross > max_gross_weight + 1e-9:
                raise ValueError(
                    f"strategy target weights breach the gross cap at {ts}: "
                    f"Σ|w|={gross:.6f} > {max_gross_weight}"
                )
            # Drop ineligible / unknown names before hedging.
            targets = {
                s: float(w) for s, w in targets.items()
                if s in col and mask_v[i, col[s]] and float(w) != 0.0
            }
            if beta_hedge:
                targets = _apply_beta_hedge(
                    targets, close, i, hedge_symbol, beta_lookback,
                    eligible=(
                        hedge_symbol in col and mask_v[i, col[hedge_symbol]]
                    ),
                )
            weights_history[ts] = dict(targets)

            # Size every leg off the equity observed at decision time, so
            # the book is order-independent (sequential sizing would let
            # the first leg's fee shrink the second leg's notional).
            equity_at_decision = equity

            for sym in sorted(set(targets) | set(legs)):
                j = col[sym]
                px = close_v[i, j]
                if not math.isfinite(px):
                    continue
                w = targets.get(sym, 0.0)
                target_qty = (
                    (equity_at_decision * w) / px if px > 0 else 0.0)
                leg = legs.get(sym)
                cur_qty = leg.qty if leg is not None else 0.0
                delta = target_qty - cur_qty
                if abs(delta * px) < _MIN_TRADE_NOTIONAL:
                    continue
                fill = (
                    px * (1.0 + slippage) if delta > 0
                    else px * (1.0 - slippage)
                )
                notional = abs(delta) * fill
                fee = fee_rate * notional
                equity -= fee
                bar_fees += fee
                bar_turnover += notional

                if leg is None:
                    leg = _Leg(symbol=sym)
                    legs[sym] = leg

                if cur_qty == 0.0 or (cur_qty > 0) == (delta > 0):
                    # Open or increase — magnitude-weighted entry.
                    if cur_qty == 0.0:
                        n_leg_opens += 1
                        leg.entry_price = fill
                        leg.entry_time = ts
                        leg.funding = 0.0
                    else:
                        tot = abs(cur_qty) + abs(delta)
                        leg.entry_price = (
                            leg.entry_price * abs(cur_qty) + fill * abs(delta)
                        ) / tot
                    leg.qty = cur_qty + delta
                else:
                    # Reduce, close, or flip.
                    closed = min(abs(delta), abs(cur_qty))
                    was_long = cur_qty > 0
                    realised = (
                        closed * (fill - leg.entry_price)
                        * (1.0 if was_long else -1.0)
                    )
                    share = closed / abs(cur_qty)
                    fund_share = leg.funding * share
                    fee_share = fee * (closed / abs(delta))
                    trades.append(CSLegTrade(
                        symbol=sym,
                        side="long" if was_long else "short",
                        quantity=closed,
                        entry_price=leg.entry_price,
                        exit_price=fill,
                        entry_time=leg.entry_time,
                        exit_time=ts,
                        pnl=realised + fund_share - fee_share,
                        pnl_pct=(
                            (realised + fund_share - fee_share)
                            / (closed * leg.entry_price) * 100.0
                            if closed * leg.entry_price > 0 else 0.0
                        ),
                        funding=fund_share,
                        fees=fee_share,
                        reason="rebalance",
                    ))
                    leg.funding -= fund_share
                    new_qty = cur_qty + delta
                    if new_qty == 0.0:
                        legs.pop(sym, None)
                    elif (new_qty > 0) != was_long:
                        # Flipped side: a fresh leg opens.
                        n_leg_opens += 1
                        leg.qty = new_qty
                        leg.entry_price = fill
                        leg.entry_time = ts
                        leg.funding = 0.0
                        leg.last_price = px
                    else:
                        leg.qty = new_qty
                if sym in legs:
                    legs[sym].last_price = px

        # ── 5. End-of-run flatten ──
        if is_last and close_at_end and legs:
            for sym in list(legs.keys()):
                j = col[sym]
                px = close_v[i, j]
                if not math.isfinite(px):
                    px = legs[sym].last_price
                if not math.isfinite(px):
                    legs.pop(sym, None)
                    continue
                notional = _close_leg(sym, ts, px, "end")
                bar_fees += fee_rate * notional
                bar_turnover += notional
            weights_history[ts] = {}

        # ── 6. Record ──
        gross_notional = 0.0
        for sym, leg in legs.items():
            px = close_v[i, col[sym]]
            if not math.isfinite(px):
                px = leg.last_price
            if math.isfinite(px):
                gross_notional += abs(leg.qty) * px
        eq_arr[i] = equity
        price_arr[i] = bar_price_pnl
        fund_arr[i] = bar_funding
        fee_arr[i] = bar_fees + bar_penalty
        turn_arr[i] = bar_turnover
        gross_arr[i] = (
            gross_notional / equity if equity > 0 else float("nan"))

    equity_curve = pd.Series(eq_arr, index=index, name="equity")
    # Bar 0's return is measured against `initial_balance` (not NaN/0), so
    # `initial_balance × (1 + returns).cumprod()` reproduces the equity
    # curve exactly, bar-0 entry costs included.  Note that the CPCV
    # runners consume `equity_curve.pct_change().dropna()`, which drops
    # bar 0 — that convention is unchanged and unaffected.
    returns = equity_curve.pct_change()
    if len(returns) > 0:
        returns.iloc[0] = (
            eq_arr[0] / float(initial_balance) - 1.0
            if initial_balance != 0 else 0.0
        )
    returns = returns.fillna(0.0)
    returns.name = "return"

    metrics = _compute_metrics(
        equity_series=equity_curve,
        trade_history=trades,
        total_fees=float(fee_arr.sum()),
        symbol=f"cross-section[{len(symbols)}]",
        strategy_name=strategy_name,
        index=index,
        n_leg_opens=n_leg_opens,
        initial_balance=initial_balance,
    )

    logger.info(
        f"[EngineCS] {period_label} complete | symbols={len(symbols)} | "
        f"bars={n} | return={metrics.total_return_pct:+.2f}% | "
        f"sharpe={metrics.sharpe_ratio:.3f} | leg_opens={n_leg_opens} | "
        f"funding_pnl={fund_arr.sum():+.2f} price_pnl={price_arr.sum():+.2f} "
        f"fees={fee_arr.sum():.2f} liquidations={len(liquidations)} | "
        f"forced_closes={len(forced_closes)}"
    )

    return CrossSectionalResult(
        metrics=metrics,
        equity_curve=equity_curve,
        trade_history=trades,
        period_label=period_label,
        returns=returns,
        turnover=pd.Series(turn_arr, index=index, name="turnover"),
        funding_pnl_series=pd.Series(fund_arr, index=index, name="funding_pnl"),
        price_pnl_series=pd.Series(price_arr, index=index, name="price_pnl"),
        fee_series=pd.Series(fee_arr, index=index, name="fees"),
        gross_exposure=pd.Series(gross_arr, index=index, name="gross"),
        funding_pnl=float(fund_arr.sum()),
        price_pnl=float(price_arr.sum()),
        total_fees=float(fee_arr.sum()),
        total_penalties=float(total_penalties),
        total_turnover=float(turn_arr.sum()),
        n_trades=n_leg_opens,
        liquidation_events=liquidations,
        forced_close_events=forced_closes,
        weights_history=weights_history,
        symbols=list(symbols),
    )


# ── Beta hedge ───────────────────────────────────────────────────────────────

def _apply_beta_hedge(
    targets: dict,
    close: pd.DataFrame,
    i: int,
    hedge_symbol: str,
    lookback: int,
    *,
    eligible: bool,
) -> dict:
    """Add the beta-neutralising hedge weight in `hedge_symbol`.

    β_p = Σ_{s ≠ hedge} w_s · cov(r_s, r_h) / var(r_h) over the
    trailing `lookback` bars; the hedge leg gets `−β_p` ADDED to
    whatever weight the strategy already asked for in that symbol.

    The hedge is deliberately NOT renormalised into the Σ|w| ≤ 1 cap:
    renormalising would re-introduce exactly the market beta the hedge
    exists to remove.  The realised gross exposure is reported on
    `CrossSectionalResult.gross_exposure`.
    """
    if not eligible or hedge_symbol not in close.columns:
        return targets
    lo = max(0, i - lookback)
    window = close.iloc[lo: i + 1]
    if len(window) < 3:
        return targets
    rets = window.pct_change().iloc[1:]
    if hedge_symbol not in rets.columns:
        return targets
    rh = rets[hedge_symbol].astype(float)
    rh_valid = rh.dropna()
    if len(rh_valid) < 2:
        return targets
    var_h = float(rh_valid.var(ddof=0))
    if not math.isfinite(var_h) or var_h <= 0:
        return targets

    beta_p = 0.0
    for sym, w in targets.items():
        if sym == hedge_symbol or sym not in rets.columns:
            continue
        pair = pd.concat([rets[sym].astype(float), rh], axis=1).dropna()
        if len(pair) < 2:
            continue
        cov = float(np.cov(
            pair.iloc[:, 0].to_numpy(), pair.iloc[:, 1].to_numpy(), ddof=0,
        )[0, 1])
        beta_p += float(w) * (cov / var_h)

    out = dict(targets)
    out[hedge_symbol] = out.get(hedge_symbol, 0.0) - beta_p
    if out[hedge_symbol] == 0.0:
        out.pop(hedge_symbol, None)
    return out


# ── Metrics (same shape/formula as engine.py and engine_perp.py) ─────────────

def _infer_candle_hours(index: pd.DatetimeIndex) -> float:
    if len(index) < 2:
        return 1.0
    delta = index[1] - index[0]
    hours = delta.total_seconds() / 3600
    return max(hours, 1 / 60)


def _compute_metrics(
    equity_series: pd.Series,
    trade_history: list,
    total_fees: float,
    symbol: str,
    strategy_name: str,
    index: pd.DatetimeIndex,
    n_leg_opens: int,
    initial_balance: float,
) -> BacktestMetrics:
    """`BacktestMetrics` over the book equity curve.

    Formula parity with `backtest.engine._compute_metrics` /
    `backtest.engine_perp._compute_metrics` so that
    `equity_curve.pct_change().dropna()` feeds
    `cpcv_common._sharpe_from_returns` with the same annualisation.

    `total_trades` is the LEG-OPEN count (the cross-sectional analogue
    of an engine "trade"), which is what the CPCV per-block event
    threshold reads.
    """
    if equity_series.empty:
        return BacktestMetrics(
            total_return_pct=0.0, annualised_return_pct=0.0,
            max_drawdown_pct=0.0, sharpe_ratio=0.0, calmar_ratio=0.0,
            volatility_pct=0.0, total_trades=0, win_rate_pct=0.0,
            profit_factor=0.0, avg_win_pct=0.0, avg_loss_pct=0.0,
            avg_trade_pct=0.0, best_trade_pct=0.0, worst_trade_pct=0.0,
            total_fees_usdt=0.0,
            start_date=str(index[0])[:10] if len(index) else "N/A",
            end_date=str(index[-1])[:10] if len(index) else "N/A",
            n_candles=0, symbol=symbol, strategy_name=strategy_name,
        )

    start_equity = float(initial_balance)
    end_equity = float(equity_series.iloc[-1])
    total_return = (end_equity - start_equity) / start_equity * 100

    n_candles = len(equity_series)
    candle_duration_h = _infer_candle_hours(index)
    years = (n_candles * candle_duration_h) / (365.25 * 24)
    if years > 0 and end_equity > 0:
        ann_return = ((end_equity / start_equity) ** (1 / years) - 1) * 100
    else:
        ann_return = 0.0

    running_max = equity_series.cummax()
    drawdown = (equity_series - running_max) / running_max * 100
    max_drawdown = float(drawdown.min())

    returns = equity_series.pct_change().dropna()
    candles_per_year = (365.25 * 24) / candle_duration_h
    vol = float(returns.std() * math.sqrt(candles_per_year)) * 100
    sharpe = ann_return / vol if vol > 0 else 0.0
    calmar = ann_return / abs(max_drawdown) if max_drawdown != 0 else 0.0

    if len(trade_history) == 0:
        return BacktestMetrics(
            total_return_pct=round(total_return, 3),
            annualised_return_pct=round(ann_return, 3),
            max_drawdown_pct=round(abs(max_drawdown), 3),
            sharpe_ratio=round(sharpe, 4),
            calmar_ratio=round(calmar, 4),
            volatility_pct=round(vol, 3),
            total_trades=int(n_leg_opens),
            win_rate_pct=0.0, profit_factor=0.0, avg_win_pct=0.0,
            avg_loss_pct=0.0, avg_trade_pct=0.0, best_trade_pct=0.0,
            worst_trade_pct=0.0,
            total_fees_usdt=round(float(total_fees), 4),
            start_date=str(index[0])[:10],
            end_date=str(index[-1])[:10],
            n_candles=n_candles, symbol=symbol,
            strategy_name=strategy_name,
        )

    wins = [t for t in trade_history if t.pnl > 0]
    losses = [t for t in trade_history if t.pnl <= 0]
    win_rate = len(wins) / len(trade_history) * 100
    gross_profit = sum(t.pnl for t in wins)
    gross_loss = abs(sum(t.pnl for t in losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")
    pnl_pcts = [t.pnl_pct for t in trade_history]

    return BacktestMetrics(
        total_return_pct=round(total_return, 3),
        annualised_return_pct=round(ann_return, 3),
        max_drawdown_pct=round(abs(max_drawdown), 3),
        sharpe_ratio=round(sharpe, 4),
        calmar_ratio=round(calmar, 4),
        volatility_pct=round(vol, 3),
        total_trades=int(n_leg_opens),
        win_rate_pct=round(win_rate, 2),
        profit_factor=(
            round(profit_factor, 4) if profit_factor != float("inf") else 9999.0
        ),
        avg_win_pct=round(
            float(np.mean([t.pnl_pct for t in wins])) if wins else 0.0, 3),
        avg_loss_pct=round(
            float(np.mean([t.pnl_pct for t in losses])) if losses else 0.0, 3),
        avg_trade_pct=round(float(np.mean(pnl_pcts)), 3),
        best_trade_pct=round(float(max(pnl_pcts)), 3),
        worst_trade_pct=round(float(min(pnl_pcts)), 3),
        total_fees_usdt=round(float(total_fees), 4),
        start_date=str(index[0])[:10],
        end_date=str(index[-1])[:10],
        n_candles=n_candles, symbol=symbol, strategy_name=strategy_name,
    )


__all__ = [
    "FEE_TAKER_PERP",
    "SLIPPAGE",
    "DEFAULT_MAINTENANCE_MARGIN_RATIO",
    "DEFAULT_LEVERAGE",
    "LIQUIDATION_PENALTY",
    "DEFAULT_INITIAL_BALANCE",
    "CSLegTrade",
    "CSLiquidationEvent",
    "CSForcedCloseEvent",
    "CrossSectionalResult",
    "align_funding_to_bars",
    "liquidation_price",
    "run_engine_cs",
]
