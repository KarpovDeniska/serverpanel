# backup.ps1 - universal backup runner driven by plan.json.
# Contract:
#   -PlanPath   : JSON plan written by BackupService (schema_version, sources, destinations).
#   -ReportPath : where to write the JSON report (schema: destinations[] with status/size/items).
# Exit code is always 0 unless the plan itself cannot be read; per-destination
# failures are surfaced in report.json (aggregated by BackupService).

param(
    [Parameter(Mandatory = $true)][string]$PlanPath,
    [Parameter(Mandatory = $true)][string]$ReportPath
)

$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"
# Force stdout/stderr to UTF-8 so Cyrillic paths and messages land in the UI
# as real characters, not cp1251 mojibake — PS 5.1 under sshd on ru-RU
# defaults to the system OEM codepage and paramiko reads as UTF-8.
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$runId = (Get-Date).ToString("yyyyMMdd_HHmmss")
# Staging lives outside ProgramData\serverpanel so that a source pointing at
# that directory (or any of its parents) does not end up copying staging into
# itself on every iteration — robocopy /MIR would loop until the disk dies.
$stagingRoot = Join-Path $env:TEMP "sp-staging\$runId"
$utf8NoBom = New-Object System.Text.UTF8Encoding $false

function Log($msg) {
    # Write straight to stdout + flush: Write-Host under sshd can sit in the
    # host's write buffer and never reach paramiko until the process exits,
    # which looks exactly like "backup hangs mid-run" in the UI.
    $line = "[$((Get-Date).ToString('HH:mm:ss'))] $msg"
    [Console]::Out.WriteLine($line)
    [Console]::Out.Flush()
}

