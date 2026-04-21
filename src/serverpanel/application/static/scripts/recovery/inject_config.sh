#!/bin/bash
# inject_config.sh - Drop autounattend.xml + serverpanel recovery scripts into the
# freshly-applied Windows volume. Expects:
#   /tmp/config.json            (shipped by serverpanel)
#   /tmp/restore.ps1
#   /tmp/restore_data.ps1
#   /tmp/install_software.ps1
#   /tmp/SetupComplete.cmd
# /mnt/win and /mnt/boot must be mounted (by apply_windows.sh).
set -e

echo "=== Injecting config and recovery scripts ==="

if [ ! -d "/mnt/win/Windows" ]; then
    echo "ERROR: /mnt/win/Windows not found. Run apply_windows.sh first."
    exit 1
fi

# 1. unattend.xml (minimal OOBE skip + autologon for first-boot restore)
echo "Writing unattend.xml..."
mkdir -p /mnt/win/Windows/Panther
cat > /mnt/win/Windows/Panther/unattend.xml << 'XMLEOF'
<?xml version="1.0" encoding="utf-8"?>
<unattend xmlns="urn:schemas-microsoft-com:unattend">
    <settings pass="specialize">
        <component name="Microsoft-Windows-Shell-Setup" processorArchitecture="amd64"
                   publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
            <ComputerName>PLACEHOLDER_HOSTNAME</ComputerName>
        </component>
        <component name="Microsoft-Windows-TerminalServices-LocalSessionManager" processorArchitecture="amd64"
                   publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
            <fDenyTSConnections>false</fDenyTSConnections>
        </component>
    </settings>
    <settings pass="oobeSystem">
        <component name="Microsoft-Windows-Shell-Setup" processorArchitecture="amd64"
                   publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
            <OOBE>
                <HideEULAPage>true</HideEULAPage>
                <HideLocalAccountScreen>true</HideLocalAccountScreen>
                <HideOnlineAccountScreens>true</HideOnlineAccountScreens>
                <HideWirelessSetupInOOBE>true</HideWirelessSetupInOOBE>
                <ProtectYourPC>3</ProtectYourPC>
            </OOBE>
            <UserAccounts>
                <AdministratorPassword>
                    <Value>PLACEHOLDER_PASSWORD</Value>
                    <PlainText>true</PlainText>
                </AdministratorPassword>
            </UserAccounts>
            <AutoLogon>
                <Enabled>true</Enabled>
                <Username>Administrator</Username>
                <Password>
                    <Value>PLACEHOLDER_PASSWORD</Value>
                    <PlainText>true</PlainText>
                </Password>
                <LogonCount>3</LogonCount>
            </AutoLogon>
        </component>
        <component name="Microsoft-Windows-International-Core" processorArchitecture="amd64"
                   publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
            <InputLocale>en-US;ru-RU</InputLocale>
            <SystemLocale>ru-RU</SystemLocale>
            <UILanguage>ru-RU</UILanguage>
            <UserLocale>ru-RU</UserLocale>
        </component>
    </settings>
</unattend>
XMLEOF

# 2. Substitute password + hostname from config.json
if [ -f /tmp/config.json ]; then
    ADMIN_PASS=$(python3 -c "import json; print(json.load(open('/tmp/config.json'))['windows']['admin_password'])" 2>/dev/null || echo "P@ssw0rd123")
    HOSTNAME=$(python3 -c "import json; print(json.load(open('/tmp/config.json'))['windows']['hostname'])" 2>/dev/null || echo "WIN-SRV")
    sed -i "s|PLACEHOLDER_PASSWORD|$ADMIN_PASS|g" /mnt/win/Windows/Panther/unattend.xml
    sed -i "s|PLACEHOLDER_HOSTNAME|$HOSTNAME|g" /mnt/win/Windows/Panther/unattend.xml
fi

# 3. Recovery scripts + config.json
echo "Copying recovery scripts..."
mkdir -p /mnt/win/Windows/Setup/Scripts
mkdir -p /mnt/win/Recovery

for f in SetupComplete.cmd restore.ps1 restore_data.ps1 install_software.ps1; do
    if [ -f "/tmp/$f" ]; then
        cp "/tmp/$f" "/mnt/win/Windows/Setup/Scripts/$f"
        echo "  $f -> C:\\Windows\\Setup\\Scripts\\"
    else
        echo "  WARN: /tmp/$f missing — skipped"
    fi
done

if [ -f /tmp/config.json ]; then
    cp /tmp/config.json /mnt/win/Recovery/config.json
    echo "  config.json -> C:\\Recovery\\"
fi

# 4. Unmount
echo "Unmounting..."
umount /mnt/win
umount /mnt/boot

echo "=== Injection done — ready to reboot ==="
