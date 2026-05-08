"""scripts/run_trial_queue.py — Trial queue orchestrator (parallel).

Reads backtest/trial_queue.json, processes queued items in parallel via
concurrent.futures.ProcessPoolExecutor, then commits results per the
CLAUDE.md trial-queue orchestrator exception.

Usage:
  python scripts/run_trial_queue.py                # run all queued items
  python scripts/run_trial_queue.py --workers 8    # cap parallelism at 8
  python scripts/run_trial_queue.py --workers 1    # sequential (debuggable)
  python scripts/run_trial_queue.py --once         # one BATCH then stop
  python scripts/run_trial_queue.py --dry-run      # print plan, no execution
  python scripts/run_trial_queue.py --reset-errors # status=error -> queued

Concurrency model (rewritten 2026-05-05 for AMD Threadripper 3960X
48-thread / 256GB host):

  1. Main process acquires the queue lock (cross-platform fcntl/msvcrt),
     loads `backtest/trial_queue.json`, and selects every entry where
     `status == "queued"` AND `needs_trial_script` is falsy.

  2. All selected items are flipped to `status = "running"` and
     persisted in ONE atomic save (tmp + os.replace). The queue file
     is then NOT touched again until every worker has returned, so
     workers never race on `trial_queue.json`.

  3. Workers spawn through ProcessPoolExecutor.  Each worker invokes
     its trial script via `subprocess.run` with `PYTHONIOENCODING=utf-8`
     in the env (Windows-stdout-encoding fix), prefixes every stdout/
     stderr line with `[<item_id>]`, and writes its result to
     `backtest/cache/trial_result_<item_id>.json` via tmp+rename.

  4. After every future has resolved, the main process collects each
     per-trial result file, runs the doc updates, persists the queue,
     and calls `commit_result` sequentially (git index serialises
     anyway).  KEEP-verdict alerts and the consecutive-failure batch
     alerter fire from the main process at the end of the batch.

  5. The proposal agent only runs after ALL parallel workers finish
     and their results have been folded back into the queue.

  6. `--once` runs one BATCH (every currently queued item in parallel)
     then stops, distinct from the prior per-item semantics.
     `--workers 1` preserves single-trial-at-a-time execution
     (in-process, no pool spawn) for debugging.

trials.log atomicity
────────────────────
Each trial subprocess appends its own row to `backtest/trials.log` via
`backtest.trials.record_trial → backtest.logs.append_jsonl`, which uses
`open(path, "a")` — POSIX O_APPEND is atomic for writes < PIPE_BUF
(~4 KB), and Windows `FILE_APPEND_DATA` is similarly atomic for short
rows. A single trials.log row stays well under that ceiling.

`acquire_trials_log_lock` / `release_trials_log_lock` (cross-platform
fcntl/msvcrt, same pattern as the queue-file lock) are exported for any
orchestrator-level direct append (none today; reserved for future
tooling — e.g. batch supersession tags written from the main process
while trials are running).

Queue item schema (v1):
  id             str   human-assigned slug, e.g. "sq-001"
  status         str   "queued" | "running" | "done" | "error"
  strategy_id    str   matches holdout_manifest.json key
  variation_id   str   matches script's VARIATION_ID constant
  script_path    str   repo-relative path to the trial script
  trial_type     str   "smoke" | "full_cpcv"
  hypothesis_one_line  str  one sentence
  source_citation      str  e.g. "research/supertrend-literature.md §Variation #2"
  literature_doc       str  repo-relative path to .md file to update
  added_by       str   always "kanin"; items enter only after human approval
  added_at       str   ISO 8601 UTC
  started_at     str|null
  finished_at    str|null
  verdict        str|null  populated from JSON summary on success
  trial_id       str|null  populated from trials.log after append
  error          str|null  first 500 chars of stderr on failure
  email_sent     bool
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

# Cross-platform file locking: fcntl is Unix-only, msvcrt is Windows-only.
# Both ship with Python stdlib; pick the right one at import time so the
# orchestrator runs on macOS / Linux dev machines AND on Windows operator
# laptops without a runtime crash.
if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


# ── Path constants ──────────────────────────────────────────────────────────

ROOT = Path(__file__).resolve().parents[1]
QUEUE_PATH = ROOT / "backtest" / "trial_queue.json"
# Runtime state lives in a separate gitignored file so Mac (definitions)
# and PC (status updates) never collide on git pull.
STATE_PATH = ROOT / "backtest" / "trial_queue_state.json"
LOCK_PATH = QUEUE_PATH.with_suffix(".lock")
RUN_LOG_PATH = ROOT / "backtest" / "trial_queue_run_log.jsonl"
CACHE_DIR = ROOT / "backtest" / "cache"
TRIALS_LOG_LOCK_PATH = ROOT / "backtest" / "trials.log.lock"

RESEND_API_URL = "https://api.resend.com/emails"
RESEND_FROM = os.environ.get("TRIAL_QUEUE_FROM", "trial-queue@crypto-bot.local")
EMAIL_RATE_LIMIT_SLEEP_S = 61  # >60s between calls = safe under 6/hr

TRIAL_TIMEOUT_S = 14_400  # 4h per CLAUDE.md compute budget
DEFAULT_MAX_WORKERS = 20  # default cap when --workers not specified

# Digest cadence: send a digest at most twice per day (every 12h)
# when there is unreported activity. 24h heartbeat fallback when
# nothing to report. The 12h interval is a Kanin preference set
# 2026-05-08 -- per-run digests under --continuous + hourly cron
# produced ~24 emails/day which was unreadable; twice/day bundles
# 12 runs of completed trials into a single digest the operator
# actually reads. Items that need attention (KEEPs, errors that
# the agent could not auto-fix) are surfaced in the digest body
# alongside the pass results so a single email covers both.
DIGEST_INTERVAL_S = 12 * 3600
HEARTBEAT_INTERVAL_S = 24 * 3600

# Auto-remediation: run a fetch script with this timeout when a trial
# emits TRIAL_ERROR_TYPE: missing_data and retry_count < 1.
FETCH_REMEDIATION_TIMEOUT_S = 120

RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
EMAIL_TO = os.environ.get("TRIAL_QUEUE_EMAIL_TO")

# Runtime fields managed in trial_queue_state.json. Definitions in
# trial_queue.json never carry these (after migration). Default values
# applied during merge if state has no entry for an item.
_RUNTIME_DEFAULTS: dict = {
    "status": "queued",
    "started_at": None,
    "finished_at": None,
    "verdict": None,
    "trial_id": None,
    "error": None,
    "email_sent": False,
    "retry_count": 0,
    "last_fetch_attempt": None,
    "needs_script_digested": False,
    # 429 auto-retry: timestamp (ISO) at which a retry_pending item is
    # eligible to flip back to queued. None when the item is not in
    # retry_pending.
    "retry_after": None,
    # Auto-build / auto-retry counters. build_attempts increments on
    # every CC auto-build invocation (success or failure). run_attempts
    # increments on every trial-subprocess attempt; the orchestrator
    # auto-requeues once on transient failure before falling through
    # to status="error".  scripter_attempts is a separate counter for
    # the claude-agent (.claude/agents/scripter.md) path that runs
    # before cc_build_helper; capped by MAX_SCRIPTER_AGENT_ATTEMPTS so
    # a persistent agent failure cannot loop the orchestrator.
    "build_attempts": 0,
    "run_attempts": 0,
    "scripter_attempts": 0,
}


# Auto-retry policy for trial subprocess errors. After a non-zero
# trial returncode (and no 429/missing-data special path triggered)
# the orchestrator requeues the item once before the standard
# error path fires. Keeps transient flakes from polluting the queue.
TRIAL_AUTO_RETRY_MAX = 1

# Data-layer blocker keywords. The auto-scripter pass skips items
# whose `error` field references unavailable data infrastructure
# (e.g. Glassnode on-chain feeds, Deribit options) so the agent
# does not waste a build attempt on a strategy that cannot actually
# run regardless of script quality.
_DATA_BLOCKER_KEYWORDS: tuple[str, ...] = (
    "glassnode",
    "deribit",
    "options data",
    "cryptoquant",
    "netflow data",
    "data dependency",
)

# Scripter agent timeout: 10 minutes is the same budget the
# cc_build_helper invocation uses; the agent is invoked once per
# needs_trial_script item via `claude -p .claude/agents/scripter.md`.
SCRIPTER_AGENT_TIMEOUT_S = 600

# Per-item cap on auto-scripter agent invocations.  After this many
# failures the item falls through to _auto_build_pass (cc_build_helper
# fallback) without re-trying the claude-agent path.  Prevents the
# runaway pattern observed 2026-05-08 where a scripter rc=1 produced
# ~1400 invocations / target across the outer while-True orchestrator
# loop before manual kill: scripter dropped failed items from `ready`,
# the items remained status=queued, the next outer iteration re-
# selected them, and the loop never terminated.  With this cap the
# scripter is bounded per-item; the cc_build_helper fallback does its
# own bounded retry via build_attempts and finally flips status=error
# so the outer loop exits cleanly.
MAX_SCRIPTER_AGENT_ATTEMPTS = 1

# Parallelism caps for the scripter and build passes.  Reserved for
# upcoming parallel-pass work: both invocations are I/O-bound
# (subprocess.run waiting on a `claude` CLI) so threads suffice.
# Capped at 4 to stay polite with the Anthropic API tier and to
# leave headroom for the ProcessPool-based trial workers that come
# after the build pass.  Not currently consumed -- the existing
# scripter+build passes are still sequential as of 2026-05-08; the
# 2026-05-08 hook-path fix to .claude/agents/*.md should eliminate
# most rc=1 fall-throughs so the sequential path is fast enough for
# typical batches of 3 items.  See the next-session handoff for the
# parallel rewrite plan.
SCRIPTER_PASS_MAX_PARALLEL = 4
BUILD_PASS_MAX_PARALLEL = 4

# Phase-1 unsupervised-run guardrails (2026-05-08).  These bound a
# single orchestrator invocation in three independent ways so that
# any future unforeseen failure cannot recur the runaway pattern.
#
# DEFAULT_MAX_BATCHES: how many run_batch iterations a single
# `python scripts/run_trial_queue.py` call performs by default.
# Set to 1 so the outer while-loop is bounded out-of-the-box; pass
# --max-batches N or --continuous to opt into multi-batch runs.
# The 2026-05-08 runaway happened because the loop was unbounded
# by default with no env-var or arg cap.
DEFAULT_MAX_BATCHES = 1

# Wall-clock cap on a single orchestrator invocation, matching the
# 4-hour PC compute budget in CLAUDE.md and .claude/rules/backtest.md.
# When elapsed time exceeds this, the loop breaks with a "wall-budget"
# reason and a digest email is dispatched.  Independent of --max-batches
# so a single batch that hangs (long-running trial subprocess) is also
# bounded.
MAX_ORCHESTRATOR_WALL_S = 4 * 3600

# Consecutive-no-progress kill switch.  After this many run_batch
# iterations in a row produce zero net queue movement (no item
# advanced from queued/needs_rerun to a terminal state AND no item
# completed successfully), the loop breaks with a "no-progress"
# reason.  Belt-and-suspenders defence against any new failure mode
# that bypasses the per-pass attempt caps.  Default 2 keeps a single
# transient flake from tripping the brake.
MAX_CONSECUTIVE_NOPROGRESS_BATCHES = 2


# 429 retry policy: backoff window before a rate-limited trial may
# rerun, and the per-item attempt cap. After RETRY_429_MAX_ATTEMPTS
# the item falls through to status="error" as normal.
RETRY_429_BACKOFF_HOURS = 0.5
RETRY_429_MAX_ATTEMPTS = 3


# ── Time helper ─────────────────────────────────────────────────────────────

def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Queue I/O ───────────────────────────────────────────────────────────────

def _merge_item(definition: dict, state_entry: dict | None) -> dict:
    """Combine a definition row + its runtime-state entry into a single
    in-memory item dict. Mac-side `drop_reason` / `defer_reason` on the
    definition override the runtime status (human decisions cannot be
    silently undone by the orchestrator).
    """
    merged = dict(definition)
    se = state_entry or {}
    for f, default in _RUNTIME_DEFAULTS.items():
        merged[f] = se.get(f, default)
    if definition.get("drop_reason"):
        merged["status"] = "dropped"
    elif (
        definition.get("defer_reason")
        and merged["status"] not in ("done", "running")
    ):
        merged["status"] = "deferred"
    return merged


def _load_or_migrate_state(definitions: dict) -> dict:
    """Return the runtime-state dict.

    First-run path: `trial_queue_state.json` is missing or empty, so we
    migrate runtime fields out of `trial_queue.json` items into the new
    state file and persist. Steady-state path: the state file is loaded
    directly. Items that existed only in local trial_queue.json before
    the file split (e.g. sq-009 from a hallucinated proposal) are not
    migrated -- they were never committed to the repo.
    """
    if STATE_PATH.exists():
        text = STATE_PATH.read_text(encoding="utf-8").strip()
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"Failed to parse {STATE_PATH}: {exc}. "
                    "Fix the JSON manually before re-running."
                ) from exc

    # First run -- migrate.
    state: dict = {
        "schema_version": 1,
        "last_digest_sent_at": None,
        "last_run_at": None,
        "items": {},
    }
    legacy_runtime_fields = (
        "status", "started_at", "finished_at", "verdict",
        "trial_id", "error", "email_sent",
    )
    n = 0
    for item in definitions.get("queue", []):
        item_id = item.get("id")
        if not item_id:
            continue
        entry = {f: item.get(f) for f in legacy_runtime_fields}
        # Initialise the new fields the file split introduces.
        entry["retry_count"] = 0
        entry["last_fetch_attempt"] = None
        entry["needs_script_digested"] = False
        state["items"][item_id] = entry
        n += 1

    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, STATE_PATH)
    print(f"Migrated {n} items to trial_queue_state.json")
    return state


def load_queue() -> dict:
    """Read definitions + runtime state, merge, return the queue dict.

    Definitions live in QUEUE_PATH (Mac-side, committed). Runtime state
    lives in STATE_PATH (PC-side, gitignored). The orchestrator and
    queue_admin both go through this merged view.
    """
    if not QUEUE_PATH.exists():
        return {
            "schema_version": 1,
            "last_digest_sent_at": None,
            "last_run_at": None,
            "queue": [],
        }
    text = QUEUE_PATH.read_text(encoding="utf-8").strip()
    if not text:
        definitions: dict = {"schema_version": 1, "queue": []}
    else:
        try:
            definitions = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Failed to parse {QUEUE_PATH}: {exc}. "
                "Fix the JSON manually before re-running."
            ) from exc

    state = _load_or_migrate_state(definitions)
    state_items = state.get("items", {})
    merged_queue = [
        _merge_item(item, state_items.get(item.get("id")))
        for item in definitions.get("queue", [])
    ]
    return {
        "schema_version": 1,
        "last_digest_sent_at": state.get("last_digest_sent_at"),
        "last_run_at": state.get("last_run_at"),
        "queue": merged_queue,
    }


def save_queue(data: dict) -> None:
    """Atomic write to STATE_PATH only. NEVER writes to QUEUE_PATH.

    Definitions are Mac-authored and committed; the orchestrator only
    touches the gitignored runtime-state file so a `git pull` on PC
    never produces a merge conflict on top of mid-batch state writes.
    """
    state = {
        "schema_version": 1,
        "last_digest_sent_at": data.get("last_digest_sent_at"),
        "last_run_at": data.get("last_run_at"),
        "items": {},
    }
    state_fields = tuple(_RUNTIME_DEFAULTS.keys())
    for item in data.get("queue", []):
        item_id = item.get("id")
        if not item_id:
            continue
        state["items"][item_id] = {f: item.get(f) for f in state_fields}
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, STATE_PATH)


def _pid_is_alive(pid: int) -> bool:
    """True iff `pid` is currently a live process.

    Uses psutil when importable (handles platform quirks); otherwise
    falls back to `os.kill(pid, 0)`. On POSIX, dead pids raise
    ProcessLookupError; PermissionError means the process exists but
    is unsignalable by the caller (still alive). On Windows, dead
    pids raise a generic OSError (errno 87 / ERROR_INVALID_PARAMETER).
    """
    if pid <= 0:
        return False
    try:
        import psutil  # type: ignore[import-not-found]
        return bool(psutil.pid_exists(pid))
    except ImportError:
        pass
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # POSIX: process exists, we can't signal it -> still alive.
        return True
    except OSError:
        # Windows dead-pid path (and any other unexpected error).
        # Best-effort liveness check: treat as dead.
        return False


def _maybe_release_stale_lock() -> None:
    """If LOCK_PATH exists, decide whether the recorded PID is still
    alive; if not, delete the lock file and print a one-line warning.

    A force-kill (SIGKILL / Stop-Process -Force) bypasses the finally
    block in release_lock(), leaving an orphan lock file. This helper
    detects that case so the operator does not have to remove it by
    hand. Live PIDs are left untouched -- the existing
    "Another instance is running" path in acquire_lock() handles them.
    """
    if not LOCK_PATH.exists():
        return
    try:
        raw = LOCK_PATH.read_text(encoding="utf-8").strip()
    except OSError as exc:
        try:
            LOCK_PATH.unlink(missing_ok=True)
        except OSError:
            pass
        print(
            f"[lock] stale lock from unreadable file ({exc}) "
            "-- auto-released",
            file=sys.stderr,
        )
        return
    try:
        pid = int(raw)
    except (TypeError, ValueError):
        try:
            LOCK_PATH.unlink(missing_ok=True)
        except OSError:
            pass
        print(
            f"[lock] stale lock with invalid PID {raw!r} "
            "-- auto-released",
            file=sys.stderr,
        )
        return
    if _pid_is_alive(pid):
        return  # Real lock; acquire_lock will surface "another instance".
    try:
        LOCK_PATH.unlink(missing_ok=True)
    except OSError as exc:
        # If unlink fails (Windows non-shared-delete on a process that
        # JUST died but whose handle hasn't been reaped yet), let the
        # subsequent acquire_lock attempt fall through to the standard
        # "another instance" path. Surface for diagnosis.
        print(
            f"[lock] dead PID {pid} detected but unlink failed ({exc}); "
            "leaving lock file in place",
            file=sys.stderr,
        )
        return
    print(
        f"[lock] stale lock from dead PID {pid} -- auto-released",
        file=sys.stderr,
    )


def acquire_lock():
    """Exclusive non-blocking lock; sys.exit(1) if another instance holds it.

    Uses msvcrt.locking on Windows (LK_NBLCK = non-blocking exclusive
    1-byte lock) and fcntl.flock on Unix (LOCK_EX | LOCK_NB). Both
    ship with Python stdlib.

    Before opening the lock file, calls `_maybe_release_stale_lock()`
    to auto-clear an orphan lock file left by a force-killed prior
    invocation. Live-PID locks are left intact and surface via the
    existing "Another instance is running" path.
    """
    _maybe_release_stale_lock()
    fd = open(LOCK_PATH, "w", encoding="utf-8")
    if sys.platform == "win32":
        try:
            msvcrt.locking(fd.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            print(
                f"Another instance is running (lock: {LOCK_PATH}). Exiting.",
                file=sys.stderr,
            )
            fd.close()
            sys.exit(1)
    else:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(
                f"Another instance is running (lock: {LOCK_PATH}). Exiting.",
                file=sys.stderr,
            )
            fd.close()
            sys.exit(1)
    fd.write(str(os.getpid()))
    fd.flush()
    return fd


def release_lock(fd) -> None:
    """Release the lock and remove the lock file. Platform-aware mirror
    of acquire_lock — msvcrt.LK_UNLCK on Windows, fcntl.LOCK_UN on Unix.
    """
    try:
        if sys.platform == "win32":
            try:
                fd.seek(0)
                msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        fd.close()
        try:
            LOCK_PATH.unlink(missing_ok=True)
        except OSError:
            pass


# ── trials.log lock (cross-platform, blocking) ──────────────────────────────

def acquire_trials_log_lock(timeout_s: float = 60.0):
    """Cross-platform exclusive lock for direct trials.log appends.

    Same fcntl/msvcrt pattern as `acquire_lock`, but BLOCKING up to
    `timeout_s` seconds: the orchestrator does not race here, it
    waits.  Returns the lock file handle; pair every acquire with
    `release_trials_log_lock(fd)` in a try/finally.

    The trial subprocesses themselves do NOT take this lock — their
    appends happen inside `backtest.logs.append_jsonl` and rely on
    POSIX O_APPEND / Windows FILE_APPEND_DATA atomicity for short
    rows.  The helper exists for any orchestrator-level direct
    append (currently none — reserved for future tooling such as
    main-process supersession tagging that must serialise with
    in-flight trial subprocesses).
    """
    fd = open(TRIALS_LOG_LOCK_PATH, "w", encoding="utf-8")
    deadline = time.time() + timeout_s
    while True:
        try:
            if sys.platform == "win32":
                msvcrt.locking(fd.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except (BlockingIOError, OSError):
            if time.time() >= deadline:
                fd.close()
                raise TimeoutError(
                    f"trials.log lock not acquired within {timeout_s}s"
                )
            time.sleep(0.05)


def release_trials_log_lock(fd) -> None:
    """Mirror of acquire_trials_log_lock; safe even on partial state."""
    try:
        if sys.platform == "win32":
            try:
                fd.seek(0)
                msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        fd.close()
        try:
            TRIALS_LOG_LOCK_PATH.unlink(missing_ok=True)
        except OSError:
            pass


# ── Summary parser ──────────────────────────────────────────────────────────

def parse_json_summary(stdout: str) -> dict | None:
    """Find sentinel '--- TRIAL SUMMARY JSON ---' in stdout, return the
    JSON object that follows.  Tolerates whitespace around sentinel,
    inline JSON on the next line, or pretty-printed JSON spanning
    multiple lines.  Returns None on any failure (never raises)."""
    lines = stdout.splitlines()
    sentinel = "--- TRIAL SUMMARY JSON ---"
    sentinel_idx = -1
    for i, line in enumerate(lines):
        if line.strip() == sentinel:
            sentinel_idx = i
            break
    if sentinel_idx < 0:
        return None

    # Skip empty lines after sentinel.
    j = sentinel_idx + 1
    while j < len(lines) and lines[j].strip() == "":
        j += 1
    if j >= len(lines):
        return None

    # Try the next non-empty line as a single-line JSON object first.
    first_attempt = lines[j]
    try:
        parsed = json.loads(first_attempt)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    # Fall back to joining all remaining lines (pretty-printed).
    blob = "\n".join(lines[j:])
    try:
        parsed = json.loads(blob)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        return None
    return None


# ── Auto-remediation helpers (Task 3) ───────────────────────────────────────

def parse_trial_error(stdout: str, stderr: str) -> dict | None:
    """Pull the structured TRIAL_ERROR_TYPE / TRIAL_ERROR_FETCH /
    TRIAL_ERROR_MSG block out of a trial script's combined output.

    Returns a dict with keys 'type', 'fetch', 'msg' (any may be None
    if the script didn't emit that line) or None if no
    TRIAL_ERROR_TYPE line is present at all.
    """
    combined = (stdout or "") + "\n" + (stderr or "")
    out: dict = {}
    for raw in combined.splitlines():
        line = raw.strip()
        if line.startswith("TRIAL_ERROR_TYPE:"):
            out["type"] = line.split(":", 1)[1].strip()
        elif line.startswith("TRIAL_ERROR_FETCH:"):
            out["fetch"] = line.split(":", 1)[1].strip()
        elif line.startswith("TRIAL_ERROR_MSG:"):
            out["msg"] = line.split(":", 1)[1].strip()
    if "type" not in out:
        return None
    out.setdefault("fetch", None)
    out.setdefault("msg", None)
    return out


def run_fetch_for_remediation(
    fetch_path_rel: str,
) -> tuple[int, str, str]:
    """Run a fetch script with a 120s timeout and return
    (returncode, stdout, stderr). returncode -1 = timeout, -2 = script
    not found, -3 = unexpected exception.
    """
    fetch_path = ROOT / fetch_path_rel
    if not fetch_path.exists():
        return (-2, "", f"fetch script not found: {fetch_path}")
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        proc = subprocess.run(
            [sys.executable, str(fetch_path)],
            capture_output=True,
            text=True,
            timeout=FETCH_REMEDIATION_TIMEOUT_S,
            cwd=str(ROOT),
            env=env,
        )
        return (proc.returncode, proc.stdout or "", proc.stderr or "")
    except subprocess.TimeoutExpired:
        return (
            -1,
            "",
            f"fetch script timed out after {FETCH_REMEDIATION_TIMEOUT_S}s",
        )
    except Exception as e:  # noqa: BLE001
        return (-3, "", f"fetch script exception: {e}")


def log_run(
    item: dict,
    returncode: int,
    summary: dict | None,
    error: str,
) -> None:
    RUN_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "id": item.get("id"),
        "strategy_id": item.get("strategy_id"),
        "variation_id": item.get("variation_id"),
        "timestamp": utcnow_iso(),
        "returncode": returncode,
        "verdict": summary.get("verdict") if summary else None,
        "error_snippet": error[:200] if error else None,
    }
    with RUN_LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


# ── Worker (ProcessPoolExecutor target) ─────────────────────────────────────

def _worker_run_trial(
    item: dict,
    root_str: str,
    timeout_s: int,
    dry_run: bool,
) -> dict:
    """Top-level (picklable) worker body: runs ONE trial subprocess.

    Writes `backtest/cache/trial_result_<item_id>.json` via tmp+rename
    on every code path. Stdout/stderr is prefixed with `[<item_id>]`
    so parallel output streams are distinguishable in the run log.
    PYTHONIOENCODING=utf-8 is forced into the subprocess env so the
    trial script's `--- TRIAL SUMMARY JSON ---` sentinel survives the
    Windows cp1252 default.
    """
    # Re-import everything explicitly: this function executes inside
    # ProcessPoolExecutor's "spawn"-started worker on macOS/Windows.
    import json as _json
    import os as _os
    import subprocess as _sp
    import sys as _sys
    from datetime import datetime as _dt
    from datetime import timezone as _tz
    from pathlib import Path as _P

    root = _P(root_str)
    item_id = str(item.get("id", "?"))
    prefix = f"[{item_id}] "

    def _emit(text: str, *, err: bool = False) -> None:
        stream = _sys.stderr if err else _sys.stdout
        if not text:
            return
        for line in text.splitlines() or [""]:
            print(f"{prefix}{line}", file=stream, flush=True)

    started_at = _dt.now(_tz.utc).isoformat()
    cache_dir = root / "backtest" / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    result_path = cache_dir / f"trial_result_{item_id}.json"

    if dry_run:
        _emit(f"would run: {item.get('script_path')} (parallel)")
        result = {
            "id": item_id,
            "returncode": 0,
            "stdout": '--- TRIAL SUMMARY JSON ---\n{"verdict":"dry-run"}',
            "stderr": "",
            "started_at": started_at,
            "finished_at": _dt.now(_tz.utc).isoformat(),
        }
    else:
        script = root / item["script_path"]
        if not script.exists():
            _emit(f"ERROR: script not found: {script}", err=True)
            result = {
                "id": item_id,
                "returncode": -2,
                "stdout": "",
                "stderr": f"script not found: {script}",
                "started_at": started_at,
                "finished_at": _dt.now(_tz.utc).isoformat(),
            }
        else:
            env = _os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"

            # Task 2: pre-run structural gate. When the manifest's
            # strategy_warmup_candles is significantly smaller than
            # the cpcv harness's _ENGINE_WARM_UP_CANDLES default (50)
            # AND the resulting per-block tradeable budget would
            # collapse below the 30-trade verdict-tree floor, inject
            # TRIAL_WARM_UP_CANDLES into the subprocess env so the
            # trial script's run_cpcv call uses the manifest warmup
            # instead of the harness default. This unblocks short-
            # window strategies (e.g. sq-012 VolumeWeightedTSMOM with
            # warmup=22) without changing any callers' default.
            try:
                import json as _json2
                manifest_path = root / "backtest" / "holdout_manifest.json"
                manifest = _json2.loads(manifest_path.read_text(encoding="utf-8"))
                strategy_id = item.get("strategy_id") or ""
                entry = manifest.get(strategy_id) or {}
                strategy_warmup = int(entry.get(
                    "strategy_warmup_candles", 50,
                ))
                # Estimate dev bars from manifest dates + timeframe.
                ds = entry.get("data_start"); de = entry.get("dev_end")
                tf = (entry.get("timeframe") or "1d").lower()
                tf_hours = {"1h": 1, "4h": 4, "1d": 24}.get(tf, 24)
                dev_bars = 0
                if ds and de:
                    import datetime as _dt2
                    ds_dt = _dt2.datetime.fromisoformat(ds)
                    de_dt = _dt2.datetime.fromisoformat(de)
                    span_h = (de_dt - ds_dt).total_seconds() / 3600.0
                    dev_bars = int(span_h / tf_hours)
                n_blocks = int(item.get("n_blocks", 10))
                if dev_bars > 0 and n_blocks > 0:
                    block_size = dev_bars // n_blocks
                    tradeable_global = block_size - 50
                    tradeable_safe = block_size - strategy_warmup
                    if tradeable_global < 30 and tradeable_safe >= 30:
                        env["TRIAL_WARM_UP_CANDLES"] = str(strategy_warmup)
                        _emit(
                            f"[pre-run gate] {item_id}: block={block_size} "
                            f"global_tradeable={tradeable_global} "
                            f"manifest_tradeable={tradeable_safe} "
                            f"-> inject TRIAL_WARM_UP_CANDLES={strategy_warmup}"
                        )
            except Exception as _gate_exc:  # noqa: BLE001
                _emit(
                    f"[pre-run gate] {item_id}: skipped ({_gate_exc.__class__.__name__}: {_gate_exc})",
                )

            _emit(f"starting trial subprocess: {script}")
            try:
                proc = _sp.run(
                    [_sys.executable, str(script)],
                    capture_output=True,
                    text=True,
                    timeout=timeout_s,
                    cwd=str(root),
                    env=env,
                )
                stdout = proc.stdout or ""
                stderr = proc.stderr or ""
                returncode = proc.returncode
            except _sp.TimeoutExpired as e:
                stdout = e.stdout if isinstance(e.stdout, str) else ""
                stderr = f"TIMEOUT after {timeout_s}s"
                returncode = -1
            except Exception as e:  # noqa: BLE001 — orchestrator catch-all
                stdout = ""
                stderr = f"subprocess exception: {e}"
                returncode = -3

            if stdout:
                _emit(stdout)
            if stderr:
                _emit(stderr, err=True)

            result = {
                "id": item_id,
                "returncode": returncode,
                # cap result-file blob sizes; full output is already
                # streamed to the operator's terminal via _emit above.
                "stdout": stdout[-200_000:],
                "stderr": stderr[-20_000:],
                "started_at": started_at,
                "finished_at": _dt.now(_tz.utc).isoformat(),
            }

    tmp = result_path.with_suffix(".json.tmp")
    tmp.write_text(_json.dumps(result, indent=2), encoding="utf-8")
    _os.replace(tmp, result_path)
    _emit(f"finished: returncode={result['returncode']}")
    return result


# ── Doc updates ─────────────────────────────────────────────────────────────

def _phase_label(item: dict) -> str:
    vid = (item.get("variation_id") or "").lower()
    if "4b" in vid:
        return "Phase 4.B outcomes"
    if "4a" in vid:
        return "Phase 4.A outcomes"
    return "Phase 4 outcomes"


def _bullet(item: dict, summary: dict, today: str) -> str:
    sr = summary.get("sr_observed", float("nan"))
    bsr = summary.get("baseline_sharpe_at_eval", float("nan"))
    dsr = summary.get("dsr_validation", float("nan"))
    n = summary.get("n_trades_total", 0)
    mt = summary.get("mt_mean_pass", "?")
    bl = summary.get("baseline_pass", "?")
    verdict = summary.get("verdict", "unknown")
    try:
        sr_s = f"{float(sr):.4f}"
    except (TypeError, ValueError):
        sr_s = str(sr)
    try:
        bsr_s = f"{float(bsr):.4f}"
    except (TypeError, ValueError):
        bsr_s = str(bsr)
    try:
        dsr_s = f"{float(dsr):.4f}"
    except (TypeError, ValueError):
        dsr_s = str(dsr)
    return (
        f"- **{item['variation_id']} ({today}):** verdict={verdict}. "
        f"sr={sr_s}, baseline_sr={bsr_s}, dsr={dsr_s}, "
        f"n_trades={n}, mt_mean_pass={mt}, baseline_pass={bl}."
    )


def update_strategies_md(item: dict, summary: dict) -> None:
    path = ROOT / "docs" / "strategies.md"
    if not path.exists():
        print(f"[queue] strategies.md missing at {path}; skipping update")
        return
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()

    today = utcnow_iso()[:10]
    bullet = _bullet(item, summary, today)
    phase_label = _phase_label(item)
    section_header_re = re.compile(
        r"^### " + re.escape(item["strategy_id"]) + r"(\s|$)"
    )

    # Locate the strategy section.
    section_start = -1
    for i, line in enumerate(lines):
        if section_header_re.match(line):
            section_start = i
            break

    if section_start < 0:
        # Append a new minimal section at end of file.
        new_block = [
            "",
            f"### {item['strategy_id']}",
            "",
            f"#### {phase_label}",
            "",
            bullet,
            "",
            "---",
        ]
        lines.extend(new_block)
        text_out = "\n".join(lines) + "\n"
    else:
        # Find the section's end (next "### " or end of file).
        section_end = len(lines)
        for j in range(section_start + 1, len(lines)):
            if lines[j].startswith("### "):
                section_end = j
                break

        # Look for the phase-label subsection within this section.
        phase_header = f"#### {phase_label}"
        phase_start = -1
        for j in range(section_start + 1, section_end):
            if lines[j].strip() == phase_header:
                phase_start = j
                break

        if phase_start >= 0:
            # Find the last bullet in this subsection (until the next
            # header or `---`).
            sub_end = section_end
            for j in range(phase_start + 1, section_end):
                stripped = lines[j].strip()
                if stripped.startswith("#### ") or stripped.startswith("### "):
                    sub_end = j
                    break
                if stripped.startswith("---"):
                    sub_end = j
                    break
            # Insert just before the first non-bullet line we find
            # AFTER the last bullet.  Walk back from sub_end to find
            # the last bullet line.
            insert_at = sub_end
            for j in range(sub_end - 1, phase_start, -1):
                if lines[j].strip().startswith("- "):
                    insert_at = j + 1
                    break
            lines.insert(insert_at, bullet)
        else:
            # Insert subsection + first bullet immediately before the
            # `---` separator or before the next `### ` header.
            insert_at = section_end
            for j in range(section_start + 1, section_end):
                if lines[j].strip().startswith("---"):
                    insert_at = j
                    break
            new_block = [
                f"#### {phase_label}",
                "",
                bullet,
                "",
            ]
            for offset, l in enumerate(new_block):
                lines.insert(insert_at + offset, l)

        text_out = "\n".join(lines) + ("\n" if text.endswith("\n") else "")

    # Update the "Last updated:" line at the top.
    out_lines = text_out.splitlines()
    for i in range(min(10, len(out_lines))):
        if out_lines[i].startswith("Last updated:"):
            out_lines[i] = f"Last updated: {today}"
            break
    text_out = "\n".join(out_lines) + (
        "\n" if text_out.endswith("\n") and not text_out.endswith("\n\n") else "\n"
    )
    path.write_text(text_out, encoding="utf-8")


def update_literature_doc(item: dict, summary: dict) -> None:
    rel = item.get("literature_doc")
    if not rel:
        return
    path = ROOT / rel
    if not path.exists():
        print(
            f"[queue] WARNING: literature_doc {rel} does not exist; "
            "skipping (literature files are human-authored — create "
            "the file by hand before queueing the trial)."
        )
        return

    today = utcnow_iso()[:10]
    sr = summary.get("sr_observed", float("nan"))
    dsr = summary.get("dsr_validation", float("nan"))
    n = summary.get("n_trades_total", 0)
    verdict = summary.get("verdict", "unknown")
    try:
        sr_s = f"{float(sr):.4f}"
    except (TypeError, ValueError):
        sr_s = str(sr)
    try:
        dsr_s = f"{float(dsr):.4f}"
    except (TypeError, ValueError):
        dsr_s = str(dsr)

    new_row = (
        f"| {item['variation_id']} | {today} | {verdict} | "
        f"{sr_s} | {dsr_s} | {n} |"
    )

    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()

    section_header_re = re.compile(r"^##\s+(Trial outcomes|Results)\s*$")
    section_idx = -1
    for i, line in enumerate(lines):
        if section_header_re.match(line.strip()):
            section_idx = i
            break

    if section_idx < 0:
        # Append a fresh section + table.
        block = [
            "",
            "## Trial outcomes",
            "",
            "| variation_id | date | verdict | sr_observed | dsr | n_trades |",
            "|---|---|---|---|---|---|",
            new_row,
            "",
        ]
        out = (text.rstrip("\n") + "\n" + "\n".join(block) + "\n")
        path.write_text(out, encoding="utf-8")
        return

    # Find the table inside the section: scan for header row followed by
    # the divider line, append new_row after the last data row.
    section_end = len(lines)
    for j in range(section_idx + 1, len(lines)):
        if lines[j].startswith("## ") and j != section_idx:
            section_end = j
            break

    header_idx = -1
    divider_idx = -1
    for j in range(section_idx + 1, section_end):
        if lines[j].strip().startswith("| variation_id"):
            header_idx = j
            if j + 1 < section_end and lines[j + 1].strip().startswith("|---"):
                divider_idx = j + 1
            break

    if header_idx < 0 or divider_idx < 0:
        # Section exists but no recognisable table; append a fresh table.
        insert_at = section_end
        block = [
            "",
            "| variation_id | date | verdict | sr_observed | dsr | n_trades |",
            "|---|---|---|---|---|---|",
            new_row,
            "",
        ]
        for offset, line in enumerate(block):
            lines.insert(insert_at + offset, line)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return

    # Locate the first non-data line after the divider.
    insert_at = section_end
    for j in range(divider_idx + 1, section_end):
        stripped = lines[j].strip()
        if not stripped.startswith("|"):
            insert_at = j
            break
    lines.insert(insert_at, new_row)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ── Commit ──────────────────────────────────────────────────────────────────

def _staged_files() -> list[str]:
    proc = subprocess.run(
        ["git", "diff", "--name-only", "--cached"],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    return [l.strip() for l in proc.stdout.splitlines() if l.strip()]


def check_commit_scope() -> bool:
    """True iff every staged file is on the orchestrator's allow-list."""
    permitted_exact = {
        "backtest/trials.log",
        "docs/strategies.md",
        "backtest/trial_queue.json",
    }

    def is_permitted(path: str) -> bool:
        if path in permitted_exact:
            return True
        if path.startswith("research/") and path.endswith(".md"):
            return True
        return False

    staged = _staged_files()
    violations = [p for p in staged if not is_permitted(p)]
    if violations:
        print(
            f"SCOPE VIOLATION: staged files outside permitted list: "
            f"{violations}",
            file=sys.stderr,
        )
        subprocess.run(["git", "reset", "HEAD"], cwd=str(ROOT))
        return False
    return True


# CITATION: status-commit helper
def _commit_status_update(items: list[dict], label: str, dry_run: bool = False) -> None:
    """Commit the trial_queue.json status field update so a sibling
    machine's `git pull` cannot conflict on the queue file mid-trial.

    Authorized by CLAUDE.md "Trial queue orchestrator exception":
    `backtest/trial_queue.json` (status field update only) is on the
    orchestrator's auto-commit allow-list.

    Architectural note: the per-item `_commit_status_update(item, label)`
    spec referenced a pre-rewrite `process_item()` flow. The current
    `run_batch()` flips an entire batch to running in ONE `save_queue`,
    so this helper accepts the full batch and produces ONE commit
    covering every id. A per-item loop would only succeed for the first
    call (the file is fully committed after that) and would generate
    empty-stage no-ops for the rest -- not what the spec wants.

    Behaviour:
      1. Stages ONLY `backtest/trial_queue.json` via `git add`.
      2. Verifies `git diff --name-only --cached` lists exactly that
         one path; on any other staged path, unstages everything and
         raises RuntimeError (same scope-guard pattern as commit_result).
      3. Runs `git commit --message <msg>` with the per-spec subject
         line (`trials: status update <ids> -> <label>`) and a body
         enumerating per-item id / strategy_id / variation_id.
      4. On git add or git commit non-zero return: prints a warning to
         stderr and returns. A failed status commit is not fatal -- the
         trial still runs; the conflict (if any) just recurs.

    `dry_run=True` mirrors `commit_result`: log the intended commit
    and return without touching git.
    """
    if not items:
        return
    if dry_run:
        ids = ", ".join(str(it.get("id")) for it in items)
        print(f"[dry-run] would commit status update for [{ids}] -> {label}")
        return

    queue_rel = "backtest/trial_queue.json"
    state_rel = "backtest/trial_queue_state.json"
    permitted = {queue_rel, state_rel}

    # 1. Stage the queue file. The orchestrator no longer mutates
    #    trial_queue.json directly (status flips go to the gitignored
    #    trial_queue_state.json instead), so this `git add` is usually
    #    a no-op; we keep the call so any Mac-side definitions edit
    #    that the operator forgot to stage gets picked up too.
    proc_add = subprocess.run(
        ["git", "add", queue_rel],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    if proc_add.returncode != 0:
        print(
            f"[queue] _commit_status_update: git add failed "
            f"(non-fatal): {proc_add.stderr.strip()}",
            file=sys.stderr,
        )
        return

    # 2. Scope guard. trial_queue_state.json is gitignored (file-split
    #    design): the orchestrator's status writes go there, and `git
    #    add` cannot stage it. So in steady state `staged` is empty --
    #    treat that as "nothing to commit" and silently return rather
    #    than logging a false-positive scope violation on every run.
    #    Anything outside the permitted set (queue.json, state.json)
    #    is still a real violation and raises.
    staged = _staged_files()
    if not staged:
        # Nothing changed to commit; the status flip already lives in
        # the gitignored state file. No-op (intentionally silent).
        return
    extras = set(staged) - permitted
    if extras:
        # Unstage everything before raising so the working tree is clean.
        subprocess.run(["git", "reset", "HEAD"], cwd=str(ROOT))
        raise RuntimeError(
            f"_commit_status_update scope violation: staged {staged}; "
            f"only {sorted(permitted)} permitted."
        )

    # 3. Build the message. Subject line follows the spec format,
    #    extended for the batch case.
    if len(items) == 1:
        item = items[0]
        subject = (
            f"trials: status update {item.get('id')} "
            f"{item.get('strategy_id')} -> {label}"
        )
    else:
        ids = ", ".join(str(it.get("id")) for it in items)
        subject = f"trials: status update [{ids}] -> {label}"

    body_lines = [subject, "", "Per-item:"]
    for it in items:
        body_lines.append(
            f"  - {it.get('id')}: {it.get('strategy_id')} / "
            f"{it.get('variation_id')}"
        )
    body_lines += [
        "",
        "Orchestrator auto-commit per CLAUDE.md "
        "Trial queue orchestrator exception (status field update only).",
    ]
    msg = "\n".join(body_lines)

    proc_commit = subprocess.run(
        ["git", "commit", "--message", msg],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    if proc_commit.returncode != 0:
        print(
            f"[queue] _commit_status_update: git commit failed "
            f"(non-fatal): {proc_commit.stderr.strip()}",
            file=sys.stderr,
        )


# ── Queue write-back (Task 2) ──────────────────────────────────────────────
#
# update_queue_item atomically reads backtest/trial_queue.json, patches
# the matching entry's runtime fields (status, verdict, trial_id,
# finished_at, error), and writes back via tmp+os.replace. This closes
# the gap where the orchestrator was previously only writing to
# trial_queue_state.json (gitignored) -- the definitions file would
# drift, surfacing as the status_drift violations validate_queue.py
# now flags.
#
# verdict_to_status maps verdict-tree outputs to the queue-level status
# enum. CPCVError shortcuts go through the post-trial classifier
# (_classify_cpcv_error) which sets status="needs_rerun" directly when
# structural; this mapping handles only the success and ordinary-retire
# paths.

_VERDICT_TO_STATUS = {
    "keep": "done",
    "retire": "retired",
    "under_tested": "under_tested",
    "dry-run": "done",
}


def verdict_to_status(verdict: str | None) -> str:
    """Map verdict-tree output to the trial_queue.json status enum.

    Unknown / null verdicts fall back to "done" so the queue is never
    left at "running" after a parsed summary; the verdict field carries
    the actual outcome regardless of mapping.
    """
    if not isinstance(verdict, str):
        return "done"
    return _VERDICT_TO_STATUS.get(verdict, "done")


def update_queue_item(sq_id: str, fields: dict) -> bool:
    """Atomically patch trial_queue.json's entry matching `sq_id`.

    Reads the definitions JSON, finds the entry, applies `fields`
    (only keys present in the patch are written), serialises with
    indent=2, and writes back via tmp+os.replace. Skipped silently
    when sq_id is not found (returns False).

    This is the prospective fix for status_drift -- the validate_queue
    check that surfaced 8 retroactive cases. After this lands, every
    completed trial writes back to definitions in lockstep with the
    state file.

    Returns True on a successful write, False on missing id or any
    JSON / IO failure.
    """
    if not isinstance(sq_id, str) or not sq_id:
        return False
    try:
        text = QUEUE_PATH.read_text(encoding="utf-8")
        data = json.loads(text)
    except (OSError, json.JSONDecodeError) as exc:
        print(
            f"[queue] update_queue_item({sq_id}): read failed: "
            f"{exc.__class__.__name__}: {exc}",
            file=sys.stderr,
        )
        return False
    queue = data.get("queue") if isinstance(data, dict) else None
    if not isinstance(queue, list):
        return False
    target = None
    for it in queue:
        if isinstance(it, dict) and it.get("id") == sq_id:
            target = it
            break
    if target is None:
        return False
    # Apply patch -- only the keys explicitly present in `fields`.
    for k, v in fields.items():
        target[k] = v
    tmp = QUEUE_PATH.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, QUEUE_PATH)
        return True
    except OSError as exc:
        print(
            f"[queue] update_queue_item({sq_id}): write failed: {exc}",
            file=sys.stderr,
        )
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def commit_result(item: dict, summary: dict, dry_run: bool) -> bool:
    if dry_run:
        print(f"[dry-run] would commit for item {item['id']}")
        return True
    files_to_stage = [
        "backtest/trials.log",
        "docs/strategies.md",
        "backtest/trial_queue.json",
    ]
    if item.get("literature_doc"):
        files_to_stage.append(item["literature_doc"])
    for f in files_to_stage:
        p = ROOT / f
        if p.exists():
            subprocess.run(["git", "add", str(p)], cwd=str(ROOT))
    if not check_commit_scope():
        return False
    # `git commit` with nothing newly staged (e.g. trial #2 in a parallel
    # batch where trial #1 already captured the trials.log rows) is a
    # no-op we treat as success — the row is still recorded in the
    # earlier commit and the queue state is honest.
    if not _staged_files():
        print(
            f"[queue] nothing new to commit for item {item['id']} "
            "(an earlier batch commit captured its trials.log row)"
        )
        return True
    verdict = summary.get("verdict", "unknown")
    sr = summary.get("sr_observed", float("nan"))
    dsr = summary.get("dsr_validation", float("nan"))
    n = summary.get("n_trades_total", 0)
    mt = summary.get("mt_mean_pass", "?")
    bl = summary.get("baseline_pass", "?")
    try:
        sr_s = f"{float(sr):.4f}"
    except (TypeError, ValueError):
        sr_s = str(sr)
    try:
        dsr_s = f"{float(dsr):.4f}"
    except (TypeError, ValueError):
        dsr_s = str(dsr)
    msg = (
        f"trials: {item['strategy_id']} {item['variation_id']} {verdict}\n\n"
        f"sr_observed={sr_s}  dsr={dsr_s}  n_trades={n}\n"
        f"mt_mean_pass={mt}  baseline_pass={bl}\n\n"
        f"Orchestrator auto-commit per CLAUDE.md trial-queue exception.\n"
        f"Item id: {item['id']}  script: {item['script_path']}"
    )
    proc = subprocess.run(
        ["git", "commit", "--message", msg],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    if proc.returncode != 0:
        print(f"git commit failed: {proc.stderr}", file=sys.stderr)
        return False
    return True


# ── Email ───────────────────────────────────────────────────────────────────
# Email policy (revised 2026-05-06, file split + digest):
#   - KEEP verdict           → single email immediately (action required)
#   - everything else        → accumulated into an 8h digest email
#                               (done / error / deferred_no_data /
#                                needs_trial_script items)
#   - 24h heartbeat fallback → minimal heartbeat-only digest if no
#                               digestable activity for a full day
#   - proposal-agent failure → one email with last 500 chars of stderr
# All Resend POSTs route through `_send_email`.


def _send_email(subject: str, body: str, dry_run: bool) -> None:
    """Low-level Resend POST.  Caller owns the subject/body shape."""
    if dry_run:
        print(f"[dry-run] would email: {subject}")
        return
    if not RESEND_API_KEY or not EMAIL_TO:
        print(
            "RESEND_API_KEY or TRIAL_QUEUE_EMAIL_TO not set; skipping email"
        )
        return
    time.sleep(EMAIL_RATE_LIMIT_SLEEP_S)
    payload = {
        "from": RESEND_FROM,
        "to": [EMAIL_TO],
        "subject": subject,
        "text": body,
    }
    try:
        import requests  # local import: only orchestrator-loop needs it

        resp = requests.post(
            RESEND_API_URL,
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json",
                "User-Agent": "crypto-bot/1.0",
            },
            json=payload,
            timeout=15,
        )
        if resp.status_code not in (200, 201):
            print(
                f"Resend API error {resp.status_code}: {resp.text[:200]}",
                file=sys.stderr,
            )
    except Exception as e:  # noqa: BLE001 — non-fatal email failure
        print(f"Email send failed (non-fatal): {e}", file=sys.stderr)


def send_keep_email(item: dict, summary: dict, dry_run: bool) -> None:
    """Action-required email for the only non-suppressed verdict."""
    subject = (
        f"trials: [KEEP] {item['strategy_id']} "
        f"{item['variation_id']} -- action required"
    )
    body_lines = [
        f"Strategy : {item['strategy_id']}",
        f"Variation: {item['variation_id']}",
        f"Verdict  : {summary.get('verdict', 'keep')}",
        f"sr_observed           : {summary.get('sr_observed', 'n/a')}",
        f"sr_zero_expected      : {summary.get('sr_zero_expected_at_eval', 'n/a')}",
        f"baseline_sr           : {summary.get('baseline_sharpe_at_eval', 'n/a')}",
        f"dsr_validation        : {summary.get('dsr_validation', 'n/a')}",
        f"n_trades_total        : {summary.get('n_trades_total', 'n/a')}",
        f"mt_mean_pass          : {summary.get('mt_mean_pass', 'n/a')}",
        f"baseline_pass         : {summary.get('baseline_pass', 'n/a')}",
        f"trade_count_pass      : {summary.get('trade_count_pass', 'n/a')}",
        f"mintrl_pass           : {summary.get('mintrl_pass', 'n/a')}",
        "",
        f"block_sharpes: {summary.get('block_sharpes', [])}",
        f"sharpe_distribution: {summary.get('sharpe_distribution', {})}",
    ]
    if "signal_event_count" in summary:
        body_lines += [
            f"signal_event_count    : {summary['signal_event_count']}",
            f"funding_settlements   : "
            f"{summary.get('funding_settlements_processed', 'n/a')}",
            f"headline_exit_reasons : "
            f"{summary.get('headline_exit_reasons', {})}",
            f"block_exit_reasons    : "
            f"{summary.get('block_exit_reasons', {})}",
        ]
    _send_email(subject, "\n".join(body_lines), dry_run)


def _send_build_failure_email(
    item: dict, log: str, dry_run: bool = False,
) -> None:
    """Notify the human when CC auto-build for a queue item fails.

    Subject names the strategy + variation; body is the tail of the
    CC build log (capped at 1500 chars to keep email size sane).
    Routed through the existing _send_email() helper so the
    dry-run / RESEND-not-set guards apply uniformly.
    """
    subject = (
        f"build failed: {item.get('strategy_id', '?')} "
        f"{item.get('variation_id', '?')}"
    )
    body = (log or "")[:1500]
    _send_email(subject, body, dry_run)


def send_proposal_failure_email(stderr_tail: str, dry_run: bool) -> None:
    """Notify the human when the proposal agent exits non-zero."""
    subject = "trials: proposal agent failed -- manual research needed"
    body = (
        "scripts/propose_next_variation.py exited non-zero from the "
        "trial-queue empty-queue branch.\n\n"
        f"Last 500 chars of stderr:\n{(stderr_tail or '')[-500:]}"
    )
    _send_email(subject, body, dry_run)


# ── Digest email (Task 5) ───────────────────────────────────────────────────

def _seconds_since(iso_ts: str | None) -> float | None:
    """Return seconds elapsed since `iso_ts`, or None if unparseable."""
    if not iso_ts:
        return None
    try:
        dt = datetime.fromisoformat(iso_ts)
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - dt).total_seconds()


def _unreported_items(queue_data: dict) -> dict:
    """Group items eligible for the next digest by section."""
    completed: list[dict] = []
    deferred: list[dict] = []
    errors: list[dict] = []
    needs_script: list[dict] = []
    for it in queue_data.get("queue", []):
        status = it.get("status")
        email_sent = bool(it.get("email_sent"))
        if status == "done" and not email_sent:
            completed.append(it)
        elif status == "deferred_no_data" and not email_sent:
            deferred.append(it)
        elif status == "error" and not email_sent:
            errors.append(it)
        elif (
            it.get("needs_trial_script")
            and not bool(it.get("needs_script_digested"))
        ):
            needs_script.append(it)
    return {
        "completed": completed,
        "deferred": deferred,
        "errors": errors,
        "needs_script": needs_script,
    }


def _heartbeat_section(queue_data: dict) -> list[str]:
    counts = {"done": 0, "deferred_no_data": 0, "error": 0, "queued": 0}
    for it in queue_data.get("queue", []):
        s = it.get("status", "")
        if s in counts:
            counts[s] += 1
    last_run = queue_data.get("last_run_at") or "(never)"
    return [
        "PC HEARTBEAT",
        "------------",
        f"Last run: {last_run}",
        (
            f"Queue summary: {counts['done']} done, "
            f"{counts['deferred_no_data']} deferred, "
            f"{counts['error']} error, {counts['queued']} queued"
        ),
    ]


def _build_digest_body(sections: dict, queue_data: dict) -> str:
    lines: list[str] = []
    if sections["completed"]:
        lines.append("COMPLETED TRIALS")
        lines.append("----------------")
        for it in sections["completed"]:
            lines.append(
                f"{it.get('id', '?')} | {it.get('strategy_id', '?')} | "
                f"{it.get('variation_id', '?')} | "
                f"{it.get('verdict') or '?'} | "
                f"sr={it.get('sharpe', 'n/a')}"
            )
        lines.append("")
    if sections["deferred"]:
        lines.append("DEFERRED (data unavailable)")
        lines.append("---------------------------")
        for it in sections["deferred"]:
            err = (it.get("error") or "no error message")
            lines.append(
                f"{it.get('id', '?')} | {it.get('strategy_id', '?')} | "
                f"{err[:120]}"
            )
        lines.append("")
    if sections["errors"]:
        lines.append("ERRORS")
        lines.append("------")
        for it in sections["errors"]:
            err = (it.get("error") or "no error message")
            lines.append(
                f"{it.get('id', '?')} | {it.get('strategy_id', '?')} | "
                f"{err[:120]}"
            )
        lines.append("")
    if sections["needs_script"]:
        lines.append("NEEDS TRIAL SCRIPT (awaiting CC session)")
        lines.append("-----------------------------------------")
        for it in sections["needs_script"]:
            q = it.get("overall_quality", "?")
            lines.append(
                f"{it.get('id', '?')} | {it.get('strategy_id', '?')} | "
                f"{it.get('variation_id', '?')} | quality={q}"
            )
        lines.append("")
    lines.extend(_heartbeat_section(queue_data))
    return "\n".join(lines)


def maybe_send_digest(queue_data: dict, dry_run: bool) -> None:
    """Send the digest email if 8h elapsed and there is unreported
    activity. Otherwise, send a heartbeat-only digest if 24h has
    elapsed since the last digest. Updates `last_digest_sent_at` and
    flips `email_sent` / `needs_script_digested` on included items.
    """
    sections = _unreported_items(queue_data)
    has_content = any(sections.values())
    elapsed = _seconds_since(queue_data.get("last_digest_sent_at"))
    digest_due = (elapsed is None) or (elapsed >= DIGEST_INTERVAL_S)

    if digest_due and has_content:
        n = sum(len(v) for v in sections.values())
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        subject = f"trials: digest -- {n} updates [{date_str}]"
        body = _build_digest_body(sections, queue_data)
        _send_email(subject, body, dry_run)
        for it in (
            sections["completed"] + sections["deferred"] + sections["errors"]
        ):
            it["email_sent"] = True
        for it in sections["needs_script"]:
            it["needs_script_digested"] = True
        queue_data["last_digest_sent_at"] = utcnow_iso()
        save_queue(queue_data)
        return

    # 24h heartbeat fallback: previous digest exists and 24h has
    # elapsed with nothing new to report.
    if elapsed is not None and elapsed >= HEARTBEAT_INTERVAL_S:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        subject = f"trials: heartbeat -- no activity [{date_str}]"
        body = "\n".join(_heartbeat_section(queue_data))
        _send_email(subject, body, dry_run)
        queue_data["last_digest_sent_at"] = utcnow_iso()
        save_queue(queue_data)


# ── Batch runner ────────────────────────────────────────────────────────────

def _select_runnable(queue_data: dict) -> list[dict]:
    """Every queued or needs-rerun item.

    `queued`     — fresh or auto-build-flipped items.
    `needs_rerun` — items the post-trial classifier flagged as
                    structural CPCVError (warmup-vs-block mismatch);
                    the pre-run gate will inject TRIAL_WARM_UP_CANDLES
                    on the next pass before the trial subprocess
                    spawns, then this status reverts to running.

    needs_trial_script=true items are routed through the auto-build
    path in run_batch() before the trial subprocess spawns.
    """
    return [
        it for it in queue_data.get("queue", [])
        if it.get("status") in ("queued", "needs_rerun")
    ]


# Task 3: post-trial CPCVError classifier. Reads keywords from the
# error message; cross-checks the manifest's strategy_warmup_candles
# against the harness's _ENGINE_WARM_UP_CANDLES (50) to decide whether
# a manifest-aware re-run with TRIAL_WARM_UP_CANDLES would have
# cleared the tradeable-bar floor.
_CPCV_STRUCTURAL_KEYWORDS = ("warm", "block", "candle")


def _classify_cpcv_error(
    item: dict, summary: dict, cpcv_err_msg: str,
) -> str:
    """Return one of "structural" / "sparse" / "unknown".

      - "structural": message names a block-size / warmup / candle
        constraint AND the manifest's strategy_warmup_candles would
        leave the per-block tradeable budget at >= 30 trades. The
        next pass should requeue the item under the pre-run gate.
      - "sparse":     headline-run trade count was 0 (genuinely no
        signal to backtest), OR n_trades_total == 0 in the summary.
      - "unknown":    everything else; caller falls through to the
        standard retire row.
    """
    msg_lc = (cpcv_err_msg or "").lower()
    has_struct_kw = any(k in msg_lc for k in _CPCV_STRUCTURAL_KEYWORDS)

    n_trades_headline = int(summary.get("n_trades_headline") or 0)
    n_trades_total = int(summary.get("n_trades_total") or 0)
    if n_trades_headline == 0 and n_trades_total == 0 and not has_struct_kw:
        return "sparse"

    if has_struct_kw:
        try:
            import json as _json
            with open(QUEUE_PATH.parent / "holdout_manifest.json", "r", encoding="utf-8") as f:
                manifest = _json.load(f)
            entry = manifest.get(str(item.get("strategy_id") or ""), {})
            strategy_warmup = int(entry.get("strategy_warmup_candles", 50))
            ds = entry.get("data_start"); de = entry.get("dev_end")
            tf = (entry.get("timeframe") or "1d").lower()
            tf_hours = {"1h": 1, "4h": 4, "1d": 24}.get(tf, 24)
            if ds and de:
                from datetime import datetime as _dt
                span_h = (
                    _dt.fromisoformat(de) - _dt.fromisoformat(ds)
                ).total_seconds() / 3600.0
                dev_bars = int(span_h / tf_hours)
                n_blocks = int(item.get("n_blocks", 10))
                if n_blocks > 0:
                    block_size = dev_bars // n_blocks
                    tradeable_safe = block_size - strategy_warmup
                    if tradeable_safe >= 30:
                        return "structural"
        except Exception:  # noqa: BLE001
            pass

    return "unknown"


def _clear_stale_result_files(items: list[dict]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    for it in items:
        p = CACHE_DIR / f"trial_result_{it.get('id')}.json"
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


def _spawn_workers(
    items: list[dict],
    n_workers: int,
    dry_run: bool,
) -> None:
    """Run the worker function over every item, in parallel or sequential.

    --workers 1 takes the in-process path so the operator can attach a
    debugger without indirection through the Pool.
    """
    if n_workers <= 1:
        for it in items:
            try:
                _worker_run_trial(it, str(ROOT), TRIAL_TIMEOUT_S, dry_run)
            except Exception as e:  # noqa: BLE001
                print(
                    f"[{it.get('id')}] worker exception: {e}",
                    file=sys.stderr,
                )
        return

    with ProcessPoolExecutor(max_workers=n_workers) as executor:
        futures = {
            executor.submit(
                _worker_run_trial, it, str(ROOT), TRIAL_TIMEOUT_S, dry_run,
            ): it
            for it in items
        }
        for fut in as_completed(futures):
            it = futures[fut]
            try:
                fut.result()
            except Exception as e:  # noqa: BLE001
                print(
                    f"[{it.get('id')}] worker exception: {e}",
                    file=sys.stderr,
                )


def process_batch_results(
    queue_data: dict,
    batch_items: list[dict],
    dry_run: bool,
) -> int:
    """Read every per-trial result file, fold into the queue, run doc
    updates, save once, then commit each item sequentially.  Returns the
    number of items that reached `status='done'`."""
    by_id = {it.get("id"): it for it in queue_data.get("queue", [])}
    done_count = 0

    for batch_item in batch_items:
        item_id = batch_item.get("id")
        item = by_id.get(item_id)
        if item is None:
            print(
                f"[queue] item {item_id} missing from queue after batch",
                file=sys.stderr,
            )
            continue

        result_path = CACHE_DIR / f"trial_result_{item_id}.json"
        if not result_path.exists():
            item["status"] = "error"
            item["error"] = "worker did not produce result file"
            item["finished_at"] = utcnow_iso()
            log_run(item, -4, None, item["error"])
            save_queue(queue_data)
            continue

        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            item["status"] = "error"
            item["error"] = f"failed to parse result file: {e}"
            item["finished_at"] = utcnow_iso()
            log_run(item, -4, None, item["error"])
            save_queue(queue_data)
            continue

        returncode = int(result.get("returncode", -3))
        stdout = result.get("stdout", "") or ""
        stderr = result.get("stderr", "") or ""
        finished_at = result.get("finished_at") or utcnow_iso()
        summary = parse_json_summary(stdout) if stdout else None
        log_run(item, returncode, summary, stderr)

        # Task 3: post-trial CPCVError classifier. Trial scripts that
        # catch CPCVError emit a sentinel-bearing retire row with
        # "cpcv_error": "<message>" inside the TRIAL SUMMARY JSON.
        # Classify before the standard error-path so a structural
        # warmup-vs-block mismatch can be requeued for the pre-run
        # gate to handle on the next pass instead of being recorded
        # as a strategy-side retire.
        if summary is not None and isinstance(summary.get("cpcv_error"), str):
            cpcv_err_msg = str(summary.get("cpcv_error") or "")
            cls = _classify_cpcv_error(item, summary, cpcv_err_msg)
            if cls == "structural":
                item["status"] = "needs_rerun"
                item["error"] = (
                    f"CPCV structural mismatch (warmup vs block size); "
                    f"will rerun via pre-run gate. {cpcv_err_msg}"
                )[:500]
                item["finished_at"] = finished_at
                save_queue(queue_data)
                print(
                    f"[queue] STRUCTURAL CPCVError {item_id}: "
                    "queued for rerun (status=needs_rerun)"
                )
                continue
            # "sparse" or "unknown" -> fall through to the standard
            # full_cpcv retire row already recorded by the trial.

        if returncode != 0 or summary is None:
            # Auto-remediation (Task 3): parse structured TRIAL_ERROR
            # block and, if the trial is missing data, run the named
            # fetch script once. Success → requeue for next batch.
            err_block = parse_trial_error(stdout, stderr)
            if err_block:
                err_type = err_block.get("type", "")
                if err_type == "deferred_no_data":
                    item["status"] = "deferred_no_data"
                    item["error"] = (err_block.get("msg") or "")[:500]
                    item["finished_at"] = finished_at
                    save_queue(queue_data)
                    print(
                        f"[queue] DEFERRED item {item_id}: "
                        f"{(err_block.get('msg') or '')[:200]}"
                    )
                    continue
                if (
                    err_type == "missing_data"
                    and item.get("retry_count", 0) < 1
                ):
                    fetch_path = err_block.get("fetch")
                    if not fetch_path:
                        # missing_data without TRIAL_ERROR_FETCH -> error
                        pass
                    elif not (ROOT / fetch_path).exists():
                        item["status"] = "deferred_no_data"
                        item["error"] = (
                            f"fetch script not found: {fetch_path}"
                        )
                        item["finished_at"] = finished_at
                        item["last_fetch_attempt"] = utcnow_iso()
                        save_queue(queue_data)
                        continue
                    else:
                        print(
                            f"[queue] auto-remediation: running "
                            f"{fetch_path} for item {item_id}"
                        )
                        rc, fout, ferr = run_fetch_for_remediation(
                            fetch_path
                        )
                        item["last_fetch_attempt"] = utcnow_iso()
                        if rc == 0:
                            item["status"] = "queued"
                            item["started_at"] = None
                            item["finished_at"] = None
                            item["error"] = None
                            item["retry_count"] = (
                                item.get("retry_count", 0) + 1
                            )
                            save_queue(queue_data)
                            print(
                                f"[queue] fetch succeeded for "
                                f"{item_id}, requeueing for retry"
                            )
                            continue
                        # Fetch failed: deferred if its output says so
                        fetch_err = parse_trial_error(fout, ferr)
                        if (
                            fetch_err
                            and fetch_err.get("type") == "deferred_no_data"
                        ):
                            item["status"] = "deferred_no_data"
                        else:
                            item["status"] = "error"
                        item["error"] = (
                            f"fetch ({fetch_path}) rc={rc}: "
                            + (ferr or fout or "")[:300]
                        )
                        item["finished_at"] = finished_at
                        save_queue(queue_data)
                        continue

            # 429 / TooManyRequests auto-retry: stash the item for
            # RETRY_429_BACKOFF_HOURS and try again, up to
            # RETRY_429_MAX_ATTEMPTS attempts. Items that exhaust the
            # cap fall through to status="error" via the block below.
            combined_err = (stderr or "") + "\n" + (stdout or "")
            is_429 = (
                "429" in combined_err
                or "TooManyRequests" in combined_err
            )
            retry_count = int(item.get("retry_count", 0))
            if is_429 and retry_count < RETRY_429_MAX_ATTEMPTS:
                from datetime import timedelta as _td
                retry_after = (
                    datetime.now(timezone.utc)
                    + _td(hours=RETRY_429_BACKOFF_HOURS)
                ).isoformat()
                item["status"] = "retry_pending"
                item["retry_after"] = retry_after
                item["retry_count"] = retry_count + 1
                item["error"] = (
                    f"HTTP 429 / TooManyRequests; attempt "
                    f"{retry_count + 1}/{RETRY_429_MAX_ATTEMPTS}; "
                    f"retry_after={retry_after}"
                )[:500]
                item["finished_at"] = finished_at
                save_queue(queue_data)
                print(
                    f"[queue] RATE_LIMITED item {item_id}: retry_pending "
                    f"until {retry_after} (attempt "
                    f"{retry_count + 1}/{RETRY_429_MAX_ATTEMPTS})"
                )
                continue

            # Fall-through: unrecognised error type or already retried.
            reason = (
                stderr[:500] if returncode != 0
                else "sentinel not found in stdout"
            )

            # Trial auto-retry: requeue once before falling through to
            # status="error". Catches transient subprocess flakes (CI
            # noise, transient OS errors) without polluting the queue.
            run_attempts = int(item.get("run_attempts", 0))
            if run_attempts < TRIAL_AUTO_RETRY_MAX:
                item["status"] = "queued"
                item["started_at"] = None
                item["finished_at"] = None
                item["run_attempts"] = run_attempts + 1
                # Preserve the stderr tail so the digest can show the
                # transient cause if the second attempt also fails.
                item["error"] = (
                    f"transient (attempt {run_attempts + 1}/"
                    f"{TRIAL_AUTO_RETRY_MAX + 1}): {reason}"
                )[:500]
                save_queue(queue_data)
                print(
                    f"[queue] AUTO-RETRY item {item_id}: requeued "
                    f"(attempt {run_attempts + 1}/"
                    f"{TRIAL_AUTO_RETRY_MAX + 1}) | {reason[:160]}"
                )
                continue

            item["status"] = "error"
            item["error"] = reason[:500]
            item["finished_at"] = finished_at
            print(f"[queue] FAILED item {item_id}: {reason[:200]}")
            save_queue(queue_data)
            continue

        try:
            update_strategies_md(item, summary)
            update_literature_doc(item, summary)
        except Exception as e:  # noqa: BLE001
            item["status"] = "error"
            item["error"] = f"doc update failed: {str(e)[:400]}"
            item["finished_at"] = finished_at
            save_queue(queue_data)
            print(
                f"[queue] doc update failed for {item_id}: {e}",
                file=sys.stderr,
            )
            continue

        # Mark done & save BEFORE commit so the commit captures done state.
        # State file (gitignored) gets the runtime row; trial_queue.json
        # (definitions, committed) gets the prospective write-back via
        # update_queue_item -- closes the status_drift gap that
        # validate_queue.py flagged retroactively for 8 prior trials.
        item["status"] = "done"
        item["verdict"] = summary.get("verdict")
        item["finished_at"] = finished_at
        save_queue(queue_data)
        update_queue_item(
            item_id,
            {
                "status": verdict_to_status(summary.get("verdict")),
                "verdict": summary.get("verdict"),
                "trial_id": summary.get("trial_id"),
                "finished_at": finished_at,
                "error": summary.get("cpcv_error"),
                "needs_trial_script": False,
            },
        )

        if not commit_result(item, summary, dry_run):
            item["status"] = "error"
            item["error"] = "commit failed or scope violation"
            save_queue(queue_data)
            continue

        print(f"[queue] done {item_id}: verdict={item['verdict']}")
        done_count += 1

        if item["verdict"] == "keep":
            send_keep_email(item, summary, dry_run)
            item["email_sent"] = True
            save_queue(queue_data)

        try:
            result_path.unlink(missing_ok=True)
        except OSError:
            pass

    return done_count


def _has_data_blocker(item: dict) -> bool:
    """True if the item's `error` field references unavailable data
    infrastructure. The auto-scripter pass skips these.
    """
    err = item.get("error")
    if not isinstance(err, str) or not err:
        return False
    err_lc = err.lower()
    return any(kw in err_lc for kw in _DATA_BLOCKER_KEYWORDS)


def _select_peer_script(
    item: dict, queue: list[dict], manifest: dict,
) -> str | None:
    """Pick the nearest peer trial script as a template for the
    scripter agent.

    Match priority:
      1. Same engine path (single-symbol vs multi-symbol per manifest
         shape: `symbol` vs `symbols`).
      2. Same timeframe.
      3. status in {done, retired} AND needs_trial_script=false (real
         working scripts only).

    Returns repo-relative script_path or None if no peer found.
    """
    sid = item.get("strategy_id") or ""
    entry = manifest.get(sid) or {}
    is_multi = "symbols" in entry
    target_tf = entry.get("timeframe")
    candidates: list[tuple[int, str]] = []
    for peer in queue:
        if not isinstance(peer, dict):
            continue
        if peer.get("id") == item.get("id"):
            continue
        if peer.get("status") not in ("done", "retired"):
            continue
        if peer.get("needs_trial_script"):
            continue
        peer_sid = peer.get("strategy_id") or ""
        peer_entry = manifest.get(peer_sid) or {}
        peer_is_multi = "symbols" in peer_entry
        if peer_is_multi != is_multi:
            continue
        score = 1
        if peer_entry.get("timeframe") == target_tf:
            score += 10
        sp = peer.get("script_path")
        if isinstance(sp, str) and (ROOT / sp).exists():
            candidates.append((score, sp))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def _auto_scripter_pass(
    items_to_run: list[dict], dry_run: bool,
) -> list[dict]:
    """Invoke the scripter agent on items whose trial script has not
    been written and whose error field carries no data-layer blocker
    keyword. Runs BEFORE _auto_build_pass so the agent-defined path
    is preferred when available; _auto_build_pass remains as the
    cc_build_helper-based fallback for legacy invocation contexts.

    Per-item behaviour:
      - Item passes the data-blocker filter and has needs_trial_script
        true: invoke `claude -p .claude/agents/scripter.md --input <json>`
        with sq_id, queue_entry, peer_script. After the agent exits,
        re-read trial_queue.json to verify needs_trial_script flipped
        to False; if so, the item is kept in the runnable set so the
        normal trial subprocess path runs it. If not, the item is
        kept (still flagged needs_trial_script) so _auto_build_pass
        picks it up via the cc_build_helper fallback in this same
        batch.
      - dry_run=True: print the would-build line, do not invoke the
        agent, drop the still-needs-build item from the runnable set.

    Parallelism (added 2026-05-08): the subprocess.run scripter
    invocations are I/O-bound (each waits on a `claude` CLI to
    return) so the orchestrator now runs up to
    SCRIPTER_PASS_MAX_PARALLEL=4 invocations concurrently via
    ThreadPoolExecutor.  Pre-flight (skip-cap / data-blocker / peer-
    select) and post-flight (re-read queue, fold results, save once)
    remain sequential so trial_queue.json writes never race.

    The agent invocation pattern matches the prompt's spec:
      claude -p ".claude/agents/scripter.md" --input '<json>'

    Returns the (potentially shrunk) list of items ready for the
    trial subprocess pool.
    """
    if not items_to_run:
        return items_to_run
    needs_build = [
        it for it in items_to_run if it.get("needs_trial_script")
    ]
    if not needs_build:
        return items_to_run

    queue_data = load_queue()
    queue_full = queue_data.get("queue", [])
    by_id = {q.get("id"): q for q in queue_full}
    try:
        manifest_text = (ROOT / "backtest" / "holdout_manifest.json").read_text(encoding="utf-8")
        manifest = json.loads(manifest_text)
    except (OSError, json.JSONDecodeError):
        manifest = {}

    # Pre-flight: classify each item. "invoke" items are batched for
    # the parallel ThreadPoolExecutor; skip cases are resolved
    # synchronously here so the parallel pool sees only real work.
    ready: list[dict] = []
    invoke_targets: list[dict] = []
    for it in items_to_run:
        if not it.get("needs_trial_script"):
            ready.append(it)
            continue
        item_id = it.get("id")
        live = by_id.get(item_id)
        prior_attempts = int((live or {}).get("scripter_attempts", 0) or 0)
        if prior_attempts >= MAX_SCRIPTER_AGENT_ATTEMPTS:
            print(
                f"[queue] auto-scripter: SKIP {item_id} -- "
                f"scripter_attempts={prior_attempts} >= cap "
                f"{MAX_SCRIPTER_AGENT_ATTEMPTS}; cc_build_helper "
                "fallback (_auto_build_pass) will handle",
            )
            ready.append(it)  # KEEP item; build_pass will run cc_build_helper
            continue
        if _has_data_blocker(it):
            print(
                f"[queue] auto-scripter: SKIP {item_id} -- data-layer "
                f"blocker in error field; cc_build_helper fallback "
                "(_auto_build_pass) will handle if applicable",
            )
            continue
        if dry_run:
            print(
                f"[dry-run] auto-scripter would invoke for {item_id} "
                f"({it.get('strategy_id')!r} / "
                f"{it.get('variation_id')!r})",
            )
            continue
        peer_script = _select_peer_script(it, queue_full, manifest)
        if not peer_script:
            print(
                f"[queue] auto-scripter: SKIP {item_id} -- no peer "
                "script available as template; cc_build_helper "
                "fallback will handle",
            )
            continue
        agent_input = json.dumps({
            "sq_id": item_id,
            "queue_entry": it,
            "peer_script": peer_script,
        })
        invoke_targets.append({
            "item": it,
            "item_id": item_id,
            "agent_input": agent_input,
            "prior_attempts": prior_attempts,
            "peer_script": peer_script,
        })

    if not invoke_targets:
        return ready

    # Parallel invocation pass.  Each thread runs subprocess.run for
    # exactly one item; no shared state mutation in the worker so
    # threads never race.  I/O-bound (each waits on the `claude` CLI)
    # so threads suffice; capped by SCRIPTER_PASS_MAX_PARALLEL.
    def _invoke(target: dict) -> tuple:
        _item_id = target["item_id"]
        cmd = [
            "claude", "-p", ".claude/agents/scripter.md",
            "--input", target["agent_input"],
        ]
        print(
            f"[queue] auto-scripter: invoking for {_item_id} "
            f"(peer={target['peer_script']})",
        )
        env = os.environ.copy()
        env.pop("ANTHROPIC_API_KEY", None)  # use OAuth, not API key
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=SCRIPTER_AGENT_TIMEOUT_S,
                cwd=str(ROOT), env=env,
            )
            return (_item_id, proc.returncode, "")
        except subprocess.TimeoutExpired:
            return (_item_id, -1, f"TIMEOUT after {SCRIPTER_AGENT_TIMEOUT_S}s")
        except FileNotFoundError:
            return (_item_id, -2, "claude CLI not found on PATH")
        except Exception as exc:  # noqa: BLE001
            return (_item_id, -3, f"{exc.__class__.__name__}: {exc}")

    n_workers = min(len(invoke_targets), SCRIPTER_PASS_MAX_PARALLEL)
    print(
        f"[queue] auto-scripter: parallel invocation -- "
        f"{len(invoke_targets)} target(s), {n_workers} worker(s)"
    )
    invoke_results: dict = {}
    with ThreadPoolExecutor(max_workers=n_workers) as executor:
        futures = {
            executor.submit(_invoke, t): t for t in invoke_targets
        }
        for fut in as_completed(futures):
            t = futures[fut]
            try:
                _id, rc, err = fut.result()
            except Exception as exc:  # noqa: BLE001
                _id = t["item_id"]
                rc = -4
                err = f"future exception: {exc.__class__.__name__}: {exc}"
            invoke_results[_id] = (rc, err)
            if rc < 0:
                print(
                    f"[queue] auto-scripter: {_id} subprocess failure "
                    f"rc={rc}; {err}",
                    file=sys.stderr,
                )

    # Post-flight: re-read trial_queue.json once.  The scripter agent
    # commits its own queue update via heredoc so the flip from
    # needs_trial_script=True to False is on disk by now.  Fold the
    # invoke_results back into queue_data, then save once.
    try:
        verify = json.loads(QUEUE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        verify = {"queue": []}
    verify_by_id = {q.get("id"): q for q in verify.get("queue", [])}

    # Reload local queue_data after the scripter's own commits to
    # avoid clobbering the scripter's own writes when we save below.
    queue_data = load_queue()
    by_id_post = {q.get("id"): q for q in queue_data.get("queue", [])}

    for target in invoke_targets:
        item_id = target["item_id"]
        it = target["item"]
        prior_attempts = target["prior_attempts"]
        live_post = by_id_post.get(item_id)
        rc, err = invoke_results.get(item_id, (-5, "no result"))

        verify_item = verify_by_id.get(item_id)
        still_needs = bool(verify_item and verify_item.get("needs_trial_script"))

        if rc == 0 and not still_needs:
            print(f"[queue] auto-scripter: BUILD_OK for {item_id}")
            it["needs_trial_script"] = False
            if live_post is not None:
                live_post["scripter_attempts"] = prior_attempts + 1
            ready.append(it)
        else:
            # Either subprocess returned non-zero OR scripter exited
            # but never flipped needs_trial_script.  Persist the failed
            # attempt counter so the next outer-loop iteration sees
            # the cap and routes to cc_build_helper.  KEEP the item in
            # `ready` so _auto_build_pass picks it up in this same
            # batch (per the 2026-05-08 fallback semantics).
            if live_post is not None:
                live_post["scripter_attempts"] = prior_attempts + 1
            print(
                f"[queue] auto-scripter: {item_id} agent did not flip "
                f"needs_trial_script; rc={rc}; "
                f"scripter_attempts={prior_attempts + 1}/"
                f"{MAX_SCRIPTER_AGENT_ATTEMPTS}; falling through to "
                "cc_build_helper (_auto_build_pass)",
                file=sys.stderr,
            )
            ready.append(it)

    save_queue(queue_data)
    return ready


def _auto_build_pass(
    items_to_run: list[dict], dry_run: bool,
) -> list[dict]:
    """Run the CC auto-build path for every selected item flagged
    needs_trial_script=true. Returns the filtered list of items that
    are ready to run trials (build succeeded, or no build needed).

    Build failures mark the item status=error, set
    item["error"] = "auto-build failed: ..." (capped at 400 chars),
    increment build_attempts, save the queue, and dispatch a build-
    failure email. The failed item is dropped from the returned list
    so the worker pool does not try to spawn its (still-missing)
    trial script.

    On success: needs_trial_script flips to False, build_attempts
    increments, and the item stays in the returned list.

    --dry-run does not invoke CC: cc_build_helper.build_strategy
    short-circuits and prints "[dry-run] would build: ...". The
    item stays needs_trial_script=true (the build did not actually
    happen), so the orchestrator drops it from this batch's runnable
    set to avoid spawning a missing trial script.

    Parallelism (added 2026-05-08): cc_build_helper.build_strategy
    is I/O-bound (subprocess.run on a `claude` CLI) so up to
    BUILD_PASS_MAX_PARALLEL=4 builds run concurrently via
    ThreadPoolExecutor.  Pre-flight (lazy import, item filter) and
    post-flight (queue mutation, single save_queue, build-failure
    email dispatch) remain sequential so trial_queue.json writes
    never race.
    """
    if not items_to_run:
        return items_to_run

    # Lazy import: cc_build_helper is only loaded when the orchestrator
    # actually has work to build, so a stale `claude` CLI on PATH does
    # not break --status / --reset-errors / --dry-run with no
    # needs_trial_script items in the queue.
    #
    # `scripts/` has no __init__.py and is not a package on sys.path,
    # so `from scripts.cc_build_helper import build_strategy` fails
    # with ModuleNotFoundError. Load by absolute file path instead --
    # no __init__.py, no sys.path mutation.
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location(
        "cc_build_helper",
        Path(__file__).resolve().parent / "cc_build_helper.py",
    )
    _mod = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    build_strategy = _mod.build_strategy

    needs_build = [
        it for it in items_to_run if it.get("needs_trial_script")
    ]
    if not needs_build:
        return items_to_run

    # Reload state fresh; mutations land via save_queue and are
    # immediately observable to the rest of run_batch().
    queue_data = load_queue()
    by_id = {it.get("id"): it for it in queue_data.get("queue", [])}

    # Pre-flight: filter into ready (no build needed) and
    # build_targets (needs build, will be parallelised).
    ready: list[dict] = []
    build_targets: list[dict] = []
    for it in items_to_run:
        if not it.get("needs_trial_script"):
            ready.append(it)
            continue
        item_id = it.get("id")
        live = by_id.get(item_id)
        if live is None:
            print(
                f"[queue] auto-build: item {item_id} missing from "
                "queue (definitions out of sync); skipping",
                file=sys.stderr,
            )
            continue
        build_targets.append({"item": it, "live": live})

    if not build_targets:
        return ready

    # In dry-run, cc_build_helper.build_strategy short-circuits
    # itself; we still call it once per item synchronously so the
    # "would build" log line fires.  No parallelism gain in dry-run.
    if dry_run:
        for target in build_targets:
            it = target["item"]
            live = target["live"]
            print(
                f"[queue] auto-build: invoking CC for {live.get('id')} "
                f"({live.get('strategy_id')} / {live.get('variation_id')})"
            )
            try:
                build_strategy(live, ROOT, dry_run=True)
            except Exception as exc:  # noqa: BLE001
                print(
                    f"[queue] auto-build: dry-run exception "
                    f"{exc.__class__.__name__}: {exc}",
                    file=sys.stderr,
                )
            live["build_attempts"] = int(live.get("build_attempts", 0)) + 1
        save_queue(queue_data)
        return ready

    # Parallel invocation pass.  cc_build_helper.build_strategy is
    # thread-safe in the sense that each call runs its own
    # subprocess.run; no shared state inside build_strategy itself.
    def _build(target: dict) -> tuple:
        live = target["live"]
        item_id = live.get("id")
        print(
            f"[queue] auto-build: invoking CC for {item_id} "
            f"({live.get('strategy_id')} / {live.get('variation_id')})"
        )
        try:
            success, log = build_strategy(live, ROOT, dry_run=False)
            return (item_id, bool(success), log)
        except Exception as exc:  # noqa: BLE001
            return (
                item_id,
                False,
                f"build_strategy raised {exc.__class__.__name__}: {exc}",
            )

    n_workers = min(len(build_targets), BUILD_PASS_MAX_PARALLEL)
    print(
        f"[queue] auto-build: parallel invocation -- "
        f"{len(build_targets)} target(s), {n_workers} worker(s)"
    )
    build_results: dict = {}
    with ThreadPoolExecutor(max_workers=n_workers) as executor:
        futures = {
            executor.submit(_build, t): t for t in build_targets
        }
        for fut in as_completed(futures):
            t = futures[fut]
            try:
                item_id, success, log = fut.result()
            except Exception as exc:  # noqa: BLE001
                item_id = t["live"].get("id")
                success = False
                log = f"future exception: {exc.__class__.__name__}: {exc}"
            build_results[item_id] = (success, log)

    # Post-flight: fold results into queue_data, save once, dispatch
    # build-failure emails sequentially (they hit the Resend rate
    # limit if fired in parallel).  Reload queue first because the
    # scripter agent or build_strategy itself may have committed
    # queue updates while the parallel calls were running.
    queue_data = load_queue()
    by_id_post = {it.get("id"): it for it in queue_data.get("queue", [])}

    failed_for_email: list[tuple] = []  # (live, log) per failure
    for target in build_targets:
        it = target["item"]
        item_id = it.get("id")
        live_post = by_id_post.get(item_id)
        if live_post is None:
            continue
        success, log = build_results.get(item_id, (False, "no result"))
        live_post["build_attempts"] = int(live_post.get("build_attempts", 0)) + 1
        if success:
            live_post["needs_trial_script"] = False
            print(
                f"[queue] auto-build: BUILD_OK for {item_id}; "
                "queueing for trial"
            )
            ready.append(it)
            it["needs_trial_script"] = False
        else:
            live_post["status"] = "error"
            live_post["error"] = (f"auto-build failed: {log}")[:400]
            live_post["finished_at"] = utcnow_iso()
            print(
                f"[queue] auto-build: FAILED for {item_id} "
                f"(build_attempts={live_post['build_attempts']}); "
                "skipping trial run",
                file=sys.stderr,
            )
            failed_for_email.append((live_post, log))

    save_queue(queue_data)

    # Build-failure email dispatch is sequential (Resend rate limit).
    for live_post, log in failed_for_email:
        _send_build_failure_email(live_post, log, dry_run)

    return ready


def run_batch(
    items_to_run: list[dict],
    n_workers: int,
    dry_run: bool,
) -> int:
    """One batch: auto-build needs_trial_script items -> mark running
    -> spawn -> collect -> docs+commit.

    Returns the number of items that reached done."""
    if not items_to_run:
        return 0

    # Auto-build pass: any items with needs_trial_script=true get
    # routed through cc_build_helper.build_strategy before workers
    # spawn. Returns the filtered list of items actually ready to run.
    # Agent-defined scripter pass (Task 3) runs first: invokes the
    # `.claude/agents/scripter.md` agent on needs_trial_script items
    # that pass the data-blocker filter. Items the scripter handles
    # successfully proceed; items it skips fall through to the
    # cc_build_helper-based _auto_build_pass below as a fallback.
    items_to_run = _auto_scripter_pass(items_to_run, dry_run)
    if not items_to_run:
        return 0
    items_to_run = _auto_build_pass(items_to_run, dry_run)
    if not items_to_run:
        return 0

    # Persist `running` state for every selected item in one save so
    # workers never race on trial_queue.json.
    queue_data = load_queue()
    started_at = utcnow_iso()
    by_id = {it.get("id"): it for it in queue_data.get("queue", [])}
    for it in items_to_run:
        live = by_id.get(it.get("id"))
        if live is None:
            continue
        live["status"] = "running"
        live["started_at"] = started_at
    save_queue(queue_data)
    # Commit the running-status transition before workers spawn so a
    # sibling machine's `git pull` cannot conflict on trial_queue.json
    # mid-trial. Authorized by CLAUDE.md "Trial queue orchestrator
    # exception" (status field update only).
    try:
        _commit_status_update(items_to_run, "running", dry_run)
    except RuntimeError as exc:
        # Scope guard tripped -- surface but do not abort the batch.
        # The trial can still run; the conflict (if any) just recurs.
        print(
            f"[queue] _commit_status_update scope guard: {exc} "
            "(non-fatal; continuing without status commit)",
            file=sys.stderr,
        )

    _clear_stale_result_files(items_to_run)

    print(
        f"\n[queue] starting batch: {len(items_to_run)} item(s), "
        f"{n_workers} worker(s)"
    )
    for it in items_to_run:
        print(
            f"  - {it['id']}: {it.get('strategy_id', '?')} / "
            f"{it.get('variation_id', '?')}"
        )

    _spawn_workers(items_to_run, n_workers, dry_run)

    # Reload AFTER workers finish — workers don't touch trial_queue.json,
    # but we still want the freshest copy in case --reset-errors or a
    # human edit raced.  (We hold the orchestrator lock, so no other
    # `run_trial_queue.py` instance is writing.)
    queue_data = load_queue()
    return process_batch_results(queue_data, items_to_run, dry_run)


# ── Reset helper ────────────────────────────────────────────────────────────

def promote_retry_pending(queue_data: dict) -> int:
    """Flip retry_pending items whose retry_after has elapsed back to
    queued so they get picked up by the next batch selection.

    A retry_pending item carries an ISO retry_after timestamp set when
    the orchestrator detected an HTTP 429 / TooManyRequests in the
    trial subprocess output. When `now >= retry_after`, the item
    becomes queued again with retry_after cleared. retry_count is
    preserved so the per-item cap (RETRY_429_MAX_ATTEMPTS) keeps
    counting across promotions.

    Returns the number of items promoted. Caller is responsible for
    calling `save_queue` after promotion.
    """
    now = datetime.now(timezone.utc)
    n = 0
    for item in queue_data.get("queue", []):
        if item.get("status") != "retry_pending":
            continue
        retry_after = item.get("retry_after")
        if not retry_after:
            continue
        try:
            ra_dt = datetime.fromisoformat(retry_after)
        except (TypeError, ValueError):
            continue
        if ra_dt <= now:
            item["status"] = "queued"
            item["retry_after"] = None
            n += 1
    return n


def reset_error_items(queue_data: dict) -> int:
    """Reset every queue item with status='error' back to 'queued'.

    Clears verdict, started_at, finished_at, and the error payload
    so the orchestrator picks the item up again on the next pass.
    Returns the number of items reset.
    """
    n = 0
    for item in queue_data.get("queue", []):
        if item.get("status") == "error":
            item["status"] = "queued"
            item["verdict"] = None
            item["started_at"] = None
            item["finished_at"] = None
            item["error"] = None
            n += 1
    return n


# ── Main loop ───────────────────────────────────────────────────────────────

def _resolve_workers(args_workers: int | None, n_items: int) -> int:
    if args_workers is not None:
        return max(1, args_workers)
    return max(1, min(n_items, DEFAULT_MAX_WORKERS))


def _print_dry_run_plan(items_to_run: list[dict], n_workers: int) -> None:
    print(
        f"[dry-run] {len(items_to_run)} queued item(s), "
        f"would run with {n_workers} worker(s):"
    )
    for it in items_to_run:
        print(
            f"  - {it['id']}: {it.get('strategy_id', '?')} / "
            f"{it.get('variation_id', '?')} -- would run in parallel"
        )


# Proposal agent timeout: 5 minutes. The agent makes network/API calls
# with no internal timeout; this caps the wait so an unresponsive
# upstream cannot hang the orchestrator indefinitely.
PROPOSAL_AGENT_TIMEOUT_S = 300


def _invoke_proposal_agent(dry_run: bool) -> None:
    # --dry-run is contractually side-effect-free: never spawn the
    # proposal-agent subprocess (which makes outbound network/API calls)
    # in dry-run mode. Print the skip line and return so the caller's
    # `break` exits the main loop cleanly.
    if dry_run:
        print("[dry-run] Queue exhausted. Proposal agent skipped.")
        return

    print("Queue is empty. Invoking proposal agent...")
    proposal_script = ROOT / "scripts" / "propose_next_variation.py"
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        proc = subprocess.run(
            [sys.executable, str(proposal_script)],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            env=env,
            timeout=PROPOSAL_AGENT_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        msg = (
            f"Proposal agent TIMEOUT after {PROPOSAL_AGENT_TIMEOUT_S}s "
            f"(no response from upstream LLM/API). Aborting cleanly; "
            "queue unchanged. Re-invoke the orchestrator to retry."
        )
        print(msg, file=sys.stderr)
        # Surface any partial output the agent emitted before the kill.
        if isinstance(exc.stdout, (bytes, str)) and exc.stdout:
            tail = (
                exc.stdout.decode("utf-8", errors="replace")
                if isinstance(exc.stdout, bytes)
                else exc.stdout
            )
            print(tail[-2000:], end="")
        if isinstance(exc.stderr, (bytes, str)) and exc.stderr:
            tail = (
                exc.stderr.decode("utf-8", errors="replace")
                if isinstance(exc.stderr, bytes)
                else exc.stderr
            )
            print(tail[-2000:], end="", file=sys.stderr)
        send_proposal_failure_email(
            f"TIMEOUT after {PROPOSAL_AGENT_TIMEOUT_S}s",
            dry_run,
        )
        return

    if proc.stdout:
        print(proc.stdout, end="")
    if proc.stderr:
        print(proc.stderr, end="", file=sys.stderr)
    if proc.returncode != 0:
        print("Proposal agent failed. Check output above.")
        send_proposal_failure_email(proc.stderr or "", dry_run)


def _print_status() -> int:
    """Print one line per queue item (id, status, verdict) and exit.

    Zero-side-effect inspection path for the operator: no lock, no
    subprocess spawn, no file write. Reads `backtest/trial_queue.json`
    via load_queue() so the merged definitions+state view (the same
    one the orchestrator sees) is what gets reported.

    Output columns are width-padded against the actual content:
      <id>  <status>  <verdict-or-blank>
    """
    queue_data = load_queue()
    items = queue_data.get("queue", [])
    if not items:
        print("(queue is empty)")
        return 0
    id_w = max(len("id"), *(len(str(it.get("id") or "")) for it in items))
    status_w = max(
        len("status"),
        *(len(str(it.get("status") or "")) for it in items),
    )
    for it in items:
        item_id = str(it.get("id") or "")
        status = str(it.get("status") or "")
        verdict = str(it.get("verdict") or "")
        print(
            f"{item_id.ljust(id_w)}  "
            f"{status.ljust(status_w)}  "
            f"{verdict}".rstrip()
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--once",
        action="store_true",
        help=(
            "Run ONE batch (every currently queued item in parallel) "
            "then stop. Equivalent to --max-batches 1 (the default). "
            "Retained for back-compat."
        ),
    )
    parser.add_argument(
        "--max-batches",
        type=int,
        default=None,
        help=(
            "Cap on run_batch iterations in a single orchestrator "
            f"invocation. Default {DEFAULT_MAX_BATCHES}; pass 0 (or "
            "--continuous) for unbounded. The default-1 cap is the "
            "Phase-1 unsupervised-run guardrail; the 2026-05-08 "
            "runaway happened because this loop was unbounded by "
            "default."
        ),
    )
    parser.add_argument(
        "--continuous",
        action="store_true",
        help=(
            "Opt into the unbounded outer-loop mode (alias for "
            "--max-batches 0). Use only under supervision; the "
            "wall-clock and no-progress watchdogs still apply."
        ),
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--reset-errors",
        action="store_true",
        help=(
            "Reset every status='error' item back to 'queued' (clears "
            "verdict, started_at, finished_at, error). Safe replacement "
            "for hand-editing the JSON. Exits without running any trial."
        ),
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help=(
            "Print one line per queue item (id, status, verdict) and "
            "exit. Zero side effects: no lock, no subprocess spawn, no "
            "file write. Use to inspect queue state from PowerShell "
            "without triggering orchestrator or proposal-agent logic."
        ),
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help=(
            "Number of parallel workers. Default: min(queued_items, "
            f"{DEFAULT_MAX_WORKERS}). --workers 1 = sequential "
            "(in-process, debuggable)."
        ),
    )
    args = parser.parse_args()

    # --status short-circuits before lock acquisition and the email-key
    # check: read-only inspection, zero side effects.
    if args.status:
        return _print_status()

    # --reset-errors short-circuits before the email-key check: it
    # mutates the queue file only, no Resend POSTs are involved.
    if args.reset_errors:
        lock_fd = acquire_lock()
        try:
            queue_data = load_queue()
            n = reset_error_items(queue_data)
            save_queue(queue_data)
            print(f"Reset {n} error item(s) to queued.")
        finally:
            release_lock(lock_fd)
        return 0

    if not args.dry_run and (not RESEND_API_KEY or not EMAIL_TO):
        print(
            "ERROR: RESEND_API_KEY and TRIAL_QUEUE_EMAIL_TO must be set "
            "(or use --dry-run to skip email)",
            file=sys.stderr,
        )
        return 1

    lock_fd = acquire_lock()
    try:
        # Queue health gate (Task 4 hook): run scripts/validate_queue.py
        # at startup so a malformed queue blocks the run before any
        # subprocess spawns. Non-zero exit prints violations and aborts
        # the orchestrator; this matches the pre-commit hook semantics
        # but at runtime, catching mid-session corruption that lands
        # via direct file edits. Best-effort: a missing validator
        # script is logged and skipped rather than blocking the run.
        validate_script = ROOT / "scripts" / "validate_queue.py"
        if validate_script.exists():
            try:
                _vrc = subprocess.run(
                    [sys.executable, str(validate_script), "--fix"],
                    capture_output=True, text=True,
                    timeout=30, cwd=str(ROOT),
                )
                if _vrc.returncode != 0:
                    print(
                        "[queue] validate_queue.py reported violations:",
                        file=sys.stderr,
                    )
                    if _vrc.stdout:
                        print(_vrc.stdout, end="", file=sys.stderr)
                    print(
                        "[queue] aborting orchestrator startup; fix "
                        "queue before re-running",
                        file=sys.stderr,
                    )
                    return 1
            except subprocess.TimeoutExpired:
                print(
                    "[queue] validate_queue.py TIMEOUT; proceeding "
                    "without queue-health gate",
                    file=sys.stderr,
                )
            except Exception as _vexc:  # noqa: BLE001
                print(
                    f"[queue] validate_queue.py exception "
                    f"({_vexc.__class__.__name__}: {_vexc}); proceeding "
                    "without queue-health gate",
                    file=sys.stderr,
                )

        # Heartbeat (Task 5): record this orchestrator invocation so the
        # digest body can show when the PC last ran. Persist immediately
        # so a crash mid-batch still records the attempt.
        startup_data = load_queue()
        startup_data["last_run_at"] = utcnow_iso()
        # 429 auto-retry: promote retry_pending items whose retry_after
        # has elapsed back to queued before batch selection.
        promoted = promote_retry_pending(startup_data)
        if promoted:
            print(
                f"[queue] promoted {promoted} retry_pending item(s) "
                "to queued (429 backoff elapsed)"
            )
        save_queue(startup_data)

        # Resolve effective batch cap.  Precedence: --continuous wins,
        # else --max-batches if given, else --once == 1, else
        # DEFAULT_MAX_BATCHES.  cap == 0 means unbounded.
        if args.continuous:
            cap = 0
        elif args.max_batches is not None:
            cap = max(0, int(args.max_batches))
        elif args.once:
            cap = 1
        else:
            cap = DEFAULT_MAX_BATCHES
        cap_label = "unbounded" if cap == 0 else str(cap)

        # Pre-flight banner: print the effective merged-state snapshot
        # of the queue + the runnable set BEFORE entering the loop, so
        # what an operator (or audit log) thinks will run matches what
        # actually runs.  The 2026-05-08 incident was prolonged because
        # trial_queue.json's status fields disagreed with
        # trial_queue_state.json's effective state, and that disagreement
        # was invisible until the loop fired.
        from collections import Counter as _PreflightCounter
        _preflight_data = load_queue()
        _preflight_runnable = _select_runnable(_preflight_data)
        _preflight_full = _preflight_data.get("queue", [])
        _status_counts = _PreflightCounter(
            it.get("status") for it in _preflight_full
        )
        print(
            f"\n[queue] pre-flight: {len(_preflight_full)} total items; "
            f"runnable={len(_preflight_runnable)}; "
            f"max_batches={cap_label}; "
            f"wall_budget_s={MAX_ORCHESTRATOR_WALL_S}; "
            f"no_progress_kill_at={MAX_CONSECUTIVE_NOPROGRESS_BATCHES} "
            "consecutive batches"
        )
        print(f"  status breakdown: {dict(_status_counts)}")
        for _it in _preflight_runnable:
            _ready_label = (
                "needs-scripter" if _it.get("needs_trial_script")
                else "ready-to-trial"
            )
            print(
                f"    {_it.get('id', '?'):<8} "
                f"{_ready_label:<15} "
                f"scripter={_it.get('scripter_attempts', 0)} "
                f"build={_it.get('build_attempts', 0)} "
                f"run={_it.get('run_attempts', 0)}  "
                f"{_it.get('strategy_id', '?')} / "
                f"{_it.get('variation_id', '?')}"
            )

        orchestrator_started_at = time.monotonic()
        batches_run = 0
        consecutive_noprogress = 0
        exit_reason = "normal"
        while True:
            # Wall-clock watchdog: bound the whole orchestrator
            # invocation to MAX_ORCHESTRATOR_WALL_S.  Independent of
            # batch count so a single hung batch (long trial
            # subprocess, scripter timeout, etc.) is also bounded.
            elapsed = time.monotonic() - orchestrator_started_at
            if elapsed > MAX_ORCHESTRATOR_WALL_S:
                exit_reason = "wall-budget"
                print(
                    f"\n[queue] WALL-BUDGET breaker fired: elapsed="
                    f"{elapsed:.0f}s > cap {MAX_ORCHESTRATOR_WALL_S}s "
                    f"after {batches_run} batch(es); breaking outer "
                    "loop. Re-invoke the orchestrator to continue.",
                    file=sys.stderr,
                )
                break

            queue_data = load_queue()
            items_to_run = _select_runnable(queue_data)

            if not items_to_run:
                if batches_run == 0:
                    _invoke_proposal_agent(args.dry_run)
                else:
                    print(
                        f"\nQueue exhausted after {batches_run} batch(es). "
                        "Proposal agent will fill queue on next run."
                    )
                # Fire-and-forget: do NOT loop into another batch after
                # the proposal agent runs. Re-invoke the orchestrator
                # (cron or manually) to pick up newly queued items.
                break

            runnable_before = len(items_to_run)

            n_workers = _resolve_workers(args.workers, len(items_to_run))

            if args.dry_run:
                _print_dry_run_plan(items_to_run, n_workers)
                print("[dry-run] no execution; exiting after plan print")
                break

            done_count = run_batch(items_to_run, n_workers, args.dry_run)
            batches_run += 1

            # No-progress watchdog: snapshot the runnable set after
            # this batch.  If the queue's runnable count did not
            # decrease AND no item completed successfully, count
            # this batch as no-progress.  Two consecutive no-progress
            # batches means an item is stuck in a way the per-pass
            # attempt caps are not catching; break with email.
            queue_after = load_queue()
            runnable_after = len(_select_runnable(queue_after))
            made_progress = (
                runnable_after < runnable_before
                or (done_count or 0) > 0
            )
            if made_progress:
                consecutive_noprogress = 0
            else:
                consecutive_noprogress += 1
                print(
                    f"[queue] no-progress watchdog: batch "
                    f"{batches_run} produced 0 done and "
                    f"runnable count unchanged ({runnable_after}); "
                    f"consecutive={consecutive_noprogress}/"
                    f"{MAX_CONSECUTIVE_NOPROGRESS_BATCHES}",
                    file=sys.stderr,
                )
                if consecutive_noprogress >= MAX_CONSECUTIVE_NOPROGRESS_BATCHES:
                    exit_reason = "no-progress"
                    print(
                        f"\n[queue] NO-PROGRESS breaker fired: "
                        f"{consecutive_noprogress} consecutive batches "
                        "with zero queue movement. Breaking outer loop. "
                        "Inspect trial_queue_state.json and the run log "
                        "to identify the stuck item(s).",
                        file=sys.stderr,
                    )
                    break

            # Batch-count watchdog (the new default cap).  cap == 0 is
            # unbounded (--continuous); any other value caps the loop.
            if cap and batches_run >= cap:
                exit_reason = "batch-cap"
                print(
                    f"\n[queue] batch cap reached: ran {batches_run}/"
                    f"{cap} batches; stopping. Re-invoke or pass "
                    "--continuous for unbounded.",
                )
                break

            if args.once:
                exit_reason = "once-flag"
                break

        elapsed_final = time.monotonic() - orchestrator_started_at
        print(
            f"\n[queue] orchestrator exit: reason={exit_reason} "
            f"batches_run={batches_run} elapsed={elapsed_final:.0f}s"
        )

        # Digest pass (Task 5): one digest email per orchestrator run
        # if 8h has elapsed and there is unreported activity, or a
        # heartbeat-only digest if 24h has elapsed with nothing new.
        final_data = load_queue()
        maybe_send_digest(final_data, args.dry_run)
    finally:
        release_lock(lock_fd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
