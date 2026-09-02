#!/usr/bin/env bash
# Mandate L enforcement (pre-commit). If a staged file SURFACES a new gap
# (keywords below) but no matching append to .memory/T2_semantic/backlog.jsonl
# is staged in the same commit, BLOCK the commit. Also validates backlog lines
# against schemas/backlog.schema.json (staged version when staged, else the
# working-tree file — so this doubles as a standalone gate-suite check:
# `bash scripts/pre_commit_backlog_check.sh` on a clean tree must exit 0).
#
# Ported 2026-09-02 from siamese-reconcile (governance port, S1.4).
#
# Install:  bash scripts/install_git_hooks.sh   — installs marker-delimited
#           blocks in .git/hooks/{pre-commit,commit-msg,post-commit} that
#           COEXIST with crypto-bot's existing hook content: the queue
#           validator, .githooks/pre-commit (sacred diff block + magic-number
#           gate + pytest -m fast) and .githooks/commit-msg (empty-message +
#           [mandate-H] gate) all keep running, unweakened.
#
# Bypass with --no-verify is FORBIDDEN without explicit owner authorization
# (Mandate L) — and it is DETECTED: this gate writes a sentinel
# ( $GIT_DIR/crypto_bot_precommit_sentinel ) with the staged tree hash, stamped
# "started" at entry and "passed" on success; scripts/post_commit_verify.sh
# (which --no-verify does NOT skip) compares the just-created commit against
# it and records a loud, persistent violation when pre-commit never ran — or
# ran, FAILED, and was bypassed anyway.
#
# Every failure carries a stable GATE_* reason code naming what unblocks it.
set -euo pipefail

cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"

KEYWORDS='surfaced|pivot_queue|finding #|deferred|fix later|for next session|TODO\(backlog\)'
BACKLOG='.memory/T2_semantic/backlog.jsonl'
SCHEMA='schemas/backlog.schema.json'
VALIDATOR='scripts/validate_backlog.py'

# python3 is a broken pyenv-win shim on this dev machine while `python` is the
# working 3.12 install (same finding as .claude/hooks/commit-scope-audit.sh).
# Try `python` first, fall back to `python3` elsewhere.
PY=""
if command -v python >/dev/null 2>&1; then PY=python
elif command -v python3 >/dev/null 2>&1; then PY=python3
fi

# ── bypass-detection sentinel (start) ────────────────────────────────────────
GIT_DIR="$(git rev-parse --git-dir 2>/dev/null || echo .git)"
SENTINEL="$GIT_DIR/crypto_bot_precommit_sentinel"
STAGED_TREE="$(git write-tree 2>/dev/null || echo unknown)"
NOW_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
printf '{"tree": "%s", "status": "started", "ts": "%s"}\n' \
  "$STAGED_TREE" "$NOW_TS" > "$SENTINEL" 2>/dev/null || true

fail() { # fail <GATE_CODE> <message> <unblocking action>
  echo "[backlog-check] BLOCKED ($1): $2" >&2
  echo "[backlog-check] To unblock: $3" >&2
  exit 1
}

staged() { git diff --cached --name-only --diff-filter=ACM; }

# ── GATE_BACKLOG_SCHEMA_INVALID ──────────────────────────────────────────────
# Validate the staged backlog when staged; otherwise the working-tree file
# (standalone / gate-suite mode). Every line must parse AND satisfy the schema.
backlog_staged=0
if git diff --cached --name-only 2>/dev/null | grep -q "^${BACKLOG//./\\.}$"; then
  backlog_staged=1
fi

if [ -n "$PY" ] && [ -f "$VALIDATOR" ]; then
  if [ "$backlog_staged" -eq 1 ]; then
    if ! git show ":$BACKLOG" 2>/dev/null | "$PY" "$VALIDATOR" -; then
      fail "GATE_BACKLOG_SCHEMA_INVALID" \
        "the staged $BACKLOG has a line that is invalid JSON or fails $SCHEMA (see above)." \
        "fix the offending line (one JSON object per line; required: id BK-NNNN, status, title, severity, owner, created_at) and re-stage."
    fi
  elif [ -f "$BACKLOG" ]; then
    if ! "$PY" "$VALIDATOR" "$BACKLOG"; then
      fail "GATE_BACKLOG_SCHEMA_INVALID" \
        "$BACKLOG (working tree) has a line that is invalid JSON or fails $SCHEMA (see above)." \
        "fix the offending line (one JSON object per line; required: id BK-NNNN, status, title, severity, owner, created_at)."
    fi
  fi
fi

# ── GATE_BACKLOG_MISSING_ENTRY: surfaced gap without a staged backlog row ────
surfacing=0
surfacing_files=""
while IFS= read -r f; do
  [ -z "$f" ] && continue
  case "$f" in
    "$BACKLOG") continue ;;
    # The gate script itself defines the keywords — its own staged source is
    # the scanner's definition, not a surfaced gap (else committing the gate
    # would false-block on its own keyword list).
    scripts/pre_commit_backlog_check.sh) continue ;;
    # Generated mirrors of the repo quote file contents verbatim; a keyword
    # inside them is a copy of an already-tracked line, not a newly surfaced
    # gap. Without this the auto post-commit repomix chore commit would
    # false-block.
    repomix-output.xml) continue ;;
    graphify-out/*) continue ;;
    # The rule docs and the archive README DESCRIBE the discipline; quoting
    # "deferred" or "fix later" in prose is not surfacing a gap.
    .claude/rules/*) continue ;;
    .claude/hooks/_archive_bash_2026-09/*) continue ;;
  esac
  if git diff --cached -U0 -- "$f" | grep -E '^\+' | grep -Eiq "$KEYWORDS"; then
    echo "[backlog-check] surfacing keyword found in staged: $f" >&2
    surfacing=1
    surfacing_files="$surfacing_files $f"
  fi
done < <(staged)

if [ "$surfacing" -eq 1 ]; then
  if [ "$backlog_staged" -eq 1 ]; then
    echo "[backlog-check] OK: backlog.jsonl is also staged — assuming the item was recorded." >&2
  else
    fail "GATE_BACKLOG_MISSING_ENTRY" \
      "a surfaced item in$surfacing_files is not recorded in $BACKLOG (Mandate L)." \
      "append a BK-NNNN entry to $BACKLOG, 'git add' it into this commit, and retry — or get explicit owner sign-off."
  fi
fi

# ── sentinel: mark this pre-commit pass as PASSED ────────────────────────────
printf '{"tree": "%s", "status": "passed", "ts": "%s"}\n' \
  "$STAGED_TREE" "$NOW_TS" > "$SENTINEL" 2>/dev/null || true
exit 0
