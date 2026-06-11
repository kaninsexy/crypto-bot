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

from backtest.cache import _holdout_bypass_ctx
from backtest.logs import append_jsonl, iter_jsonl_filtered


# ── Module-level path configuration ──────────────────────────────────────────
# Override these in tests via monkeypatch.setattr; call _reset_manifest_cache()
# after changing _MANIFEST_PATH so the lru_cache reflects the new file.

_MANIFEST_PATH: Path = Path("backtest/holdout_manifest.json")
_ACCESS_LOG_PATH: Path = Path("backtest/holdout_access.log")
_CACHE_DIR: Path = Path("backtest/cache/ohlcv")
_PERP_CACHE_DIR: Path = Path("backtest/cache/perp")


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

# A manifest entry must carry exactly one of these substrate
# specifiers.  `symbol` and `symbols` are the historical single- and
# multi-symbol shapes; `legs` is the Phase 4.B two-leg shape (perp +
# spot) introduced for FundingRateHarvest_BTC.
_MANIFEST_SUBSTRATE_FIELDS: tuple[str, ...] = ("symbol", "symbols", "legs")


def _validate_legs(sid: str, legs: object) -> None:
    """Validate the shape of a `legs` field on a manifest entry.

    Schema rule: `legs` is a dict with exactly the keys 'spot' and
    'perp', each a non-empty manifest-format symbol string
    (BASE/QUOTE).  Variation #2's multi-pair basket schema is NOT in
    scope for this validator — that's a separate (future) extension.
    """
    if not isinstance(legs, dict):
        raise ManifestSchemaError(
            f"Manifest entry for '{sid}' has 'legs' that is not a dict."
        )
    expected = {"spot", "perp"}
    if set(legs.keys()) != expected:
        raise ManifestSchemaError(
            f"Manifest entry for '{sid}' 'legs' must have exactly the "
            f"keys {sorted(expected)}; got {sorted(legs.keys())}."
        )
    for leg_name, sym in legs.items():
        if not isinstance(sym, str) or "/" not in sym:
            raise ManifestSchemaError(
                f"Manifest entry for '{sid}' 'legs.{leg_name}' must be "
                f"a 'BASE/QUOTE' string; got {sym!r}."
            )


