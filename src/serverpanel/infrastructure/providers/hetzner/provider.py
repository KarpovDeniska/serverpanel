"""Hetzner Dedicated Server provider — implements ServerProvider protocol."""

from __future__ import annotations

from serverpanel.domain.enums import Capability
from serverpanel.domain.models import (
    FirewallRule,
    IPAddress,
    RescueInfo,
    ResetResult,
    ReverseDNS,
    ServerInfo,
    ServerStatus,
    SSHKey,
    TrafficData,
)
from serverpanel.infrastructure.providers.hetzner.robot_api import HetznerRobotAPI


class HetznerDedicatedProvider:
    """Hetzner Dedicated Server provider via Robot API."""

    DISPLAY_NAME = "Hetzner Dedicated"
    DESCRIPTION = "Manage Hetzner dedicated servers via Robot API"

    SUPPORTED = {
        Capability.RESCUE_MODE,
        Capability.HARDWARE_RESET,
        Capability.SOFTWARE_RESET,
        Capability.WAKE_ON_LAN,
        Capability.FIREWALL,
        Capability.RDNS,
        Capability.SSH_KEYS,
        Capability.TRAFFIC_STATS,
        Capability.STORAGE_BOX,
    }

    def __init__(self, robot_user: str, robot_password: str, **kwargs):
        self._api = HetznerRobotAPI(robot_user, robot_password)

    @property
    def provider_name(self) -> str:
        return "hetzner_dedicated"

    @property
    def display_name(self) -> str:
        return self.DISPLAY_NAME

    def supports(self, capability: Capability) -> bool:
        return capability in self.SUPPORTED

    # --- Server info ---

    async def list_servers(self) -> list[ServerInfo]:
        servers = await self._api.get_servers()
        return [
            ServerInfo(
                server_id=str(s["server"]["server_number"]),
                name=s["server"].get("server_name", ""),
                ip_address=s["server"].get("server_ip"),
                status=s["server"].get("status", "unknown"),
                product=s["server"].get("product"),
                datacenter=s["server"].get("dc"),
            )
            for s in servers
        ]

    async def get_server(self, server_id: str) -> ServerInfo:
        data = await self._api.get_server(int(server_id))
        s = data.get("server", data)
        return ServerInfo(
            server_id=str(s.get("server_number", server_id)),
            name=s.get("server_name", ""),
            ip_address=s.get("server_ip"),
            status=s.get("status", "unknown"),
            product=s.get("product"),
            datacenter=s.get("dc"),
        )

    async def get_server_status(self, server_id: str) -> ServerStatus:
        data = await self._api.get_server(int(server_id))
        s = data.get("server", data)
        rescue = await self._api.get_rescue(int(server_id))
        return ServerStatus(
            server_id=server_id,
            status=s.get("status", "unknown"),
            is_rescue=rescue.get("rescue", {}).get("active", False),
        )

    # --- Power management ---

    async def reset_server(self, server_id: str, reset_type: str) -> ResetResult:
        await self._api.reset_server(int(server_id), reset_type)
        return ResetResult(server_id=server_id, reset_type=reset_type)

    async def wake_on_lan(self, server_id: str) -> None:
        await self._api.wake_on_lan(int(server_id))

    # --- Rescue mode ---

    async def activate_rescue(
        self,
        server_id: str,
        os: str = "linux",
        arch: int = 64,
        authorized_keys: list[str] | None = None,
    ) -> RescueInfo:
        data = await self._api.activate_rescue(int(server_id), os, arch, authorized_keys)
        r = data.get("rescue", data)
        return RescueInfo(
            active=True,
            os=r.get("os"),
            password=r.get("password"),
            ssh_keys=r.get("authorized_key", []),
        )

    async def deactivate_rescue(self, server_id: str) -> None:
        await self._api.deactivate_rescue(int(server_id))

    async def get_rescue_status(self, server_id: str) -> RescueInfo:
        data = await self._api.get_rescue(int(server_id))
        r = data.get("rescue", data)
        return RescueInfo(
            active=r.get("active", False),
            os=r.get("os"),
            password=r.get("password"),
            ssh_keys=r.get("authorized_key", []),
        )

    # --- Network ---

    async def get_ips(self, server_id: str) -> list[IPAddress]:
        data = await self._api.get_ips(int(server_id))
        return [
            IPAddress(
                ip=item["ip"]["ip"],
                ptr=item["ip"].get("server_ip"),
                server_id=server_id,
            )
            for item in data
        ]

    async def get_rdns(self, server_id: str, ip: str) -> ReverseDNS:
        data = await self._api.get_rdns(ip)
        r = data.get("rdns", data)
        return ReverseDNS(ip=r.get("ip", ip), hostname=r.get("ptr", ""))

    async def set_rdns(self, server_id: str, ip: str, hostname: str) -> ReverseDNS:
        await self._api.set_rdns(ip, hostname)
        return ReverseDNS(ip=ip, hostname=hostname)

    # --- Firewall ---

    async def get_firewall_rules(self, server_id: str) -> list[FirewallRule]:
        data = await self._api.get_firewall(int(server_id))
        rules = data.get("firewall", {}).get("rules", {}).get("input", [])
        return [
            FirewallRule(
                direction="in",
                protocol=r.get("protocol"),
                port=r.get("dst_port"),
                src_ip=r.get("src_ip"),
                action=r.get("action", "accept"),
                comment=r.get("name"),
            )
            for r in rules
        ]

    async def set_firewall_rules(
        self, server_id: str, rules: list[FirewallRule]
    ) -> list[FirewallRule]:
        # Robot API expects specific format for firewall update
        raise NotImplementedError("Firewall rule setting not yet implemented")

    # --- SSH keys ---

    async def list_ssh_keys(self) -> list[SSHKey]:
        data = await self._api.get_ssh_keys()
        return [
            SSHKey(
                name=item["key"]["name"],
                fingerprint=item["key"]["fingerprint"],
                data=item["key"]["data"],
            )
            for item in data
        ]

    async def create_ssh_key(self, name: str, data: str) -> SSHKey:
        result = await self._api.create_ssh_key(name, data)
        k = result.get("key", result)
        return SSHKey(name=k["name"], fingerprint=k["fingerprint"], data=k["data"])

    async def delete_ssh_key(self, fingerprint: str) -> None:
        await self._api.delete_ssh_key(fingerprint)

    # --- Traffic ---

    async def get_traffic(self, server_id: str, period: str = "month") -> TrafficData:
        data = await self._api.get_traffic(int(server_id))
        t = data.get("traffic", {})
        return TrafficData(
            server_id=server_id,
            period=period,
            incoming_gb=t.get("in", 0) / (1024**3) if t.get("in") else 0,
            outgoing_gb=t.get("out", 0) / (1024**3) if t.get("out") else 0,
            total_gb=t.get("sum", 0) / (1024**3) if t.get("sum") else 0,
        )

    # --- Lifecycle ---

    async def close(self) -> None:
        await self._api.close()
