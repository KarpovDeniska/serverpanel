# watchdog.ps1 -- runs AFTER backup.ps1 in the trigger.cmd wrapper.
#
# Purpose: if backup.ps1 did NOT produce a fresh report.json (e.g. the
# Task-Scheduler ExecutionTimeLimit killed it mid-run), send a Telegram
# alert ourselves so silence cannot be mistaken for success.
#
# backup.ps1 itself already sends a final ?/??/? when it finishes
# normally -- so the watchdog only speaks up in the killed-or-crashed case.

param(
    [Parameter(Mandatory = $true)][string]$PlanPath,
    [Parameter(Mandatory = $true)][string]$ReportPath,
    [Parameter(Mandatory = $true)][int]$StartedEpoch
)

$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)

function Log($msg) {
    [Console]::Out.WriteLine("[watchdog $((Get-Date).ToString('HH:mm:ss'))] $msg")
    [Console]::Out.Flush()
}

# A fresh report (written AFTER the wrapper started) means backup.ps1 ran
# to completion -- nothing to do, it already notified.
if (Test-Path -LiteralPath $ReportPath) {
    $rep = Get-Item -LiteralPath $ReportPath
    # TotalSeconds is already a double — do NOT [double]::Parse it; that path
    # calls ToString() first and crashes on locales with comma decimals (ru-RU).
    $repEpoch = [int]($rep.LastWriteTimeUtc - [DateTime]'1970-01-01').TotalSeconds
    if ($repEpoch -ge $StartedEpoch) {
        Log "fresh report present -- backup.ps1 already notified"
        exit 0
    }
}

Log "no fresh report -> assuming backup was killed / crashed"

try {
    $plan = Get-Content -LiteralPath $PlanPath -Raw -Encoding UTF8 | ConvertFrom-Json
} catch {
    Log "cannot read plan.json ($_) -- no Telegram credentials, giving up"
    exit 0
}

$tg = $null
if ($plan.PSObject.Properties.Name -contains 'notifications' -and $plan.notifications) {
    if ($plan.notifications.PSObject.Properties.Name -contains 'telegram' -and $plan.notifications.telegram) {
        $tg = $plan.notifications.telegram
    }
}

if (-not $tg -or -not $tg.bot_token -or -not $tg.chat_id) {
    Log "Telegram not configured in plan.notifications -- giving up"
    exit 0
}

$configName = try { [string]$plan.config_name } catch { "<unknown>" }
$hostName   = try { [string](hostname) } catch { "<unknown-host>" }
$ts         = (Get-Date).ToString("yyyy-MM-dd HH:mm")

# ASCII-only text -- PS 5.1 reads .ps1 files without BOM as cp1251 on ru-RU,
# which corrupts any non-ASCII literal inside the script. Keep the watchdog
# locale-proof by sticking to plain English + cross symbol.
$text = @"
[X] Backup hung / killed by timeout

Host:   $hostName
Config: $configName
When:   $ts

backup.ps1 did not produce a fresh report. Task Scheduler most likely
killed the job on ExecutionTimeLimit (30 min). Check server logs.
"@

$uri = "https://api.telegram.org/bot$($tg.bot_token)/sendMessage"
$body = @{
    chat_id = $tg.chat_id
    text    = $text
}

try {
    Invoke-RestMethod -Uri $uri -Method Post -Body $body -TimeoutSec 15 | Out-Null
    Log "Telegram alert sent"
} catch {
    Log "Telegram send failed: $_"
}

exit 0
