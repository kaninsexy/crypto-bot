"""
backtest/generate_holdout_manifest.py — Holdout manifest generator.

Two public entry points:

    generate_initial()
        First-time manifest creation.  Errors if the manifest file already
        exists (ManifestAlreadyExists).  Also touches holdout_access.log so
        the file exists for git tracking from day one.

    regenerate_manifest(strategies=None)
        Redraw the split for the listed strategies (all if None).  For each
        strategy whose holdout_start changed, appends a regenerated=true event
        to holdout_access.log and prints a STALE DSR warning to stderr.

Split logic
───────────
1. For each strategy, look up its symbol(s):
     - Single-symbol strategies: config.STRATEGY_SYMBOLS[strategy_id]
     - DualMomentum: the full 3-asset rotation universe (see _MULTI_SYMBOL_OVERRIDES)
2. Query the L1 parquet cache for each symbol's actual data bounds.
3. Multi-symbol intersection: data_start = max(starts), data_end = min(ends).
4. 80/20 calendar split:
       dev_end = holdout_start = data_start + 0.80 * (data_end - data_start)
   The split is half-open on both sides; no row ever belongs to both windows.

Modifying this file requires human approval per CLAUDE.md (validation harness).

CLI usage (optional convenience):
    python -m backtest.generate_holdout_manifest init
    python -m backtest.generate_holdout_manifest regen [STRATEGY_ID ...]
"""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

import config
from backtest.logs import append_jsonl


# ── Module-level path configuration (override in tests via monkeypatch) ───────

_MANIFEST_PATH: Path = Path("backtest/holdout_manifest.json")
_ACCESS_LOG_PATH: Path = Path("backtest/holdout_access.log")
_CACHE_DIR: Path = Path("backtest/cache/ohlcv")
_TIMEFRAME: str = "1h"

# DualMomentum rotates across a 3-asset universe; config.STRATEGY_SYMBOLS only
# records its primary symbol.  Any future multi-symbol strategy needs an entry here.
_MULTI_SYMBOL_OVERRIDES: dict[str, list[str]] = {
    "DualMomentum": ["BTC/USDT", "ETH/USDT", "BNB/USDT"],
}

_MONTHS_RE = re.compile(r"_(\d+)mo\.parquet$")


# ── Exception ─────────────────────────────────────────────────────────────────

class ManifestAlreadyExists(FileExistsError):
    """Raised by generate_initial when the manifest file already exists."""


# ── Internal helpers ──────────────────────────────────────────────────────────

def _get_strategy_symbols(strategy_id: str) -> list[str]:
    if strategy_id in _MULTI_SYMBOL_OVERRIDES:
        return _MULTI_SYMBOL_OVERRIDES[strategy_id]
    return [config.STRATEGY_SYMBOLS[strategy_id]]


def _parse_months(p: Path) -> int:
    """Parse the {N} from a cache filename ending in _{N}mo.parquet."""
    m = _MONTHS_RE.search(p.name)
    if m is None:
        raise ValueError(
            f"Cache file '{p.name}' does not match expected naming "
            "convention '{{symbol}}_{{timeframe}}_{{N}}mo.parquet'."
        )
    return int(m.group(1))


def _find_cache_file(symbol: str) -> Path:
    """Return the highest-month-count parquet file for (symbol, _TIMEFRAME)."""
    prefix = symbol.replace("/", "-")
    candidates = list(_CACHE_DIR.glob(f"{prefix}_{_TIMEFRAME}_*.parquet"))
    if not candidates:
        raise FileNotFoundError(
            f"No cache file for {symbol} {_TIMEFRAME} in {_CACHE_DIR}"
        )
    return max(candidates, key=_parse_months)


