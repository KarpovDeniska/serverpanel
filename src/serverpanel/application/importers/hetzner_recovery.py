"""Import a legacy hetzner-recovery config.yaml as ProviderConfig + Server + StorageConfig + BackupConfig.

The old project hardcoded paths and software flags inside backup_daily.ps1 / upload_storagebox.ps1.
This importer translates that hardcoded behavior into serverpanel-native records, so the user can
migrate without re-typing the whole setup.

Idempotent: re-running with the same target user/server updates rows in place (except encrypted
blobs, which are re-written).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from serverpanel.infrastructure.crypto import encrypt_json
from serverpanel.infrastructure.database.models import (
    BackupConfig,
    ProviderConfig,
    Server,
    StorageConfig,
    User,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Legacy backup plan — DEPRECATED ARTIFACT
#
# These are the exact source paths hardcoded in the old hetzner-recovery
# project's backup_daily.ps1 / upload_storagebox.ps1. This importer exists
# ONLY to migrate that one deprecated setup; it is NOT a template for new
# installations. New users should create backup configs via the UI form with
# their own paths. See docs/migration.md for deprecation timeline.
# ---------------------------------------------------------------------------
LEGACY_DAILY_SOURCES: list[dict[str, Any]] = [
    # Live 1C DB — VSS shadow + zip (matches Stage 2 of backup_daily.ps1)
    {"alias": "UNF",                  "type": "vss_dir", "path": r"D:\1С\БД\UNF",              "compress": "zip"},
    # From C: drive
    {"alias": "1c_license",           "type": "dir",     "path": r"C:\ProgramData\1C\licenses", "compress": "none"},
    {"alias": "ibases",               "type": "file",    "path": r"C:\Users\Administrator\AppData\Roaming\1C\1CEStart\ibases.v8i", "compress": "none"},
    {"alias": "1c_settings",          "type": "dir",     "path": r"C:\Users\Administrator\AppData\Roaming\1C\1cv8", "compress": "none"},
    # From D: drive
    {"alias": "1c_files",             "type": "dir",     "path": r"D:\1С\БД\Файлы",             "compress": "none"},
    {"alias": "1c_obrabotki",         "type": "dir",     "path": r"D:\1С\Обработки",            "compress": "none"},
    {"alias": "rutoken",              "type": "dir",     "path": r"D:\Soft\rutoken",            "compress": "none"},
    {"alias": "1c_licenses_archive",  "type": "dir",     "path": r"D:\Soft\Лицензии 1С",        "compress": "none"},
    {"alias": "xray_config",          "type": "file",    "path": r"D:\Personal folders\dkarpov\projects\tools\xray\config_xhttp.json", "compress": "none"},
    {"alias": "xray_winsw",           "type": "file",    "path": r"D:\Personal folders\dkarpov\projects\tools\xray\WinSW.xml",         "compress": "none"},
]

# Weekly — IIS config (Stage 1 + Sunday block in upload_storagebox.ps1).
LEGACY_WEEKLY_SOURCES: list[dict[str, Any]] = [
    {"alias": "iis_inetpub",  "type": "dir", "path": r"C:\inetpub",  "compress": "none"},
    {"alias": "iis_winacme",  "type": "dir", "path": r"C:\win-acme", "compress": "none"},
]


def load_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


async def import_legacy_config(
    db: AsyncSession,
    config_path: Path,
    user_email: str,
    private_key_text: str | None = None,
    rescue_private_key_text: str | None = None,
) -> dict[str, int]:
    """Import config.yaml for `user_email`. Returns created/updated row IDs.

    `private_key_text` overrides `storage_box.ssh_key_path` — useful when the key
    lives only on the operator's disk and shouldn't be referenced by path on the server.
    """
    cfg = load_yaml(config_path)
    if not cfg:
        raise ValueError(f"Empty or invalid YAML: {config_path}")

    user = await _user_by_email(db, user_email)
    if user is None:
        raise RuntimeError(f"User not found: {user_email} (create via UI/auth first)")

    hetzner = cfg.get("hetzner") or {}
    sb = cfg.get("storage_box") or {}
    win = cfg.get("windows") or {}
    software = {k: bool(v) for k, v in (cfg.get("software") or {}).items()}

    if not hetzner.get("robot_user") or not hetzner.get("server_number"):
        raise ValueError("hetzner.robot_user and hetzner.server_number are required")
    if not sb.get("host") or not sb.get("user"):
        raise ValueError("storage_box.host and storage_box.user are required")

    pcfg = await _upsert_provider(db, user.id, hetzner)
    srv = await _upsert_server(db, pcfg.id, hetzner, rescue_private_key_text)
    stor = await _upsert_storage(db, srv.id, sb, private_key_text)

    daily_id = await _upsert_daily_backup(db, srv.id, stor.id)
    weekly_id = await _upsert_weekly_backup(db, srv.id, stor.id)

    # store windows recovery defaults as hints on Server.extra
    extra = dict(srv.extra or {})
    extra.setdefault("recovery_defaults", {}).update({
        "hostname": win.get("hostname") or "WIN-SRV",
        "software": software,
    })
    if win.get("product_key"):
        extra["recovery_defaults"]["product_key"] = win["product_key"]
    # admin_password is sensitive — store encrypted on Server.ssh_key_encrypted? No, that's SSH-specific.
    # For now we skip persisting admin_password; user re-enters it on the recovery form.
    srv.extra = extra
    db.add(srv)

    await db.commit()

    return {
        "user_id": user.id,
        "provider_config_id": pcfg.id,
        "server_id": srv.id,
        "storage_config_id": stor.id,
        "backup_daily_id": daily_id,
        "backup_weekly_id": weekly_id,
    }


# ----------------------------------------------------------------------
# Upserts
# ----------------------------------------------------------------------


async def _user_by_email(db: AsyncSession, email: str) -> User | None:
    r = await db.execute(select(User).where(User.email == email))
    return r.scalar_one_or_none()


async def _upsert_provider(
    db: AsyncSession, user_id: int, hetzner: dict
) -> ProviderConfig:
    name = f"hetzner-{hetzner['server_number']}"
    r = await db.execute(
        select(ProviderConfig).where(
            ProviderConfig.user_id == user_id,
            ProviderConfig.provider_type == "hetzner_dedicated",
            ProviderConfig.name == name,
        )
    )
    row = r.scalar_one_or_none()
    creds = {
        "robot_user": hetzner["robot_user"],
        "robot_password": hetzner.get("robot_password", ""),
    }
    if row is None:
        row = ProviderConfig(
            user_id=user_id,
            provider_type="hetzner_dedicated",
            name=name,
            credentials_encrypted=encrypt_json(creds),
        )
        db.add(row)
        await db.flush()
    else:
        row.credentials_encrypted = encrypt_json(creds)
        db.add(row)
    return row


async def _upsert_server(
    db: AsyncSession,
    provider_config_id: int,
    hetzner: dict,
    rescue_private_key_text: str | None,
) -> Server:
    r = await db.execute(
        select(Server).where(
            Server.provider_config_id == provider_config_id,
            Server.provider_server_id == str(hetzner["server_number"]),
        )
    )
    row = r.scalar_one_or_none()
    ssh_creds: dict[str, Any] | None = None
    if rescue_private_key_text:
        ssh_creds = {"private_key": rescue_private_key_text}
    if row is None:
        row = Server(
            provider_config_id=provider_config_id,
            provider_server_id=str(hetzner["server_number"]),
            name=f"hetzner-{hetzner['server_number']}",
            ip_address=hetzner.get("server_ip"),
            os_type="windows",
            ssh_username="Administrator",
            ssh_port=22,
            ssh_key_encrypted=encrypt_json(ssh_creds) if ssh_creds else None,
            check_ports=[3389, 22],
            extra={},
        )
        db.add(row)
        await db.flush()
    else:
        row.ip_address = hetzner.get("server_ip") or row.ip_address
        if ssh_creds is not None:
            row.ssh_key_encrypted = encrypt_json(ssh_creds)
        db.add(row)
    return row


async def _upsert_storage(
    db: AsyncSession, server_id: int, sb: dict, private_key_text: str | None
) -> StorageConfig:
    r = await db.execute(
        select(StorageConfig).where(
            StorageConfig.server_id == server_id,
            StorageConfig.storage_type == "hetzner_storagebox",
        )
    )
    row = r.scalar_one_or_none()
    connection: dict[str, Any] = {
        "host": sb["host"],
        "user": sb["user"],
        "port": int(sb.get("port") or 23),
    }
    if private_key_text:
        connection["private_key"] = private_key_text
    elif sb.get("ssh_key_path"):
        connection["ssh_key_path"] = sb["ssh_key_path"]
    if row is None:
        row = StorageConfig(
            server_id=server_id,
            storage_type="hetzner_storagebox",
            name=f"SB {sb['user']}",
            connection_encrypted=encrypt_json(connection),
            base_path="/backups",
        )
        db.add(row)
        await db.flush()
    else:
        row.connection_encrypted = encrypt_json(connection)
        db.add(row)
    return row


async def _upsert_daily_backup(
    db: AsyncSession, server_id: int, storage_id: int
) -> int:
    name = "legacy-daily"
    r = await db.execute(
        select(BackupConfig).where(
            BackupConfig.server_id == server_id,
            BackupConfig.name == name,
        )
    )
    row = r.scalar_one_or_none()
    destinations = [{
        "kind": "storage",
        "storage_config_id": storage_id,
        "base_path": "backups/daily",
        "aliases": [],
        "date_folder": True,
        "frequency": "daily",
    }]
    if row is None:
        row = BackupConfig(
            server_id=server_id,
            name=name,
            sources=LEGACY_DAILY_SOURCES,
            destinations=destinations,
            schedule="03:00",
            rotation_days=14,
        )
        db.add(row)
        await db.flush()
    else:
        row.sources = LEGACY_DAILY_SOURCES
        row.destinations = destinations
        row.schedule = row.schedule or "03:00"
        db.add(row)
    return row.id


async def _upsert_weekly_backup(
    db: AsyncSession, server_id: int, storage_id: int
) -> int:
    name = "legacy-weekly-iis"
    r = await db.execute(
        select(BackupConfig).where(
            BackupConfig.server_id == server_id,
            BackupConfig.name == name,
        )
    )
    row = r.scalar_one_or_none()
    destinations = [{
        "kind": "storage",
        "storage_config_id": storage_id,
        "base_path": "backups/weekly",
        "aliases": [],
        "date_folder": True,
        "frequency": "weekly",
    }]
    if row is None:
        row = BackupConfig(
            server_id=server_id,
            name=name,
            sources=LEGACY_WEEKLY_SOURCES,
            destinations=destinations,
            schedule="weekly:Sun@04:00",
            rotation_days=28,
        )
        db.add(row)
        await db.flush()
    else:
        row.sources = LEGACY_WEEKLY_SOURCES
        row.destinations = destinations
        row.schedule = row.schedule or "weekly:Sun@04:00"
        db.add(row)
    return row.id
