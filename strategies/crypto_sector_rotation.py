"""strategies/crypto_sector_rotation.py -- Phase 4 sq-029 strategy.

Long-only sector-rotation strategy on a fixed crypto universe at 1D.
Hypothesis: a portfolio that rotates each rebalance bar into the
single top-performing predefined 'sector' of cryptocurrencies (e.g.
Layer-1s, DeFi, Payment) earns a positive risk-adjusted return,
because the cross-sectional momentum effect documented at the
single-asset level (Drogen et al. 2023) aggregates up to the
sector level when the within-sector dispersion is dominated by a
common sector factor.

Sector definitions (closed universe, 11 spot symbols):

  L1_SmartContract     ETH, SOL, BNB, AVAX, ADA, DOT  (n=6)
  Payment_StoreOfValue BTC, XRP, LTC                  (n=3)
  DeFi_Oracle          UNI, LINK                      (n=2)

These groupings reflect the canonical 'crypto sector' partition
used by FTSE-Russell, Coingecko categories and the Hoffstein /
Drogen (2023) basket-construction methodology -- L1 platforms,
payment / store-of-value cohorts, and infrastructure / DeFi
tokens form the three highest-weight sectors in the
crypto-equity literature.

Algorithm per bar (daily):

  1. On each rebalance bar (every `holding_period` bars), for every
     sector compute the equal-weight average prior `lookback_period`
     return across its constituent symbols:

         sector_ret[s] = mean_{sym in s}( close[t]/close[t-L] - 1 )

  2. Rank sectors by sector_ret DESCENDING.  Pick the top sector as
     the target.
  3. Long-only: BUY every symbol in the target sector that we do
     not already hold.  SELL every held symbol that is no longer
     in the target sector (rotation closes losing-sector legs and
     opens winning-sector legs on the same bar).
  4. Equal weight WITHIN the target sector: position_fraction =
     1 / size(target sector), so a 6-coin sector deploys ~1/6 of
     equity per leg, a 2-coin sector ~1/2 per leg, and the
     full target sector aggregates to ~100% notional.
  5. Between rebalance bars: every symbol receives HOLD so existing
     legs are kept for the full holding_period.

Long-only by design: Han, Kang & Ryu (2024) report the
cryptocurrency cross-sectional momentum effect is concentrated
in winners; loser portfolios rebound and inflict losses on
shorts.  Grobys (2025) further documents severe momentum
crashes that asymmetrically punish short legs.  We therefore
take only the top sector long and never short the bottom
sector.

Contract with backtest.engine_multi (mirrors
strategies.cross_sectional_momentum.CrossSectionalMomentumStrategy):

  * `symbols`   -- full list of universe pairs.
  * `lookback_days` -- engine_multi.min_history_bars =
                       lookback_days + 2; we set this to
                       lookback_period + 1 so the engine waits
                       until at least one well-defined return
                       per symbol exists before invoking the
                       strategy.
  * `position_fraction(df, n_active)` -- equal weight within the
                       current target sector; returns
                       1 / target_sector_size when a target is
                       active, 0 during warmup or when no target
                       is set.
  * `generate_signals(prices)` -- dict[symbol, Signal] for the bar.

Citations:
- Drogen, L.; Hoffstein, C.; Otte, K. (2023). "Cross-sectional
  Momentum in Cryptocurrency Markets." SSRN.  Long-only
  best-performing-asset momentum with a 30-day lookback and
  7-day hold delivered excess returns vs BTC over 2018-2022.
- Han, C.; Kang, B.; Ryu, J. (2024). "Time-Series and
  Cross-Sectional Momentum in the Cryptocurrency Market."
  SSRN.  Momentum is concentrated in winners; long-only beats
  long-short.
- Grobys, K. (2025). "Cryptocurrency momentum has (not) its
  moments." Financial Markets and Portfolio Management.
  Crypto momentum profits are subject to severe crashes;
  the effect concentrates in large-caps and a single outlier
  can dominate the cross-section.
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np
import pandas as pd

from strategies.base import Signal


# CITATION: crypto-sector-rotation-literature
# Sector partition over the 11-symbol Phase 4 daily universe.  L1
# platforms, payment / store-of-value, and DeFi / oracle infra are
# the three highest-weight cuts of the Hoffstein / Drogen (2023)
# basket-construction methodology and the FTSE-Russell crypto
# taxonomy.  Closed universe -- every symbol the strategy is asked
# to trade must appear in exactly one sector.
DEFAULT_SECTORS: dict[str, list[str]] = {
    "L1_SmartContract": [
        "ETH/USDT", "SOL/USDT", "BNB/USDT",
        "AVAX/USDT", "ADA/USDT", "DOT/USDT",
    ],
    "Payment_StoreOfValue": [
        "BTC/USDT", "XRP/USDT", "LTC/USDT",
    ],
    "DeFi_Oracle": [
        "UNI/USDT", "LINK/USDT",
    ],
}


class CryptoSectorRotationStrategy:
    """Long-only sector-rotation strategy.

    Self-contained: callers supply `dict[symbol, DataFrame]` of OHLCV
    slices ending at the current bar via generate_signals; the
    strategy returns `dict[symbol, Signal]`.  Engine_multi consumes
    `position_fraction` to size BUYs equal-weight within the current
    target sector.
    """

    def __init__(
        self,
        symbols: list[str],
        timeframe: str = "1d",
        # CITATION: crypto-sector-rotation-literature
        # Drogen et al. (2023) §3 use a 30-day prior-return lookback
        # for the long-only winner-momentum basket on daily crypto
        # bars.  Han et al. (2024) confirm 30-day windows fall within
        # the formation horizon over which winner persistence holds.
        lookback_period: int = 30,
        # CITATION: crypto-sector-rotation-literature
        # Drogen et al. (2023) hold the formed winner portfolio for
        # 7 days before re-ranking.  Holding the full window avoids
        # over-rotation and lets the documented sector persistence
        # play out before the next sector ranking.
        holding_period: int = 7,
        # CITATION: crypto-sector-rotation-literature
        # Top-1 sector capture: this rotates into the single
        # best-performing sector each rebalance.  The Drogen et al.
        # (2023) winner-tail pattern is the long-only top-quintile
        # of single-asset returns; on the sector level the analogue
        # is top-1 of three sectors (= top quintile when sectors
        # have similar weight).
        top_n_sectors: int = 1,
        # CITATION: crypto-sector-rotation-literature
        # Engine default initial_balance for the Phase 4 backtest harness.
        notional_capital: float = 10_000.0,
        sectors: Optional[dict[str, list[str]]] = None,
        name: str = "CryptoSectorRotation",
    ):
        if not symbols or len(symbols) < 2:
            raise ValueError("symbols must contain at least 2 entries")
        if lookback_period < 1:
            raise ValueError("lookback_period must be >= 1")
        if holding_period < 1:
            raise ValueError("holding_period must be >= 1")
        if top_n_sectors < 1:
            raise ValueError("top_n_sectors must be >= 1")

        self.name = str(name)
        self.symbols: list[str] = list(symbols)
        self.timeframe = str(timeframe)
        self.lookback_period = int(lookback_period)
        self.holding_period = int(holding_period)
        self.top_n_sectors = int(top_n_sectors)
        self.notional_capital = float(notional_capital)

        # Filter the supplied sector partition to the universe at
        # hand.  Drop any sector that ends up empty.  Reject the
        # strategy if any universe symbol is unmapped or if the
        # filtered partition has too few sectors to rank.
        raw_sectors = sectors if sectors is not None else DEFAULT_SECTORS
        universe = set(self.symbols)
        filtered: dict[str, list[str]] = {}
        for sector_name, sector_symbols in raw_sectors.items():
            present = [s for s in sector_symbols if s in universe]
            if present:
                filtered[sector_name] = present
        unmapped = sorted(
            universe - {s for syms in filtered.values() for s in syms}
        )
        if unmapped:
            raise ValueError(
                f"Symbols not assigned to any sector: {unmapped!r}. "
                f"Add them to a sector partition or remove from the "
                f"strategy universe."
            )
        if len(filtered) < 2:
            raise ValueError(
                f"At least 2 non-empty sectors required for ranking; "
                f"got {len(filtered)}."
            )
        if self.top_n_sectors > len(filtered):
            raise ValueError(
                f"top_n_sectors={self.top_n_sectors} > "
                f"n_sectors={len(filtered)} after universe filtering."
            )
        # Reject overlap: a symbol must appear in exactly one sector.
        seen: set[str] = set()
        for sector_name, sector_symbols in filtered.items():
            for sym in sector_symbols:
                if sym in seen:
                    raise ValueError(
                        f"Symbol {sym!r} appears in more than one sector."
                    )
                seen.add(sym)
        self.sectors: dict[str, list[str]] = filtered
        self._sector_of: dict[str, str] = {
            sym: name for name, syms in filtered.items() for sym in syms
        }

        # engine_multi.min_history_bars = strategy.lookback_days + 2.
        # We need lookback_period + 1 closes per symbol to form one
        # well-defined return; pad via lookback_days so the engine
        # waits for at least one full return per symbol before the
        # first signal call.
        self.lookback_days: int = self.lookback_period + 1

        # Rebalance scheduler -- counter increments per generate_signals
        # call; first call after engine warmup is treated as a rebalance.
        self._held: set[str] = set()
        self._bars_since_rebalance: int = 0
        self._first_signal: bool = True

        # Tracks the current target sector membership so
        # position_fraction can size each new long as 1 / sector_size.
        # None during warmup / no-rebalance bars.
        self._current_target_size: Optional[int] = None

    # -- Engine sizing hook ---------------------------------------------------

    def position_fraction(
        self,
        df: pd.DataFrame,
        n_active: int,
        max_concentration_mult: float = 2.0,
    ) -> float:
        """Equal weight within the current target sector.

        Returns 1 / target_sector_size when a target sector is
        active.  Engine_multi only invokes position_fraction on
        symbols receiving BUY this bar; the strategy only emits BUY
        for the target sector's members, so 1/size correctly sizes
        each new leg as one-Nth of equity (full sector allocation
        when all legs fill).
        """
        if self._current_target_size is None or self._current_target_size <= 0:
            return 0.0
        return float(1.0 / float(self._current_target_size))

    # -- Signal generation ----------------------------------------------------

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        """Single-symbol entry point.

        engine_multi is the canonical caller and uses
        generate_signals (plural).  This method is provided for
        BaseStrategy-interface compatibility checks; for a basket
        strategy a single-symbol view cannot rank sectors, so it
        always returns HOLD with a not-applicable reason.
        """
        if df is None or len(df) == 0:
            price = 0.0
        else:
            price = float(df["close"].iloc[-1])
        return Signal(
            action="HOLD", strategy=self.name, price=price,
            reason=(
                "single-symbol view not applicable to sector rotation; "
                "use generate_signals via engine_multi"
            ),
        )

    def generate_signals(
        self,
        prices: dict[str, pd.DataFrame],
    ) -> dict[str, Signal]:
        """Map of symbol -> Signal for the current bar."""
        prices_now: dict[str, float] = {
            sym: self._latest_close(prices.get(sym)) for sym in self.symbols
        }

        # Decide whether this bar is a rebalance bar.
        if self._first_signal:
            self._first_signal = False
            self._bars_since_rebalance = 0
            is_rebalance = True
        else:
            self._bars_since_rebalance += 1
            if self._bars_since_rebalance >= self.holding_period:
                self._bars_since_rebalance = 0
                is_rebalance = True
            else:
                is_rebalance = False

        if not is_rebalance:
            # Mid-holding bar: HOLD every symbol so engine preserves
            # existing legs through the holding period.
            out: dict[str, Signal] = {}
            for sym in self.symbols:
                in_held = sym in self._held
                reason = (
                    f"holding | bars_since_rebalance="
                    f"{self._bars_since_rebalance}/{self.holding_period}"
                ) if in_held else (
                    f"flat | bars_since_rebalance="
                    f"{self._bars_since_rebalance}/{self.holding_period}"
                )
                out[sym] = Signal(
                    action="HOLD", strategy=self.name,
                    price=prices_now.get(sym, 0.0),
                    reason=reason,
                )
            return out

        # Rebalance path -------------------------------------------------------
        # 1. Per-symbol prior return.
        prior_returns: dict[str, float] = {}
        for sym in self.symbols:
            df = prices.get(sym)
            if df is None or len(df) < self.lookback_period + 1:
                continue
            close = df["close"].astype(float).to_numpy()
            window = close[-(self.lookback_period + 1):]
            if np.any(window <= 0) or not np.all(np.isfinite(window)):
                continue
            prior_close = float(window[0])
            current_close = float(window[-1])
            if prior_close <= 0:
                continue
            ret = (current_close / prior_close) - 1.0
            if math.isfinite(ret):
                prior_returns[sym] = ret

        # 2. Per-sector mean return (sectors with no scored member skipped).
        sector_returns: dict[str, float] = {}
        for sector_name, sector_symbols in self.sectors.items():
            scored = [
                prior_returns[s] for s in sector_symbols
                if s in prior_returns
            ]
            if scored:
                sector_returns[sector_name] = float(np.mean(scored))

        out_rb: dict[str, Signal] = {}

        # 3. If we cannot rank yet (need at least top_n_sectors scored
        # sectors), HOLD everything and keep current_target_size cleared.
        if len(sector_returns) < self.top_n_sectors:
            self._current_target_size = None
            for sym in self.symbols:
                out_rb[sym] = Signal(
                    action="HOLD", strategy=self.name,
                    price=prices_now.get(sym, 0.0),
                    reason=(
                        f"warmup | scored_sectors={len(sector_returns)} < "
                        f"top_n_sectors={self.top_n_sectors}"
                    ),
                )
            return out_rb

        # 4. Rank sectors DESCENDING; build the target symbol set as
        # the union of the top top_n_sectors sectors.  Default
        # top_n_sectors=1 makes this exactly the single-best sector;
        # n>1 supports a future variation widening the long basket.
        ranked = sorted(
            sector_returns.items(), key=lambda kv: kv[1], reverse=True,
        )
        target_sectors: list[str] = [
            name for name, _ in ranked[: self.top_n_sectors]
        ]
        target_symbols: list[str] = []
        for sname in target_sectors:
            target_symbols.extend(self.sectors[sname])
        target_set: set[str] = set(target_symbols)
        # Equal weight within the target basket: 1/N where N = total
        # symbols across all top_n_sectors target sectors.
        self._current_target_size = (
            len(target_symbols) if target_symbols else None
        )

        target_label = "+".join(target_sectors)
        target_return_str = " | ".join(
            f"{name}={sector_returns[name] * 100:+.2f}%"
            for name, _ in ranked
        )

        # 5. Emit signals.
        for sym in self.symbols:
            price = prices_now.get(sym, 0.0)
            sym_sector = self._sector_of.get(sym, "?")
            sym_score = prior_returns.get(sym)
            score_str = (
                f"prior_ret={sym_score * 100:+.2f}%"
                if sym_score is not None else "prior_ret=NA"
            )

            in_target = sym in target_set
            in_held = sym in self._held

            if in_target and not in_held:
                self._held.add(sym)
                out_rb[sym] = Signal(
                    action="BUY", strategy=self.name, price=price,
                    reason=(
                        f"enter-target-sector | sector={sym_sector} | "
                        f"target={target_label} | {score_str}"
                    ),
                    order_type="market",
                )
            elif (not in_target) and in_held:
                self._held.discard(sym)
                out_rb[sym] = Signal(
                    action="SELL", strategy=self.name, price=price,
                    reason=(
                        f"exit-rotation | sector={sym_sector} | "
                        f"new_target={target_label} | {score_str}"
                    ),
                    order_type="market",
                )
            else:
                if in_held:
                    reason = (
                        f"holding-target | sector={sym_sector} | "
                        f"target={target_label} | {score_str}"
                    )
                else:
                    reason = (
                        f"flat-non-target | sector={sym_sector} | "
                        f"target={target_label} | {score_str}"
                    )
                out_rb[sym] = Signal(
                    action="HOLD", strategy=self.name, price=price,
                    reason=reason,
                )

        # Annotate the metadata of one signal with the per-sector
        # ranks so log readers can reconstruct the rebalance decision.
        # Using the first symbol keeps the field structure identical
        # across symbols and avoids inflating every Signal's payload.
        first_sym = self.symbols[0]
        if first_sym in out_rb:
            out_rb[first_sym].metadata = {
                **(out_rb[first_sym].metadata or {}),
                "sector_ranking": target_return_str,
                "target_sectors": list(target_sectors),
                "target_size": self._current_target_size,
            }

        return out_rb

    # -- Helpers --------------------------------------------------------------

    @staticmethod
    def _latest_close(df: Optional[pd.DataFrame]) -> float:
        if df is None or len(df) == 0:
            return 0.0
        return float(df["close"].iloc[-1])
