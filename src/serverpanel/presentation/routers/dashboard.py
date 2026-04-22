"""Dashboard — overview of all servers."""

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from serverpanel.application.services.backup_service import BackupService
from serverpanel.infrastructure.database.models import User
from serverpanel.infrastructure.database.repositories.servers import ServerRepository
from serverpanel.presentation.dependencies import get_current_user, get_db
from serverpanel.presentation.routers.servers import _compute_backup_summary
from serverpanel.presentation.templates import templates

log = logging.getLogger(__name__)

router = APIRouter(tags=["dashboard"])


@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    repo = ServerRepository(db)
    servers = await repo.get_for_user(user.id)

    # Per-server backup summary so the dashboard cards show an at-a-glance
    # status (green/yellow/red) without needing to open each server page.
    backup_summaries: dict[int, dict] = {}
    for s in servers:
        try:
            backup_summaries[s.id] = await _compute_backup_summary(db, s.id)
        except Exception:
            log.exception("dashboard backup summary failed for server %s", s.id)
            backup_summaries[s.id] = {"configs_count": 0, "counts": {}, "latest": None}

    return templates.TemplateResponse(request, "dashboard.html", {
        "user": user,
        "servers": servers,
        "backup_summaries": backup_summaries,
    })


@router.post("/dashboard/backups/sync-all")
async def sync_all_backups(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Walk every server the user owns and pull scheduled-run reports
    from it into BackupHistory. Convenience equivalent of clicking Sync
    on every backup page — covers all servers in one go."""
    servers = await ServerRepository(db).get_for_user(user.id)
    total_created = 0
    errors: list[str] = []
    for s in servers:
        if not s.ssh_key_encrypted:
            continue
        try:
            created = await BackupService(db).sync_reports_from_server(s)
            total_created += created
        except Exception as e:
            errors.append(f"{s.name}: {e}")
            log.exception("sync-all failed for server %s", s.id)

    if errors:
        msg = f"Imported {total_created}, errors: " + "; ".join(errors)[:400]
        return RedirectResponse(f"/?toast=err:{msg}", status_code=302)
    return RedirectResponse(
        f"/?toast=ok:Imported {total_created} new run(s) across {len(servers)} server(s)",
        status_code=302,
    )
