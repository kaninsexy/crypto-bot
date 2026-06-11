"""
backtest/per_bar_store.py — Per-bar return series persistence (gate spec v2).

The 2026-06 gate-recalibration audit found that no per-bar strategy or
benchmark return series was ever persisted, making the S1 alpha/IR
gate and any holdout bootstrap impossible to evaluate retroactively
(docs/gate_recalibration_audit_2026-06.md §2 "UNRECOVERABLE", §6).
This module closes that gap: every trial run persists its per-bar
strategy returns (and the aligned benchmark series when available) to

    backtest/reports/per_bar_returns/<trial_id>.parquet

keyed by the trials.log trial_id so audits join the two losslessly.

Wiring is at the API layer, not per-script: `backtest.trials.
record_trial` accepts the series as optional keyword arguments and
calls `persist_per_bar_returns` after the row append succeeds, so any
caller that has the series persists it with zero extra logic.
runner.py (dev_cpcv + final_gate) and the trial-script template
pattern pass them.

Persistence failure policy: a failed parquet write must not invalidate
an already-recorded trial row — the row is the primary record.  We
warn and return None instead of raising.
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

# Module attribute so tests can monkeypatch the destination.
_PER_BAR_DIR: Path = Path("backtest/reports/per_bar_returns")


def persist_per_bar_returns(
    trial_id: str,
    strategy_returns: np.ndarray,
    benchmark_returns: Optional[np.ndarray] = None,
    index: Optional[pd.DatetimeIndex] = None,
    meta: Optional[dict] = None,
) -> Optional[Path]:
    """Write one trial's per-bar return series to parquet.

    Args:
      trial_id:          trials.log trial_id (uuid hex) — the filename
                         and join key.
      strategy_returns:  Per-bar strategy return series (the same
                         array fed to DSR/MinTRL/verdict).
      benchmark_returns: Optional per-bar benchmark (B&H) series.
                         When its length differs from the strategy
                         series (engine warmup trim, CPCV purge gaps)
                         it is TAIL-aligned and NaN-padded at the head
                         so both columns share one frame; consumers
                         drop NaN rows for aligned analysis.
      index:             Optional DatetimeIndex of the same length as
                         strategy_returns.  When omitted (e.g. CPCV
                         block concat, which has gaps), a RangeIndex
                         is used and the series is positional-only.
      meta:              Optional small dict stored as stringified
                         frame attrs (strategy_id, window, bpy, ...).

    Returns:
      The written Path, or None when the write failed (warned) or the
      strategy series is empty.
    """
    rs = np.asarray(strategy_returns, dtype=float)
    if rs.size == 0:
        warnings.warn(
            f"[per_bar_store] empty strategy_returns for trial "
            f"{trial_id!r}; nothing persisted",
            stacklevel=2,
        )
        return None

    n = rs.size
    cols: dict = {"strategy_return": rs}
    if benchmark_returns is not None:
        rb = np.asarray(benchmark_returns, dtype=float)
        if rb.size >= n:
            cols["benchmark_return"] = rb[-n:]
        else:
            pad = np.full(n - rb.size, np.nan)
            cols["benchmark_return"] = np.concatenate([pad, rb])

    if index is not None and len(index) == n:
        idx = index
    else:
        if index is not None:
            warnings.warn(
                f"[per_bar_store] index length {len(index)} != "
                f"strategy_returns length {n} for trial {trial_id!r}; "
                "falling back to RangeIndex",
                stacklevel=2,
            )
        idx = pd.RangeIndex(n)

    df = pd.DataFrame(cols, index=idx)
    if meta:
        df.attrs.update({str(k): str(v) for k, v in meta.items()})

    try:
        _PER_BAR_DIR.mkdir(parents=True, exist_ok=True)
        path = _PER_BAR_DIR / f"{trial_id}.parquet"
        df.to_parquet(path)
        return path
    except Exception as exc:  # noqa: BLE001 — persistence must not
        # invalidate the already-recorded trial row.
        warnings.warn(
            f"[per_bar_store] failed to persist per-bar returns for "
            f"trial {trial_id!r}: {exc.__class__.__name__}: {exc}",
            stacklevel=2,
        )
        return None


def load_per_bar_returns(trial_id: str) -> pd.DataFrame:
    """Read back one trial's per-bar frame.  Raises FileNotFoundError
    when the trial predates per-bar persistence (pre-2026-06-11 rows
    are unrecoverable per the audit)."""
    path = _PER_BAR_DIR / f"{trial_id}.parquet"
    if not path.exists():
        raise FileNotFoundError(
            f"no per-bar return series persisted for trial {trial_id!r} "
            f"({path}); trials recorded before 2026-06-11 predate "
            "per-bar persistence"
        )
    return pd.read_parquet(path)


__all__ = ["persist_per_bar_returns", "load_per_bar_returns"]
