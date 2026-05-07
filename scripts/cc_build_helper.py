"""scripts/cc_build_helper.py -- auto-build a strategy implementation
via Claude Code when the orchestrator hits a queue item with
needs_trial_script=true.

Public surface:

    build_strategy(item: dict, repo_root: Path,
                   dry_run: bool = False) -> tuple[bool, str]
        Returns (success, log). Side-effects: spawns `claude --print`
        with a structured prompt; on success runs three independent
        verification subprocesses that mirror the orchestrator's own
        contract (import + py_compile + --status). Caller is
        responsible for flipping queue fields based on the success
        bool.

The CC invocation has a 10-minute timeout. Verification subprocesses
run with their own short timeouts (60s each).

Output of this module is ASCII-only (Windows cp1252 compatible).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path


# Timeouts. CC build is allowed up to 10 minutes; each post-build
# verification gets up to 60 seconds.
CC_BUILD_TIMEOUT_S = 600
VERIFY_TIMEOUT_S = 60


_SNAKE_CASE_RE_1 = re.compile(r"(.)([A-Z][a-z]+)")
_SNAKE_CASE_RE_2 = re.compile(r"([a-z0-9])([A-Z])")


def _to_snake_case(name: str) -> str:
    """Convert StrategyId / VolumeWeightedTSMOM -> volume_weighted_tsmom.

    Two-pass regex to keep acronym runs together: first pass splits
    after a lowercase or digit, second pass splits before a capital
    that follows a non-capital. VolumeWeightedTSMOM ->
    Volume_Weighted_TSMOM -> volume_weighted_tsmom.
    """
    s = _SNAKE_CASE_RE_1.sub(r"\1_\2", name)
    s = _SNAKE_CASE_RE_2.sub(r"\1_\2", s)
    return s.lower()


def _format_citations(citations) -> str:
    """Render the queue item's citation list as a numbered text block.

    Defensive against missing fields; never raises. Returns "(none)"
    when the input is empty or malformed.
    """
    if not citations or not isinstance(citations, list):
        return "(none)"
    lines: list[str] = []
    for i, c in enumerate(citations, start=1):
        if not isinstance(c, dict):
            continue
        authors = c.get("authors") or "(authors unknown)"
        year = c.get("year") or ""
        source = c.get("source") or ""
        finding = c.get("key_finding") or ""
        head = f"{i}. {authors} ({year}), {source}".rstrip(", ")
        lines.append(head)
        if finding:
            lines.append(f"   key_finding: {finding}")
    return "\n".join(lines) if lines else "(none)"


# Build prompt template -- ALL constraint blocks are verbatim per
# scripts/cc_build_helper.py spec. Do not summarise or drop any.
_BUILD_PROMPT_TEMPLATE = """\
You are building a new strategy implementation. Read these files
BEFORE writing any code:
  - backtest/holdout_manifest.json (symbol/timeframe schema)
  - strategies/supertrend.py (BaseStrategy interface: generate_signal
    singular, not generate_signals plural)
  - scripts/run_intraday_seasonality_effects_trial.py (single-symbol
    harness template: BacktestEngine.run + run_cpcv)
  - scripts/run_meanreversion_btc_residual_phase4a_trial.py
    (multi-symbol harness template: run_engine_multi + run_cpcv_multi)
  - research/mean-reversion-btc-residual-literature.md
    (literature doc structure template)

Symbol rule: check holdout_manifest.json for the correct symbol
form. 1D single-symbol strategies use BTC/USDT (spot-style, matches
the OHLCV parquet cache). Do NOT use BTC/USDT:USDT for 1D single-
symbol entries.

Harness rule: if manifest entry has "symbol" (singular) use
BacktestEngine.run + run_cpcv. If it has "symbols" (plural) use
run_engine_multi + run_cpcv_multi. Wrong harness = crash.

ASCII-only rule: ALL print() output in trial scripts must be
ASCII-only. No Unicode characters anywhere in stdout/stderr paths.
Windows cp1252 terminal compatibility is required.

