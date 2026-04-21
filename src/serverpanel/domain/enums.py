"""Domain enumerations."""

from enum import StrEnum


class Capability(StrEnum):
    """What a server provider can do. Not all providers support everything."""

    RESCUE_MODE = "rescue_mode"
    HARDWARE_RESET = "hardware_reset"
    SOFTWARE_RESET = "software_reset"
    WAKE_ON_LAN = "wake_on_lan"
    FIREWALL = "firewall"
    RDNS = "rdns"
    SSH_KEYS = "ssh_keys"
    TRAFFIC_STATS = "traffic_stats"
    STORAGE_BOX = "storage_box"
    VNC_CONSOLE = "vnc_console"
    REINSTALL_OS = "reinstall_os"


class ResetType(StrEnum):
    SOFTWARE = "sw"
    HARDWARE = "hw"
    POWER = "power"
    MANUAL = "man"


class ServerStatusType(StrEnum):
    RUNNING = "running"
    READY = "ready"
    RESCUE = "rescue"
    INSTALLING = "installing"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


class RecoveryStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


class BackupStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"   # some destinations OK, some failed
    FAILED = "failed"


class UserRole(StrEnum):
    ADMIN = "admin"
    USER = "user"
