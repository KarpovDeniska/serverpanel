"""Provider protocols — core abstraction for multi-provider support."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from serverpanel.domain.enums import Capability
from serverpanel.domain.models import (
    FileInfo,
    FirewallRule,
    IPAddress,
    RescueInfo,
    ResetResult,
    ReverseDNS,
    ServerInfo,
    ServerStatus,
    SnapshotInfo,
    SSHKey,
    TrafficData,
)


@runtime_checkable
class ServerProvider(Protocol):
    """Protocol that any server provider must satisfy.

    All methods are async. If a provider does not support a capability,
    the method should raise NotImplementedError. The application layer
    checks supports() before calling.
    """

    @property
    def provider_name(self) -> str:
        """Unique identifier: 'hetzner_dedicated', 'ovh_dedicated', etc."""
        ...

    @property
    def display_name(self) -> str:
        """Human-readable name for UI."""
        ...

    def supports(self, capability: Capability) -> bool:
        """Check if this provider supports a given capability."""
        ...

    # --- Server info ---

    async def list_servers(self) -> list[ServerInfo]:
        ...

    async def get_server(self, server_id: str) -> ServerInfo:
        ...

    async def get_server_status(self, server_id: str) -> ServerStatus:
        ...

    # --- Power management ---

    async def reset_server(self, server_id: str, reset_type: str) -> ResetResult:
        ...

    async def wake_on_lan(self, server_id: str) -> None:
        ...

    # --- Rescue mode ---

    async def activate_rescue(
        self,
        server_id: str,
        os: str = "linux",
        arch: int = 64,
        authorized_keys: list[str] | None = None,
    ) -> RescueInfo:
        ...

    async def deactivate_rescue(self, server_id: str) -> None:
        ...

    async def get_rescue_status(self, server_id: str) -> RescueInfo:
        ...

    # --- Network ---

    async def get_ips(self, server_id: str) -> list[IPAddress]:
        ...

    async def get_rdns(self, server_id: str, ip: str) -> ReverseDNS:
        ...

    async def set_rdns(self, server_id: str, ip: str, hostname: str) -> ReverseDNS:
        ...

    # --- Firewall ---

    async def get_firewall_rules(self, server_id: str) -> list[FirewallRule]:
        ...

    async def set_firewall_rules(
        self, server_id: str, rules: list[FirewallRule]
    ) -> list[FirewallRule]:
        ...

    # --- SSH keys ---

    async def list_ssh_keys(self) -> list[SSHKey]:
        ...

    async def create_ssh_key(self, name: str, data: str) -> SSHKey:
        ...

    async def delete_ssh_key(self, fingerprint: str) -> None:
        ...

    # --- Traffic ---

    async def get_traffic(self, server_id: str, period: str = "month") -> TrafficData:
        ...

    # --- Lifecycle ---

    async def close(self) -> None:
        ...


@runtime_checkable
class StorageProvider(Protocol):
    """Protocol for storage services (Hetzner Storage Box, S3, etc.)."""

    @property
    def storage_type(self) -> str:
        ...

    async def list_files(self, path: str = "/") -> list[FileInfo]:
        ...

    async def read_file(self, path: str) -> bytes:
        ...

    async def write_file(self, path: str, data: bytes) -> None:
        ...

    async def delete(self, path: str) -> None:
        ...

    async def get_file_info(self, path: str) -> FileInfo:
        ...

    # --- Snapshots (if supported) ---

    async def list_snapshots(self) -> list[SnapshotInfo]:
        ...

    async def create_snapshot(self, comment: str = "") -> SnapshotInfo:
        ...

    async def revert_snapshot(self, snapshot_id: str) -> None:
        ...

    async def delete_snapshot(self, snapshot_id: str) -> None:
        ...

    async def close(self) -> None:
        ...
