"""scripts/run_trial_queue.py — Trial queue orchestrator.

Reads backtest/trial_queue.json, processes queued items sequentially,
commits results per the CLAUDE.md trial-queue orchestrator exception.

Usage:
  python scripts/run_trial_queue.py          # process all queued items
  python scripts/run_trial_queue.py --once   # process one item then exit
  python scripts/run_trial_queue.py --dry-run  # print plan, no execution

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
LOCK_PATH = QUEUE_PATH.with_suffix(".lock")
RUN_LOG_PATH = ROOT / "backtest" / "trial_queue_run_log.jsonl"

RESEND_API_URL = "https://api.resend.com/emails"
RESEND_FROM = os.environ.get("TRIAL_QUEUE_FROM", "trial-queue@crypto-bot.local")
EMAIL_RATE_LIMIT_SLEEP_S = 61  # >60s between calls = safe under 6/hr

TRIAL_TIMEOUT_S = 14_400  # 4h per CLAUDE.md compute budget

RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
EMAIL_TO = os.environ.get("TRIAL_QUEUE_EMAIL_TO")


# ── Time helper ─────────────────────────────────────────────────────────────

def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Queue I/O ───────────────────────────────────────────────────────────────

def load_queue() -> dict:
    """Read QUEUE_PATH. Empty/absent file returns the canonical empty queue.

    The `batch_alert_sent_at_position` field is defaulted to 0 when
    absent so legacy queue files (pre-2026-05-04) keep working without
    an explicit migration.
    """
    if not QUEUE_PATH.exists():
        return {
            "schema_version": 1,
            "batch_alert_sent_at_position": 0,
            "queue": [],
        }
    text = QUEUE_PATH.read_text(encoding="utf-8").strip()
    if not text:
        return {
            "schema_version": 1,
            "batch_alert_sent_at_position": 0,
            "queue": [],
        }
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Failed to parse {QUEUE_PATH}: {exc}. "
            "Fix the JSON manually before re-running."
        ) from exc
    if "batch_alert_sent_at_position" not in data:
        data["batch_alert_sent_at_position"] = 0
    return data


def save_queue(data: dict) -> None:
    """Atomic write: tmp file + os.replace."""
    tmp = QUEUE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, QUEUE_PATH)


def acquire_lock():
    """Exclusive non-blocking lock; sys.exit(1) if another instance holds it.

    Uses msvcrt.locking on Windows (LK_NBLCK = non-blocking exclusive
    1-byte lock) and fcntl.flock on Unix (LOCK_EX | LOCK_NB). Both
    ship with Python stdlib.
    """
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


# ── Core ────────────────────────────────────────────────────────────────────

def find_next_queued(queue_data: dict) -> dict | None:
    for item in queue_data.get("queue", []):
        if item.get("status") == "queued":
            if item.get("needs_trial_script"):
                # Email notification handled in process_item path;
                # here just skip silently (notification is sent
                # by the proposal agent email, not the orchestrator).
                continue
            return item
    return None


def run_item(item: dict, dry_run: bool) -> tuple[int, str, str]:
    """Returns (returncode, stdout, stderr)."""
    script = ROOT / item["script_path"]
    if not script.exists():
        return -2, "", f"script not found: {script}"
    if dry_run:
        print(f"[dry-run] would run: {script}")
        return 0, '--- TRIAL SUMMARY JSON ---\n{"verdict":"dry-run"}', ""
    try:
        result = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True,
            text=True,
            timeout=TRIAL_TIMEOUT_S,
            cwd=str(ROOT),
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired as e:
        return -1, e.stdout or "", f"TIMEOUT after {TRIAL_TIMEOUT_S}s"
    except Exception as e:  # noqa: BLE001 — orchestrator catch-all
        return -3, "", str(e)


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
# Email policy (revised 2026-05-04):
#   - KEEP verdict          → single email immediately (action required)
#   - error / non-keep      → logged to trial_queue_run_log.jsonl only
#   - N consecutive non-keep → one batch summary email (gated by
#                              `batch_alert_sent_at_position`)
#   - proposal-agent failure → one email with last 500 chars of stderr
# All three Resend POSTs route through `_send_email`.


CONSECUTIVE_FAIL_ALERT = int(
    os.environ.get("TRIAL_QUEUE_FAIL_ALERT_EVERY", "5")
)


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


def send_batch_summary_email(
    items: list[dict],
    dry_run: bool,
) -> None:
    """One email summarising the last N consecutive non-keep results."""
    n = len(items)
    subject = f"trials: {n} consecutive non-keep results -- batch summary"
    header = f"{'id':<10} {'strategy':<20} {'variation':<32} {'verdict':<14} {'sr_observed':<12}"
    rows = [header, "-" * len(header)]
    for item in items:
        sr = item.get("_sr_observed", "n/a")
        try:
            sr_str = f"{float(sr):+.4f}"
        except (TypeError, ValueError):
            sr_str = str(sr)
        rows.append(
            f"{str(item.get('id','?'))[:10]:<10} "
            f"{str(item.get('strategy_id','?'))[:20]:<20} "
            f"{str(item.get('variation_id','?'))[:32]:<32} "
            f"{str(item.get('verdict') or item.get('status') or '?')[:14]:<14} "
            f"{sr_str:<12}"
        )
    _send_email(subject, "\n".join(rows), dry_run)


def send_proposal_failure_email(stderr_tail: str, dry_run: bool) -> None:
    """Notify the human when the proposal agent exits non-zero."""
    subject = "trials: proposal agent failed -- manual research needed"
    body = (
        "scripts/propose_next_variation.py exited non-zero from the "
        "trial-queue empty-queue branch.\n\n"
        f"Last 500 chars of stderr:\n{(stderr_tail or '')[-500:]}"
    )
    _send_email(subject, body, dry_run)


def _completed_items(queue_data: dict) -> list[dict]:
    """Items in `queue_data['queue']` whose status is done OR error,
    ordered by (finished_at asc).  Items missing finished_at sort
    last (kept stable by index)."""
    items = [
        it for it in queue_data.get("queue", [])
        if it.get("status") in ("done", "error")
    ]
    return sorted(items, key=lambda it: it.get("finished_at") or "")


def _is_non_keep(item: dict) -> bool:
    if item.get("status") == "error":
        return True
    return item.get("verdict") != "keep"


def maybe_send_batch_alert(queue_data: dict, dry_run: bool) -> None:
    """Send the batch-summary email if the LAST CONSECUTIVE_FAIL_ALERT
    completed items are all non-keep AND we have not already alerted
    at the current completed-count.  Updates
    `queue_data['batch_alert_sent_at_position']` and persists."""
    completed = _completed_items(queue_data)
    n_completed = len(completed)
    threshold = CONSECUTIVE_FAIL_ALERT
    if threshold <= 0 or n_completed < threshold:
        return
    last_n = completed[-threshold:]
    if not all(_is_non_keep(it) for it in last_n):
        return

    last_alert_pos = int(queue_data.get("batch_alert_sent_at_position", 0))
    if n_completed <= last_alert_pos:
        # Already alerted at this completed-count; do not re-alert.
        return

    # Enrich with sr_observed for the body table; absent in queue
    # items by design (we never persisted the full summary), but the
    # parsed JSON summary at run time isn't retained either — fall
    # back to "n/a".
    enriched = [{**it, "_sr_observed": it.get("sharpe", "n/a")} for it in last_n]
    send_batch_summary_email(enriched, dry_run)
    queue_data["batch_alert_sent_at_position"] = n_completed
    save_queue(queue_data)


# ── Main loop ───────────────────────────────────────────────────────────────

def process_item(item: dict, queue_data: dict, dry_run: bool) -> None:
    print(
        f"\n[queue] processing {item['id']}: "
        f"{item['strategy_id']} / {item['variation_id']}"
    )

    item["status"] = "running"
    item["started_at"] = utcnow_iso()
    save_queue(queue_data)

    returncode, stdout, stderr = run_item(item, dry_run)
    summary = parse_json_summary(stdout)
    log_run(item, returncode, summary, stderr)

    if returncode != 0 or summary is None:
        reason = (
            stderr[:500] if returncode != 0
            else "sentinel not found in stdout"
        )
        print(f"[queue] FAILED item {item['id']}: {reason[:200]}")
        item["status"] = "error"
        item["error"] = reason[:500]
        item["finished_at"] = utcnow_iso()
        save_queue(queue_data)
        # No per-item error email — log_run already records this.
        # Batch alert below catches consecutive failures.
        maybe_send_batch_alert(queue_data, dry_run)
        return

    try:
        update_strategies_md(item, summary)
        update_literature_doc(item, summary)
    except Exception as e:  # noqa: BLE001
        print(f"[queue] doc update failed: {e}", file=sys.stderr)
        item["status"] = "error"
        item["error"] = f"doc update failed: {str(e)[:400]}"
        item["finished_at"] = utcnow_iso()
        save_queue(queue_data)
        maybe_send_batch_alert(queue_data, dry_run)
        return

    committed = commit_result(item, summary, dry_run)
    if not committed:
        item["status"] = "error"
        item["error"] = "commit failed or scope violation"
        item["finished_at"] = utcnow_iso()
        save_queue(queue_data)
        maybe_send_batch_alert(queue_data, dry_run)
        return

    item["status"] = "done"
    item["verdict"] = summary.get("verdict")
    item["finished_at"] = utcnow_iso()
    save_queue(queue_data)
    print(f"[queue] done {item['id']}: verdict={item['verdict']}")

    # Per-item email policy: only KEEP triggers an immediate alert.
    # Any other verdict (retire, under_tested, ...) goes into the
    # batch-summary alerter, which fires once per N consecutive
    # non-keep completions.
    if item["verdict"] == "keep":
        send_keep_email(item, summary, dry_run)
        item["email_sent"] = True
        save_queue(queue_data)
    maybe_send_batch_alert(queue_data, dry_run)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.dry_run and (not RESEND_API_KEY or not EMAIL_TO):
        print(
            "ERROR: RESEND_API_KEY and TRIAL_QUEUE_EMAIL_TO must be set "
            "(or use --dry-run to skip email)",
            file=sys.stderr,
        )
        return 1

    lock_fd = acquire_lock()
    try:
        processed = 0
        while True:
            queue_data = load_queue()
            item = find_next_queued(queue_data)
            if item is None:
                if processed == 0:
                    # Queue is empty (or only needs_trial_script
                    # items remain) — invoke proposal agent.
                    print("Queue is empty. Invoking proposal agent...")
                    proposal_script = ROOT / "scripts" / "propose_next_variation.py"
                    _result = subprocess.run(
                        [sys.executable, str(proposal_script)],
                        capture_output=True,
                        text=True,
                        cwd=str(ROOT),
                    )
                    # Surface the agent's stdout/stderr so the operator
                    # still sees what happened in this run's log.
                    if _result.stdout:
                        print(_result.stdout, end="")
                    if _result.stderr:
                        print(_result.stderr, end="", file=sys.stderr)
                    if _result.returncode != 0:
                        print("Proposal agent failed. Check output above.")
                        send_proposal_failure_email(
                            _result.stderr or "", args.dry_run,
                        )
                else:
                    print(
                        f"\nQueue exhausted after {processed} item(s). "
                        "Proposal agent will fill queue on next run."
                    )
                # Fire-and-forget: do NOT loop back into the queue
                # after the proposal agent runs. Re-invoke the
                # orchestrator (cron or manually) to pick up the
                # newly queued items.
                break
            process_item(item, queue_data, args.dry_run)
            processed += 1
            if args.once:
                break
    finally:
        release_lock(lock_fd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