function Write-Utf8($path, $text) {
    $dir = Split-Path $path -Parent
    if ($dir -and -not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
    [System.IO.File]::WriteAllText($path, $text, $utf8NoBom)
}

function Get-PathSize($path) {
    if (-not (Test-Path -LiteralPath $path)) { return 0 }
    try {
        if ((Get-Item -LiteralPath $path).PSIsContainer) {
            $sum = Get-ChildItem -LiteralPath $path -Recurse -File -Force -ErrorAction SilentlyContinue |
                Measure-Object -Property Length -Sum
            return [int64]($sum.Sum)
        }
        return [int64]((Get-Item -LiteralPath $path).Length)
    } catch { return 0 }
}

function Ensure-Dir($path) {
    if (-not (Test-Path -LiteralPath $path)) {
        New-Item -ItemType Directory -Path $path -Force | Out-Null
    }
}

# ---------------------------------------------------------------------------
# VSS
# ---------------------------------------------------------------------------
$script:vssShadows = @{}  # driveLetter -> @{ id; device }

function Invoke-VssShadow($driveLetter) {
    if ($script:vssShadows.ContainsKey($driveLetter)) {
        return $script:vssShadows[$driveLetter].device
    }
    try {
        $r = (Get-WmiObject -List Win32_ShadowCopy).Create("${driveLetter}:\", "ClientAccessible")
        $obj = Get-WmiObject Win32_ShadowCopy | Where-Object { $_.ID -eq $r.ShadowID }
        $device = $obj.DeviceObject
        $script:vssShadows[$driveLetter] = @{ id = $r.ShadowID; device = $device }
        Log "VSS shadow created for ${driveLetter}: -> $device"
        return $device
    } catch {
        Log "VSS ERROR for ${driveLetter}: $_"
        return $null
    }
}

function Remove-AllVssShadows() {
    foreach ($kv in $script:vssShadows.GetEnumerator()) {
        try {
            $obj = Get-WmiObject Win32_ShadowCopy | Where-Object { $_.ID -eq $kv.Value.id }
            if ($obj) { $obj.Delete() }
            Log "VSS shadow deleted for $($kv.Key):"
        } catch {
            Log "VSS delete error for $($kv.Key): $_"
        }
    }
    $script:vssShadows.Clear()
}

function Copy-FromVss($srcAbsolutePath, $dstPath) {
    # srcAbsolutePath is D:\Foo\Bar - we shadow D: and copy from shadow
    $driveLetter = $srcAbsolutePath.Substring(0, 1)
    $rel = $srcAbsolutePath.Substring(2)  # strip "D:"
    $device = Invoke-VssShadow $driveLetter
    if (-not $device) {
        throw "VSS unavailable for ${driveLetter}:"
    }
    $linkName = Join-Path $env:TEMP ("vss_" + [Guid]::NewGuid().ToString("N"))
    cmd /c "mklink /d `"$linkName`" `"${device}\`"" | Out-Null
    try {
        $fullSrc = Join-Path $linkName $rel.TrimStart('\')
        if (-not (Test-Path -LiteralPath $fullSrc)) {
            throw "path not found in shadow: $fullSrc"
        }
        Ensure-Dir $dstPath
        robocopy $fullSrc $dstPath /MIR /R:2 /W:5 /NP /NFL /NDL /NJH /NJS | Out-Null
        if ($LASTEXITCODE -ge 8) { throw "robocopy exit $LASTEXITCODE" }
    } finally {
        cmd /c "rmdir `"$linkName`"" 2>$null | Out-Null
    }
}

# ---------------------------------------------------------------------------
# Source materialization into staging (for storage uploads / compression)
# ---------------------------------------------------------------------------
function Stage-Source($src, $stageBase) {
    $stagePath = Join-Path $stageBase $src.alias
    Ensure-Dir (Split-Path $stagePath -Parent)
    switch ($src.type) {
        "dir" {
            Ensure-Dir $stagePath
            robocopy $src.path $stagePath /MIR /R:2 /W:5 /NP /NFL /NDL /NJH /NJS /XD $stagingRoot | Out-Null
            if ($LASTEXITCODE -ge 8) { throw "robocopy exit $LASTEXITCODE" }
        }
        "file" {
            Copy-Item -LiteralPath $src.path -Destination $stagePath -Force
        }
        "vss_dir" {
            Copy-FromVss $src.path $stagePath
        }
        default { throw "unknown source.type: $($src.type)" }
    }
    if ($src.compress -eq "zip") {
        $zipPath = "$stagePath.zip"
        if (Test-Path -LiteralPath $zipPath) { Remove-Item -LiteralPath $zipPath -Force }
        # Compress-Archive in PS 5.1 silently uses a 32-bit stream size and
        # blows up with "Stream was too long" once any single file > 2 GB (or
        # total > 2 GB depending on the path). System.IO.Compression.ZipFile
        # uses Zip64 automatically, so call it directly.
        Add-Type -AssemblyName System.IO.Compression
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        if ((Get-Item -LiteralPath $stagePath).PSIsContainer) {
            [System.IO.Compression.ZipFile]::CreateFromDirectory(
                $stagePath, $zipPath,
                $script:ZipLevel,
                $false  # includeBaseDirectory: contents only, matches old Compress-Archive `path\*` behavior
            )
        } else {
            # single file — create archive and write one entry
            $zipStream = [System.IO.File]::Open($zipPath, [System.IO.FileMode]::Create)
            try {
                $zip = New-Object System.IO.Compression.ZipArchive($zipStream, [System.IO.Compression.ZipArchiveMode]::Create)
                try {
                    [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
                        $zip, $stagePath, (Split-Path $stagePath -Leaf),
                        $script:ZipLevel
                    ) | Out-Null
                } finally { $zip.Dispose() }
            } finally { $zipStream.Dispose() }
        }
        Remove-Item -LiteralPath $stagePath -Recurse -Force -ErrorAction SilentlyContinue
        return $zipPath
    }
    return $stagePath
}

# ---------------------------------------------------------------------------
# Local destination
# ---------------------------------------------------------------------------
function Invoke-LocalDestination($dest, $sourcesByAlias, $today) {
    $rec = @{ index = $dest.index; kind = "local"; status = "pending"; items = @(); size_bytes = 0; error = $null }
    $aliases = if ($dest.aliases.Count -eq 0) { @($sourcesByAlias.Keys) } else { @($dest.aliases) }
    $baseRoot = if ($dest.date_folder) { Join-Path $dest.base_path $today } else { $dest.base_path }
    Ensure-Dir $baseRoot

    foreach ($alias in $aliases) {
        $src = $sourcesByAlias[$alias]
        if (-not $src) {
            $rec.items += @{ alias = $alias; status = "skipped"; error = "source alias not found" }
            continue
        }
        $dstPath = Join-Path $baseRoot $alias
        try {
            switch ($src.type) {
                "dir"     {
                    Ensure-Dir $dstPath
                    robocopy $src.path $dstPath /MIR /R:2 /W:5 /NP /NFL /NDL /NJH /NJS | Out-Null
                    if ($LASTEXITCODE -ge 8) { throw "robocopy exit $LASTEXITCODE" }
                }
                "file"    {
                    Ensure-Dir (Split-Path $dstPath -Parent)
                    Copy-Item -LiteralPath $src.path -Destination $dstPath -Force
                }
                "vss_dir" { Copy-FromVss $src.path $dstPath }
                default   { throw "unknown source.type: $($src.type)" }
            }
            if ($src.compress -eq "zip") {
                $zipPath = "$dstPath.zip"
                if (Test-Path -LiteralPath $zipPath) { Remove-Item -LiteralPath $zipPath -Force }
                # See Stage-Source for why we don't use Compress-Archive (2 GB limit in PS 5.1).
                Add-Type -AssemblyName System.IO.Compression
                Add-Type -AssemblyName System.IO.Compression.FileSystem
                if ((Get-Item -LiteralPath $dstPath).PSIsContainer) {
                    [System.IO.Compression.ZipFile]::CreateFromDirectory(
                        $dstPath, $zipPath,
                        $script:ZipLevel,
                        $false
                    )
                } else {
                    $zipStream = [System.IO.File]::Open($zipPath, [System.IO.FileMode]::Create)
                    try {
                        $zip = New-Object System.IO.Compression.ZipArchive($zipStream, [System.IO.Compression.ZipArchiveMode]::Create)
                        try {
                            [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
                                $zip, $dstPath, (Split-Path $dstPath -Leaf),
                                $script:ZipLevel
                            ) | Out-Null
                        } finally { $zip.Dispose() }
                    } finally { $zipStream.Dispose() }
                }
                Remove-Item -LiteralPath $dstPath -Recurse -Force -ErrorAction SilentlyContinue
                $dstPath = $zipPath
            }
            $sz = Get-PathSize $dstPath
            $rec.items += @{ alias = $alias; status = "success"; path = $dstPath; size_bytes = $sz }
            $rec.size_bytes += $sz
            Log "local[$($dest.index)] $alias OK ($([math]::Round($sz/1MB,1)) MB)"
        } catch {
            $rec.items += @{ alias = $alias; status = "failed"; error = "$_" }
            Log "local[$($dest.index)] $alias FAILED: $_"
        }
    }

    # Rotation - only meaningful when date_folder is on
    if ($dest.date_folder -and $dest.rotation_days -gt 0) {
        $cutoff = (Get-Date).AddDays(-$dest.rotation_days).ToString("yyyy-MM-dd")
        try {
            Get-ChildItem -LiteralPath $dest.base_path -Directory -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -match '^\d{4}-\d{2}-\d{2}$' -and $_.Name -lt $cutoff } |
                ForEach-Object {
                    Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue
                    Log "local[$($dest.index)] rotated $($_.Name)"
                }
        } catch { Log "rotation warn: $_" }
    }

    $ok = @($rec.items | Where-Object { $_.status -eq "success" }).Count
    $failed = @($rec.items | Where-Object { $_.status -eq "failed" }).Count
    $rec.status = if ($ok -eq 0 -and $failed -gt 0) { "failed" } elseif ($failed -gt 0) { "partial" } else { "success" }
    return $rec
}

# ---------------------------------------------------------------------------
# Storage destination (hetzner_storagebox over SFTP/SCP)
# ---------------------------------------------------------------------------
function Invoke-StorageDestination($dest, $sourcesByAlias, $today, $stageBase) {
    $rec = @{ index = $dest.index; kind = "storage"; status = "pending"; items = @(); size_bytes = 0; error = $null }

    if ($dest.frequency -eq "weekly" -and (Get-Date).DayOfWeek -ne [DayOfWeek]::Sunday) {
        $rec.status = "skipped"
        $rec.error = "weekly frequency, not Sunday"
        return $rec
    }

    if ($dest.storage_type -ne "hetzner_storagebox") {
        $rec.status = "failed"
        $rec.error = "storage_type '$($dest.storage_type)' not supported in backup.ps1"
        return $rec
    }

    $conn = $dest.connection
    $sbHost = $conn.host
    $sbUser = $conn.user
    $sbPort = if ($conn.port) { [int]$conn.port } else { 23 }
    $sshOpts = @(
        "-P", "$sbPort",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "UserKnownHostsFile=$env:ProgramData\serverpanel\known_hosts"
    )
    # sftp base options (without -b — we pass a real batch file per call).
    # Passing `-b -` + piping stdin from PowerShell hangs in SSH sessions on
    # Windows Server: stdin does not close cleanly → sftp waits forever.
    $sftpOptsBase = @(
        "-P", "$sbPort",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "UserKnownHostsFile=$env:ProgramData\serverpanel\known_hosts"
    )
    $keyFile = $null
    $keyFileTemp = $false
    try {
        if ($conn.private_key) {
            $keyFile = Join-Path $env:TEMP ("sp_sbkey_" + [Guid]::NewGuid().ToString("N"))
            Write-Utf8 $keyFile $conn.private_key
            & icacls $keyFile /inheritance:r 2>&1 | Out-Null
            & icacls $keyFile /grant:r "${env:USERNAME}:F" 2>&1 | Out-Null
            $keyFileTemp = $true
        } elseif ($conn.ssh_key_path) {
            $keyFile = [Environment]::ExpandEnvironmentVariables($conn.ssh_key_path)
        }
        if ($keyFile) {
            $sshOpts += @("-i", $keyFile)
            $sftpOptsBase += @("-i", $keyFile)
        }

        # Helper: run sftp with a batch file via Start-Process (fully detached
        # stdio). Calling `& sftp ...` from PowerShell under an SSH session on
        # Windows Server hangs because inherited stdin/stdout pipes from sshd
        # never close. Start-Process with explicit file redirects sidesteps
        # that entirely.
        $RunSftpBatch = {
            param($batchText)
            $batchFile = Join-Path $env:TEMP ("sp_sftp_" + [Guid]::NewGuid().ToString("N") + ".batch")
            $outFile = "$batchFile.out"
            $errFile = "$batchFile.err"
            Write-Utf8 $batchFile $batchText
            try {
                $args = $sftpOptsBase + @("-b", $batchFile, "${sbUser}@${sbHost}")
                $proc = Start-Process -FilePath 'C:\Windows\System32\OpenSSH\sftp.exe' `
                    -ArgumentList $args `
                    -NoNewWindow -Wait -PassThru `
                    -RedirectStandardOutput $outFile `
                    -RedirectStandardError $errFile
                $stdout = if (Test-Path $outFile) { Get-Content $outFile -Raw } else { "" }
                $stderr = if (Test-Path $errFile) { Get-Content $errFile -Raw } else { "" }
                $script:LASTEXITCODE = $proc.ExitCode
                $combined = @()
                if ($stdout) { $combined += ($stdout -split "`r?`n") }
                if ($stderr) { $combined += ($stderr -split "`r?`n") }
                return ,$combined
            } finally {
                Remove-Item -LiteralPath $batchFile -Force -ErrorAction SilentlyContinue
                Remove-Item -LiteralPath $outFile -Force -ErrorAction SilentlyContinue
                Remove-Item -LiteralPath $errFile -Force -ErrorAction SilentlyContinue
            }
        }

        $remoteBase = ($dest.base_path -replace '\\', '/').TrimEnd('/')
        $remoteRoot = if ($dest.date_folder) { "$remoteBase/$today" } else { $remoteBase }
        Log "storage[$($dest.index)] target ${sbUser}@${sbHost}:${sbPort} -> $remoteRoot"

        # Create remote dir tree (mkdir per segment - sftp ignores "already exists")
        $parts = $remoteRoot.Trim('/').Split('/')
        $mkdirBatch = ""
        $cur = ""
        foreach ($p in $parts) {
            $cur = if ($cur) { "$cur/$p" } else { $p }
            $mkdirBatch += "-mkdir $cur`n"
        }
        Log "storage[$($dest.index)] sftp mkdir tree..."
        & $RunSftpBatch $mkdirBatch | Out-Null
        Log "storage[$($dest.index)] sftp mkdir done"

        # Same inherited-stdio problem applies to scp — wrap it in Start-Process.
        $RunScp = {
            param([string]$local, [string]$remote, [bool]$recursive)
            $outFile = Join-Path $env:TEMP ("sp_scp_" + [Guid]::NewGuid().ToString("N") + ".out")
            $errFile = "$outFile.err"
            try {
                $scpArgs = @()
                if ($recursive) { $scpArgs += "-r" }
                $scpArgs = $sshOpts + $scpArgs + @($local, "${sbUser}@${sbHost}:${remote}")
                $proc = Start-Process -FilePath 'C:\Windows\System32\OpenSSH\scp.exe' `
                    -ArgumentList $scpArgs `
                    -NoNewWindow -Wait -PassThru `
                    -RedirectStandardOutput $outFile `
                    -RedirectStandardError $errFile
                return $proc.ExitCode
            } finally {
                Remove-Item -LiteralPath $outFile -Force -ErrorAction SilentlyContinue
                Remove-Item -LiteralPath $errFile -Force -ErrorAction SilentlyContinue
            }
        }

        $aliases = if ($dest.aliases.Count -eq 0) { @($sourcesByAlias.Keys) } else { @($dest.aliases) }
        foreach ($alias in $aliases) {
            $src = $sourcesByAlias[$alias]
            if (-not $src) {
                $rec.items += @{ alias = $alias; status = "skipped"; error = "source alias not found" }
                continue
            }
            try {
                Log "storage[$($dest.index)] $alias staging (type=$($src.type), path=$($src.path))..."
                $staged = Stage-Source $src $stageBase
                $leaf = Split-Path $staged -Leaf
                $remoteTarget = "$remoteRoot/$leaf"
                $sz = Get-PathSize $staged
                Log "storage[$($dest.index)] $alias staged ($([math]::Round($sz/1MB,1)) MB) -> scp to $remoteTarget"

                $isDir = (Get-Item -LiteralPath $staged).PSIsContainer
                $scpExit = & $RunScp $staged $remoteTarget $isDir
                if ($scpExit -ne 0) { throw "scp exit $scpExit" }

                $rec.items += @{ alias = $alias; status = "success"; remote_path = $remoteTarget; size_bytes = $sz }
                $rec.size_bytes += $sz
                Log "storage[$($dest.index)] $alias -> $remoteTarget OK ($([math]::Round($sz/1MB,1)) MB)"
            } catch {
                $rec.items += @{ alias = $alias; status = "failed"; error = "$_" }
                Log "storage[$($dest.index)] $alias FAILED: $_"
            }
        }

        # Remote rotation - list $remoteBase, delete date folders older than cutoff
        if ($dest.date_folder -and $dest.rotation_days -gt 0) {
            $cutoff = (Get-Date).AddDays(-$dest.rotation_days).ToString("yyyy-MM-dd")
            try {
                $listing = & $RunSftpBatch "ls -1 $remoteBase/`n"
                $oldDirs = $listing |
                    ForEach-Object { $_.Trim() } |
                    Where-Object { $_ -match '^\d{4}-\d{2}-\d{2}$' -and $_ -lt $cutoff }
                foreach ($d in $oldDirs) {
                    $rmCmd = "rm $remoteBase/$d/*`nrmdir $remoteBase/$d`n"
                    & $RunSftpBatch $rmCmd | Out-Null
                    Log "storage[$($dest.index)] rotated $remoteBase/$d"
                }
            } catch { Log "storage rotation warn: $_" }
        }
    } finally {
        if ($keyFileTemp -and $keyFile -and (Test-Path -LiteralPath $keyFile)) {
            Remove-Item -LiteralPath $keyFile -Force -ErrorAction SilentlyContinue
        }
    }

    $ok = @($rec.items | Where-Object { $_.status -eq "success" }).Count
    $failed = @($rec.items | Where-Object { $_.status -eq "failed" }).Count
    $rec.status = if ($ok -eq 0 -and $failed -gt 0) { "failed" } elseif ($failed -gt 0) { "partial" } else { "success" }
    return $rec
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
try {
    $plan = Get-Content -Raw -LiteralPath $PlanPath -Encoding UTF8 | ConvertFrom-Json
} catch {
    Log "FATAL: cannot read plan: $_"
    Write-Utf8 $ReportPath (@{
        error = "cannot read plan: $_"
        destinations = @()
    } | ConvertTo-Json -Depth 5)
    exit 2
}

Log "=== backup run $runId - config '$($plan.config_name)' ==="
Log "sources: $($plan.sources.Count), destinations: $($plan.destinations.Count), today: $($plan.date_folder)"

# Resolve zip compression level once (plan.options.zip_level is "fastest"|"optimal";
# default to Fastest — 2-3x faster than Optimal at the cost of ~15% size).
$script:zipLevelRaw = "fastest"
if ($plan.PSObject.Properties.Name -contains 'options' -and $plan.options -and $plan.options.zip_level) {
    $script:zipLevelRaw = [string]$plan.options.zip_level
}
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
$script:ZipLevel = if ($script:zipLevelRaw -ieq "optimal") {
    $script:ZipLevel
} else {
    [System.IO.Compression.CompressionLevel]::Fastest
}
Log "zip compression: $($script:zipLevelRaw)"

$sourcesByAlias = @{}
foreach ($s in $plan.sources) { $sourcesByAlias[$s.alias] = $s }

Log "staging root: $stagingRoot"
Ensure-Dir $stagingRoot

$results = @()
foreach ($dest in $plan.destinations) {
    $rec = $null
    Log "destination[$($dest.index)] kind=$($dest.kind) starting"
    try {
        if ($dest.kind -eq "local") {
            $rec = Invoke-LocalDestination $dest $sourcesByAlias $plan.date_folder
        } elseif ($dest.kind -eq "storage") {
            $rec = Invoke-StorageDestination $dest $sourcesByAlias $plan.date_folder $stagingRoot
        } else {
            $rec = @{ index = $dest.index; kind = $dest.kind; status = "failed"; error = "unknown kind"; items = @(); size_bytes = 0 }
        }
    } catch {
        $rec = @{ index = $dest.index; kind = $dest.kind; status = "failed"; error = "$_"; items = @(); size_bytes = 0 }
        Log "destination[$($dest.index)] fatal: $_"
    }
    Log "destination[$($dest.index)] done status=$($rec.status)"
    $results += $rec
}

Remove-AllVssShadows

# Cleanup staging
try {
    if (Test-Path -LiteralPath $stagingRoot) {
        Remove-Item -LiteralPath $stagingRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
} catch { }

$report = @{
    run_id = $runId
    run_at = (Get-Date).ToUniversalTime().ToString("o")
    config_id = $plan.config_id
    config_name = $plan.config_name
    destinations = $results
}
Write-Utf8 $ReportPath ($report | ConvertTo-Json -Depth 20)

# ---------------------------------------------------------------------------
# Telegram notifications
# ---------------------------------------------------------------------------
# Aggregate per-destination statuses → overall run status.
$okCount     = @($results | Where-Object { $_.status -eq "success" }).Count
$failedCount = @($results | Where-Object { $_.status -eq "failed"  }).Count
$partialAny  = @($results | Where-Object { $_.status -eq "partial" }).Count
if ($results.Count -eq 0)        { $overall = "failed" }
elseif ($failedCount -gt 0 -and $okCount -gt 0) { $overall = "partial" }
elseif ($failedCount -gt 0)      { $overall = "failed" }
elseif ($partialAny -gt 0)       { $overall = "partial" }
else                             { $overall = "success" }

$tg = $null
if ($plan.PSObject.Properties.Name -contains 'notifications' -and $plan.notifications) {
    if ($plan.notifications.PSObject.Properties.Name -contains 'telegram' -and $plan.notifications.telegram) {
        $tg = $plan.notifications.telegram
    }
}

# Always send a status message — "silent on success" means silence is
# indistinguishable from "server died a month ago and nobody noticed".
# A daily ✅ becomes a heartbeat; its absence is itself the alert.
if ($tg -and $tg.bot_token -and $tg.chat_id) {
    try {
        $totalMb = [math]::Round((($results | Measure-Object -Property size_bytes -Sum).Sum) / 1MB, 1)
        $icon = switch ($overall) {
            "success" { "✅" }
            "partial" { "⚠️" }
            default   { "❌" }
        }
        $lines = @(
            "$icon Backup <b>$overall</b>: $($plan.config_name)"
            "Host: $env:COMPUTERNAME | Run: $runId"
            "Destinations: ok=$okCount failed=$failedCount partial=$partialAny | Size: ${totalMb} MB"
        )
        # Detail failing destinations only (success list can be long and noisy).
        foreach ($r in $results) {
            if ($r.status -ne "success") {
                $err = if ($r.error) { $r.error } else { "(no error message)" }
                $lines += "• [$($r.index)] $($r.kind) $($r.status): $err"
            }
        }
        $text = ($lines -join "`n")
        $body = @{ chat_id = $tg.chat_id; text = $text; parse_mode = "HTML"; disable_web_page_preview = $true } | ConvertTo-Json -Compress
        $url  = "https://api.telegram.org/bot$($tg.bot_token)/sendMessage"
        Invoke-RestMethod -Uri $url -Method Post -ContentType "application/json; charset=utf-8" -Body ([System.Text.Encoding]::UTF8.GetBytes($body)) -TimeoutSec 30 | Out-Null
        Log "telegram alert sent (overall=$overall)"
    } catch {
        Log "telegram alert FAILED: $_"
    }
}

Log "=== backup finished, overall=$overall, report: $ReportPath ==="
exit 0