def _get_symbol_bounds(symbol: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Return (data_start, data_end) from the L1 parquet cache for symbol."""
    path = _find_cache_file(symbol)
    idx = pd.read_parquet(path).index
    return idx.min(), idx.max()


def _compute_entry(strategy_id: str) -> dict:
    """Compute a manifest entry for strategy_id from live L1 cache bounds."""
    symbols = _get_strategy_symbols(strategy_id)
    bounds = [_get_symbol_bounds(sym) for sym in symbols]

    # Multi-symbol intersection: use the period all symbols share.
    data_start = max(b[0] for b in bounds)
    data_end = min(b[1] for b in bounds)

    span = data_end - data_start
    dev_end = data_start + span * 0.80
    holdout_start = dev_end

    entry: dict = {
        "timeframe": _TIMEFRAME,
        "data_start": data_start.isoformat(),
        "data_end": data_end.isoformat(),
        "dev_end": dev_end.isoformat(),
        "holdout_start": holdout_start.isoformat(),
    }
    if len(symbols) == 1:
        entry["symbol"] = symbols[0]
    else:
        entry["symbols"] = symbols
    return entry


# ── Public entry points ───────────────────────────────────────────────────────

def generate_initial() -> None:
    """Create holdout_manifest.json for the first time.

    Raises ManifestAlreadyExists if the file already exists — the only
    sanctioned path to rewriting an existing manifest is regenerate_manifest().

    Also touches holdout_access.log if absent so the file is present for
    git tracking from day one.
    """
    if _MANIFEST_PATH.exists():
        raise ManifestAlreadyExists(
            f"Manifest already exists at {_MANIFEST_PATH}. "
            "Use regenerate_manifest() to update an existing manifest."
        )

    manifest: dict = {}
    for strategy_id in config.STRATEGY_SYMBOLS:
        manifest[strategy_id] = _compute_entry(strategy_id)

    _MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    _MANIFEST_PATH.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    if not _ACCESS_LOG_PATH.exists():
        _ACCESS_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _ACCESS_LOG_PATH.touch()

    print(f"[generate_initial] Wrote {_MANIFEST_PATH} ({len(manifest)} strategies).")
    print(f"[generate_initial] Access log: {_ACCESS_LOG_PATH}")


def regenerate_manifest(strategies: list[str] | None = None) -> None:
    """Redraw the holdout split for specified strategies (all if None).

    For each strategy whose holdout_start changed:
      - Appends a regenerated=true event to holdout_access.log (one per strategy).
    If any strategy changed:
      - Prints a STALE DSR warning to stderr naming affected strategies.
      - Appends one stale_dsr_warning event to holdout_access.log.

    Raises FileNotFoundError if the manifest does not exist.
    """
    if not _MANIFEST_PATH.exists():
        raise FileNotFoundError(
            f"Manifest not found at {_MANIFEST_PATH}. "
            "Call generate_initial() first."
        )

    old_manifest: dict = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    target_ids: list[str] = (
        list(config.STRATEGY_SYMBOLS.keys()) if strategies is None else strategies
    )

    new_manifest = dict(old_manifest)
    stale: list[str] = []

    for sid in target_ids:
        new_entry = _compute_entry(sid)
        old_entry = old_manifest.get(sid, {})
        old_hs = old_entry.get("holdout_start")
        new_hs = new_entry["holdout_start"]

        if old_hs != new_hs:
            stale.append(sid)
            append_jsonl(_ACCESS_LOG_PATH, {
                "ts": datetime.now(timezone.utc).isoformat(),
                "strategy_id": sid,
                "regenerated": True,
                "old_holdout_start": old_hs,
                "new_holdout_start": new_hs,
            })

        new_manifest[sid] = new_entry

    _MANIFEST_PATH.write_text(json.dumps(new_manifest, indent=2), encoding="utf-8")

    if stale:
        warning = (
            "STALE DSR: the following strategies had their split redrawn. "
            "Prior holdout DSR results are invalid and must be recomputed "
            f"on the new split: {stale}"
        )
        print(warning, file=sys.stderr)
        append_jsonl(_ACCESS_LOG_PATH, {
            "ts": datetime.now(timezone.utc).isoformat(),
            "type": "stale_dsr_warning",
            "message": warning,
            "affected_strategies": stale,
        })

    print(
        f"[regenerate_manifest] Updated {len(target_ids)} strategies; "
        f"{len(stale)} had changed bounds.",
        file=sys.stderr if stale else sys.stdout,
    )


# ── CLI convenience ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Holdout manifest generator")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("init", help="Generate initial manifest (errors if already exists)")
    regen_p = sub.add_parser("regen", help="Regenerate for all or specific strategies")
    regen_p.add_argument(
        "strategies", nargs="*",
        help="Strategy IDs to regenerate (omit for all)",
    )
    args = parser.parse_args()

    if args.cmd == "init":
        generate_initial()
    elif args.cmd == "regen":
        regenerate_manifest(args.strategies if args.strategies else None)
    else:
        parser.print_help()
        sys.exit(1)
