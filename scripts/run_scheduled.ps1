# scripts/run_scheduled.ps1 -- Task Scheduler entry point for
# \CryptoBotTrialQueue. Replaces the prior direct-python action.
#
# 1. cd to the repo root (derived from $PSScriptRoot so the script
#    works from any CWD Task Scheduler hands us)
# 2. git pull --ff-only with logging (continue on failure)
# 3. python scripts\run_trial_queue.py --once with logging
#
# Designed to run unattended under SYSTEM or "Run whether user is
# logged on or not". No interactive prompts, no env assumptions.

$ErrorActionPreference = "Continue"

# Repo root is $PSScriptRoot's parent (this script lives in scripts\).
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location -Path $RepoRoot

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

# --- 4. Trial queue (one batch then exit) ---------------------------
"=== trial_queue at $Timestamp ===" | Out-File -Append -FilePath $QueueLog -Encoding utf8
$QueueScript = Join-Path $RepoRoot "scripts\run_trial_queue.py"
python $QueueScript --once 2>&1 | Out-File -Append -FilePath $QueueLog -Encoding utf8

exit $LASTEXITCODE
