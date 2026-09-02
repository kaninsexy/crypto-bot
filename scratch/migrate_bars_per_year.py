"""
scratch/migrate_bars_per_year.py — one-shot migration (gate spec v2).

Inserts explicit `bars_per_year=BARS_PER_YEAR` kwargs into every trial
script's dsr_from_cpcv_result / mintrl_from_cpcv_result /
compute_verdict call, plus a per-script BARS_PER_YEAR constant derived
from the manifest timeframe.  Idempotent (skips files already
migrated).  py_compiles every touched file; prints a manual-attention
list for anything it could not transform safely.
"""
import json
import py_compile
import re
import sys
from pathlib import Path

ROOT = Path(r"C:\crypto-bot")
MANIFEST = json.loads(
    (ROOT / "backtest" / "holdout_manifest.json").read_text(encoding="utf-8")
)

CALL_NAMES = (
    "dsr_from_cpcv_result",
    "mintrl_from_cpcv_result",
    "compute_verdict",
)

manual: list[str] = []
touched: list[str] = []

for path in sorted((ROOT / "scripts").glob("*.py")):
    text = path.read_text(encoding="utf-8")
    if not any(c + "(" in text for c in CALL_NAMES):
        continue
    if "BARS_PER_YEAR" in text:
        continue  # already migrated (or hand-edited)

    m_sid = re.search(r'^STRATEGY_ID\s*=\s*"([^"]+)"', text, re.M)
    if not m_sid:
        manual.append(f"{path.name}: no STRATEGY_ID constant")
        continue
    sid = m_sid.group(1)
    entry = MANIFEST.get(sid)
    if entry is None:
        manual.append(f"{path.name}: STRATEGY_ID {sid!r} not in manifest")
        continue
    tf = entry["timeframe"]

    # 1. Insert the constant right after the STRATEGY_ID line.
    const_block = (
        f'{m_sid.group(0)}\n'
        "# Gate spec v2 (2026-06-11): explicit bar frequency for the\n"
        "# units-correct DSR / MinTRL / verdict (manifest timeframe).\n"
        "from backtest.dsr import bars_per_year_for_timeframe\n"
        f'BARS_PER_YEAR = bars_per_year_for_timeframe("{tf}")'
    )
    new_text = text.replace(m_sid.group(0), const_block, 1)

    # 2. Insert the kwarg into each multiline kwarg-style call.
    #    Pattern: name( <newline> (indented kwarg lines) <indent>)
    counter = [0]
    for name in CALL_NAMES:
        pattern = re.compile(
            r"(" + name + r"\(\n(?:[ \t]+[\w*]+=[^\n]*\n)+)([ \t]*\))"
        )

        def _ins(m: "re.Match[str]", _c=counter) -> str:
            body, close = m.group(1), m.group(2)
            last_line = body.rstrip("\n").rsplit("\n", 1)[-1]
            indent = last_line[: len(last_line) - len(last_line.lstrip())]
            _c[0] += 1
            return body + f"{indent}bars_per_year=BARS_PER_YEAR,\n" + close

        new_text = pattern.sub(_ins, new_text)
    n_inserted = counter[0]

    leftover = sum(
        1 for c in CALL_NAMES
        for m in re.finditer(re.escape(c) + r"\(", new_text)
    )
    inserted_marks = new_text.count("bars_per_year=BARS_PER_YEAR")
    if inserted_marks < leftover:
        manual.append(
            f"{path.name}: {leftover} call(s), only {inserted_marks} "
            "migrated — check remaining call shapes by hand"
        )

    if n_inserted == 0:
        manual.append(f"{path.name}: no call matched the kwarg pattern")
        continue

    path.write_text(new_text, encoding="utf-8")
    try:
        py_compile.compile(str(path), doraise=True)
    except py_compile.PyCompileError as exc:
        manual.append(f"{path.name}: COMPILE FAILED after edit: {exc}")
        continue
    touched.append(f"{path.name} (tf={tf}, calls={n_inserted})")

print("touched:")
for t in touched:
    print("  ", t)
print("manual attention:")
for m in manual:
    print("  ", m)
sys.exit(0)
