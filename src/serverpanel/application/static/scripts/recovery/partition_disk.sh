#!/bin/bash
# partition_disk.sh - MBR partition for Windows Server 2022 (BIOS boot).
# Usage: ./partition_disk.sh /dev/sda
set -e

DISK="${1:-/dev/sda}"

echo "=== Partitioning $DISK ==="
echo "WARNING: All data on $DISK will be destroyed!"

umount ${DISK}* 2>/dev/null || true

parted -s "$DISK" mklabel msdos
parted -s "$DISK" mkpart primary ntfs 1MiB 512MiB
parted -s "$DISK" set 1 boot on
parted -s "$DISK" mkpart primary ntfs 512MiB 100%

echo "Partitions created:"
parted -s "$DISK" print

echo "Formatting ${DISK}1 (Boot)..."
mkntfs -f "${DISK}1"
echo "Formatting ${DISK}2 (Windows)..."
mkntfs -f "${DISK}2"

echo "=== Partitioning done ==="