BaseStrategy interface: implement generate_signal(self, df) -> Signal
(singular, scalar, what the engine calls). generate_signals
(vector) is optional but generate_signal is mandatory.

Sacred-harness rule: never edit backtest/trials.py,
backtest/holdout.py, backtest/holdout_manifest.json schema,
CLAUDE.md, docs/MASTER_PLAN.md, docs/validation_framework.md.

CPCVError handler: the trial script must catch CPCVError and write
a sentinel-bearing retire row exactly as in the single-symbol
harness template. Do not let CPCVError propagate uncaught.

TRIAL SUMMARY JSON sentinel: the trial script must print
'--- TRIAL SUMMARY JSON ---' followed by a JSON object on the
next line. The orchestrator parses this; missing sentinel = error.

Strategy: {strategy_id}
Variation: {variation_id}
Hypothesis: {hypothesis_one_line}
Implementation notes: {implementation_notes}
Citations:
{citations_formatted}

Files to create:
  1. strategies/{strategy_module}.py  (strategy class)
  2. {script_path}  (trial script)
  3. {literature_doc}  (literature doc)
File to update:
  4. backtest/holdout_manifest.json  (add manifest entry)

Verifications to run after creating files:
  python -c "from strategies.{strategy_module} import \
    {strategy_class}; print('import ok')"
  python -m py_compile {script_path} && echo compile_ok
  python scripts/run_trial_queue.py --status

If any verification fails: print 'BUILD_FAILED: <reason>' and stop.
If all pass: print 'BUILD_OK' and commit with heredoc message:
  feat: auto-build {strategy_id} {variation_id} implementation
