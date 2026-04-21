"""Install history repository."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select

from serverpanel.infrastructure.database.models import (
    InstallHistory,
)
from serverpanel.infrastructure.database.repositories.base import BaseRepository


class InstallHistoryRepository(BaseRepository[InstallHistory]):
    model = InstallHistory

    async def get_for_server(self, server_id: int) -> Sequence[InstallHistory]:
        result = await self.session.execute(
            select(InstallHistory)
            .where(InstallHistory.server_id == server_id)
            .order_by(InstallHistory.id.desc())
            .limit(20)
        )
        return result.scalars().all()

    async def get_running_for_server(self, server_id: int) -> InstallHistory | None:
        result = await self.session.execute(
            select(InstallHistory)
            .where(
                InstallHistory.server_id == server_id,
                InstallHistory.status == "running",
            )
        )
        return result.scalars().first()
