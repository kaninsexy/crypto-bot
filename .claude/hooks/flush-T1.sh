#!/usr/bin/env bash
# SubagentStop hook. Flushes the agent's T0 working scratch to T1
# episodic on stop, then truncates T0. Append-only to T1.
# Path scheme per architecture.md A.4 step 3:
#   .memory/T1_episodic/episodes/<UTC-date>/<agent>/<UTC-time>.jsonl
INPUT=$(cat)
CWD=$(echo "$INPUT" | jq -r '.cwd // ""')
[ -z "$CWD" ] && CWD="$PWD"

AGENT_NAME=$(echo "$INPUT" | jq -r '.agent_name // ""')
if [ -z "$AGENT_NAME" ] || [ "$AGENT_NAME" = "null" ]; then
  TRANSCRIPT_PATH=$(echo "$INPUT" | jq -r '.transcript_path // ""')
  if [ -n "$TRANSCRIPT_PATH" ]; then
    AGENT_NAME=$(basename "$TRANSCRIPT_PATH" .jsonl)
  fi
fi
if [ -z "$AGENT_NAME" ]; then
  AGENT_NAME="unknown"
fi

SCRATCH="$CWD/.memory/T0_working/$AGENT_NAME/scratch.md"
if [ ! -s "$SCRATCH" ]; then
  exit 0
fi

UTC_DATE=$(date -u +%Y-%m-%d)
UTC_TIME=$(date -u +%H%M%S)
UTC_ISO=$(date -u +%FT%TZ)
DEST_DIR="$CWD/.memory/T1_episodic/episodes/$UTC_DATE/$AGENT_NAME"
mkdir -p "$DEST_DIR" || exit 1
DEST="$DEST_DIR/$UTC_TIME.jsonl"

jq -nc \
  --arg ts "$UTC_ISO" \
  --arg agent "$AGENT_NAME" \
  --rawfile content "$SCRATCH" \
  '{ts:$ts, agent:$agent, content:$content}' >> "$DEST" || exit 1

: > "$SCRATCH"
exit 0
