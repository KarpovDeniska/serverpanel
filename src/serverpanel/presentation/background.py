"""Supervised background task runner.

Fire-and-forget `asyncio.create_task(...)` loses exceptions silently and
leaves history rows stuck in 'running'. This wrapper:

- Opens a fresh DB session (request session closes when HTTP response returns).
- Re-loads the history row inside the new session.
- On any exception, marks history as 'failed' with the error message so UI
  does not show an eternally 'running' task.
- Logs the exception with traceback.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from serverpanel.infrastructure.database.engine import get_session_factory

log = logging.getLogger(__name__)


def run_supervised[HistoryT](
    history_cls: type[HistoryT],
    history_id: int,
    worker: Callable[[AsyncSession, HistoryT], Awaitable[None]],
    *,
    label: str,
) -> asyncio.Task:
    """Schedule `worker(db, history)` with guaranteed failure reporting.

    `worker` is responsible for its own success path (status='success'). On
    exception, this wrapper overwrites status to 'failed'.
    """

    async def _runner() -> None:
        factory = get_session_factory()
        async with factory() as db:
            history = await db.get(history_cls, history_id)
            if history is None:
                log.error("%s: history %s not found in background task", label, history_id)
                return
            try:
                await worker(db, history)
            except Exception as e:
                log.exception("%s: background task failed", label)
                try:
                    fresh = await db.get(history_cls, history_id)
                    if fresh is not None and getattr(fresh, "status", None) != "success":
                        fresh.status = "failed"
                        if hasattr(fresh, "error_message"):
                            fresh.error_message = f"{type(e).__name__}: {e}"
                        if hasattr(fresh, "completed_at"):
                            fresh.completed_at = datetime.datetime.now(datetime.UTC)
                        db.add(fresh)
                except Exception:
                    log.exception("%s: failed to record failure for history %s", label, history_id)
            finally:
                try:
                    await db.commit()
                except Exception:
                    log.exception("%s: final commit failed", label)

    return asyncio.create_task(_runner(), name=f"{label}-{history_id}")
