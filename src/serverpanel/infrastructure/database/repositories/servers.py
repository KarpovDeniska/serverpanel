"""Server and ProviderConfig repositories — always scoped to user_id."""

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from serverpanel.infrastructure.database.models import ProviderConfig, Server
from serverpanel.infrastructure.database.repositories.base import BaseRepository


class ProviderConfigRepository(BaseRepository[ProviderConfig]):
    model = ProviderConfig

    async def get_for_user(self, user_id: int) -> list[ProviderConfig]:
        result = await self.session.execute(
            select(ProviderConfig)
            .where(ProviderConfig.user_id == user_id)
            .order_by(ProviderConfig.name)
        )
        return list(result.scalars().all())

    async def get_by_id_for_user(self, id: int, user_id: int) -> ProviderConfig | None:
        result = await self.session.execute(
            select(ProviderConfig)
            .where(ProviderConfig.id == id, ProviderConfig.user_id == user_id)
        )
        return result.scalar_one_or_none()


class ServerRepository(BaseRepository[Server]):
    model = Server

    async def get_for_user(self, user_id: int) -> list[Server]:
        result = await self.session.execute(
            select(Server)
            .join(ProviderConfig)
            .where(ProviderConfig.user_id == user_id)
            .options(selectinload(Server.provider_config))
            .order_by(Server.name)
        )
        return list(result.scalars().all())

    async def get_by_id_for_user(self, id: int, user_id: int) -> Server | None:
        result = await self.session.execute(
            select(Server)
            .join(ProviderConfig)
            .where(Server.id == id, ProviderConfig.user_id == user_id)
            .options(selectinload(Server.provider_config))
        )
        return result.scalar_one_or_none()

    async def get_by_provider_server_id(
        self, provider_config_id: int, provider_server_id: str
    ) -> Server | None:
        result = await self.session.execute(
            select(Server).where(
                Server.provider_config_id == provider_config_id,
                Server.provider_server_id == provider_server_id,
            )
        )
        return result.scalar_one_or_none()
