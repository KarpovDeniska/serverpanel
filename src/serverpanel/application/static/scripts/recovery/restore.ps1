# restore.ps1 - Post-install orchestrator for the freshly-booted Windows.
# Reads C:\Recovery\config.json (shipped by serverpanel through inject_config.sh),
# writes progress to C:\recovery_status.json and optionally scp's the same file
# to Storage Box for serverpanel to poll.
#
# Expected config.json schema (produced by RecoveryService._build_windows_config):
#   {
#     "storage_box": { "host", "user", "port", "private_key"? },
#     "windows":     { "product_key"?, "admin_password"?, "hostname"? },
#     "software":    { "1c_platform": bool, "git": bool, "iis": bool, ... },
#     "recover_d_drive": bool,
#     "daily_folder": "YYYY-MM-DD" | "latest"
#   }

$ErrorActionPreference = "Continue"
$configPath = "C:\Recovery\config.json"
$logFile    = "C:\recovery_restore.log"
$statusFile = "C:\recovery_status.json"

function Log($msg) {
    $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $msg"
    Write-Host $line
    Add-Content -LiteralPath $logFile -Value $line -Encoding UTF8
}

function Report-Status($step, $progress, $message) {
    $status = @{
        step      = $step
        progress  = $progress
        message   = $message
        timestamp = (Get-Date -Format "o")
    } | ConvertTo-Json
    $utf8NoBom = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText($statusFile, $status, $utf8NoBom)
    Log "$step ($progress%): $message"

    if ($global:SB_HOST -and $global:SB_USER) {
        try {
            $port = if ($global:SB_PORT) { $global:SB_PORT } else { 23 }
            if ($global:SB_KEYFILE) {
                & scp -P $port -i $global:SB_KEYFILE -o StrictHostKeyChecking=accept-new `
                    $statusFile "$($global:SB_USER)@$($global:SB_HOST):/backups/status/recovery_status.json" 2>$null
            } else {
                & scp -P $port -o StrictHostKeyChecking=accept-new `
                    $statusFile "$($global:SB_USER)@$($global:SB_HOST):/backups/status/recovery_status.json" 2>$null
            }
        } catch { }
    }
}

if (!(Test-Path -LiteralPath $configPath)) {
    Log "ERROR: config.json not found: $configPath"
    exit 1
}

$cfg = Get-Content -Raw -LiteralPath $configPath -Encoding UTF8 | ConvertFrom-Json
$install = $cfg.software
$daily   = if ($cfg.daily_folder) { $cfg.daily_folder } else { "latest" }

$global:SB_HOST = $cfg.storage_box.host
$global:SB_USER = $cfg.storage_box.user
$global:SB_PORT = if ($cfg.storage_box.port) { [int]$cfg.storage_box.port } else { 23 }
$global:SB_KEYFILE = $null

# Stage SSH key if provided
if ($cfg.storage_box.private_key) {
    $keyFile = Join-Path $env:ProgramData "serverpanel\sb_key"
    $keyDir = Split-Path $keyFile -Parent
    if (!(Test-Path -LiteralPath $keyDir)) { New-Item -ItemType Directory -Path $keyDir -Force | Out-Null }
    $utf8NoBom = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText($keyFile, $cfg.storage_box.private_key, $utf8NoBom)
    icacls $keyFile /inheritance:r 2>&1 | Out-Null
    icacls $keyFile /grant:r "${env:USERNAME}:F" 2>&1 | Out-Null
    $global:SB_KEYFILE = $keyFile
}

Report-Status "init" 5 "Config loaded, starting recovery"

