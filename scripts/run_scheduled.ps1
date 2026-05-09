# scripts/run_scheduled.ps1 -- Task Scheduler entry point for
# \CryptoBotTrialQueue. Replaces the prior direct-python action.
#
# 1. cd to the repo root (derived from $PSScriptRoot so the script
#    works from any CWD Task Scheduler hands us)
# 2. git pull --ff-only with logging (continue on failure)
# 3. python scripts\run_trial_queue.py --continuous with logging
#
# Designed to run unattended under SYSTEM or "Run whether user is
# logged on or not". No interactive prompts, no env assumptions.
#
# 2026-05-08: switched --once -> --continuous. The cron tick fires
# hourly; each tick that finds the lock free starts a continuous
# orchestrator run that holds the lock for up to 4h (the wall budget
# in run_trial_queue.py:MAX_ORCHESTRATOR_WALL_S). Subsequent hourly
# ticks see the lock held and no-op cleanly. PC utilisation under
# this mode is ~95% (vs ~16% under --once with hourly cron).

$ErrorActionPreference = "Continue"

# Repo root is $PSScriptRoot's parent (this script lives in scripts\).
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location -Path $RepoRoot

# --- 0. Kill switch (early-exit before any work) --------------------
# 2026-05-09: Phase 4 crypto grinding paused per Kanin -- pivot to
# Phase 6 (IBKR equities). Touch C:\crypto-bot\.cron-pause to stop
# cron from running any LLM-spending work. Remove the file to
# resume. Reversible without touching Task Scheduler.
$PauseFile = Join-Path $RepoRoot ".cron-pause"
if (Test-Path $PauseFile) {
    $PauseLog = Join-Path $RepoRoot "logs\cron_pause.log"
    "=== cron paused (skipped) at $(Get-Date -Format 'yyyy-MM-ddTHH:mm:ssK') ===" | `
        Out-File -Append -FilePath $PauseLog -Encoding utf8
    exit 0
}

# Ensure logs directory exists (idempotent).
$LogDir = Join-Path $RepoRoot "logs"
if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
}

$GitPullLog  = Join-Path $LogDir "git_pull.log"
$WarmLog     = Join-Path $LogDir "warm_trends.log"
$QueueLog    = Join-Path $LogDir "trial_queue.log"
$Timestamp   = Get-Date -Format "yyyy-MM-ddTHH:mm:ssK"

# --- 1. git pull --ff-only (failure is logged but does not abort) ----
"=== git pull at $Timestamp ===" | Out-File -Append -FilePath $GitPullLog -Encoding utf8
try {
    git pull --ff-only 2>&1 | Out-File -Append -FilePath $GitPullLog -Encoding utf8
    $PullExit = $LASTEXITCODE
    if ($PullExit -ne 0) {
        "git pull exited $PullExit -- continuing to run queue anyway" |
            Out-File -Append -FilePath $GitPullLog -Encoding utf8
    }
} catch {
    "git pull threw exception: $_ -- continuing to run queue anyway" |
        Out-File -Append -FilePath $GitPullLog -Encoding utf8
}

# --- 2. Force UTF-8 in the python subprocess so cp1252 doesn't crash --
$env:PYTHONIOENCODING = "utf-8"

# --- 3. Pre-warm Google Trends cache before the orchestrator runs ---
# Any Google Trends strategy in the queue (e.g. AttentionMomentum,
# ContrarianSearchVolume) hits Trends 429 immediately if the parquet
# cache is cold. Warm step is non-fatal: failures log and continue
# so a Trends outage does not block non-GT strategies.
"=== warm_trends at $Timestamp ===" | Out-File -Append -FilePath $WarmLog -Encoding utf8
$WarmScript = Join-Path $RepoRoot "scripts\warm_google_trends_cache.py"
python $WarmScript 2>&1 | Out-File -Append -FilePath $WarmLog -Encoding utf8
$WarmExit = $LASTEXITCODE
if ($WarmExit -ne 0) {
    "warm_google_trends_cache exited $WarmExit -- continuing to run queue anyway" |
        Out-File -Append -FilePath $WarmLog -Encoding utf8
}

# --- 4. Trial queue (continuous: drain queue then exit) -------------
# --continuous: orchestrator loops until queue is empty, wall-budget
# breaker fires (4h), or no-progress breaker fires (2 consecutive
# zero-progress batches). All three breakers are defined in
# run_trial_queue.py and are independent of the cron schedule.
"=== trial_queue at $Timestamp ===" | Out-File -Append -FilePath $QueueLog -Encoding utf8
$QueueScript = Join-Path $RepoRoot "scripts\run_trial_queue.py"
python $QueueScript --continuous 2>&1 | Out-File -Append -FilePath $QueueLog -Encoding utf8
$QueueExit = $LASTEXITCODE

# --- 5. Phase 5 paper-ledger resolution update ----------------------
# Polls Polymarket Gamma /markets/{id} for each open paper-traded
# entry and updates status + realized P&L on resolution. Cheap
# (read-only API calls), idempotent, runs every cron tick so
# resolutions are detected within an hour of finalization on UMA.
# Failures are logged but do not abort the cron tick (no abort here
# anyway; this is the last step). Independent of trial-queue.
$LedgerLog = Join-Path $LogDir "paper_ledger.log"
"=== paper_ledger update at $Timestamp ===" | Out-File -Append -FilePath $LedgerLog -Encoding utf8
$LedgerScript = Join-Path $RepoRoot "phase5\paper_ledger.py"
if (Test-Path $LedgerScript) {
    python $LedgerScript update 2>&1 | Out-File -Append -FilePath $LedgerLog -Encoding utf8
}

exit $QueueExit
