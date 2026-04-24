"""
backtest/holdout.py — Holdout data accessor (part of the sacred harness).

Owns the schema for backtest/holdout_access.log and enforces the
single-access invariant: each strategy's holdout window may be read
exactly once per split epoch (one access since the most recent
regenerated=true event for that strategy, or one access ever if never
regenerated).

Modifying this file requires human approval per CLAUDE.md.

Caller string convention
─────────────────────────
load_holdout requires a structured `caller` argument:

    <phase>.<strategy_id>.<purpose>

  phase        one of: phase3c  phase3d  phase4  phase5  manual
  strategy_id  must match a manifest key exactly (case-sensitive)
  purpose      one of: final_dsr  regression_check  manual_inspection

Valid examples:
  phase3c.VWAP.final_dsr
  phase4.BearShort.regression_check
  manual.GridTrading.manual_inspection

Invalid examples (raise InvalidCallerFormat):
  VWAP.final_dsr                 — missing phase segment
  phase99.VWAP.final_dsr         — unknown phase
  phase3c.VWAP.random_poke       — unknown purpose

After regex shape-validation passes, the strategy_id segment is checked
against the manifest.  A well-formed caller whose strategy_id is absent
from the manifest raises StrategyNotInManifest (not InvalidCallerFormat).

Access log schema (backtest/holdout_access.log)
────────────────────────────────────────────────
Normal access event written by load_holdout:
  {
    "ts":          ISO-8601 UTC string,
    "strategy_id": str,
    "caller":      str,
    "reason":      str,
    "git_commit":  str  (short SHA, "unknown" if git unavailable),
    "n_rows":      int,
    "regenerated": false
  }

Regeneration event written by generate_holdout_manifest.regenerate_manifest:
  {
    "ts":                ISO-8601 UTC string,
    "strategy_id":       str,
    "regenerated":       true,
    "old_holdout_start": str,
    "new_holdout_start": str
  }

Single-access invariant
────────────────────────
_has_prior_access scans the access log for the strategy in file order
(chronological, since the log is append-only).  A regenerated=true event
resets the "has been accessed" flag for that strategy.  Normal access
events set it.  The flag's final value determines whether load_holdout
may proceed.
"""

import functools
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from backtest.logs import append_jsonl, iter_jsonl_filtered


# ── Module-level path configuration ──────────────────────────────────────────
# Override these in tests via monkeypatch.setattr; call _reset_manifest_cache()
# after changing _MANIFEST_PATH so the lru_cache reflects the new file.

_MANIFEST_PATH: Path = Path("backtest/holdout_manifest.json")
_ACCESS_LOG_PATH: Path = Path("backtest/holdout_access.log")
_CACHE_DIR: Path = Path("backtest/cache/ohlcv")


# ── Exceptions ────────────────────────────────────────────────────────────────

class ManifestNotFound(FileNotFoundError):
    """Manifest file is absent."""

class ManifestSchemaError(ValueError):
    """Manifest JSON is malformed or missing required fields."""

class StrategyNotInManifest(KeyError):
    """strategy_id is not a key in the manifest."""

class HoldoutAlreadyAccessed(RuntimeError):
    """Holdout has already been accessed for this strategy since the last
    regenerated=true event (or ever, if never regenerated)."""

class InvalidCallerFormat(ValueError):
    """caller string does not match the structured convention."""


# ── Caller validation ─────────────────────────────────────────────────────────

_CALLER_RE = re.compile(
    r"^(phase3c|phase3d|phase4|phase5|manual)"
    r"\.([A-Za-z][A-Za-z0-9_]*)"
    r"\.(final_dsr|regression_check|manual_inspection)$"
)


# ── Manifest ──────────────────────────────────────────────────────────────────

_MANIFEST_REQUIRED_FIELDS: frozenset[str] = frozenset({
    "timeframe", "data_start", "data_end", "dev_end", "holdout_start",
})


