@echo off
echo [%date% %time%] SetupComplete started >> C:\recovery.log

REM Firewall: RDP + SSH
netsh advfirewall firewall set rule group="remote desktop" new enable=Yes
netsh advfirewall firewall add rule name="OpenSSH" dir=in action=allow protocol=TCP localport=22

REM OpenSSH Server
powershell -Command "Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0" >> C:\recovery.log 2>&1
powershell -Command "Start-Service sshd; Set-Service -Name sshd -StartupType Automatic" >> C:\recovery.log 2>&1

REM Run serverpanel restore
powershell -ExecutionPolicy Bypass -File C:\Windows\Setup\Scripts\restore.ps1 >> C:\recovery.log 2>&1

echo [%date% %time%] SetupComplete finished >> C:\recovery.log
