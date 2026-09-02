#!/usr/bin/env python
"""Append the three Phase 4.F Binance UM entries to the holdout manifest.

`backtest/holdout_manifest.json` is a sacred-harness file: an agent may not
rewrite it, and even an authorized edit must be provably ADDITIVE. This script
is that proof. It:

  1. loads the current manifest and the staged proposal
     (`backtest/proposed_manifest_entries_binance_um.json`);
  2. refuses if any proposed key already exists (idempotent: re-running after a
     successful apply is a no-op, not a silent overwrite);
  3. adds ONLY the three new top-level keys, leaving every existing entry
     byte-identical — asserted by comparing the pre/post JSON of each old key;
  4. re-validates the whole file through `backtest.holdout.load_manifest`, so a
     malformed addition fails here rather than at the next trial;
  5. appends one `"added": true` audit event per entry to
     `backtest/holdout_access.log` via the existing writer
     (`backtest.logs.append_jsonl`), caller `phase4f.manifest_add`.

Run with `--dry-run` to see the diff without writing.

Why the entries look the way they do. The manifest schema admits exactly one
of `symbol` / `symbols` / `legs`, and none can express a DYNAMIC universe
(listings and delistings inside the window). These three rows therefore carry
`symbol: "BTCUSDT"` as the dev/holdout BOUNDARY ANCHOR only — not the traded
instrument — and name the real universe by reference in `notes`
(`universe_ref=`). The `substrate=binance_um` tag in the same notes is what
routes `load_dev`/`load_holdout` to the perp archive; without it the anchor
symbol's shape would send it to the Phase 4.E SPOT cache, which is the open
question the proposal file flagged and this change resolves.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

MANIFEST = REPO / "backtest" / "holdout_manifest.json"
PROPOSAL = REPO / "backtest" / "proposed_manifest_entries_binance_um.json"
ACCESS_LOG = REPO / "backtest" / "holdout_access.log"

CALLER = "phase4f.manifest_add"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change; write nothing")
    args = ap.parse_args(argv[1:])

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    proposal = json.loads(PROPOSAL.read_text(encoding="utf-8"))
    entries = proposal["entries"]

    before = {k: json.dumps(v, sort_keys=True) for k, v in manifest.items()}

    clashes = [k for k in entries if k in manifest]
    if clashes:
        print(f"REFUSING: these keys already exist in the manifest: {clashes}")
        print("This script is additive-only. Nothing was written.")
        return 1

    for sid, entry in entries.items():
        manifest[sid] = entry
        print(f"  + {sid}  ({entry['timeframe']}, anchor {entry['symbol']}, "
              f"dev_end {entry['dev_end'][:10]}, holdout_start "
              f"{entry['holdout_start'][:10]})")

    # (3) prove additivity: every pre-existing key must be untouched.
    after = {k: json.dumps(v, sort_keys=True) for k, v in manifest.items()}
    for k, v in before.items():
        if after.get(k) != v:
            print(f"ABORT: existing entry '{k}' would change. Nothing written.")
            return 1
    print(f"  additive check OK: {len(before)} existing entries unchanged")

    if args.dry_run:
        print("--dry-run: nothing written.")
        return 0

    text = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    MANIFEST.write_text(text, encoding="utf-8")

    # (4) re-validate the written file through the harness's own loader.
    from backtest import holdout as _h

    _h._reset_manifest_cache()
    loaded = _h.load_manifest()
    missing = [k for k in entries if k not in loaded]
    if missing:
        print(f"ABORT: written manifest does not load back {missing}")
        return 1
    print(f"  load_manifest() OK: {len(loaded)} entries, all schema-valid")

    # (5) audit trail. These are ANNOTATIONS, not accesses. `_has_prior_access`
    # treats every non-`regenerated` event carrying a strategy_id as a read, so
    # without `backtest.holdout._is_non_read_annotation` these three rows would
    # have consumed the single-access flag for all three strategies before any
    # of them ran — load_holdout would refuse their first and only final_gate,
    # and the manifest addition would have destroyed the guarantee it exists to
    # record. The `"added": true` field with NO `n_rows` is what marks them.
    from backtest.logs import append_jsonl

    ts = datetime.now(timezone.utc).isoformat()
    for sid in entries:
        append_jsonl(ACCESS_LOG, {
            "ts": ts,
            "strategy_id": sid,
            "caller": CALLER,
            "reason": "Phase 4.F perp-structural manifest entry added "
                      "(additive; no holdout data read)",
            "added": True,
            "regenerated": False,
        })
    print(f"  appended {len(entries)} 'added' events to {ACCESS_LOG.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
