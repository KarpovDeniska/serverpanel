"""One-shot seed importer for manual smoke tests.

Populates the DB with:
  - User (reuses existing one by email, or creates a new admin with the given password).
  - ProviderConfig (stub — credentials optional; if Robot creds absent, server
    shows up in UI but rescue / reset / install won't work — backup/SSH do).
  - Server with IP + SSH key.
  - StorageConfig with Hetzner Storage Box credentials.

Idempotent: re-running with the same email/ip/name updates encrypted blobs in place.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from serverpanel.infrastructure.auth.backend import hash_password
from serverpanel.infrastructure.crypto import encrypt_json
from serverpanel.infrastructure.database.models import (
    ProviderConfig,
    Server,
    StorageConfig,
    User,
)


def _read(path: str | Path | None) -> str | None:
    if not path:
        return None
    return Path(path).read_text(encoding="utf-8")


async def seed(
    db: AsyncSession,
    *,
    admin_email: str,
    admin_password: str | None,
    server_ip: str,
    server_ssh_username: str,
    server_ssh_key_path: str | Path | None,
    server_ssh_password: str | None,
    sb_host: str,
    sb_user: str,
    sb_port: int,
    sb_ssh_key_path: str | Path | None,
    sb_password: str | None,
    provider_name: str = "hetzner-dedicated",
    server_name: str = "hetzner-windows",
    storage_name: str = "hetzner-storagebox",
    robot_user: str | None = None,
    robot_password: str | None = None,
) -> dict[str, int]:
    """Create/update user, provider, server and storage rows. Return row IDs."""
    # --- User ---
    user = (await db.execute(select(User).where(User.email == admin_email))).scalar_one_or_none()
    if user is None:
        if not admin_password:
            raise ValueError(
                f"user {admin_email} does not exist and --admin-password was not supplied"
            )
        user = User(
            email=admin_email,
            password_hash=hash_password(admin_password),
            display_name="admin",
            role="admin",
            is_active=True,
        )
        db.add(user)
        await db.flush()
        print(f"  created user {admin_email} (admin)")
    else:
        print(f"  user {admin_email} exists → id={user.id}")

    # --- ProviderConfig ---
    creds = {
        "robot_user": robot_user or "",
        "robot_password": robot_password or "",
    }
    provider = (
        await db.execute(
            select(ProviderConfig).where(
                ProviderConfig.user_id == user.id,
                ProviderConfig.name == provider_name,
            )
        )
    ).scalar_one_or_none()
    if provider is None:
        provider = ProviderConfig(
            user_id=user.id,
            provider_type="hetzner_dedicated",
            name=provider_name,
            credentials_encrypted=encrypt_json(creds),
            is_active=True,
        )
        db.add(provider)
        await db.flush()
        print(f"  created provider_config '{provider_name}' → id={provider.id}")
    else:
        provider.credentials_encrypted = encrypt_json(creds)
        db.add(provider)
        print(f"  updated provider_config '{provider_name}' → id={provider.id}")

    # --- Server ---
    ssh_blob: dict = {}
    if server_ssh_password:
        ssh_blob["password"] = server_ssh_password
    key_text = _read(server_ssh_key_path)
    if key_text:
        ssh_blob["private_key"] = key_text
    server = (
        await db.execute(
            select(Server).where(
                Server.provider_config_id == provider.id,
                Server.ip_address == server_ip,
            )
        )
    ).scalar_one_or_none()
    if server is None:
        server = Server(
            provider_config_id=provider.id,
            provider_server_id=server_ip,  # placeholder; Robot API id unknown without Robot creds
            name=server_name,
            ip_address=server_ip,
            os_type="windows",
            ssh_username=server_ssh_username,
            ssh_port=22,
            ssh_key_encrypted=encrypt_json(ssh_blob) if ssh_blob else None,
            check_ports=[22, 3389, 443],
            extra={},
        )
        db.add(server)
        await db.flush()
        print(f"  created server {server_ip} → id={server.id}")
    else:
        server.ssh_username = server_ssh_username
        if ssh_blob:
            server.ssh_key_encrypted = encrypt_json(ssh_blob)
        db.add(server)
        print(f"  updated server {server_ip} → id={server.id}")

    # --- StorageConfig ---
    sb_conn: dict = {
        "host": sb_host,
        "user": sb_user,
        "port": sb_port,
    }
    sb_key_text = _read(sb_ssh_key_path)
    if sb_key_text:
        sb_conn["private_key"] = sb_key_text
    if sb_password:
        sb_conn["password"] = sb_password
    storage = (
        await db.execute(
            select(StorageConfig).where(
                StorageConfig.server_id == server.id,
                StorageConfig.name == storage_name,
            )
        )
    ).scalar_one_or_none()
    if storage is None:
        storage = StorageConfig(
            server_id=server.id,
            storage_type="hetzner_storagebox",
            name=storage_name,
            connection_encrypted=encrypt_json(sb_conn),
            base_path="/",
        )
        db.add(storage)
        await db.flush()
        print(f"  created storage_config '{storage_name}' → id={storage.id}")
    else:
        storage.connection_encrypted = encrypt_json(sb_conn)
        db.add(storage)
        print(f"  updated storage_config '{storage_name}' → id={storage.id}")

    await db.commit()
    return {
        "user_id": user.id,
        "provider_config_id": provider.id,
        "server_id": server.id,
        "storage_config_id": storage.id,
    }
