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
$runId = (Get-Date).ToString("yyyyMMdd_HHmmss")
$stagingRoot = Join-Path $env:ProgramData "serverpanel\staging\$runId"
$utf8NoBom = New-Object System.Text.UTF8Encoding $false

function Log($msg) {
    Write-Host "[$((Get-Date).ToString('HH:mm:ss'))] $msg"
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
            robocopy $src.path $stagePath /MIR /R:2 /W:5 /NP /NFL /NDL /NJH /NJS | Out-Null
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
        if ((Get-Item -LiteralPath $stagePath).PSIsContainer) {
            Compress-Archive -Path (Join-Path $stagePath '*') -DestinationPath $zipPath -CompressionLevel Optimal -Force
        } else {
            Compress-Archive -LiteralPath $stagePath -DestinationPath $zipPath -CompressionLevel Optimal -Force
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
                if ((Get-Item -LiteralPath $dstPath).PSIsContainer) {
                    Compress-Archive -Path (Join-Path $dstPath '*') -DestinationPath $zipPath -CompressionLevel Optimal -Force
                } else {
                    Compress-Archive -LiteralPath $dstPath -DestinationPath $zipPath -CompressionLevel Optimal -Force
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

        # Helper: run sftp with a one-shot batch file and always delete it.
        $RunSftpBatch = {
            param($batchText)
            $batchFile = Join-Path $env:TEMP ("sp_sftp_" + [Guid]::NewGuid().ToString("N") + ".batch")
            Write-Utf8 $batchFile $batchText
            try {
                $out = & sftp @sftpOptsBase -b $batchFile "${sbUser}@${sbHost}" 2>&1
                return ,$out
            } finally {
                Remove-Item -LiteralPath $batchFile -Force -ErrorAction SilentlyContinue
            }
        }

        $remoteBase = ($dest.base_path -replace '\\', '/').TrimEnd('/')
        $remoteRoot = if ($dest.date_folder) { "$remoteBase/$today" } else { $remoteBase }

        # Create remote dir tree (mkdir per segment - sftp ignores "already exists")
        $parts = $remoteRoot.Trim('/').Split('/')
        $mkdirBatch = ""
        $cur = ""
        foreach ($p in $parts) {
            $cur = if ($cur) { "$cur/$p" } else { $p }
            $mkdirBatch += "-mkdir $cur`n"
        }
        & $RunSftpBatch $mkdirBatch | Out-Null

        $aliases = if ($dest.aliases.Count -eq 0) { @($sourcesByAlias.Keys) } else { @($dest.aliases) }
        foreach ($alias in $aliases) {
            $src = $sourcesByAlias[$alias]
            if (-not $src) {
                $rec.items += @{ alias = $alias; status = "skipped"; error = "source alias not found" }
                continue
            }
            try {
                $staged = Stage-Source $src $stageBase
                $leaf = Split-Path $staged -Leaf
                $remoteTarget = "$remoteRoot/$leaf"
                $sz = Get-PathSize $staged

                $isDir = (Get-Item -LiteralPath $staged).PSIsContainer
                if ($isDir) {
                    & scp @sshOpts -r $staged "${sbUser}@${sbHost}:$remoteTarget" 2>&1 | Out-Null
                } else {
                    & scp @sshOpts $staged "${sbUser}@${sbHost}:$remoteTarget" 2>&1 | Out-Null
                }
                if ($LASTEXITCODE -ne 0) { throw "scp exit $LASTEXITCODE" }

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

$sourcesByAlias = @{}
foreach ($s in $plan.sources) { $sourcesByAlias[$s.alias] = $s }

Ensure-Dir $stagingRoot

$results = @()
foreach ($dest in $plan.destinations) {
    $rec = $null
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
Log "=== backup finished, report: $ReportPath ==="
exit 0
