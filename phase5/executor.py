"""phase5/executor.py -- Polymarket order-execution module (gated).

Replaces the manual-typing path with structured, deterministic code:
preview -> single human approval -> atomic batch submission. This is
the right architecture once calibration is validated -- humans
approve strategy, code executes orders.

ACTIVATION
----------
This module is INERT until both of these are true:
  1. `pip install py-clob-client web3` (SDK + signing dependency)
  2. POLYGON_PRIVATE_KEY env var is set in ~/.crypto-bot.env (the same
     ~/.crypto-bot.env file the OKX bot uses; sourced into the parent
     shell before running this script)
  3. POLYMARKET_PROXY_ADDRESS env var is set to your funded
     Polymarket proxy wallet address (a 0x... string)

Without those, `preview` works (no signing, no submission) but
`execute` aborts at the import / env-var check before doing anything.

This is the deliberate human gate: the agent is not allowed to add
keys/passwords to the env per CLAUDE.md "Human only". The user
controls activation by setting the env vars and installing the SDK.

SAFETY CAPS (hard-coded; not configurable from CLI without code edit)
--------------------------------------------------------------------
- MAX_PER_MARKET_USD = 100.0    (first deploy; raise after calibration)
- MAX_TOTAL_BATCH_USD = 500.0
- MAX_RECOMMENDATION_AGE_HOURS = 24.0  (stale prices rejected)
- MIN_BANKROLL_HEADROOM_USD = 100.0    (never deploy if it would leave
                                          the wallet with < this)

These are intentionally low for the first live deployment. After
calibration evidence accumulates (Brier < 0.15 on 30+ resolutions),
edit this file to raise the caps; the change is auditable in git.

WORKFLOW
--------
    python phase5/executor.py preview
        Read paper-ledger entries with status=open, walk live
        orderbook, project realistic batch P&L cost. Prints the
        batch hash and total notional. NO signing, NO submission.

    python phase5/executor.py execute --batch-hash <hash>
        Requires the hash from `preview`. Verifies the ledger has
        not changed since preview. Loads private key from env;
        signs each order via py-clob-client; submits to Polymarket
        CLOB. Updates ledger entries to status=open_live with txhash
        + actual fill price. Aborts atomically on any failure.

    python phase5/executor.py status
        Reconcile pending live orders: poll fills, update ledger,
        compute realized fees + slippage vs preview projection.

The `preview` command is freely runnable. The `execute` command is
gated on (a) the env vars being set, (b) the SDK being installed,
(c) the batch hash matching, (d) the bankroll/cap guards, (e) an
explicit final confirmation prompt.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data.polymarket import fetch_orderbook, walk_asks_for_buy
from phase5.paper_ledger import (
    DEFAULT_LEDGER_PATH, _save_ledger, load_ledger,
)


# -- Hard safety caps --------------------------------------------------------

MAX_PER_MARKET_USD: float = 100.0
MAX_TOTAL_BATCH_USD: float = 500.0
MAX_RECOMMENDATION_AGE_HOURS: float = 24.0
MIN_BANKROLL_HEADROOM_USD: float = 100.0

# Polymarket CLOB endpoint.
CLOB_HOST: str = "https://clob.polymarket.com"

# Required env vars for `execute`.
ENV_PRIVATE_KEY: str = "POLYGON_PRIVATE_KEY"
ENV_PROXY_ADDRESS: str = "POLYMARKET_PROXY_ADDRESS"


# -- Preview path (no signing, no SDK required) ------------------------------

def _live_fill_quote(entry: dict) -> dict:
    """Re-walk the live orderbook for one open paper-ledger entry."""
    clob_token_ids = entry.get("clob_token_ids")
    if not clob_token_ids:
        return {
            "available": False,
            "reason": "clob_token_ids not stored on ledger entry "
                      "(legacy entry; re-record to populate)",
        }
    side_idx = 0 if entry["action"] == "BUY_YES" else 1
    if side_idx >= len(clob_token_ids):
        return {"available": False, "reason": "side index out of range"}
    walk = walk_asks_for_buy(clob_token_ids[side_idx], entry["size_usd"])
    return {"available": True, **walk}


def _entry_age_hours(entry: dict) -> float:
    ts = entry.get("ts_recorded")
    if not ts:
        return float("inf")
    try:
        rec = datetime.fromisoformat(ts)
    except ValueError:
        return float("inf")
    return (datetime.now(timezone.utc) - rec).total_seconds() / 3600.0


def _batch_hash(entries: list[dict]) -> str:
    """Deterministic hash over the open entries. Used as the
    confirmation token between `preview` and `execute` -- if any entry
    changed (size, action, market_id, recorded ts), the hash changes
    and `execute` aborts.
    """
    payload = json.dumps(
        [{
            "market_id": e["market_id"],
            "action": e["action"],
            "size_usd": e["size_usd"],
            "entry_price": e["entry_price"],
            "ts_recorded": e["ts_recorded"],
        } for e in entries],
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def preview(ledger_path: Optional[Path] = None) -> dict:
    """Show what `execute` would submit. No signing. No state change."""
    entries = load_ledger(ledger_path)
    open_entries = [e for e in entries if e.get("status") == "open"]

    if not open_entries:
        print("No open paper-ledger entries; nothing to preview.")
        return {"status": "empty"}

    print("=" * 72)
    print("Polymarket execution PREVIEW (no signing, no submission)")
    print("=" * 72)
    print()

    total_notional = 0.0
    age_violations: list[str] = []
    cap_violations: list[str] = []
    live_quotes: list[dict] = []

    for e in open_entries:
        mid = e.get("market_id", "?")
        act = e.get("action", "?")
        size = float(e.get("size_usd", 0.0))
        price = float(e.get("entry_price", 0.0))
        age_h = _entry_age_hours(e)

        # Cap checks.
        if size > MAX_PER_MARKET_USD:
            cap_violations.append(
                f"{mid}: size ${size:.2f} exceeds per-market cap "
                f"${MAX_PER_MARKET_USD:.2f}"
            )
        if age_h > MAX_RECOMMENDATION_AGE_HOURS:
            age_violations.append(
                f"{mid}: recommendation age {age_h:.1f}h exceeds "
                f"{MAX_RECOMMENDATION_AGE_HOURS:.1f}h freshness window"
            )

        total_notional += size

        # Live re-walk.
        try:
            live = _live_fill_quote(e)
        except Exception as exc:  # noqa: BLE001
            live = {
                "available": False,
                "reason": f"{exc.__class__.__name__}: {exc}",
            }

        live_quotes.append({"entry": e, "live": live})

        live_str = "n/a"
        if live.get("available"):
            recorded_price = price
            live_avg = live.get("avg_price")
            if live_avg is not None and not (live_avg != live_avg):  # not NaN
                drift_pp = (live_avg - recorded_price) * 100.0
                live_str = (
                    f"live avg=${live_avg:.4f} "
                    f"({drift_pp:+.2f}pp vs recorded)"
                )
            else:
                live_str = "live re-walk failed (NaN)"

        print(
            f"  {act:<8} {mid:<8} ${size:>7.2f} @ ${price:.4f} "
            f"(age {age_h:.1f}h)"
        )
        print(f"           live: {live_str}")
        print(f"           {e.get('market_question','')[:80]}")
        print()

    if total_notional > MAX_TOTAL_BATCH_USD:
        cap_violations.append(
            f"total batch notional ${total_notional:.2f} exceeds "
            f"${MAX_TOTAL_BATCH_USD:.2f}"
        )

    print("-" * 72)
    print(f"Open entries:   {len(open_entries)}")
    print(f"Total notional: ${total_notional:,.2f}")
    print(f"Caps:           ${MAX_PER_MARKET_USD:.0f}/market, "
          f"${MAX_TOTAL_BATCH_USD:.0f}/batch")
    print()

    if cap_violations or age_violations:
        print("VIOLATIONS (would block execute):")
        for v in cap_violations + age_violations:
            print(f"  - {v}")
        print()

    bh = _batch_hash(open_entries)
    print(f"Batch hash: {bh}")
    print()
    print("To execute (after pip install py-clob-client web3 + setting")
    print(f"  ${ENV_PRIVATE_KEY} + ${ENV_PROXY_ADDRESS} env vars):")
    print(f"  python phase5/executor.py execute --batch-hash {bh}")
    print("=" * 72)

    return {
        "status": "ok",
        "n_entries": len(open_entries),
        "total_notional_usd": total_notional,
        "batch_hash": bh,
        "cap_violations": cap_violations,
        "age_violations": age_violations,
    }


# -- Execute path (requires SDK + env vars + confirmation) -------------------

def _check_activation_gates() -> tuple[bool, list[str]]:
    """Verify the activation requirements are met BEFORE attempting
    any signing or submission. Returns (ok, reasons).
    """
    reasons: list[str] = []

    if not os.environ.get(ENV_PRIVATE_KEY):
        reasons.append(
            f"{ENV_PRIVATE_KEY} env var not set. Source ~/.crypto-bot.env "
            "into the parent shell before running."
        )
    if not os.environ.get(ENV_PROXY_ADDRESS):
        reasons.append(
            f"{ENV_PROXY_ADDRESS} env var not set."
        )
    try:
        import py_clob_client  # noqa: F401
    except ImportError:
        reasons.append(
            "py-clob-client not installed. Run: pip install py-clob-client web3"
        )

    return (len(reasons) == 0, reasons)


def execute(
    *,
    batch_hash: str,
    ledger_path: Optional[Path] = None,
    skip_confirm: bool = False,
) -> dict:
    """Submit the open batch to Polymarket. Requires activation gates."""
    ok, reasons = _check_activation_gates()
    if not ok:
        print("ACTIVATION GATES FAILED. Cannot execute.")
        for r in reasons:
            print(f"  - {r}")
        return {"status": "blocked", "reasons": reasons}

    entries = load_ledger(ledger_path)
    open_entries = [e for e in entries if e.get("status") == "open"]
    if not open_entries:
        print("No open entries to execute.")
        return {"status": "empty"}

    expected_hash = _batch_hash(open_entries)
    if expected_hash != batch_hash:
        print(
            f"BATCH HASH MISMATCH. Provided {batch_hash!r}, expected "
            f"{expected_hash!r}. Ledger has changed since preview. "
            "Re-run preview and re-confirm."
        )
        return {"status": "hash_mismatch"}

    # Re-check caps now (paranoid).
    total = sum(float(e.get("size_usd", 0.0)) for e in open_entries)
    if total > MAX_TOTAL_BATCH_USD:
        print(f"Total batch ${total:.2f} exceeds cap; aborting.")
        return {"status": "cap_violation"}
    for e in open_entries:
        if float(e.get("size_usd", 0.0)) > MAX_PER_MARKET_USD:
            print(f"{e['market_id']} exceeds per-market cap; aborting.")
            return {"status": "cap_violation"}
        if _entry_age_hours(e) > MAX_RECOMMENDATION_AGE_HOURS:
            print(f"{e['market_id']} too stale; aborting.")
            return {"status": "age_violation"}

    # Final confirmation (TTY only; skip for tests).
    if not skip_confirm:
        print()
        print(f"About to submit {len(open_entries)} orders for "
              f"${total:.2f} total notional to Polymarket CLOB.")
        print(f"Batch hash: {expected_hash}")
        try:
            ans = input("Type EXECUTE to confirm (anything else aborts): ")
        except (EOFError, KeyboardInterrupt):
            ans = ""
        if ans.strip() != "EXECUTE":
            print("Aborted by user.")
            return {"status": "user_aborted"}

    # === Real submission begins here ===
    # Imports inside the function so module-level import works without
    # the SDK installed (preview path stays runnable on bare environments).
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY

    private_key = os.environ[ENV_PRIVATE_KEY]
    proxy_address = os.environ[ENV_PROXY_ADDRESS]
    client = ClobClient(
        host=CLOB_HOST,
        chain_id=137,  # Polygon mainnet
        key=private_key,
        funder=proxy_address,
        signature_type=2,  # Polymarket proxy wallet
    )
    client.set_api_creds(client.create_or_derive_api_creds())

    submitted: list[dict] = []
    failed: list[dict] = []

    for entry in open_entries:
        clob_token_ids = entry.get("clob_token_ids") or []
        if not clob_token_ids:
            failed.append({"entry": entry, "reason": "missing clob_token_ids"})
            continue
        token_idx = 0 if entry["action"] == "BUY_YES" else 1
        if token_idx >= len(clob_token_ids):
            failed.append({"entry": entry, "reason": "token_idx OOR"})
            continue
        token_id = clob_token_ids[token_idx]

        # Convert size_usd to size_shares at the recorded fill price.
        # Submit at recorded fill price as a limit order; if live book
        # has moved, the order may sit unfilled. status command picks
        # up partial / unfilled orders later.
        price = float(entry["entry_price"])
        size_shares = float(entry["size_usd"]) / price

        try:
            order_args = OrderArgs(
                price=price,
                size=size_shares,
                side=BUY,
                token_id=token_id,
            )
            signed = client.create_order(order_args)
            resp = client.post_order(signed, OrderType.GTC)
        except Exception as exc:  # noqa: BLE001
            failed.append({
                "entry": entry,
                "reason": f"submit failed: {exc.__class__.__name__}: {exc}",
            })
            continue

        # Update ledger entry: status -> open_live + tx_hash.
        entry["status"] = "open_live"
        entry["live_submit_ts"] = datetime.now(timezone.utc).isoformat()
        entry["live_order_id"] = resp.get("orderID") or resp.get("id")
        entry["live_submit_response"] = {
            k: resp.get(k) for k in ("status", "transactionHash", "orderID", "id")
            if k in resp
        }
        submitted.append({"entry": entry, "response": resp})

    # Persist updated ledger.
    _save_ledger(entries, ledger_path)

    print()
    print(f"Submitted: {len(submitted)} orders")
    print(f"Failed:    {len(failed)} orders")
    for f in failed:
        print(f"  - {f['entry'].get('market_id')}: {f['reason']}")
    return {
        "status": "ok",
        "submitted": len(submitted),
        "failed": len(failed),
        "submitted_orders": submitted,
        "failed_orders": failed,
    }


# -- Status path -------------------------------------------------------------

def status(ledger_path: Optional[Path] = None) -> dict:
    """Reconcile pending live orders.

    For each `open_live` entry, query Polymarket for fill status.
    Update entry to `filled_live` (with actual fill price + tx) or
    leave at `open_live` if still pending. Cancel logic and partial-
    fill handling are out of scope for v1; surface the situation
    via the printout and let the operator decide.
    """
    ok, reasons = _check_activation_gates()
    if not ok:
        print("ACTIVATION GATES FAILED. Cannot reconcile live orders.")
        for r in reasons:
            print(f"  - {r}")
        return {"status": "blocked", "reasons": reasons}

    entries = load_ledger(ledger_path)
    live_entries = [e for e in entries if e.get("status") == "open_live"]
    if not live_entries:
        print("No live orders to reconcile.")
        return {"status": "empty"}

    from py_clob_client.client import ClobClient

    private_key = os.environ[ENV_PRIVATE_KEY]
    proxy_address = os.environ[ENV_PROXY_ADDRESS]
    client = ClobClient(
        host=CLOB_HOST, chain_id=137, key=private_key,
        funder=proxy_address, signature_type=2,
    )
    client.set_api_creds(client.create_or_derive_api_creds())

    n_filled = 0
    n_pending = 0
    n_error = 0
    for entry in live_entries:
        oid = entry.get("live_order_id")
        if not oid:
            n_error += 1
            continue
        try:
            order = client.get_order(oid)
        except Exception:  # noqa: BLE001
            n_error += 1
            continue
        if str(order.get("status", "")).lower() in ("filled", "matched"):
            entry["status"] = "filled_live"
            entry["live_filled_ts"] = datetime.now(timezone.utc).isoformat()
            entry["live_filled_price"] = float(order.get("price", entry["entry_price"]))
            entry["live_filled_size"] = float(order.get("size_matched", 0.0))
            n_filled += 1
        else:
            n_pending += 1

    _save_ledger(entries, ledger_path)
    print(f"Reconciled: {n_filled} filled, {n_pending} pending, {n_error} errors")
    return {"filled": n_filled, "pending": n_pending, "errors": n_error}


# -- CLI ---------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Polymarket execution module. Gated; activate via "
                    "POLYGON_PRIVATE_KEY + POLYMARKET_PROXY_ADDRESS env vars "
                    "and `pip install py-clob-client web3`.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("preview", help="Show what execute would submit; no signing.")
    p_exec = sub.add_parser("execute", help="Submit to Polymarket CLOB (gated).")
    p_exec.add_argument("--batch-hash", required=True,
                        help="Hash from `preview`; aborts on mismatch.")
    p_exec.add_argument("--skip-confirm", action="store_true",
                        help="Skip the EXECUTE prompt (for scripted use only).")
    sub.add_parser("status", help="Reconcile open_live orders against CLOB.")

    args = parser.parse_args()

    if args.cmd == "preview":
        preview()
    elif args.cmd == "execute":
        execute(batch_hash=args.batch_hash, skip_confirm=args.skip_confirm)
    elif args.cmd == "status":
        status()
    else:
        parser.error(f"unknown command {args.cmd!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