function Download-FromSB($remote, $local) {
    $dir = Split-Path $local -Parent
    if ($dir -and -not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
    if ($global:SB_KEYFILE) {
        & scp -P $global:SB_PORT -i $global:SB_KEYFILE -o StrictHostKeyChecking=accept-new -r `
            "$($global:SB_USER)@$($global:SB_HOST):$remote" "$local" 2>&1 | Out-Null
    } else {
        & scp -P $global:SB_PORT -o StrictHostKeyChecking=accept-new -r `
            "$($global:SB_USER)@$($global:SB_HOST):$remote" "$local" 2>&1 | Out-Null
    }
}

$softwareDir = "C:\Recovery\Software"
if (!(Test-Path -LiteralPath $softwareDir)) { New-Item -ItemType Directory -Path $softwareDir -Force | Out-Null }

# --- Software installation (driven by boolean flags in config.software) ---

if ($install.'1c_platform') {
    Report-Status "1c_platform" 15 "Installing 1C Platform..."
    Download-FromSB "/backups/software/1c_platform_setup.exe" "$softwareDir\1c_setup.exe"
    if (Test-Path -LiteralPath "$softwareDir\1c_setup.exe") {
        Start-Process "$softwareDir\1c_setup.exe" -ArgumentList "/S" -Wait -NoNewWindow
        Log "1C Platform installed"
    } else { Log "WARN: 1C platform installer not on SB" }
}

if ($install.'1c_thin_client') {
    Report-Status "1c_thin" 20 "Installing 1C Thin Client..."
    Download-FromSB "/backups/software/1c_thin_setup.exe" "$softwareDir\1c_thin.exe"
    if (Test-Path -LiteralPath "$softwareDir\1c_thin.exe") {
        Start-Process "$softwareDir\1c_thin.exe" -ArgumentList "/S" -Wait -NoNewWindow
        Log "1C Thin Client installed"
    }
}

if ($install.cryptopro) {
    Report-Status "cryptopro" 25 "Installing CryptoPro CSP..."
    Download-FromSB "/backups/software/cryptopro_setup.msi" "$softwareDir\cryptopro.msi"
    if (Test-Path -LiteralPath "$softwareDir\cryptopro.msi") {
        Start-Process "msiexec" -ArgumentList "/i `"$softwareDir\cryptopro.msi`" /quiet /norestart" -Wait -NoNewWindow
        Log "CryptoPro installed"
    }
}

if ($install.git) {
    Report-Status "git" 30 "Installing Git..."
    Download-FromSB "/backups/software/git_setup.exe" "$softwareDir\git.exe"
    if (Test-Path -LiteralPath "$softwareDir\git.exe") {
        Start-Process "$softwareDir\git.exe" -ArgumentList "/VERYSILENT /NORESTART" -Wait -NoNewWindow
        Log "Git installed"
    }
}

if ($install.nodejs) {
    Report-Status "nodejs" 35 "Installing Node.js..."
    Download-FromSB "/backups/software/nodejs_setup.msi" "$softwareDir\nodejs.msi"
    if (Test-Path -LiteralPath "$softwareDir\nodejs.msi") {
        Start-Process "msiexec" -ArgumentList "/i `"$softwareDir\nodejs.msi`" /quiet" -Wait -NoNewWindow
        Log "Node.js installed"
    }
}

if ($install.python) {
    Report-Status "python" 40 "Installing Python..."
    Download-FromSB "/backups/software/python_setup.exe" "$softwareDir\python.exe"
    if (Test-Path -LiteralPath "$softwareDir\python.exe") {
        Start-Process "$softwareDir\python.exe" -ArgumentList "/quiet InstallAllUsers=1 PrependPath=1" -Wait -NoNewWindow
        Log "Python installed"
    }
}

if ($install.wsl) {
    Report-Status "wsl" 45 "Installing WSL..."
    wsl --install --no-distribution 2>&1 | Out-Null
    Log "WSL installed"
}

if ($install.iis) {
    Report-Status "iis" 50 "Installing IIS..."
    Install-WindowsFeature -Name Web-Server -IncludeManagementTools | Out-Null
    Log "IIS installed"
}

if ($install.office) {
    Report-Status "office" 55 "Installing Office..."
    Download-FromSB "/backups/software/office_setup.exe" "$softwareDir\office_setup.exe"
    if (Test-Path -LiteralPath "$softwareDir\office_setup.exe") {
        Start-Process "$softwareDir\office_setup.exe" -ArgumentList "/configure office.xml" -Wait -NoNewWindow
        Log "Office installed"
    }
}

# --- Data restore ---

Report-Status "restore_license" 60 "Restoring 1C license..."
Download-FromSB "/backups/daily/$daily/1c_license/*" "C:\ProgramData\1C\licenses\"
Log "1C license restored"

Report-Status "restore_ibases" 65 "Restoring 1C ibases.v8i..."
Download-FromSB "/backups/daily/$daily/ibases.v8i" "C:\Users\Administrator\AppData\Roaming\1C\1CEStart\ibases.v8i"
Log "ibases.v8i restored"

if ($install.xray) {
    Report-Status "xray" 75 "Restoring Xray VPN..."
    $xrayDir = "D:\Personal folders\dkarpov\projects\tools\xray"
    if (!(Test-Path -LiteralPath $xrayDir)) { New-Item -ItemType Directory -Path $xrayDir -Force | Out-Null }
    Download-FromSB "/backups/daily/$daily/xray_config.json" "$xrayDir\config_xhttp.json"
    Download-FromSB "/backups/daily/$daily/xray_winsw.xml"   "$xrayDir\WinSW.xml"
    Download-FromSB "/backups/software/xray/*"               "$xrayDir\"
    if (Test-Path -LiteralPath "$xrayDir\WinSW.exe") {
        & "$xrayDir\WinSW.exe" install 2>$null
        & "$xrayDir\WinSW.exe" start 2>$null
        Log "Xray service registered"
    }
}

if ($install.winacme -and $install.iis) {
    Report-Status "winacme" 80 "Restoring win-acme + IIS config..."
    Download-FromSB "/backups/software/win-acme/*" "C:\win-acme\"
    Download-FromSB "/backups/weekly/*/iis/*"     "C:\inetpub\"
    Log "win-acme and IIS config restored"
}

# --- Optional D: rebuild (scenario "both") ---

if ($cfg.recover_d_drive) {
    Report-Status "restore_d" 85 "Rebuilding D: drive..."
    $rawDisk = Get-Disk | Where-Object { $_.PartitionStyle -eq 'RAW' }
    if ($rawDisk) {
        $rawDisk | Initialize-Disk -PartitionStyle MBR -PassThru |
            New-Partition -UseMaximumSize -DriveLetter D |
            Format-Volume -FileSystem NTFS -NewFileSystemLabel Data -Confirm:$false
        Log "D: initialized"
    }
    Download-FromSB "/backups/daily/$daily/UNF/*"          "D:\1С\БД\UNF\"
    Download-FromSB "/backups/daily/$daily/1c_files/*"     "D:\1С\БД\Файлы\"
    Download-FromSB "/backups/daily/$daily/1c_obrabotki/*" "D:\1С\Обработки\"
    Log "D: data restored"
}

# --- Cleanup autologon / temp ---

Report-Status "cleanup" 95 "Finalizing..."
Remove-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon" -Name "AutoAdminLogon" -ErrorAction SilentlyContinue
Remove-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon" -Name "DefaultPassword" -ErrorAction SilentlyContinue
Remove-Item -LiteralPath "C:\Recovery\Software" -Recurse -Force -ErrorAction SilentlyContinue

Report-Status "done" 100 "Recovery finished"
Log "=== RECOVERY FINISHED ==="
