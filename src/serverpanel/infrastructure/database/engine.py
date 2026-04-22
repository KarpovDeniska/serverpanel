"""Async database engine and session factory."""

import datetime
import logging
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from serverpanel.config import get_settings

log = logging.getLogger(__name__)

_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine():
    global _engine
    if _engine is None:
        settings = get_settings()
        # Ensure data directory exists for SQLite
        if settings.database_url.startswith("sqlite"):
            db_path = settings.database_url.split("///")[-1]
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        _engine = create_async_engine(
            settings.database_url,
            echo=settings.debug,
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _session_factory


async def init_db() -> None:
    """Run Alembic migrations programmatically.

    Legacy DBs that already have tables but no `alembic_version` are stamped
    to head on first startup so existing data isn't touched.
    """
    import asyncio

    from alembic.config import Config
    from sqlalchemy import inspect

    from alembic import command

    engine = get_engine()
    async with engine.connect() as conn:
        def _probe(sync_conn):
            insp = inspect(sync_conn)
            return ("users" in insp.get_table_names(),
                    "alembic_version" in insp.get_table_names())
        has_tables, has_alembic = await conn.run_sync(_probe)

    ini_path = Path(__file__).resolve().parents[3].parent / "alembic.ini"
    cfg = Config(str(ini_path))
    cfg.set_main_option("sqlalchemy.url", get_settings().database_url)

    def _run():
        if has_tables and not has_alembic:
            command.stamp(cfg, "head")
        else:
            command.upgrade(cfg, "head")

    await asyncio.to_thread(_run)


async def cleanup_stale_runs() -> None:
    """Mark orphaned 'running' history rows as 'failed' on startup.

    Process restart kills background asyncio tasks. Without this, rows stay
    'running' forever and block new starts (recovery has a guard against
    concurrent runs per server).

    No cutoff: if the process has just started, every row still in 'running'
    belongs to a task that did not survive the restart, regardless of age.
    """
    from sqlalchemy import update

    from serverpanel.infrastructure.database.models import (
        BackupHistory,
        InstallHistory,
        RecoveryHistory,
    )

    async with get_session_factory()() as db:
        for model in (InstallHistory, RecoveryHistory, BackupHistory):
            stmt = (
                update(model)
                .where(model.status == "running")
                .values(
                    status="failed",
                    error_message="Interrupted by process restart",
                    completed_at=datetime.datetime.now(datetime.UTC),
                )
            )
            result = await db.execute(stmt)
            if result.rowcount:
                log.warning(
                    "Marked %d stale '%s' row(s) as failed on startup",
                    result.rowcount,
                    model.__tablename__,
                )
        await db.commit()


async def dispose_db() -> None:
    """Dispose engine on shutdown."""
    global _engine, _session_factory
    if _engine:
        await _engine.dispose()
        _engine = None
        _session_factory = None
