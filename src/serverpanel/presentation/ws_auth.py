"""WebSocket authentication + resource ownership checks.

WS endpoints previously accepted any client knowing the `history_id`. Now
they reject connections from unauthenticated or unauthorized users before
the handshake is accepted.
"""

from __future__ import annotations

import logging

from fastapi import WebSocket, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from serverpanel.infrastructure.database.engine import get_session_factory
from serverpanel.infrastructure.database.models import (
    BackupConfig,
    BackupHistory,
    InstallHistory,
    ProviderConfig,
    RecoveryHistory,
    Server,
    User,
)

log = logging.getLogger(__name__)


async def _user_owns_server(db: AsyncSession, user_id: int, server_id: int) -> bool:
    stmt = (
        select(Server.id)
        .join(ProviderConfig, ProviderConfig.id == Server.provider_config_id)
        .where(Server.id == server_id, ProviderConfig.user_id == user_id)
    )
    return (await db.execute(stmt)).scalar_one_or_none() is not None


async def _history_belongs_to_server(
    db: AsyncSession, history, history_cls: type, server_id: int
) -> bool:
    if history_cls in (InstallHistory, RecoveryHistory):
        return history.server_id == server_id
    if history_cls is BackupHistory:
        cfg = await db.get(BackupConfig, history.backup_config_id)
        return cfg is not None and cfg.server_id == server_id
    return False


async def authorize_ws(
    websocket: WebSocket,
    server_id: int,
    history_id: int,
    history_cls: type,
) -> bool:
    """Verify session user owns the server, and history belongs to that server.

    Closes WS with policy-violation code on failure. Returns True if accepted.
    """
    user_id = websocket.session.get("user_id") if "session" in websocket.scope else None
    if not user_id:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return False

    async with get_session_factory()() as db:
        user = await db.get(User, user_id)
        if user is None or not user.is_active:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return False

        if not await _user_owns_server(db, user.id, server_id):
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return False

        history = await db.get(history_cls, history_id)
        if history is None or not await _history_belongs_to_server(
            db, history, history_cls, server_id
        ):
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return False

    return True
