"""Backup domain schemas — pydantic models for BackupConfig.sources / destinations."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field


class BackupSource(BaseModel):
    """One source to back up from the target server.

    - `dir`: mirror directory via robocopy.
    - `file`: copy single file.
    - `vss_dir`: create VSS shadow copy of the drive, then robocopy from shadow.
      Use for live DB directories (1C, SQL, etc.).
    """

    type: Literal["dir", "file", "vss_dir"] = "dir"
    alias: str                     # UNF, 1c_license, xray_config, ...
    path: str                      # absolute path on the target server
    compress: Literal["none", "zip"] = "none"


class LocalDestination(BaseModel):
    """Write backup to another disk on the same server (cross-backup)."""

    kind: Literal["local"] = "local"
    base_path: str                 # C:\Backups or D:\Backups\from_C
    aliases: list[str] = Field(default_factory=list)  # empty = all sources
    rotation_days: int | None = None                  # None = inherit BackupConfig.rotation_days
    date_folder: bool = False                         # <base>/<yyyy-MM-dd>/<alias>


class StorageDestination(BaseModel):
    """Upload backup to a StorageConfig (Hetzner Storage Box, S3, SFTP)."""

    kind: Literal["storage"] = "storage"
    storage_config_id: int                            # FK → storage_configs.id
    base_path: str = "backups/daily"
    aliases: list[str] = Field(default_factory=list)
    rotation_days: int | None = None
    date_folder: bool = True
    frequency: Literal["daily", "weekly"] = "daily"   # weekly = only on Sunday


BackupDestination = Annotated[
    LocalDestination | StorageDestination,
    Field(discriminator="kind"),
]


class BackupPlan(BaseModel):
    """Full plan — stored as JSON in BackupConfig.sources + destinations."""

    sources: list[BackupSource]
    destinations: list[BackupDestination]
