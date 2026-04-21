"""User repository."""

from sqlalchemy import select

from serverpanel.infrastructure.database.models import User
from serverpanel.infrastructure.database.repositories.base import BaseRepository


class UserRepository(BaseRepository[User]):
    model = User

    async def get_by_email(self, email: str) -> User | None:
        result = await self.session.execute(
            select(User).where(User.email == email)
        )
        return result.scalar_one_or_none()

    async def email_exists(self, email: str) -> bool:
        user = await self.get_by_email(email)
        return user is not None

    async def create_user(
        self,
        email: str,
        password_hash: str,
        display_name: str | None = None,
        role: str = "user",
    ) -> User:
        user = User(
            email=email,
            password_hash=password_hash,
            display_name=display_name or email.split("@")[0],
            role=role,
        )
        return await self.create(user)
