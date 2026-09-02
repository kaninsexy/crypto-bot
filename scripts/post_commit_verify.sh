#!/usr/bin/env bash
# post_commit_verify.sh — --no-verify bypass detection.
# Ported 2026-09-02 from siamese-reconcile (governance port, S1.4).
#
# git skips pre-commit under --no-verify, but it does NOT skip post-commit.
# scripts/pre_commit_backlog_check.sh writes a sentinel
# ( $GIT_DIR/crypto_bot_precommit_sentinel ) carrying the staged tree hash:
# "started" at entry, "passed" on success. This hook compares the just-created
# commit's tree against it:
#
#   sentinel status=passed + matching tree  -> pre-commit ran green: clean.
#   sentinel status=started + matching tree -> pre-commit ran, FAILED, and the
#                                              commit happened anyway: VIOLATION.
#   sentinel absent / tree mismatch         -> pre-commit never ran for this
#                                              content (--no-verify): VIOLATION.
#
# A violation cannot be un-committed here (post-commit can't block), but the
# evidence SURVIVES: a JSON record is appended to the observability path used
# by .claude/hooks/observe.py (.memory/T1_episodic/observations/<date>/,
# OBSERVATION_DIR overrides for tests) and a loud warning is printed. Mandate
# L's "--no-verify is forbidden without owner authorization" is now auditable.
#
# The sentinel is consumed either way, so one pre-commit pass can never vouch
# for more than one commit. Always exits 0 (recording, not gating).
set -uo pipefail

GIT_DIR="$(git rev-parse --git-dir 2>/dev/null || echo .git)"
SENTINEL="$GIT_DIR/crypto_bot_precommit_sentinel"
SHA="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
COMMIT_TREE="$(git rev-parse 'HEAD^{tree}' 2>/dev/null || echo unknown)"
NOW_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

PY=""
if command -v python >/dev/null 2>&1; then PY=python
elif command -v python3 >/dev/null 2>&1; then PY=python3
fi

read_field() { # read_field <key>
  [ -n "$PY" ] || { echo ""; return; }
  "$PY" -c 'import json,sys;print(json.load(open(sys.argv[1])).get(sys.argv[2],""))' \
    "$SENTINEL" "$1" 2>/dev/null || echo ""
}

verdict="clean"
reason=""
if [ -f "$SENTINEL" ]; then
  sent_tree="$(read_field tree)"
  sent_status="$(read_field status)"
  rm -f "$SENTINEL" 2>/dev/null || true   # consumed: one pass vouches once
  if [ "$sent_tree" = "$COMMIT_TREE" ] && [ "$sent_status" = "passed" ]; then
    verdict="clean"
  elif [ "$sent_tree" = "$COMMIT_TREE" ]; then
    verdict="violation"
    reason="pre-commit ran and FAILED for this exact tree, but the commit was created anyway (--no-verify after a failing gate)"
  else
    verdict="violation"
    reason="pre-commit sentinel does not match this commit's tree — pre-commit never ran for this content (--no-verify)"
  fi
else
  verdict="violation"
  reason="no pre-commit sentinel — pre-commit never ran (--no-verify)"
fi

if [ "$verdict" = "violation" ]; then
  PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
  OBS_DIR="${OBSERVATION_DIR:-$PROJECT_DIR/.memory/T1_episodic/observations/$(date -u +%Y-%m-%d)}"
  mkdir -p "$OBS_DIR" 2>/dev/null || true
  REC="$OBS_DIR/$(date -u +%H%M%S)_violation_commit_gate.json"
  printf '{"ts": "%s", "event": "commit_gate_violation", "sha": "%s", "detail": "pre-commit bypassed -- Mandate L requires owner authorization for --no-verify", "reason": "%s"}\n' \
    "$NOW_TS" "$SHA" "$reason" > "$REC" 2>/dev/null || true
  echo "==================================================================" >&2
  echo "[commit-gate] VIOLATION: $reason" >&2
  echo "[commit-gate] commit $SHA was created WITHOUT a passing pre-commit run." >&2
  echo "[commit-gate] Mandate L: --no-verify requires explicit owner authorization." >&2
  echo "[commit-gate] Recorded: $REC" >&2
  echo "==================================================================" >&2
fi
exit 0
