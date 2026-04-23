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
# runId must be UTC so that BackupService.sync_reports_from_server can
# compute a correct duration: started_at is parsed from this string as a
# naive datetime, completed_at is parsed from report.run_at (ISO UTC) and
# then stored in a DB column without tz — both sides must share the same
# time base or `completed_at - started_at` becomes minus-the-offset.
$runId = (Get-Date).ToUniversalTime().ToString("yyyyMMdd_HHmmss")
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

# -----------------------------------------------------------------------------
# progress.json writer — BackupService poller reads this during a live run.
# Atomic write (temp + move) so the reader never catches half-flushed JSON.
# -----------------------------------------------------------------------------
$script:ProgressPath = Join-Path (Split-Path $ReportPath -Parent) "progress.json"
$script:ProgressState = @{
    bytes_total  = 0
    bytes_done   = 0
    current_item = ""
}

function Write-ProgressTick {
    param(
        [int64]$BytesDone = $script:ProgressState.bytes_done,
        [int64]$BytesTotal = $script:ProgressState.bytes_total,
        [string]$CurrentItem = $script:ProgressState.current_item
    )
    $script:ProgressState.bytes_done = $BytesDone
    $script:ProgressState.bytes_total = $BytesTotal
    $script:ProgressState.current_item = $CurrentItem
    $payload = @{
        bytes_total  = [int64]$BytesTotal
        bytes_done   = [int64]$BytesDone
        current_item = [string]$CurrentItem
        updated_at   = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    } | ConvertTo-Json -Compress
    $tmp = "$($script:ProgressPath).tmp"
    try {
        [System.IO.File]::WriteAllText($tmp, $payload, $utf8NoBom)
        Move-Item -LiteralPath $tmp -Destination $script:ProgressPath -Force
    } catch {
        # Progress is best-effort: a failed tick must never abort the backup.
    }
}

