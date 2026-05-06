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

# --- 3. Trial queue (one batch then exit) ---------------------------
"=== trial_queue at $Timestamp ===" | Out-File -Append -FilePath $QueueLog -Encoding utf8
$QueueScript = Join-Path $RepoRoot "scripts\run_trial_queue.py"
python $QueueScript --once 2>&1 | Out-File -Append -FilePath $QueueLog -Encoding utf8

exit $LASTEXITCODE
