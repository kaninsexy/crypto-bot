"""scripts/setup_windows.py

One-time Windows PC setup for the trial-queue operator host:
  - pip install -r requirements.txt (uses sys.executable's pip)
  - powercfg: prevent sleep on AC power
  - print Task Scheduler "wake to run" instructions
  - check critical environment variables

Run once per machine after cloning the repo. Idempotent.
"""

from __future__ import annotations

import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8',
                              errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8',
                              errors='replace')

if sys.platform != "win32":
    print("This script is for Windows only.")
    sys.exit(0)

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = ROOT / "requirements.txt"

# (var_name, optional, note)
REQUIRED_VARS = [
    ("OKX_API_KEY", False, ""),
    ("OKX_API_SECRET", False, ""),
    ("OKX_PASSPHRASE", False, ""),
    ("LUNARCRUSH_API_KEY", False, ""),
    ("GLASSNODE_API_KEY", True,
     "OPTIONAL -- on-chain trials will defer without it"),
    ("RESEND_API_KEY", False, ""),
    ("TRIAL_QUEUE_EMAIL_TO", False, ""),
]


def _check_anthropic_or_openrouter() -> tuple[str, bool]:
    if os.environ.get("OPENROUTER_API_KEY"):
        return ("OPENROUTER_API_KEY", True)
    if os.environ.get("ANTHROPIC_API_KEY"):
        return ("ANTHROPIC_API_KEY", True)
    return ("OPENROUTER_API_KEY or ANTHROPIC_API_KEY", False)


def _pip_install() -> int:
    if not REQUIREMENTS.exists():
        print("requirements.txt not found at " + str(REQUIREMENTS))
        return 1
    print("Installing packages from " + str(REQUIREMENTS) + "...")
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-r", str(REQUIREMENTS)],
        cwd=str(ROOT),
    )
    return proc.returncode


def _set_power_plan() -> None:
    print("Setting AC power plan: standby-timeout-ac 0 (never sleep)...")
    subprocess.run(["powercfg", "/change", "standby-timeout-ac", "0"])


def _print_task_scheduler_instructions() -> None:
    print()
    print("=== Task Scheduler: enable wake-to-run ===")
    print("To make a scheduled task wake the PC on its trigger:")
    print()
    print("1. Open Task Scheduler (run: taskschd.msc)")
    print("2. Locate the trial-queue task in the Task Scheduler Library")
    print("3. Right-click the task -> Properties")
    print("4. Click the 'Conditions' tab")
    print("5. Under the 'Power' section, check the box labelled:")
    print("     'Wake the computer to run this task'")
    print("6. Click OK to save")
    print()
    print("Also recommended: on the 'General' tab, set 'Run whether")
    print("user is logged on or not' so the task fires from a locked")
    print("session over RDP / Tailscale.")
    print()


def _check_env_vars() -> int:
    print("=== Environment variable check ===")
    n_missing_required = 0
    for name, optional, note in REQUIRED_VARS:
        present = bool(os.environ.get(name))
        status = "OK" if present else "MISSING"
        suffix = " -- " + note if note else ""
        print("  " + status.ljust(8) + name + suffix)
        if not present and not optional:
            n_missing_required += 1
    name, ok = _check_anthropic_or_openrouter()
    status = "OK" if ok else "MISSING"
    note = "" if ok else " -- one of these is required"
    print("  " + status.ljust(8) + name + note)
    if not ok:
        n_missing_required += 1
    return n_missing_required


def main() -> int:
    print("=" * 60)
    print("crypto-bot Windows setup")
    print("=" * 60)
    print()

    rc = _pip_install()
    if rc != 0:
        print("WARNING: pip install returned " + str(rc))

    _set_power_plan()
    _print_task_scheduler_instructions()
    _check_env_vars()

    print()
    print("Setup complete. Missing vars above must be set before "
          "affected trials can run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
