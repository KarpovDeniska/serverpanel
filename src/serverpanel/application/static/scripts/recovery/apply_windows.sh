#!/bin/bash
# apply_windows.sh - Apply Windows Server 2022 WIM from /tmp/win.iso to sda2.
# Assumes partition_disk.sh has been run. Mounts /mnt/boot and /mnt/win on exit.
set -e

DISK="${1:-/dev/sda}"
ISO="/tmp/win.iso"
WIM_INDEX="${2:-2}"   # 2=Standard, 4=Datacenter

echo "=== Apply Windows from $ISO to $DISK ==="

if [ ! -f "$ISO" ]; then
    echo "ERROR: ISO not found: $ISO"
    exit 1
fi

which wimapply >/dev/null 2>&1 || {
    echo "Installing wimtools..."
    apt-get update && apt-get install -y wimtools ntfs-3g
}

mkdir -p /mnt/iso /mnt/win /mnt/boot
mount -o loop "$ISO" /mnt/iso

echo "Available images in install.wim:"
wiminfo /mnt/iso/sources/install.wim

echo "Applying image (index $WIM_INDEX) to ${DISK}2..."
wimapply /mnt/iso/sources/install.wim "$WIM_INDEX" "${DISK}2"

echo "Mounting partitions..."
ntfs-3g "${DISK}2" /mnt/win
ntfs-3g "${DISK}1" /mnt/boot

echo "Preparing bootloader..."
cp /mnt/win/Windows/Boot/PCAT/bootmgr /mnt/boot/
mkdir -p /mnt/boot/Boot
cp -r /mnt/win/Windows/Boot/PCAT/* /mnt/boot/Boot/ 2>/dev/null || true

umount /mnt/iso

echo "=== Windows applied ==="
echo "Partitions still mounted:"
echo "  /mnt/boot = ${DISK}1"
echo "  /mnt/win  = ${DISK}2"
