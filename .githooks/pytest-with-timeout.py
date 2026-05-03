#!/usr/bin/env python3
"""Run `pytest -m fast` with a 60s ceiling.

macOS lacks the GNU `timeout` binary by default, so the wrapper is
written in Python (always available since the rest of the hook
toolchain depends on it).

Exit codes: passthrough from pytest (0=ok, 1=fail, 5=no tests, etc.),
124 on timeout (mirrors GNU coreutils convention).
"""
from __future__ import annotations

import subprocess
import sys

CEILING_SECONDS = 60


def main() -> int:
    try:
        result = subprocess.run(
            ["pytest", "-m", "fast", "--no-header", "-q"],
            timeout=CEILING_SECONDS,
        )
        return result.returncode
    except subprocess.TimeoutExpired:
        print(
            f"BLOCKED: pytest -m fast exceeded {CEILING_SECONDS}s ceiling.",
            file=sys.stderr,
        )
        return 124


if __name__ == "__main__":
    sys.exit(main())
