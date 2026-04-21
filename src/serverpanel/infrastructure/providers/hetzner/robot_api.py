"""Async Hetzner Robot API client via httpx."""

from __future__ import annotations

import httpx

from serverpanel.domain.exceptions import (
    ProviderAuthError,
    ProviderError,
    ProviderNotFoundError,
    ProviderRateLimitError,
)

BASE_URL = "https://robot-ws.your-server.de"


class HetznerRobotAPI:
    """Low-level async client for Hetzner Robot REST API."""

    def __init__(self, user: str, password: str):
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            auth=(user, password),
            timeout=30.0,
        )

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        resp = await self._client.request(method, path, **kwargs)
        if resp.status_code == 401:
            raise ProviderAuthError("Invalid Robot API credentials", provider="hetzner")
        if resp.status_code == 404:
            raise ProviderNotFoundError(f"Not found: {path}", provider="hetzner")
        if resp.status_code == 429:
            raise ProviderRateLimitError("Rate limit exceeded", provider="hetzner")
        if resp.status_code >= 400:
            raise ProviderError(
                f"Robot API error {resp.status_code}: {resp.text}",
                provider="hetzner",
                status_code=resp.status_code,
            )
        if resp.content:
            return resp.json()
        return {}

    # --- Servers ---

    async def get_servers(self) -> list[dict]:
        return await self._request("GET", "/server")

    async def get_server(self, server_number: int) -> dict:
        return await self._request("GET", f"/server/{server_number}")

    # --- Reset ---

    async def reset_server(self, server_number: int, reset_type: str = "hw") -> dict:
        return await self._request(
            "POST", f"/reset/{server_number}", data={"type": reset_type}
        )

    # --- Wake on LAN ---

    async def wake_on_lan(self, server_number: int) -> dict:
        return await self._request("POST", f"/wol/{server_number}")

    # --- Rescue ---

    async def activate_rescue(
        self,
        server_number: int,
        os: str = "linux",
        arch: int = 64,
        authorized_keys: list[str] | None = None,
    ) -> dict:
        data = {"os": os, "arch": arch}
        if authorized_keys:
            data["authorized_key[]"] = authorized_keys
        return await self._request(
            "POST", f"/boot/{server_number}/rescue", data=data
        )

    async def get_rescue(self, server_number: int) -> dict:
        return await self._request("GET", f"/boot/{server_number}/rescue")

    async def deactivate_rescue(self, server_number: int) -> dict:
        return await self._request("DELETE", f"/boot/{server_number}/rescue")

    # --- Boot ---

    async def get_boot(self, server_number: int) -> dict:
        return await self._request("GET", f"/boot/{server_number}")

    # --- IP ---

    async def get_ips(self, server_number: int) -> list[dict]:
        data = await self._request("GET", "/ip")
        return [
            item for item in data
            if str(item.get("ip", {}).get("server_number")) == str(server_number)
        ]

    async def get_ip(self, ip: str) -> dict:
        return await self._request("GET", f"/ip/{ip}")

    # --- Reverse DNS ---

    async def get_rdns(self, ip: str) -> dict:
        return await self._request("GET", f"/rdns/{ip}")

    async def set_rdns(self, ip: str, hostname: str) -> dict:
        return await self._request("POST", f"/rdns/{ip}", data={"ptr": hostname})

    async def delete_rdns(self, ip: str) -> dict:
        return await self._request("DELETE", f"/rdns/{ip}")

    # --- Firewall ---

    async def get_firewall(self, server_number: int) -> dict:
        return await self._request("GET", f"/firewall/{server_number}")

    async def update_firewall(self, server_number: int, rules: dict) -> dict:
        return await self._request(
            "POST", f"/firewall/{server_number}", data=rules
        )

    # --- SSH Keys ---

    async def get_ssh_keys(self) -> list[dict]:
        return await self._request("GET", "/key")

    async def get_ssh_key(self, fingerprint: str) -> dict:
        return await self._request("GET", f"/key/{fingerprint}")

    async def create_ssh_key(self, name: str, data: str) -> dict:
        return await self._request("POST", "/key", data={"name": name, "data": data})

    async def delete_ssh_key(self, fingerprint: str) -> dict:
        return await self._request("DELETE", f"/key/{fingerprint}")

    # --- Traffic ---

    async def get_traffic(self, server_number: int) -> dict:
        return await self._request(
            "POST", "/traffic",
            data={
                "ip": [],
                "subnet": [],
                "type": "month",
                "from": "",
                "to": "",
            },
        )

    # --- Storage Box ---

    async def get_storage_boxes(self) -> list[dict]:
        return await self._request("GET", "/storagebox")

    async def get_storage_box(self, storagebox_id: int) -> dict:
        return await self._request("GET", f"/storagebox/{storagebox_id}")

    async def get_storage_box_snapshots(self, storagebox_id: int) -> list[dict]:
        return await self._request("GET", f"/storagebox/{storagebox_id}/snapshot")

    async def create_storage_box_snapshot(self, storagebox_id: int) -> dict:
        return await self._request("POST", f"/storagebox/{storagebox_id}/snapshot")

    async def delete_storage_box_snapshot(
        self, storagebox_id: int, snapshot_name: str
    ) -> dict:
        return await self._request(
            "DELETE", f"/storagebox/{storagebox_id}/snapshot/{snapshot_name}"
        )

    async def revert_storage_box_snapshot(
        self, storagebox_id: int, snapshot_name: str
    ) -> dict:
        return await self._request(
            "POST", f"/storagebox/{storagebox_id}/snapshot/{snapshot_name}"
        )

    # --- Lifecycle ---

    async def close(self) -> None:
        await self._client.aclose()
