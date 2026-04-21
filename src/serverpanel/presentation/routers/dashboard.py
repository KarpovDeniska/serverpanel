"""Dashboard — overview of all servers."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from serverpanel.infrastructure.database.models import User
from serverpanel.infrastructure.database.repositories.servers import ServerRepository
from serverpanel.presentation.dependencies import get_current_user, get_db
from serverpanel.presentation.templates import templates

router = APIRouter(tags=["dashboard"])

@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    repo = ServerRepository(db)
    servers = await repo.get_for_user(user.id)
    return templates.TemplateResponse(request, "dashboard.html", {
        "user": user,
        "servers": servers,
    })
