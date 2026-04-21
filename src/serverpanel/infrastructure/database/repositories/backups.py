"""Backup repositories."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from serverpanel.infrastructure.database.models import (
    BackupConfig,
    BackupHistory,
    ProviderConfig,
    Server,
    StorageConfig,
)
from serverpanel.infrastructure.database.repositories.base import BaseRepository


class BackupConfigRepository(BaseRepository[BackupConfig]):
    model = BackupConfig

    async def get_with_server(self, id: int) -> BackupConfig | None:
        result = await self.session.execute(
            select(BackupConfig)
            .where(BackupConfig.id == id)
            .options(
                selectinload(BackupConfig.server).selectinload(Server.provider_config)
            )
        )
        return result.scalar_one_or_none()

    async def list_for_server(self, server_id: int) -> list[BackupConfig]:
        result = await self.session.execute(
            select(BackupConfig)
            .where(BackupConfig.server_id == server_id)
            .order_by(BackupConfig.name)
        )
        return list(result.scalars().all())

    async def list_for_user(self, user_id: int) -> list[BackupConfig]:
        result = await self.session.execute(
            select(BackupConfig)
            .join(Server, Server.id == BackupConfig.server_id)
            .join(ProviderConfig, ProviderConfig.id == Server.provider_config_id)
            .where(ProviderConfig.user_id == user_id)
            .order_by(BackupConfig.name)
        )
        return list(result.scalars().all())


class BackupHistoryRepository(BaseRepository[BackupHistory]):
    model = BackupHistory

    async def list_for_config(self, config_id: int, limit: int = 20) -> list[BackupHistory]:
        result = await self.session.execute(
            select(BackupHistory)
            .where(BackupHistory.backup_config_id == config_id)
            .order_by(BackupHistory.id.desc())
            .limit(limit)
        )
        return list(result.scalars().all())


class StorageConfigRepository(BaseRepository[StorageConfig]):
    model = StorageConfig

    async def list_for_server(self, server_id: int) -> list[StorageConfig]:
        result = await self.session.execute(
            select(StorageConfig)
            .where(StorageConfig.server_id == server_id)
            .order_by(StorageConfig.name)
        )
        return list(result.scalars().all())

    async def get_by_id_for_user(
        self, storage_id: int, user_id: int
    ) -> StorageConfig | None:
        """Return the storage config only if the owning server belongs to user_id.

        Prevents IDOR: a user knowing another user's StorageConfig id cannot
        read or use it.
        """
        result = await self.session.execute(
            select(StorageConfig)
            .join(Server, Server.id == StorageConfig.server_id)
            .join(ProviderConfig, ProviderConfig.id == Server.provider_config_id)
            .where(StorageConfig.id == storage_id, ProviderConfig.user_id == user_id)
        )
        return result.scalar_one_or_none()
