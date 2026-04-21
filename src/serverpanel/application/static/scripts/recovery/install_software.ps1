# install_software.ps1 - Single-installer helper. Called from restore.ps1 or manually.
# Usage: .\install_software.ps1 -Name "git" -InstallerPath "C:\path\to\git.exe"

param(
    [Parameter(Mandatory)][string]$Name,
    [Parameter(Mandatory)][string]$InstallerPath
)

$silentArgs = @{
    "1c_platform"    = "/S"
    "1c_thin_client" = "/S"
    "cryptopro"      = "/quiet /norestart"
    "git"            = "/VERYSILENT /NORESTART"
    "nodejs"         = "/quiet"
    "python"         = "/quiet InstallAllUsers=1 PrependPath=1"
    "office"         = "/configure office.xml"
}

if (!(Test-Path -LiteralPath $InstallerPath)) {
    Write-Error "Installer not found: $InstallerPath"
    exit 1
}

if (-not $silentArgs.ContainsKey($Name)) {
    Write-Error "Unknown software: $Name"
    exit 1
}

$args = $silentArgs[$Name]
$ext = [System.IO.Path]::GetExtension($InstallerPath).ToLower()

if ($ext -eq ".msi") {
    Start-Process "msiexec" -ArgumentList "/i `"$InstallerPath`" $args" -Wait -NoNewWindow
} else {
    Start-Process $InstallerPath -ArgumentList $args -Wait -NoNewWindow
}

Write-Host "$Name installed successfully"