@functools.lru_cache(maxsize=1)
def load_manifest() -> dict:
    """Return the holdout manifest dict.  Cached after first load.

    Raises ManifestNotFound if the file is absent.
    Raises ManifestSchemaError if JSON is malformed, entries are
    missing required fields, or substrate specifiers are malformed.
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
        present = [f for f in _MANIFEST_SUBSTRATE_FIELDS if f in entry]
        if len(present) == 0:
            raise ManifestSchemaError(
                f"Manifest entry for '{sid}' must have one of "
                f"{list(_MANIFEST_SUBSTRATE_FIELDS)}."
            )
        if len(present) > 1:
            raise ManifestSchemaError(
                f"Manifest entry for '{sid}' has more than one of "
                f"{list(_MANIFEST_SUBSTRATE_FIELDS)}; got {present}. "
                "Exactly one substrate specifier is allowed."
            )
        if "legs" in entry:
            _validate_legs(sid, entry["legs"])
        if "funding_cadence_hours" in entry:
            cadence = entry["funding_cadence_hours"]
            # Reject bools because bool is a subclass of int and
            # `isinstance(True, int)` is True; we want strict integers.
            if isinstance(cadence, bool) or not isinstance(cadence, int):
                raise ManifestSchemaError(
                    f"Manifest entry for '{sid}' 'funding_cadence_hours' "
                    f"must be an int; got {type(cadence).__name__}."
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


def _get_legs(entry: dict) -> dict | None:
    """Return the entry's `legs` dict (e.g. {'spot': 'BTC/USDT',
    'perp': 'BTC/USDT'}) or None when the entry is single- or
    multi-symbol.

    Callers can branch on `_get_legs(entry) is not None` to dispatch
    between the spot OHLCV path and the perp+spot two-leg path.
    """
    return entry.get("legs")


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


def _load_perp_df(symbol_manifest: str, timeframe: str) -> pd.DataFrame:
    """Load perp OHLCV from `_PERP_CACHE_DIR`.

    The perp cache uses the OKX `instId` form ("BTC-USDT-SWAP") as
    the filename prefix per `data.okx_perp.PERP_CACHE_DIR` convention.
    Translation goes through `data.okx_perp.manifest_to_okx_instid`
    so the manifest-→-OKX boundary lives in the data layer (per
    Phase 4.B G3a).  Mirrors `_load_symbol_df` for byte-level
    discoverability.
    """
    # Local import: the data layer is the canonical translator (G3a).
    # Importing here avoids a top-level dependency on the data
    # package from sacred-harness code.
    from data.okx_perp import manifest_to_okx_instid

    prefix = manifest_to_okx_instid(symbol_manifest)
    candidates = list(_PERP_CACHE_DIR.glob(f"{prefix}_{timeframe}_*.parquet"))
    if not candidates:
        raise FileNotFoundError(
            f"No perp cache file for {symbol_manifest} {timeframe} in "
            f"{_PERP_CACHE_DIR}"
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


def _build_legs_dict(
    legs: dict,
    timeframe: str,
    after_ts: pd.Timestamp | None,
    before_ts: pd.Timestamp | None,
) -> dict[str, pd.DataFrame]:
    """Load and filter OHLCV for a `legs` entry.

    Returns a dict `{"spot": DataFrame, "perp": DataFrame}` with each
    frame indexed by UTC timestamp and OHLCV columns.  No 'symbol'
    column is added: the dict keys carry the leg identity, and downstream
    cpcv_perp / engine_perp consume the two frames separately.
    """
    spot_part = _load_symbol_df(legs["spot"], timeframe)
    perp_part = _load_perp_df(legs["perp"], timeframe)
    if after_ts is not None:
        spot_part = spot_part[spot_part.index >= after_ts]
        perp_part = perp_part[perp_part.index >= after_ts]
    if before_ts is not None:
        spot_part = spot_part[spot_part.index < before_ts]
        perp_part = perp_part[perp_part.index < before_ts]
    return {"spot": spot_part.copy(), "perp": perp_part.copy()}


# ── Public accessors ──────────────────────────────────────────────────────────

def load_dev(strategy_id: str) -> pd.DataFrame | dict[str, pd.DataFrame]:
    """Return OHLCV rows in [data_start, holdout_start) for strategy_id.

    Not audited — may be called freely during iteration.

    Return shape depends on the manifest entry's substrate:
      - `symbol`  → DataFrame with a constant 'symbol' column.
      - `symbols` → DataFrame with rows concatenated across symbols
                    and a 'symbol' column distinguishing them.
      - `legs`    → dict `{"spot": DataFrame, "perp": DataFrame}`,
                    each frame's columns are open/high/low/close/volume.
                    No 'symbol' column is added; the dict keys carry
                    the leg identity.  Consumed by `backtest.cpcv_perp`
                    and `backtest.engine_perp`.

    Raises StrategyNotInManifest if strategy_id is absent from the manifest.
    """
    manifest = load_manifest()
    if strategy_id not in manifest:
        raise StrategyNotInManifest(f"'{strategy_id}' not found in holdout manifest.")
    entry = manifest[strategy_id]
    holdout_start = pd.Timestamp(entry["holdout_start"])
    # 2026-06-11 bug fix (contract-preserving): this function's
    # documented contract is "[data_start, holdout_start)" but the
    # implementation passed after_ts=None, returning every cached row
    # before holdout_start.  Invisible while cache files started
    # exactly at data_start; the 2026-06-11 extended-window backfill
    # made caches deeper than some entries' data_start (e.g.
    # FundingRateHarvest_BTC: spot/perp cached from 2020-11 but
    # data_start funding-bound at 2021-08-31), so the lower bound now
    # has to be applied for the manifest to stay the substrate's
    # source of truth.
    data_start = pd.Timestamp(entry["data_start"])

    legs = _get_legs(entry)
    if legs is not None:
        return _build_legs_dict(
            legs,
            entry["timeframe"],
            after_ts=data_start,
            before_ts=holdout_start,
        )

    return _build_df(
        _get_symbols(entry),
        entry["timeframe"],
        after_ts=data_start,
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


# Regeneration events appended after this instant must carry a
# non-empty `caller` field to reset the single-access flag (and the
# final-gate guard in trials.py).  2026-06-11, work-order item 7d: the
# 2026-05-08 AttentionMomentum access-flag reset was an unattributed
# regenerated=true event (no caller, old==new holdout_start) — an
# append-path with no caller validation that silently re-opened the
# holdout.  Historical events at or before the cutoff are
# grandfathered (they were legitimate, just unattributed); the
# write-side counterpart is generate_holdout_manifest.regenerate_
# manifest, which now requires caller + reason.
_REGEN_ATTRIBUTION_REQUIRED_AFTER: str = "2026-06-11T00:00:00+00:00"


def _regen_resets_access(event: dict) -> bool:
    """True iff a regenerated=true event is honoured as a reset of the
    single-access flag / final-gate guard.  Grandfathers pre-cutoff
    events; post-cutoff events must carry a non-empty caller."""
    ts = event.get("ts")
    if isinstance(ts, str) and ts <= _REGEN_ATTRIBUTION_REQUIRED_AFTER:
        return True
    caller = event.get("caller")
    return isinstance(caller, str) and bool(caller.strip())


def _has_prior_access(strategy_id: str) -> bool:
    """Return True if strategy has an uncleared access in the log.

    Scans events in file order (chronological).  A regenerated=true
    event clears the flag — IF it passes `_regen_resets_access`
    (attributed, or grandfathered pre-2026-06-11); a normal access
    event sets it.
    """
    has_access = False
    for event in iter_jsonl_filtered(
        _ACCESS_LOG_PATH,
        lambda e: e.get("strategy_id") == strategy_id,
    ):
        if event.get("regenerated") is True:
            if _regen_resets_access(event):
                has_access = False
        else:
            has_access = True
    return has_access


def load_holdout(
    strategy_id: str,
    *,
    caller: str,
    reason: str,
) -> pd.DataFrame | dict[str, pd.DataFrame]:
    """Return OHLCV rows in [holdout_start, data_end) for strategy_id.

    Appends exactly one event to backtest/holdout_access.log on success.

    Return shape mirrors `load_dev`: a DataFrame for `symbol` /
    `symbols` entries, a `{"spot", "perp"}` dict for `legs` entries.

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

    # 4. Load and filter data — bypass enforcement so holdout.py is the only
    #    authorised path for accessing holdout rows.
    entry = manifest[strategy_id]
    holdout_start = pd.Timestamp(entry["holdout_start"])
    legs = _get_legs(entry)
    token = _holdout_bypass_ctx.set(True)
    try:
        if legs is not None:
            result = _build_legs_dict(
                legs,
                entry["timeframe"],
                after_ts=holdout_start,
                before_ts=None,
            )
            n_rows = sum(len(df) for df in result.values())
        else:
            result = _build_df(
                _get_symbols(entry),
                entry["timeframe"],
                after_ts=holdout_start,
                before_ts=None,
            )
            n_rows = len(result)
    finally:
        _holdout_bypass_ctx.reset(token)

    # 5. Append audit event — after data is loaded so n_rows is known.
    event = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "strategy_id": strategy_id,
        "caller": caller,
        "reason": reason,
        "git_commit": _get_git_commit(),
        "n_rows": n_rows,
        "regenerated": False,
    }
    append_jsonl(_ACCESS_LOG_PATH, event)

    return result
