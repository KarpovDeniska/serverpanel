"""Settings routes."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

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
