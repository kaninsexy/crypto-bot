#!/usr/bin/env bash
# Hook validation tests. Run from repo root: bash .claude/hooks/_test_hooks.sh
# Surfaces pass/fail counts. Exits non-zero if any test fails.
set -u
PASS=0
FAIL=0
note() { echo "--- $* ---"; }
ok()   { PASS=$((PASS+1)); echo "PASS: $*"; }
no()   { FAIL=$((FAIL+1)); echo "FAIL: $*"; }

CWD="$(pwd)"
STATE_DIR="$CWD/.memory/T1_episodic/_state"
COUNTER="$STATE_DIR/phase4b_failure_count.txt"
mkdir -p "$STATE_DIR"

# ---- failcount-update.sh ----
note "failcount-update.sh"

echo '0' > "$COUNTER"
echo '{"tool_response":{"stdout":"VERDICT=PASS\n"}}' \
  | .claude/hooks/failcount-update.sh
RC=$?
if [ "$RC" -eq 0 ] && [ "$(cat "$COUNTER")" = "0" ]; then ok "PASS marker resets counter to 0"; else no "PASS marker (rc=$RC, counter=$(cat "$COUNTER"))"; fi

echo '0' > "$COUNTER"
echo '{"tool_response":{"stdout":"VERDICT=FAIL\n"}}' \
  | .claude/hooks/failcount-update.sh
RC=$?
if [ "$RC" -eq 0 ] && [ "$(cat "$COUNTER")" = "1" ]; then ok "FAIL marker increments counter from 0 to 1"; else no "FAIL marker (rc=$RC, counter=$(cat "$COUNTER"))"; fi

echo '2' > "$COUNTER"
echo '{"tool_response":{"stdout":"VERDICT=FAIL\n"}}' \
  | .claude/hooks/failcount-update.sh >/dev/null 2>&1
RC=$?
if [ "$RC" -eq 0 ] && [ "$(cat "$COUNTER")" = "3" ]; then ok "FAIL marker increments counter from 2 to 3"; else no "FAIL increment from 2 (rc=$RC, counter=$(cat "$COUNTER"))"; fi

echo '0' > "$COUNTER"
echo '{"tool_response":{"stdout":"unrelated output\n"}}' \
  | .claude/hooks/failcount-update.sh
RC=$?
if [ "$RC" -eq 0 ] && [ "$(cat "$COUNTER")" = "0" ]; then ok "no marker is a no-op"; else no "no-marker (rc=$RC, counter=$(cat "$COUNTER"))"; fi

echo '0' > "$COUNTER"
echo '{"tool_response":{"stdout":"VERDICT=PASS\nVERDICT=FAIL\n"}}' \
  | .claude/hooks/failcount-update.sh >/dev/null 2>&1
RC=$?
if [ "$RC" -eq 2 ]; then ok "two markers exit 2 (ambiguous)"; else no "ambiguous markers (rc=$RC)"; fi

# Restore counter
echo '0' > "$COUNTER"

# ---- path-allowlist.sh ----
note "path-allowlist.sh"

echo '{"tool_input":{"file_path":".memory/T2_semantic/facts.md"}}' \
  | .claude/hooks/path-allowlist.sh >/dev/null 2>&1
RC=$?
if [ "$RC" -eq 2 ]; then ok "blocks non-allowlisted path (facts.md)"; else no "block non-allowlisted (rc=$RC)"; fi

echo '{"tool_input":{"file_path":".memory/T2_semantic/_pending_review.jsonl"}}' \
  | .claude/hooks/path-allowlist.sh
RC=$?
if [ "$RC" -eq 0 ]; then ok "accepts allowlisted path (_pending_review.jsonl)"; else no "accept allowlisted (rc=$RC)"; fi

echo '{"tool_input":{"file_path":".memory/T2_semantic/citations/foo.md"}}' \
  | .claude/hooks/path-allowlist.sh >/dev/null 2>&1
RC=$?
if [ "$RC" -eq 2 ]; then ok "blocks T2 sibling (citations/)"; else no "block citations (rc=$RC)"; fi

# ---- commit-format.sh ----
note "commit-format.sh"

echo '{"tool_input":{"command":"git commit -F - <<EOF\nadd thing\nEOF"}}' \
  | .claude/hooks/commit-format.sh >/dev/null 2>&1
RC=$?
if [ "$RC" -eq 2 ]; then ok "blocks bad subject (no conventional prefix)"; else no "block bad subject (rc=$RC)"; fi

echo '{"tool_input":{"command":"git commit -F - <<EOF\nfeat(curator): add hourly cron agent\nEOF"}}' \
  | .claude/hooks/commit-format.sh
RC=$?
if [ "$RC" -eq 0 ]; then ok "accepts good non-agent commit (no trailer, no token)"; else no "accept good non-agent (rc=$RC)"; fi

echo '{"tool_input":{"command":"git commit -m \"feat(scope): inline\""}}' \
  | .claude/hooks/commit-format.sh >/dev/null 2>&1
