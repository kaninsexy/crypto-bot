"""
backtest/logs.py — JSONL read/write plumbing.

No schema knowledge lives here. Callers supply the dicts; this module
serialises them to disk and streams them back. Field names, types, and
validation belong to whichever module owns each log's schema.

An absent log file is a valid state — read_jsonl and iter_jsonl_filtered
both yield nothing rather than raising.
"""

import json
from collections.abc import Callable, Iterator
from pathlib import Path


def append_jsonl(path: Path, event: dict) -> None:
    """Append one JSON object as a newline-terminated line.

    Creates parent directories and the file if they do not already exist.
    datetime/Path values are coerced to str via default=str so callers
    do not need to pre-serialise timestamps.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, default=str) + "\n"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line)


def read_jsonl(path: Path) -> Iterator[dict]:
    """Yield parsed dicts from a JSONL file.

    Returns an empty iterator if the file is absent — an empty log is a
    valid state, not an error.  Blank lines are skipped silently.
    """
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if raw:
                yield json.loads(raw)


def iter_jsonl_filtered(
    path: Path,
    predicate: Callable[[dict], bool],
) -> Iterator[dict]:
    """Yield only events for which predicate returns True.

    Designed for streaming scans so the caller never loads the whole log
    into memory.  Used by holdout.py to scan access events per strategy.
    """
    for event in read_jsonl(path):
        if predicate(event):
            yield event
