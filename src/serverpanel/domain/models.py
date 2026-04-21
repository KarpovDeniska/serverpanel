"""Pydantic domain models — provider-agnostic data structures."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class ServerInfo(BaseModel):
    """Server information returned by a provider."""

    server_id: str
    name: str
    ip_address: str | None = None
    status: str = "unknown"
    product: str | None = None
    datacenter: str | None = None
    os: str | None = None
    metadata: dict = {}


class ServerStatus(BaseModel):
    """Current server status."""

    server_id: str
    status: str
    is_rescue: bool = False


class RescueInfo(BaseModel):
    """Rescue mode activation result."""

    active: bool
    os: str | None = None
    password: str | None = None
    ssh_keys: list[str] = []


class ResetResult(BaseModel):
    """Server reset result."""

    server_id: str
    reset_type: str
    success: bool = True


class IPAddress(BaseModel):
    """IP address information."""

    ip: str
    version: int = 4  # 4 or 6
    ptr: str | None = None
    server_id: str | None = None
    is_main: bool = False


class ReverseDNS(BaseModel):
    ip: str
    hostname: str


class FirewallRule(BaseModel):
    """Firewall rule."""

    direction: str = "in"  # in, out
    protocol: str | None = None  # tcp, udp, icmp
    port: str | None = None  # "80", "1000-2000"
    src_ip: str | None = None
    dst_ip: str | None = None
    action: str = "accept"  # accept, drop
    comment: str | None = None


class SSHKey(BaseModel):
    """SSH key in provider account."""

    name: str
    fingerprint: str
    data: str
    created_at: datetime | None = None


class TrafficData(BaseModel):
    """Traffic statistics."""

    server_id: str
    period: str
    incoming_gb: float = 0.0
    outgoing_gb: float = 0.0
    total_gb: float = 0.0


class FileInfo(BaseModel):
    """File/directory info from storage."""

    name: str
    path: str
    is_dir: bool = False
    size: int = 0
    modified_at: datetime | None = None


class SnapshotInfo(BaseModel):
    """Storage snapshot info."""

    id: str
    name: str
    comment: str = ""
    created_at: datetime | None = None
    size: str | None = None
