#!/usr/bin/env bash
# Pre-commit hook: validate trial_queue.json health before allowing
# the commit. Runs scripts/validate_queue.py; exits non-zero on any
# violation, blocking the commit. Triggered only when the staged
# diff includes backtest/trial_queue.json.
set -e

# Skip when the commit doesn't touch the queue file -- keeps unrelated
# commits fast and avoids spurious failures on doc-only changes.
if git diff --cached --name-only --diff-filter=ACMR | grep -qE '^backtest/trial_queue\.json$'; then
    python scripts/validate_queue.py || {
        echo "[pre-commit-queue-validate] queue health check failed; commit blocked." >&2
        exit 1
    }
fi
exit 0
