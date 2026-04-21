"""Generic async CRUD repository."""

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from serverpanel.infrastructure.database.models import Base


class BaseRepository[T: Base]:
    """Base repository with common CRUD operations.

    All queries that touch user-owned data must filter by user_id.
    """

    model: type[T]

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_id(self, id: int) -> T | None:
        return await self.session.get(self.model, id)

    async def get_all(self) -> Sequence[T]:
        result = await self.session.execute(select(self.model))
        return result.scalars().all()

    async def create(self, obj: T) -> T:
        self.session.add(obj)
        await self.session.flush()
        await self.session.refresh(obj)
        return obj

    async def update(self, obj: T) -> T:
        await self.session.flush()
        await self.session.refresh(obj)
        return obj

    async def delete(self, obj: T) -> None:
        await self.session.delete(obj)
        await self.session.flush()

    async def commit(self) -> None:
        await self.session.commit()
