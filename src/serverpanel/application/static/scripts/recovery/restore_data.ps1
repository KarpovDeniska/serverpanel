# restore_data.ps1 - Pull data from Storage Box over scp.
# Driven by restore.json with schema:
#   {
#     "storage_box": { "host", "user", "port", "private_key"? },
#     "sources": [ { "remote": "<remote path>", "local": "<local path>" }, ... ],
#     "daily_folder": "latest"  (optional; used in remote path interpolation as {daily})
#   }
#
# If `sources` is empty a small hardcoded default set is used - kept for the
# legacy hetzner-recovery flow. Extend `sources` to customize.

param(
    [Parameter(Mandatory)][string]$ConfigPath
)

$ErrorActionPreference = "Continue"

if (!(Test-Path -LiteralPath $ConfigPath)) {
    Write-Error "config not found: $ConfigPath"
    exit 1
}

$cfg = Get-Content -Raw -LiteralPath $ConfigPath -Encoding UTF8 | ConvertFrom-Json
$SB = $cfg.storage_box
$daily = if ($cfg.daily_folder) { $cfg.daily_folder } else { "latest" }

# --- SSH key (if provided) ---
$keyFile = $null
$keyTemp = $false
try {
    if ($SB.private_key) {
        $keyFile = Join-Path $env:TEMP ("sp_sb_" + [Guid]::NewGuid().ToString("N"))
        $utf8NoBom = New-Object System.Text.UTF8Encoding $false
        [System.IO.File]::WriteAllText($keyFile, $SB.private_key, $utf8NoBom)
        # Grant by SID — $env:USERNAME resolves to "<HOST>$" under SYSTEM and
        # icacls leaves the file with zero ACEs, breaking scp/ssh key load.
        $mySid = ([System.Security.Principal.WindowsIdentity]::GetCurrent()).User.Value
        icacls $keyFile /inheritance:r 2>&1 | Out-Null
        icacls $keyFile /grant:r "*${mySid}:F" "*S-1-5-18:F" "*S-1-5-32-544:F" 2>&1 | Out-Null
        $keyTemp = $true
    }

    function SB-Download($remote, $local) {
        $dir = Split-Path $local -Parent
        if ($dir -and -not (Test-Path -LiteralPath $dir)) {
            New-Item -ItemType Directory -Path $dir -Force | Out-Null
        }
        $port = if ($SB.port) { [int]$SB.port } else { 23 }
        $target = "$($SB.user)@$($SB.host):$remote"
        if ($keyFile) {
            & scp -P $port -i $keyFile -o StrictHostKeyChecking=accept-new -r $target $local 2>&1
        } else {
            & scp -P $port -o StrictHostKeyChecking=accept-new -r $target $local 2>&1
        }
    }

    $sources = if ($cfg.sources -and $cfg.sources.Count -gt 0) { $cfg.sources } else {
        @(
            @{ remote = "/backups/daily/$daily/UNF/*";          local = "D:\1С\БД\UNF\" },
            @{ remote = "/backups/daily/$daily/1c_files/*";     local = "D:\1С\БД\Файлы\" },
            @{ remote = "/backups/daily/$daily/1c_obrabotki/*"; local = "D:\1С\Обработки\" },
            @{ remote = "/backups/daily/$daily/rutoken/*";      local = "D:\Soft\rutoken\" },
            @{ remote = "/backups/daily/$daily/1c_licenses_archive/*"; local = "D:\Soft\Лицензии 1С\" },
            @{ remote = "/backups/daily/$daily/xray_config.json"; local = "D:\Personal folders\dkarpov\projects\tools\xray\config_xhttp.json" }
        )
    }

    foreach ($s in $sources) {
        $remote = $s.remote -replace '\{daily\}', $daily
        Write-Host "Restoring $remote -> $($s.local)"
        SB-Download $remote $s.local
    }

    Write-Host "Done."
} finally {
    if ($keyTemp -and $keyFile -and (Test-Path -LiteralPath $keyFile)) {
        Remove-Item -LiteralPath $keyFile -Force -ErrorAction SilentlyContinue
    }
}
