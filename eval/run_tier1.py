# SPDX-License-Identifier: LicenseRef-Proprietary
# Copyright (c) 2026 Kanin Srijundorn. All rights reserved.
"""Tier-1 eval harness — deterministic fixtures for the hook gate layer.

Ported 2026-09-02 from siamese-reconcile ``eval/run_tier1.py`` and re-pointed
at crypto-bot's hooks and fixtures. This is the second of the three
deterministic gates named in ``.claude/rules/escalation.md`` §0
(``pytest`` -> ``eval/run_tier1.py`` -> the trial verdict tree): it proves the
hooks actually block what they claim to block.

Tier 1 regression-tests the HOOKS (the security/observability gates) against
pinned fixtures. Each fixture declares a hook, a stdin payload, and the
expected exit code (0 = allow, 2 = block). Run after editing any hook.

Why it matters here specifically: before this port every crypto-bot guard was
a bash script that parsed stdin with ``jq``. ``jq`` is not installed on this
machine, so each exited 127 — which Claude Code treats as non-blocking — and
the whole guard layer failed open, silently, for months. Nothing detected it
because nothing EXERCISED the hooks. That is what this harness is for, and it
is why ``--self-check`` exists: a gate that cannot go red certifies the gap it
was meant to catch.

Usage:
    python eval/run_tier1.py                 # the gate
    python eval/run_tier1.py --self-check    # prove the gate can FAIL
    python eval/run_tier1.py --hooks-dir P   # run the fixtures against P

Fixture JSONL fields (one JSON object per line):
- desc          : human-readable name (historical §14 ids appear verbatim here)
- hook          : filename under .claude/hooks/
- eval_type     : "checkpoint" (deterministic gate fixtures; k=1 sufficient)
- payload       : JSON object piped to the hook's stdin
- payload_raw   : raw string stdin instead of payload (malformed-input cases)
- env           : opt-in env overrides (e.g. SACRED_OVERRIDE_FILES)
- expect_exit   : required exit code
- expect_stderr_contains : optional substring asserted in stderr
- sandbox_project_dir    : if true, CLAUDE_PROJECT_DIR points at a fresh temp
                           dir for this fixture (hooks that WRITE, e.g.
                           session-end, stay hermetic)
- sandbox_seed           : optional {relative_path: content} map written into
                           the sandbox before the run (synthetic .memory
                           trees for session-start fixtures)
- expect_write_glob      : optional glob, evaluated relative to the sandbox
                           dir, asserted non-empty after the run
- expect_stdout_contains : optional substring (or list of substrings)
                           asserted in stdout

Lines starting with ``#`` are comments and are skipped, so a fixture file can
carry a header explaining what it pins (see
``path-allowlist-override-scope.jsonl``).

--hooks-dir (or env TIER1_HOOKS_DIR) points the same fixtures at an alternate
hooks directory — used by ``--self-check``, the discrimination check: a
deliberately neutered hook copy must make these fixtures FAIL.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HOOKS = ROOT / ".claude" / "hooks"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "hooks"

# Ambient env vars that CHANGE a hook's decision. Fixtures assert the hooks'
# controlled behavior, so these must NOT leak in from the launching shell and
# silently defeat a fixture — e.g. a port session exports SACRED_OVERRIDE_FILES
# (to edit authorized sacred files), which would flip every sacred-block
# fixture to allow. A fixture that WANTS one of these sets it explicitly via
# its own "env" object. (Playbooks template lesson, its BK-0007.)
_AMBIENT_OVERRIDES = frozenset(
    {
        "SACRED_OVERRIDE_FILES",
        "CLAUDE_WRITE_ORIGIN",
        "SESSION_START_CONTEXT",
        "SESSION_START_RECALL",
        "POLICY_ENGINE_DISABLED",
        "POLICY_ENGINE_POLICY_INLINE",
        "POLICY_ENGINE_POLICY_DIR",
        "POLICY_ENGINE_STATE_DIR",
        "POLICY_ENGINE_MAP_PATH",
    }
)


def run_fixture(fx: dict, hooks_dir: Path) -> tuple[bool, str]:
    hook = hooks_dir / fx["hook"]
    if not hook.exists():
        return False, f"hook not found: {hook}"
    stdin_data = fx["payload_raw"] if "payload_raw" in fx else json.dumps(fx["payload"])
    env = {k: v for k, v in os.environ.items() if k not in _AMBIENT_OVERRIDES}
    env.update(
        {
            "CLAUDE_PROJECT_DIR": str(ROOT),
            "BLOCK_RECORD_DIR": tempfile.gettempdir() + "/_tier1_eval_blk",
            "OBSERVATION_DIR": tempfile.gettempdir() + "/_tier1_eval_obs",
        }
    )
    env.update(fx.get("env", {}))  # fixture-declared overrides (opt-in)

    # A fixture that names a RELATIVE policy-engine state dir means "a private
    # one" — resolve it under the temp dir so a run never writes session state
    # into the repo (and two fixtures never share a latched gate epoch).
    rel_state = env.get("POLICY_ENGINE_STATE_DIR")
    if rel_state and not os.path.isabs(rel_state):
        env["POLICY_ENGINE_STATE_DIR"] = os.path.join(tempfile.gettempdir(), rel_state)

    sandbox: tempfile.TemporaryDirectory[str] | None = None
    try:
        if fx.get("sandbox_project_dir"):
            sandbox = tempfile.TemporaryDirectory(prefix="tier1_sandbox_")
            env["CLAUDE_PROJECT_DIR"] = sandbox.name
            for rel, content in (fx.get("sandbox_seed") or {}).items():
                seed_path = Path(sandbox.name) / rel
                seed_path.parent.mkdir(parents=True, exist_ok=True)
                seed_path.write_text(content, encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(hook)],
            input=stdin_data,
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            env=env,
        )
        got, want = proc.returncode, fx["expect_exit"]
        if got != want:
            return False, f"expected exit {want}, got {got} (stderr: {proc.stderr.strip()[:200]})"
        needle = fx.get("expect_stderr_contains")
        if needle and needle not in proc.stderr:
            return False, f"stderr missing {needle!r} (stderr: {proc.stderr.strip()[:200]})"
        out_needles = fx.get("expect_stdout_contains")
        if out_needles:
            if isinstance(out_needles, str):
                out_needles = [out_needles]
            for n in out_needles:
                if n not in proc.stdout:
                    return False, f"stdout missing {n!r} (stdout: {proc.stdout.strip()[:200]})"
        glob_pat = fx.get("expect_write_glob")
        if glob_pat:
            base = Path(sandbox.name) if sandbox else ROOT
            if not list(base.glob(glob_pat)):
                return False, f"expected write matching {glob_pat!r} under {base}, found none"
        return True, "ok"
    finally:
        if sandbox is not None:
            sandbox.cleanup()


# Hooks whose blocking behavior the self-check neuters. Each is replaced by a
# stub that always exits 0 — the classic fail-open shape, and literally what
# the archived bash hooks did on a jq-less machine.
_NEUTERABLE = (
    "path-allowlist.py",
    "no-secrets-in-bash.py",
    "no-deploy.py",
    "commit-guard.py",
    "provenance-guard.py",
    "curator-write-allowlist.py",
    "citation-required.py",
)

_FAIL_OPEN_STUB = "import sys\nsys.stdin.read()\nsys.exit(0)\n"


def self_check() -> int:
    """Prove the fixtures can go RED (.claude/rules/vertical_slice_loops.md,
    "the discriminating-check rule": a check that cannot fail is worse than no
    check, because it certifies the gap it was meant to catch).

    Copies the hooks into a temp dir, replaces every blocking guard with a
    fail-open stub, and asserts the suite FAILS. If it passes there, the
    fixtures are not actually exercising the block decision.
    """
    with tempfile.TemporaryDirectory(prefix="tier1_selfcheck_") as tmp:
        broken = Path(tmp) / "hooks"
        shutil.copytree(DEFAULT_HOOKS, broken, ignore=shutil.ignore_patterns("__pycache__"))
        for name in _NEUTERABLE:
            target = broken / name
            if target.exists():
                target.write_text(_FAIL_OPEN_STUB, encoding="utf-8")
        print("== self-check: running the SAME fixtures against fail-open stubs ==")
        rc = _run_suite(broken, quiet=True)
    if rc == 0:
        print(
            "\nSELF-CHECK FAILED: the suite PASSED against hooks that block nothing.\n"
            "The fixtures are not exercising the block decision — fix them before\n"
            "trusting a green Tier-1 run.",
            file=sys.stderr,
        )
        return 1
    print("\nSELF-CHECK OK: neutered hooks make the suite go RED, as required.")
    return 0


def _run_suite(hooks_dir: Path, quiet: bool = False) -> int:
    fixture_files = sorted(FIXTURES.glob("*.jsonl"))
    if not fixture_files:
        print("no fixtures found under", FIXTURES)
        return 2
    total = passed = 0
    failures: list[str] = []
    for ff in fixture_files:
        if not quiet:
            print(f"\n# {ff.name}")
        for line in ff.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            fx = json.loads(line)
            total += 1
            ok, detail = run_fixture(fx, hooks_dir)
            tag = f"[{fx.get('eval_type', 'checkpoint')}]"
            if ok:
                passed += 1
                if not quiet:
                    print(f"  PASS {tag} {fx['desc']}")
            else:
                failures.append(f"{ff.name}: {fx['desc']} — {detail}")
                if not quiet:
                    print(f"  FAIL {tag} {fx['desc']} — {detail}")
    print(f"\nTier-1: {passed}/{total} passed (hooks: {hooks_dir})")
    if failures:
        if not quiet:
            print("\nFAILURES:")
            for f in failures:
                print("  -", f)
        else:
            print(f"  ({len(failures)} fixture(s) failed, as the self-check requires)")
        return 1
    return 0


def main(argv: list[str]) -> int:
    if "--self-check" in argv[1:]:
        return self_check()
    hooks_dir = DEFAULT_HOOKS
    env_override = os.environ.get("TIER1_HOOKS_DIR")
    if env_override:
        hooks_dir = Path(env_override)
    args = list(argv[1:])
    if "--hooks-dir" in args:
        i = args.index("--hooks-dir")
        try:
            hooks_dir = Path(args[i + 1])
        except IndexError:
            print("usage: python eval/run_tier1.py [--hooks-dir PATH]", file=sys.stderr)
            return 2
    if not hooks_dir.is_dir():
        print(f"hooks dir not found: {hooks_dir}", file=sys.stderr)
        return 2
    return _run_suite(hooks_dir)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
