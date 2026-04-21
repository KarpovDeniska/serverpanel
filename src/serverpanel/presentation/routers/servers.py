"""Server management routes — CRUD and actions."""

import asyncio
import socket

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from serverpanel.domain.exceptions import ProviderError
from serverpanel.infrastructure.crypto import decrypt_json, encrypt_json
from serverpanel.infrastructure.database.models import ProviderConfig, Server, User
from serverpanel.infrastructure.database.repositories.servers import (
    ProviderConfigRepository,
    ServerRepository,
)
from serverpanel.infrastructure.providers import create_provider, list_provider_types
from serverpanel.presentation.dependencies import get_current_user, get_db
from serverpanel.presentation.templates import templates

router = APIRouter(prefix="/servers", tags=["servers"])

# ---- Helpers ----

async def _get_server_or_404(
    server_id: int, user: User, db: AsyncSession
) -> Server:
    repo = ServerRepository(db)
    server = await repo.get_by_id_for_user(server_id, user.id)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    return server


def _get_provider_for_server(server: Server):
    """Create a provider instance from server's provider config."""
    credentials = decrypt_json(server.provider_config.credentials_encrypted)
    return create_provider(server.provider_config.provider_type, credentials)


async def _check_port(ip: str, port: int, timeout: float = 3.0) -> bool:
    """Check if a TCP port is open (async)."""
    loop = asyncio.get_event_loop()
    try:
        await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: socket.create_connection((ip, port), timeout=timeout).close(),
            ),
            timeout=timeout + 1,
        )
        return True
    except Exception:
        return False


# ---- Pages ----