RC=$?
if [ "$RC" -eq 2 ]; then ok "blocks commit without heredoc body"; else no "no-heredoc (rc=$RC)"; fi

# Trailer-based agent detection (architecture.md E.3)
echo '{"tool_input":{"command":"git commit -F - <<EOF\nfeat(scope): description\n\n[mandate-H]\n\nCo-authored-by: Claude <noreply@anthropic.com>\nEOF"}}' \
  | .claude/hooks/commit-format.sh
RC=$?
if [ "$RC" -eq 0 ]; then ok "accepts agent commit (Co-authored-by + [mandate-H])"; else no "agent w/ trailer + token (rc=$RC)"; fi

echo '{"tool_input":{"command":"git commit -F - <<EOF\nfeat(scope): description\n\nCo-authored-by: Claude <noreply@anthropic.com>\nEOF"}}' \
  | .claude/hooks/commit-format.sh >/dev/null 2>&1
RC=$?
if [ "$RC" -eq 2 ]; then ok "blocks agent commit (Co-authored-by, no token)"; else no "agent w/ trailer no token (rc=$RC)"; fi

echo '{"tool_input":{"command":"git commit -F - <<EOF\nfeat(scope): description\n\nbody only\nEOF"}}' \
  | .claude/hooks/commit-format.sh
RC=$?
if [ "$RC" -eq 0 ]; then ok "accepts human commit (no trailer, no token)"; else no "human no trailer no token (rc=$RC)"; fi

echo '{"tool_input":{"command":"git commit -F - <<EOF\nfeat(scope): description\n\n[mandate-H]\n\nbody\nEOF"}}' \
  | .claude/hooks/commit-format.sh
RC=$?
if [ "$RC" -eq 0 ]; then ok "accepts human commit (no trailer, voluntary token)"; else no "human no trailer voluntary token (rc=$RC)"; fi

# ---- flush-T1.sh ----
note "flush-T1.sh"

TEST_AGENT="test-flush-agent-$$"
SCRATCH_DIR="$CWD/.memory/T0_working/$TEST_AGENT"
mkdir -p "$SCRATCH_DIR"
echo "scratch test content" > "$SCRATCH_DIR/scratch.md"
TODAY=$(date -u +%Y-%m-%d)
T1_DIR="$CWD/.memory/T1_episodic/episodes/$TODAY/$TEST_AGENT"
echo "{\"agent_name\":\"$TEST_AGENT\",\"cwd\":\"$CWD\"}" \
  | .claude/hooks/flush-T1.sh
RC=$?
if [ "$RC" -eq 0 ] && [ ! -s "$SCRATCH_DIR/scratch.md" ] && [ -d "$T1_DIR" ] && [ -n "$(ls -A "$T1_DIR" 2>/dev/null)" ]; then
  ok "flush-T1 copies content and truncates scratch"
else
  no "flush-T1 (rc=$RC, scratch_size=$(wc -c < "$SCRATCH_DIR/scratch.md" 2>/dev/null), t1_dir_listing=$(ls -A "$T1_DIR" 2>/dev/null))"
fi
# Verify JSONL content
if [ -d "$T1_DIR" ]; then
  T1_FILE=$(ls -1 "$T1_DIR" | head -n1)
  if [ -n "$T1_FILE" ] && jq -e '.content' "$T1_DIR/$T1_FILE" >/dev/null 2>&1; then
    ok "flush-T1 wrote valid JSONL with .content key"
  else
    no "flush-T1 JSONL structure"
  fi
fi
# Cleanup
rm -rf "$SCRATCH_DIR" "$T1_DIR"

# Empty scratch -> no-op
mkdir -p "$SCRATCH_DIR"
: > "$SCRATCH_DIR/scratch.md"
echo "{\"agent_name\":\"$TEST_AGENT\",\"cwd\":\"$CWD\"}" \
  | .claude/hooks/flush-T1.sh
RC=$?
if [ "$RC" -eq 0 ] && [ ! -d "$T1_DIR" ]; then ok "flush-T1 no-op on empty scratch"; else no "flush-T1 empty no-op (rc=$RC, t1_dir_exists=$([ -d "$T1_DIR" ] && echo y || echo n))"; fi
rm -rf "$SCRATCH_DIR"

# ---- run-tests-fast.sh ----
note "run-tests-fast.sh (smoke)"
echo "{\"cwd\":\"$CWD\"}" | .claude/hooks/run-tests-fast.sh >/dev/null 2>&1
RC=$?
# Acceptable: 0 (passed or no-tests-soft-skip). Anything else = real test failure.
if [ "$RC" -eq 0 ]; then
  ok "run-tests-fast smoke (rc=0; tests passed or soft-skipped)"
else
  no "run-tests-fast smoke (rc=$RC); investigate before committing"
fi

# ---- summary ----
echo ""
echo "================================="
echo "PASS: $PASS  FAIL: $FAIL"
echo "================================="
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
