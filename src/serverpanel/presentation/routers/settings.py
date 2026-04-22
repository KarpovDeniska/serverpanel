"""Settings routes."""

import io

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from serverpanel.application.services.self_backup_service import (
    SelfBackupError,
    suggested_filename,
    write_self_backup,
)
from serverpanel.infrastructure.database.models import User
from serverpanel.infrastructure.database.repositories.servers import ProviderConfigRepository
from serverpanel.infrastructure.providers import list_provider_types
from serverpanel.presentation.dependencies import get_current_user, get_db
from serverpanel.presentation.templates import templates

router = APIRouter(prefix="/settings", tags=["settings"])

@router.get("/", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    provider_repo = ProviderConfigRepository(db)
    providers = await provider_repo.get_for_user(user.id)
    return templates.TemplateResponse(request, "settings.html", {
        "user": user,
        "providers": providers,
        "provider_types": list_provider_types(),
    })


@router.post("/self-backup")
async def self_backup(user: User = Depends(get_current_user)) -> StreamingResponse:
    buf = io.BytesIO()
    try:
        write_self_backup(buf)
    except SelfBackupError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{suggested_filename()}"'},
    )