function Clear-ProgressFile {
    # Called at the very end so that a finished run does not leave a stale
    # progress.json that the poller would still interpret as "running".
    Remove-Item -LiteralPath $script:ProgressPath -Force -ErrorAction SilentlyContinue
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

    # Rotation - only meaningful when date_folder is on.
    # Logic mirrored in src/serverpanel/domain/rotation.py (select_expired +
    # compute_cutoff) — tests/test_domain/test_rotation.py pins the contract.
    # Keep both sides in sync when touching the regex or the cutoff semantics.
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
            # Restrict key file permissions (ssh refuses "world-readable" keys).
            # Grant by SID, not $env:USERNAME — under SYSTEM the latter expands
            # to "<HOST>$" (computer account), which icacls cannot resolve,
            # leaving the file with NO ACEs after /inheritance:r and scp
            # fails with "Load key: Permission denied" → exit 255.
            $mySid = ([System.Security.Principal.WindowsIdentity]::GetCurrent()).User.Value
            & icacls $keyFile /inheritance:r 2>&1 | Out-Null
            & icacls $keyFile /grant:r "*${mySid}:F" "*S-1-5-18:F" "*S-1-5-32-544:F" 2>&1 | Out-Null
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

        # scp via Start-Process -Wait -PassThru. We can't poll lifetime from
        # the main thread (main is blocked), so the heartbeat — updating
        # progress.json's `updated_at` — is started once at scope entry as a
        # background job and stopped in `finally`. It does NOT move bytes_done:
        # that advances discretely after each alias completes.
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
                $stderrText = ""
                try {
                    $stderrText = (Get-Content -LiteralPath $errFile -Raw -ErrorAction SilentlyContinue)
                    if ($null -eq $stderrText) { $stderrText = "" }
                } catch { $stderrText = "" }
                return @{ ExitCode = $proc.ExitCode; StdErr = $stderrText.Trim() }
            } finally {
                Remove-Item -LiteralPath $outFile -Force -ErrorAction SilentlyContinue
                Remove-Item -LiteralPath $errFile -Force -ErrorAction SilentlyContinue
            }
        }

        $aliases = if ($dest.aliases.Count -eq 0) { @($sourcesByAlias.Keys) } else { @($dest.aliases) }

        # Pre-compute an upfront total (sum of source sizes) so the UI can
        # render a % right away. For `vss_dir`+zip sources this is the raw
        # dir size — the actual zip will be smaller, so `bytes_done` may
        # overshoot toward the end; the UI clamps to 100 %.
        $planTotal = 0
        foreach ($a in $aliases) {
            $s = $sourcesByAlias[$a]
            if ($s) { $planTotal += [int64](Get-PathSize $s.path) }
        }
        Write-ProgressTick -BytesDone 0 -BytesTotal $planTotal -CurrentItem ""

        # Heartbeat job: while the main thread is blocked in scp/zip, this
        # background job just rewrites progress.json's `updated_at` every 5s
        # so the panel's stall detector does not fire for a genuinely-slow
        # multi-GB upload. It does NOT touch bytes_done/bytes_total — the
        # main thread owns those.
        $heartbeatJob = Start-Job -ScriptBlock {
            param($progressPath)
            while ($true) {
                Start-Sleep -Seconds 5
                if (Test-Path -LiteralPath $progressPath) {
                    try {
                        $raw = Get-Content -LiteralPath $progressPath -Raw
                        $obj = $raw | ConvertFrom-Json
                        $obj.updated_at = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
                        $out = $obj | ConvertTo-Json -Compress
                        $tmp = "$progressPath.tmp"
                        [System.IO.File]::WriteAllText($tmp, $out, (New-Object System.Text.UTF8Encoding $false))
                        Move-Item -LiteralPath $tmp -Destination $progressPath -Force -ErrorAction SilentlyContinue
                    } catch { }
                }
            }
        } -ArgumentList $script:ProgressPath

        $planDone = 0
        foreach ($alias in $aliases) {
            $src = $sourcesByAlias[$alias]
            if (-not $src) {
                $rec.items += @{ alias = $alias; status = "skipped"; error = "source alias not found" }
                continue
            }
            Write-ProgressTick -BytesDone $planDone -BytesTotal $planTotal -CurrentItem $alias
            try {
                Log "storage[$($dest.index)] $alias staging (type=$($src.type), path=$($src.path))..."
                $staged = Stage-Source $src $stageBase
                $leaf = Split-Path $staged -Leaf
                $remoteTarget = "$remoteRoot/$leaf"
                $sz = Get-PathSize $staged
                Log "storage[$($dest.index)] $alias staged ($([math]::Round($sz/1MB,1)) MB) -> scp to $remoteTarget"

                # Purge stale remote target before scp. Reason: scp -r on an
                # EXISTING directory creates `target/<source-basename>/`
                # inside it instead of merging, which leaves
                # `.../1c_files/1c_files/...` on repeat runs — and then old
                # files block fresh writes with "Permission denied". ssh on
                # port 23 = Hetzner Storage Box limited shell; it accepts
                # `rm -rf`. Ignore exit code: "nothing to delete" is fine,
                # a transport failure will surface via scp anyway. NB: ssh
                # uses lowercase -p (scp/sftp use -P) — can't reuse $sshOpts.
                Log "storage[$($dest.index)] $alias purging stale remote $remoteTarget"
                $sshCmdOpts = @(
                    "-p", "$sbPort",
                    "-i", "$keyFile",
                    "-o", "BatchMode=yes",
                    "-o", "StrictHostKeyChecking=accept-new",
                    "-o", "UserKnownHostsFile=$env:ProgramData\serverpanel\known_hosts"
                )
                $rmArgs = $sshCmdOpts + @("${sbUser}@${sbHost}", "rm -rf '$remoteTarget'")
                $rmOut = Join-Path $env:TEMP ("sp_rm_" + [Guid]::NewGuid().ToString("N") + ".out")
                $rmErr = "$rmOut.err"
                try {
                    Start-Process -FilePath 'C:\Windows\System32\OpenSSH\ssh.exe' `
                        -ArgumentList $rmArgs -NoNewWindow -Wait -PassThru `
                        -RedirectStandardOutput $rmOut `
                        -RedirectStandardError  $rmErr | Out-Null
                } finally {
                    Remove-Item $rmOut, $rmErr -Force -ErrorAction SilentlyContinue
                }

                $isDir = (Get-Item -LiteralPath $staged).PSIsContainer
                $scpRes = & $RunScp $staged $remoteTarget $isDir
                if ($scpRes.ExitCode -ne 0) {
                    # Keep stderr tail short — it goes into the JSON report.
                    $tail = $scpRes.StdErr
                    if ($tail.Length -gt 1200) { $tail = $tail.Substring($tail.Length - 1200) }
                    throw "scp exit $($scpRes.ExitCode): $tail"
                }

                # Integrity: re-stat the remote object and compare byte counts.
                # scp can exit 0 on a truncated upload (connection reset mid-
                # stream, ENOSPC on the Storage Box, SIGTERM), so "exit 0"
                # alone is not proof the file is whole. Hetzner SB's limited
                # shell does not ship sha256sum, but `sftp ls -l` is always
                # available — size check closes the 90% case (abort / quota /
                # timeout). Directories are skipped here; a recursive size
                # walk is a separate follow-up.
                $remoteSize = $null
                $integrityStatus = $null
                if ($isDir) {
                    $integrityStatus = "skipped_dir"
                } else {
                    $lsLines = & $RunSftpBatch "ls -l $remoteTarget`n"
                    foreach ($line in $lsLines) {
                        $t = $line.Trim()
                        # `-rwxr-xr-x 1 user group 12345 Apr 23 14:30 path`
                        if ($t.StartsWith('-')) {
                            $cols = $t -split '\s+'
                            if ($cols.Count -ge 5) {
                                $parsed = [int64]0
                                if ([int64]::TryParse($cols[4], [ref]$parsed)) {
                                    $remoteSize = $parsed
                                    break
                                }
                            }
                        }
                    }
                    if ($null -eq $remoteSize) {
                        throw "integrity: cannot stat remote $remoteTarget (sftp ls -l returned no file entry)"
                    }
                    if ($remoteSize -ne [int64]$sz) {
                        throw "integrity: size mismatch on $remoteTarget (local=$sz remote=$remoteSize)"
                    }
                    $integrityStatus = "verified"
                }

                $rec.items += @{ alias = $alias; status = "success"; remote_path = $remoteTarget; size_bytes = $sz; remote_size = $remoteSize; integrity = $integrityStatus }
                $rec.size_bytes += $sz
                $planDone += [int64]$sz
                Write-ProgressTick -BytesDone $planDone -BytesTotal $planTotal -CurrentItem $alias
                Log "storage[$($dest.index)] $alias -> $remoteTarget OK ($([math]::Round($sz/1MB,1)) MB)"
            } catch {
                $rec.items += @{ alias = $alias; status = "failed"; error = "$_" }
                Log "storage[$($dest.index)] $alias FAILED: $_"
            }
        }

        # Stop the heartbeat job — all aliases processed.
        try {
            Stop-Job -Job $heartbeatJob -ErrorAction SilentlyContinue
            Remove-Job -Job $heartbeatJob -Force -ErrorAction SilentlyContinue
        } catch { }

        # Remote rotation — list $remoteBase, delete date folders older than
        # cutoff. Uses ssh `rm -rf` because date folders contain nested
        # source subdirs, and sftp's `rm $dir/*` is not recursive (it only
        # removes first-level files, then `rmdir` fails on the still-non-
        # empty dir, silently leaving old backups around forever).
        # Selection logic mirrored in src/serverpanel/domain/rotation.py —
        # tests/test_domain/test_rotation.py pins the "full path vs basename"
        # regression that made this loop a no-op from day one.
        if ($dest.date_folder -and $dest.rotation_days -gt 0) {
            $cutoff = (Get-Date).AddDays(-$dest.rotation_days).ToString("yyyy-MM-dd")
            try {
                # sftp `ls -1` returns FULL paths (`backups/daily/2026-04-04`),
                # not just basenames. Strip to basename before matching —
                # otherwise the date regex never matches and rotation is a
                # silent no-op (this has been broken since day one).
                $listing = & $RunSftpBatch "ls -1 $remoteBase/`n"
                $oldDirs = $listing |
                    ForEach-Object { ($_.Trim() -split '/')[-1] } |
                    Where-Object { $_ -match '^\d{4}-\d{2}-\d{2}$' -and $_ -lt $cutoff }
                $rotSshOpts = @(
                    "-p", "$sbPort",
                    "-i", "$keyFile",
                    "-o", "BatchMode=yes",
                    "-o", "StrictHostKeyChecking=accept-new",
                    "-o", "UserKnownHostsFile=$env:ProgramData\serverpanel\known_hosts"
                )
                foreach ($d in $oldDirs) {
                    $rotOut = Join-Path $env:TEMP ("sp_rot_" + [Guid]::NewGuid().ToString("N") + ".out")
                    $rotErr = "$rotOut.err"
                    try {
                        $rotArgs = $rotSshOpts + @("${sbUser}@${sbHost}", "rm -rf '$remoteBase/$d'")
                        $rp = Start-Process -FilePath 'C:\Windows\System32\OpenSSH\ssh.exe' `
                            -ArgumentList $rotArgs -NoNewWindow -Wait -PassThru `
                            -RedirectStandardOutput $rotOut `
                            -RedirectStandardError  $rotErr
                        if ($rp.ExitCode -eq 0) {
                            Log "storage[$($dest.index)] rotated $remoteBase/$d"
                        } else {
                            $et = Get-Content -LiteralPath $rotErr -Raw -ErrorAction SilentlyContinue
                            Log "storage[$($dest.index)] rotation FAIL $remoteBase/$d exit=$($rp.ExitCode): $et"
                        }
                    } finally {
                        Remove-Item $rotOut, $rotErr -Force -ErrorAction SilentlyContinue
                    }
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
# Live date string for date_folder layout — NEVER use $plan.date_folder here.
# $plan.date_folder is frozen at install_schedule time and would pin every
# nightly run into the same date folder (breaking rotation + colliding scp
# writes with "Permission denied" on overwrite).
$todayStr = Get-Date -Format "yyyy-MM-dd"
Log "sources: $($plan.sources.Count), destinations: $($plan.destinations.Count), today: $todayStr"

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
            $rec = Invoke-LocalDestination $dest $sourcesByAlias $todayStr
        } elseif ($dest.kind -eq "storage") {
            $rec = Invoke-StorageDestination $dest $sourcesByAlias $todayStr $stagingRoot
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

# Progress file must disappear now — otherwise the poller would keep reading
# it on future runs and show a "running" state for a job that already ended.
Clear-ProgressFile

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
        # Manual sum — Measure-Object -Property does not see hashtable keys
        # in PS 5.1 (only PSCustomObject properties), so piping $results at
        # it silently returned 0 MB even on multi-GB runs.
        $totalBytes = 0
        foreach ($r in $results) {
            if ($r.size_bytes) { $totalBytes += [int64]$r.size_bytes }
        }
        $totalMb = [math]::Round($totalBytes / 1MB, 1)
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
        # For partial ones the destination-level `error` is empty — the real
        # cause lives on per-item entries, list the failing aliases instead.
        foreach ($r in $results) {
            if ($r.status -eq "success") { continue }
            $lines += "• [$($r.index)] $($r.kind) $($r.status)"
            if ($r.error) { $lines += "  $($r.error)" }
            $failedItems = @($r.items | Where-Object { $_.status -eq "failed" })
            foreach ($it in $failedItems) {
                $itemErr = if ($it.error) { $it.error } else { "(no error)" }
                # Trim long stderr tails to keep the message readable.
                if ($itemErr.Length -gt 220) { $itemErr = $itemErr.Substring(0, 220) + "…" }
                $lines += "  - $($it.alias): $itemErr"
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