@functools.lru_cache(maxsize=1)
def load_manifest() -> dict:
    """Return the holdout manifest dict.  Cached after first load.

    Raises ManifestNotFound if the file is absent.
    Raises ManifestSchemaError if JSON is malformed or entries are
    missing required fields.
    """
    path = _MANIFEST_PATH
    if not path.exists():
        raise ManifestNotFound(f"Holdout manifest not found: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ManifestSchemaError(f"Manifest JSON parse error: {exc}") from exc
    if not isinstance(raw, dict):
        raise ManifestSchemaError("Manifest root must be a JSON object.")
    for sid, entry in raw.items():
        if not isinstance(entry, dict):
            raise ManifestSchemaError(
                f"Manifest entry for '{sid}' must be a dict."
            )
        if "symbol" not in entry and "symbols" not in entry:
            raise ManifestSchemaError(
                f"Manifest entry for '{sid}' must have 'symbol' or 'symbols'."
            )
        missing = _MANIFEST_REQUIRED_FIELDS - entry.keys()
        if missing:
            raise ManifestSchemaError(
                f"Manifest entry for '{sid}' missing required fields: {sorted(missing)}"
            )
    return raw


def _reset_manifest_cache() -> None:
    """Clear the lru_cache on load_manifest.  Call after patching _MANIFEST_PATH."""
    load_manifest.cache_clear()


# ── OHLCV loading ─────────────────────────────────────────────────────────────

def _get_symbols(entry: dict) -> list[str]:
    if "symbols" in entry:
        return list(entry["symbols"])
    return [entry["symbol"]]


_MONTHS_RE = re.compile(r"_(\d+)mo\.parquet$")


def _parse_months(path: Path) -> int:
    """Extract the month count from a cache filename ending in _{N}mo.parquet.

    Raises ValueError if the filename does not match the expected convention.
    """
    m = _MONTHS_RE.search(path.name)
    if m is None:
        raise ValueError(
            f"Cache file '{path.name}' does not match the expected "
            "naming convention '{{symbol}}_{{timeframe}}_{{N}}mo.parquet'."
        )
    return int(m.group(1))


def _load_symbol_df(symbol: str, timeframe: str) -> pd.DataFrame:
    """Load OHLCV DataFrame for (symbol, timeframe) from the L1 parquet cache.

    When multiple files match (e.g. 12mo and 36mo), picks the one with the
    highest month count so the broadest date range is used.

    Raises FileNotFoundError if no matching file exists.
    Raises ValueError if a matched filename has no parseable {N}mo suffix.
    """
    prefix = symbol.replace("/", "-")
    candidates = list(_CACHE_DIR.glob(f"{prefix}_{timeframe}_*.parquet"))
    if not candidates:
        raise FileNotFoundError(
            f"No cache file for {symbol} {timeframe} in {_CACHE_DIR}"
        )
    best = max(candidates, key=_parse_months)
    return pd.read_parquet(best)


def _build_df(symbols: list[str], timeframe: str, after_ts: pd.Timestamp | None,
              before_ts: pd.Timestamp | None) -> pd.DataFrame:
    """Load and filter OHLCV for one or more symbols.

    Returns a DataFrame with a 'symbol' column always present.
    For a single symbol the column is a constant.  For multi-symbol the
    rows are concatenated and sorted by timestamp.
    """
    frames = []
    for sym in symbols:
        part = _load_symbol_df(sym, timeframe)
        if after_ts is not None:
            part = part[part.index >= after_ts]
        if before_ts is not None:
            part = part[part.index < before_ts]
        part = part.copy()
        part["symbol"] = sym
        frames.append(part)

    if len(frames) == 1:
        return frames[0]

    combined = pd.concat(frames).sort_index()
    return combined


# ── Public accessors ──────────────────────────────────────────────────────────

def load_dev(strategy_id: str) -> pd.DataFrame:
    """Return OHLCV rows in [data_start, holdout_start) for strategy_id.

    Not audited — may be called freely during iteration.

    Returns a DataFrame with columns open/high/low/close/volume/symbol.
    Single-symbol and multi-symbol strategies return the same shape.

    Raises StrategyNotInManifest if strategy_id is absent from the manifest.
    """
    manifest = load_manifest()
    if strategy_id not in manifest:
        raise StrategyNotInManifest(f"'{strategy_id}' not found in holdout manifest.")
    entry = manifest[strategy_id]
    holdout_start = pd.Timestamp(entry["holdout_start"])
    return _build_df(
        _get_symbols(entry),
        entry["timeframe"],
        after_ts=None,
        before_ts=holdout_start,
    )


def _get_git_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _has_prior_access(strategy_id: str) -> bool:
    """Return True if strategy has an uncleared access in the log.

    Scans events in file order (chronological).  A regenerated=true event
    clears the flag; a normal access event sets it.
    """
    has_access = False
    for event in iter_jsonl_filtered(
        _ACCESS_LOG_PATH,
        lambda e: e.get("strategy_id") == strategy_id,
    ):
        if event.get("regenerated") is True:
            has_access = False
        else:
            has_access = True
    return has_access


def load_holdout(
    strategy_id: str,
    *,
    caller: str,
    reason: str,
) -> pd.DataFrame:
    """Return OHLCV rows in [holdout_start, data_end) for strategy_id.

    Appends exactly one event to backtest/holdout_access.log on success.

    Raises:
        InvalidCallerFormat    — caller string does not match convention
        StrategyNotInManifest  — caller format valid but strategy_id absent
        HoldoutAlreadyAccessed — strategy already accessed since last regen
    """
    # 1. Validate caller format — before any audit event or data access.
    if _CALLER_RE.match(caller) is None:
        raise InvalidCallerFormat(
            f"caller {caller!r} does not match <phase>.<strategy_id>.<purpose>. "
            "Valid phases: phase3c phase3d phase4 phase5 manual. "
            "Valid purposes: final_dsr regression_check manual_inspection."
        )

    # 2. Confirm strategy exists in manifest.
    manifest = load_manifest()
    if strategy_id not in manifest:
        raise StrategyNotInManifest(
            f"'{strategy_id}' not found in holdout manifest."
        )

    # 3. Enforce single-access invariant.
    if _has_prior_access(strategy_id):
        raise HoldoutAlreadyAccessed(
            f"Holdout for '{strategy_id}' has already been accessed. "
            "Call regenerate_manifest() to reset the access window."
        )

    # 4. Load and filter data.
    entry = manifest[strategy_id]
    holdout_start = pd.Timestamp(entry["holdout_start"])
    result = _build_df(
        _get_symbols(entry),
        entry["timeframe"],
        after_ts=holdout_start,
        before_ts=None,
    )

    # 5. Append audit event — after data is loaded so n_rows is known.
    event = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "strategy_id": strategy_id,
        "caller": caller,
        "reason": reason,
        "git_commit": _get_git_commit(),
        "n_rows": len(result),
        "regenerated": False,
    }
    append_jsonl(_ACCESS_LOG_PATH, event)

    return result
