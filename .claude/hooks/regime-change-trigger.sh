#!/usr/bin/env bash
# Regime-change file-watcher (every-minute cron).
# Compares the current deterministic regime label in regime.txt to the
# last-seen value in regime_last_seen.txt; on change, writes
# analyst_trigger.txt = triggered=true so the next research-manager cron
# tick fires an extra cycle. The 8h research-manager prompt's step 1
# consumes the trigger by writing triggered=false before proceeding.
#
# First run is a no-op: regime_last_seen.txt is created from the current
# regime.txt value. Missing regime.txt is also a no-op (cold-start /
# detector-not-yet-running case).
#
# This hook is intentionally idempotent and side-effect-free except for
# writes to the two state files under .memory/T1_episodic/_state/.
# Architecture.md F.2 Day 14, D.4 step 1 (trigger consumption).

set -u

REPO_ROOT="${REPO_ROOT:-$HOME/dev/crypto-bot}"
STATE_DIR="$REPO_ROOT/.memory/T1_episodic/_state"
REGIME_FILE="$STATE_DIR/regime.txt"
LAST_SEEN_FILE="$STATE_DIR/regime_last_seen.txt"
TRIGGER_FILE="$STATE_DIR/analyst_trigger.txt"

mkdir -p "$STATE_DIR" || exit 1

if [ ! -s "$REGIME_FILE" ]; then
  exit 0
fi

CURRENT=$(tr -d '[:space:]' < "$REGIME_FILE" 2>/dev/null)
if [ -z "$CURRENT" ]; then
  exit 0
fi

if [ ! -s "$LAST_SEEN_FILE" ]; then
  printf '%s\n' "$CURRENT" > "$LAST_SEEN_FILE"
  exit 0
fi

LAST=$(tr -d '[:space:]' < "$LAST_SEEN_FILE" 2>/dev/null)

if [ "$CURRENT" = "$LAST" ]; then
  exit 0
fi

UTC_ISO=$(date -u +%FT%TZ)
printf 'triggered=true ts=%s\n' "$UTC_ISO" > "$TRIGGER_FILE"
printf '%s\n' "$CURRENT" > "$LAST_SEEN_FILE"
exit 0
