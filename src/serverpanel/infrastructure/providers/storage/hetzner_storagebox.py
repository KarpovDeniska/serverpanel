"""Hetzner Storage Box — StorageProvider implementation over SFTP (paramiko)."""

from __future__ import annotations

import asyncio
import os
import stat
from datetime import UTC, datetime
from pathlib import PurePosixPath

import paramiko

from serverpanel.domain.models import FileInfo, SnapshotInfo
from serverpanel.infrastructure.providers.storage import register_storage


class HetznerStorageBox:
    """Hetzner Storage Box via SFTP.

    Auth: either private SSH key (path) or password. Port 23 is default.
    All paramiko ops are blocking → wrapped with asyncio.to_thread.
    """

    DISPLAY_NAME = "Hetzner Storage Box"
    DESCRIPTION = "Hetzner Storage Box over SFTP"

    def __init__(
        self,
        host: str,
        user: str,
        port: int = 23,
        ssh_key_path: str | None = None,
        password: str | None = None,
        **kwargs,
    ):
        self._host = host
        self._user = user
        self._port = port
        self._key_path = ssh_key_path
        self._password = password
        self._transport: paramiko.Transport | None = None
        self._sftp: paramiko.SFTPClient | None = None
        self._lock = asyncio.Lock()

    @property
    def storage_type(self) -> str:
        return "hetzner_storagebox"

    # --- Connection management ---

    def _connect_sync(self) -> None:
        transport = paramiko.Transport((self._host, self._port))
        if self._key_path:
            key_path = os.path.expanduser(self._key_path)
            try:
                key = paramiko.Ed25519Key.from_private_key_file(key_path)
            except paramiko.SSHException:
                key = paramiko.RSAKey.from_private_key_file(key_path)
            transport.connect(username=self._user, pkey=key)
        elif self._password:
            transport.connect(username=self._user, password=self._password)
        else:
            raise ValueError("Either ssh_key_path or password is required")
        self._transport = transport
        self._sftp = paramiko.SFTPClient.from_transport(transport)

    async def _ensure_connected(self) -> paramiko.SFTPClient:
        async with self._lock:
            if self._sftp is None or self._transport is None or not self._transport.is_active():
                await asyncio.to_thread(self._connect_sync)
            return self._sftp  # type: ignore[return-value]

    # --- Helpers ---

    @staticmethod
    def _norm(path: str) -> str:
        return str(PurePosixPath("/") / path.lstrip("/"))

    def _ensure_remote_dir_sync(self, sftp: paramiko.SFTPClient, remote_dir: str) -> None:
        remote_dir = self._norm(remote_dir)
        if remote_dir in ("/", ""):
            return
        dirs_to_create: list[str] = []
        path = remote_dir
        while path and path != "/":
            try:
                sftp.stat(path)
                break
            except FileNotFoundError:
                dirs_to_create.append(path)
                path = str(PurePosixPath(path).parent)
        for d in reversed(dirs_to_create):
            sftp.mkdir(d)

    @staticmethod
    def _stat_to_info(name: str, path: str, attr: paramiko.SFTPAttributes) -> FileInfo:
        is_dir = stat.S_ISDIR(attr.st_mode) if attr.st_mode else False
        modified = (
            datetime.fromtimestamp(attr.st_mtime, tz=UTC)
            if attr.st_mtime
            else None
        )
        return FileInfo(
            name=name,
            path=path,
            is_dir=is_dir,
            size=attr.st_size or 0,
            modified_at=modified,
        )

    # --- StorageProvider protocol ---

    async def list_files(self, path: str = "/") -> list[FileInfo]:
        sftp = await self._ensure_connected()
        remote = self._norm(path)

        def _op() -> list[FileInfo]:
            try:
                entries = sftp.listdir_attr(remote)
            except FileNotFoundError:
                return []
            return [
                self._stat_to_info(
                    name=a.filename,
                    path=str(PurePosixPath(remote) / a.filename),
                    attr=a,
                )
                for a in entries
            ]

        return await asyncio.to_thread(_op)

    async def read_file(self, path: str) -> bytes:
        sftp = await self._ensure_connected()
        remote = self._norm(path)

        def _op() -> bytes:
            with sftp.open(remote, "rb") as f:
                return f.read()

        return await asyncio.to_thread(_op)

    async def write_file(self, path: str, data: bytes) -> None:
        sftp = await self._ensure_connected()
        remote = self._norm(path)
        remote_dir = str(PurePosixPath(remote).parent)

        def _op() -> None:
            self._ensure_remote_dir_sync(sftp, remote_dir)
            with sftp.open(remote, "wb") as f:
                f.write(data)

        await asyncio.to_thread(_op)

    async def delete(self, path: str) -> None:
        sftp = await self._ensure_connected()
        remote = self._norm(path)

        def _rmtree(p: str) -> None:
            try:
                attr = sftp.stat(p)
            except FileNotFoundError:
                return
            if stat.S_ISDIR(attr.st_mode or 0):
                for child in sftp.listdir_attr(p):
                    _rmtree(str(PurePosixPath(p) / child.filename))
                sftp.rmdir(p)
            else:
                sftp.remove(p)

        await asyncio.to_thread(_rmtree, remote)

    async def get_file_info(self, path: str) -> FileInfo:
        sftp = await self._ensure_connected()
        remote = self._norm(path)

        def _op() -> FileInfo:
            attr = sftp.stat(remote)
            return self._stat_to_info(
                name=PurePosixPath(remote).name,
                path=remote,
                attr=attr,
            )

        return await asyncio.to_thread(_op)

    # --- Snapshots: Storage Box does support them, but via separate API. Not wired yet. ---

    async def list_snapshots(self) -> list[SnapshotInfo]:
        raise NotImplementedError("Storage Box snapshots not implemented yet")

    async def create_snapshot(self, comment: str = "") -> SnapshotInfo:
        raise NotImplementedError("Storage Box snapshots not implemented yet")

    async def revert_snapshot(self, snapshot_id: str) -> None:
        raise NotImplementedError("Storage Box snapshots not implemented yet")

    async def delete_snapshot(self, snapshot_id: str) -> None:
        raise NotImplementedError("Storage Box snapshots not implemented yet")

    async def close(self) -> None:
        async with self._lock:
            sftp, transport = self._sftp, self._transport
            self._sftp = None
            self._transport = None

        def _op() -> None:
            if sftp is not None:
                sftp.close()
            if transport is not None:
                transport.close()

        await asyncio.to_thread(_op)


register_storage("hetzner_storagebox", HetznerStorageBox)