Then print git log -1 and stop.
"""


def _render_prompt(item: dict) -> tuple[str, str, str]:
    """Render the CC build prompt and return
    (prompt, strategy_module, strategy_class) so the caller can
    reuse the derived names for the post-build verification step.
    """
    strategy_id = str(item.get("strategy_id") or "")
    variation_id = str(item.get("variation_id") or "")
    strategy_module = _to_snake_case(strategy_id)
    strategy_class = f"{strategy_id}Strategy"
    citations_formatted = _format_citations(item.get("citations"))
    prompt = _BUILD_PROMPT_TEMPLATE.format(
        strategy_id=strategy_id,
        variation_id=variation_id,
        hypothesis_one_line=str(item.get("hypothesis_one_line") or ""),
        implementation_notes=str(item.get("implementation_notes") or ""),
        citations_formatted=citations_formatted,
        script_path=str(item.get("script_path") or ""),
        literature_doc=str(item.get("literature_doc") or ""),
        strategy_module=strategy_module,
        strategy_class=strategy_class,
    )
    return prompt, strategy_module, strategy_class


def _run_post_build_verifications(
    item: dict,
    repo_root: Path,
    strategy_module: str,
    strategy_class: str,
) -> tuple[bool, str]:
    """Independent verification: do not trust CC's self-reported pass.

    Three subprocesses, each gated by VERIFY_TIMEOUT_S:
      1. Strategy module imports cleanly.
      2. Trial script compiles (py_compile).
      3. Orchestrator --status exits 0 and lists the queue item id.

    Returns (ok, detail). On failure, detail names the failing step
    and includes the captured stderr tail.
    """
    item_id = str(item.get("id") or "")
    script_path = str(item.get("script_path") or "")

    # 1. Import check.
    cmd1 = [
        sys.executable, "-c",
        (
            f"from strategies.{strategy_module} import "
            f"{strategy_class}; print('ok')"
        ),
    ]
    try:
        proc = subprocess.run(
            cmd1, capture_output=True, text=True,
            timeout=VERIFY_TIMEOUT_S, cwd=str(repo_root),
        )
    except subprocess.TimeoutExpired:
        return (False, f"verify[import]: TIMEOUT after {VERIFY_TIMEOUT_S}s")
    if proc.returncode != 0 or "ok" not in (proc.stdout or ""):
        tail = (proc.stderr or proc.stdout or "")[-400:]
        return (False, f"verify[import]: rc={proc.returncode} {tail}")

    # 2. Trial-script compile.
    if script_path:
        cmd2 = [sys.executable, "-m", "py_compile", script_path]
        try:
            proc = subprocess.run(
                cmd2, capture_output=True, text=True,
                timeout=VERIFY_TIMEOUT_S, cwd=str(repo_root),
            )
        except subprocess.TimeoutExpired:
            return (
                False,
                f"verify[py_compile]: TIMEOUT after {VERIFY_TIMEOUT_S}s",
            )
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "")[-400:]
            return (False, f"verify[py_compile]: rc={proc.returncode} {tail}")

    # 3. Orchestrator --status sanity (verifies imports of the
    #    orchestrator module path AND that the queue is parseable).
    cmd3 = [
        sys.executable, "scripts/run_trial_queue.py", "--status",
    ]
    try:
        proc = subprocess.run(
            cmd3, capture_output=True, text=True,
            timeout=VERIFY_TIMEOUT_S, cwd=str(repo_root),
        )
    except subprocess.TimeoutExpired:
        return (
            False,
            f"verify[--status]: TIMEOUT after {VERIFY_TIMEOUT_S}s",
        )
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-400:]
        return (False, f"verify[--status]: rc={proc.returncode} {tail}")
    if item_id and item_id not in (proc.stdout or ""):
        # Queue item disappeared -- something went wrong with the
        # CC build that we did not catch above.
        return (
            False,
            f"verify[--status]: item {item_id} missing from queue listing",
        )

    return (True, "post-build verifications passed")


def build_strategy(
    item: dict,
    repo_root: Path,
    dry_run: bool = False,
) -> tuple[bool, str]:
    """Invoke Claude Code to build the implementation for a queue
    item with needs_trial_script=true. Returns (success, log).

    dry_run=True is contractually side-effect-free: prints the
    "would build" line and returns success without spawning CC. The
    caller (orchestrator) is responsible for using this so a
    --dry-run orchestrator pass never invokes the network-bound
    CC subprocess.
    """
    strategy_id = str(item.get("strategy_id") or "(unknown)")
    variation_id = str(item.get("variation_id") or "(unknown)")

    if dry_run:
        msg = (
            f"[dry-run] would build: {strategy_id} {variation_id}"
        )
        print(msg)
        return (True, msg)

    prompt, strategy_module, strategy_class = _render_prompt(item)

    cmd = ["claude", "--print", prompt]
    # Strip ANTHROPIC_API_KEY so the claude CLI uses its OAuth session
    # (Claude Max plan) rather than billing the API key. The Machine-
    # scope env carries ANTHROPIC_API_KEY for the proposal agent's
    # Anthropic fallback; that key MUST NOT be visible to `claude`.
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=CC_BUILD_TIMEOUT_S,
            cwd=str(repo_root),
            env=env,
        )
    except subprocess.TimeoutExpired:
        return (False, f"TIMEOUT after {CC_BUILD_TIMEOUT_S}s")
    except FileNotFoundError as exc:
        return (
            False,
            f"`claude` CLI not found on PATH ({exc}); CC auto-build "
            "requires the Claude Code CLI to be installed.",
        )
    except Exception as exc:  # noqa: BLE001 -- orchestrator catch-all
        return (False, f"CC subprocess exception: {exc}")

    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    log = stdout + ("\n[stderr tail]\n" + stderr if stderr else "")

    if proc.returncode != 0 and "BUILD_OK" not in stdout:
        return (False, log[-2000:])
    if "BUILD_FAILED" in stdout:
        return (False, log[-2000:])
    if "BUILD_OK" not in stdout:
        return (
            False,
            "neither BUILD_OK nor BUILD_FAILED found in CC output\n"
            + log[-2000:],
        )

    # Independent verifications. Even if CC self-reports BUILD_OK
    # we re-run the three checks to catch hallucinated success.
    ok, detail = _run_post_build_verifications(
        item, repo_root, strategy_module, strategy_class,
    )
    if not ok:
        return (False, f"post-build verification failed: {detail}\n{log[-1000:]}")

    return (True, log)