@router.get("/", response_class=HTMLResponse)
async def list_servers(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    repo = ServerRepository(db)
    servers = await repo.get_for_user(user.id)
    return templates.TemplateResponse(request, "servers/list.html", {
        "user": user,
        "servers": servers,
    })


@router.get("/add", response_class=HTMLResponse)
async def add_server_page(
    request: Request,
    user: User = Depends(get_current_user),
):
    return templates.TemplateResponse(request, "servers/add.html", {
        "user": user,
        "provider_types": list_provider_types(),
        "error": None,
    })


@router.post("/add")
async def add_server(
    request: Request,
    provider_type: str = Form(...),
    provider_name: str = Form(...),
    credential_user: str = Form(...),
    credential_password: str = Form(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Add provider credentials and auto-discover servers."""
    credentials = {"robot_user": credential_user, "robot_password": credential_password}

    # Test credentials by listing servers
    try:
        provider = create_provider(provider_type, credentials)
        discovered = await provider.list_servers()
        await provider.close()
    except ProviderError as e:
        if "401" in str(e) or "Unauthorized" in str(e) or "credentials" in str(e).lower():
            error_msg = "Неверные credentials. Убедитесь, что используете webservice-логин (начинается с #ws+), а не основной логин Robot."
        else:
            error_msg = f"Ошибка провайдера: {e}"
        return templates.TemplateResponse(request, "servers/add.html", {
            "user": user,
            "provider_types": list_provider_types(),
            "error": error_msg,
        }, status_code=400)
    except Exception as e:
        return templates.TemplateResponse(request, "servers/add.html", {
            "user": user,
            "provider_types": list_provider_types(),
            "error": f"Ошибка подключения: {e}",
        }, status_code=400)

    # Save provider config
    provider_repo = ProviderConfigRepository(db)
    config = ProviderConfig(
        user_id=user.id,
        provider_type=provider_type,
        name=provider_name,
        credentials_encrypted=encrypt_json(credentials),
    )
    await provider_repo.create(config)

    # Save discovered servers
    server_repo = ServerRepository(db)
    for info in discovered:
        server = Server(
            provider_config_id=config.id,
            provider_server_id=info.server_id,
            name=info.name or f"Server {info.server_id}",
            ip_address=info.ip_address,
            os_type=info.os,
            extra=info.metadata,
        )
        await server_repo.create(server)

    return RedirectResponse(url="/servers", status_code=302)


@router.get("/{server_id}", response_class=HTMLResponse)
async def server_detail(
    server_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    server = await _get_server_or_404(server_id, user, db)

    # Get provider capabilities — degrade gracefully if provider is misconfigured.
    capabilities = []
    try:
        provider = _get_provider_for_server(server)
        from serverpanel.domain.enums import Capability
        for cap in Capability:
            if provider.supports(cap):
                capabilities.append(cap.value)
        await provider.close()
    except Exception:
        import logging
        logging.getLogger(__name__).warning(
            "capability probe failed for server %s", server.id, exc_info=True,
        )

    from serverpanel.infrastructure.database.repositories.backups import (
        StorageConfigRepository,
    )
    storages = await StorageConfigRepository(db).list_for_server(server.id)

    return templates.TemplateResponse(request, "servers/detail.html", {
        "user": user,
        "server": server,
        "capabilities": capabilities,
        "storages": storages,
    })


# ---- API / HTMX endpoints ----

@router.get("/{server_id}/status", response_class=HTMLResponse)
async def server_status(
    server_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """HTMX partial — returns server status fragment."""
    server = await _get_server_or_404(server_id, user, db)

    status_data = {
        "online": False,
        "api_status": "unknown",
        "is_rescue": False,
        "ports": {},
    }

    # Check ports
    if server.ip_address:
        check_ports = server.check_ports or [3389, 22, 443]
        port_checks = await asyncio.gather(
            *[_check_port(server.ip_address, p) for p in check_ports],
            return_exceptions=True,
        )
        status_data["ports"] = {
            p: (r is True) for p, r in zip(check_ports, port_checks)
        }
        status_data["online"] = any(r is True for r in port_checks)

    # Check via provider API
    try:
        provider = _get_provider_for_server(server)
        srv_status = await provider.get_server_status(server.provider_server_id)
        status_data["api_status"] = srv_status.status
        status_data["is_rescue"] = srv_status.is_rescue
        await provider.close()
    except Exception as e:
        status_data["api_status"] = f"error: {e}"

    return templates.TemplateResponse(request, "partials/server_status.html", {
        "server": server,
        "status": status_data,
    })


@router.post("/{server_id}/reset", response_class=HTMLResponse)
async def reset_server(
    server_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Reset server via provider API."""
    server = await _get_server_or_404(server_id, user, db)
    form = await request.form()
    reset_type = form.get("reset_type", "sw")

    try:
        provider = _get_provider_for_server(server)
        await provider.reset_server(server.provider_server_id, reset_type)
        await provider.close()
        message = f"Reset ({reset_type}) выполнен"
        success = True
    except ProviderError as e:
        message = f"Ошибка: {e}"
        success = False

    return templates.TemplateResponse(request, "partials/action_result.html", {
        "message": message,
        "success": success,
    })


@router.post("/{server_id}/rescue", response_class=HTMLResponse)
async def toggle_rescue(
    server_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Activate rescue mode via provider API."""
    server = await _get_server_or_404(server_id, user, db)

    try:
        provider = _get_provider_for_server(server)
        rescue = await provider.get_rescue_status(server.provider_server_id)
        if rescue.active:
            await provider.deactivate_rescue(server.provider_server_id)
            message = "Rescue mode отключён"
        else:
            result = await provider.activate_rescue(server.provider_server_id)
            message = f"Rescue mode активирован. Пароль: {result.password}"
        await provider.close()
        success = True
    except ProviderError as e:
        message = f"Ошибка: {e}"
        success = False

    return templates.TemplateResponse(request, "partials/action_result.html", {
        "message": message,
        "success": success,
    })


@router.post("/{server_id}/wol", response_class=HTMLResponse)
async def wake_on_lan(
    server_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Send Wake-on-LAN."""
    server = await _get_server_or_404(server_id, user, db)

    try:
        provider = _get_provider_for_server(server)
        await provider.wake_on_lan(server.provider_server_id)
        await provider.close()
        message = "WoL пакет отправлен"
        success = True
    except ProviderError as e:
        message = f"Ошибка: {e}"
        success = False

    return templates.TemplateResponse(request, "partials/action_result.html", {
        "message": message,
        "success": success,
    })



# ---- Sub-routers ----
from serverpanel.presentation.routers.backups import router as backups_router  # noqa: E402
from serverpanel.presentation.routers.install import router as install_router  # noqa: E402
from serverpanel.presentation.routers.recovery import router as recovery_router  # noqa: E402
from serverpanel.presentation.routers.storages import router as storages_router  # noqa: E402

router.include_router(install_router, prefix="/{server_id}/install")
router.include_router(backups_router, prefix="/{server_id}/backups")
router.include_router(recovery_router, prefix="/{server_id}/recovery")
router.include_router(storages_router, prefix="/{server_id}/storages")
