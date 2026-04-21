"""Recovery history repository."""

from __future__ import annotations

from sqlalchemy import select

from serverpanel.infrastructure.database.models import RecoveryHistory
from serverpanel.infrastructure.database.repositories.base import BaseRepository


class RecoveryHistoryRepository(BaseRepository[RecoveryHistory]):
    model = RecoveryHistory

    async def list_for_server(self, server_id: int, limit: int = 20) -> list[RecoveryHistory]:
        result = await self.session.execute(
            select(RecoveryHistory)
            .where(RecoveryHistory.server_id == server_id)
            .order_by(RecoveryHistory.id.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def get_running_for_server(self, server_id: int) -> RecoveryHistory | None:
        result = await self.session.execute(
            select(RecoveryHistory)
            .where(
                RecoveryHistory.server_id == server_id,
                RecoveryHistory.status == "running",
            )
        )
        return result.scalars().first()
