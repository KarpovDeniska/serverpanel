"""OS Install routes — wizard, execution, progress."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from serverpanel.application.catalogs.os_images import get_image_by_id, get_images_for_provider
from serverpanel.application.catalogs.server_templates import get_templates
from serverpanel.application.catalogs.software import CATEGORY_LABELS, get_software_grouped
from serverpanel.application.services.install_service import InstallService
from serverpanel.infrastructure.crypto import decrypt_json
from serverpanel.infrastructure.database.models import InstallHistory, Server, User
from serverpanel.infrastructure.database.repositories.install import InstallHistoryRepository
from serverpanel.infrastructure.database.repositories.servers import ServerRepository
from serverpanel.infrastructure.providers import create_provider
from serverpanel.presentation.background import run_supervised
from serverpanel.presentation.dependencies import get_current_user, get_db
from serverpanel.presentation.progress import WsProgressReporter
from serverpanel.presentation.templates import templates
from serverpanel.presentation.websocket import ws_manager
from serverpanel.presentation.ws_auth import authorize_ws

router = APIRouter(tags=["install"])

async def _get_server_or_404(server_id: int, user: User, db: AsyncSession) -> Server:
    repo = ServerRepository(db)
    server = await repo.get_by_id_for_user(server_id, user.id)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    return server


@router.get("", response_class=HTMLResponse)
async def install_index(
    server_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    server = await _get_server_or_404(server_id, user, db)
    repo = InstallHistoryRepository(db)
    history = await repo.get_for_server(server.id)
    return templates.TemplateResponse(request, "install/index.html", {
        "user": user,
        "server": server,
        "history": history,
    })


@router.get("/new", response_class=HTMLResponse)
async def install_wizard(
    server_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    server = await _get_server_or_404(server_id, user, db)
    provider_type = server.provider_config.provider_type

    # Get available OS images for this provider
    os_images = get_images_for_provider(provider_type)

    # Get software catalog grouped by category
    software = get_software_grouped()

    # Get SSH keys from provider — soft failure, log and continue
    ssh_keys = []
    try:
        credentials = decrypt_json(server.provider_config.credentials_encrypted)
        provider = create_provider(provider_type, credentials)
        ssh_keys = await provider.list_ssh_keys()
        await provider.close()
    except Exception:
        import logging
        logging.getLogger(__name__).warning(
            "list_ssh_keys failed for provider %s (server %s)",
            provider_type, server.id, exc_info=True,
        )

    return templates.TemplateResponse(request, "install/wizard.html", {
        "user": user,
        "server": server,
        "os_images": os_images,
        "software": software,
        "category_labels": CATEGORY_LABELS,
        "ssh_keys": ssh_keys,
        "server_templates": get_templates(),
    })


@router.post("/new")
async def start_install(
    server_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    server = await _get_server_or_404(server_id, user, db)

    # Check no running install
    repo = InstallHistoryRepository(db)
    running = await repo.get_running_for_server(server.id)
    if running:
        raise HTTPException(400, "Установка уже выполняется")

    # Parse form
    form = await request.form()
    os_image_id = form.get("os_image_id")
    if not os_image_id:
        raise HTTPException(400, "Выберите ОС")

    os_image = get_image_by_id(os_image_id)
    if not os_image:
        raise HTTPException(400, "Неизвестный образ ОС")

    software_ids = form.getlist("software_ids")
    hostname = form.get("hostname", "server").strip() or "server"
    custom_ssh_key = (form.get("custom_ssh_key") or "").strip()
    enable_firewall = form.get("enable_firewall") == "on"
    open_ports_str = form.get("open_ports", "22").strip()
    open_ports = [int(p.strip()) for p in open_ports_str.split(",") if p.strip().isdigit()]
    if 22 not in open_ports:
        open_ports.insert(0, 22)

    # Collect SSH keys
    ssh_keys = []
    if custom_ssh_key:
        ssh_keys.append(custom_ssh_key)
    # TODO: resolve fingerprints to actual key data from provider if needed

    config = {
        "os_image_id": os_image.id,
        "os_image_name": os_image.name,
        "software_ids": software_ids,
        "hostname": hostname,
        "ssh_keys": ssh_keys,
        "enable_firewall": enable_firewall,
        "open_ports": open_ports,
    }

    # Create history
    history = InstallHistory(
        server_id=server.id,
        status="pending",
        progress=0,
        log=[],
        config=config,
    )
    await repo.create(history)
    history_id = history.id
    server_db_id = server.id

    async def _worker(bg_db, bg_history):
        from sqlalchemy import select as sa_select
        result = await bg_db.execute(
            sa_select(Server)
            .where(Server.id == server_db_id)
            .options(selectinload(Server.provider_config))
        )
        bg_server = result.scalar_one()
        reporter = WsProgressReporter(f"install-{bg_history.id}")
        await InstallService(bg_db, reporter=reporter).run(bg_server, bg_history)

    run_supervised(InstallHistory, history_id, _worker, label="install")

    return RedirectResponse(
        url=f"/servers/{server_id}/install/progress/{history_id}",
        status_code=302,
    )


@router.get("/progress/{history_id}", response_class=HTMLResponse)
async def install_progress(
    server_id: int,
    history_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    server = await _get_server_or_404(server_id, user, db)
    repo = InstallHistoryRepository(db)
    history = await repo.get_by_id(history_id)
    if not history or history.server_id != server.id:
        raise HTTPException(404, "Not found")
    return templates.TemplateResponse(request, "install/progress.html", {
        "user": user,
        "server": server,
        "history": history,
    })


@router.websocket("/ws/{history_id}")
async def install_ws(server_id: int, history_id: int, websocket: WebSocket):
    if not await authorize_ws(websocket, server_id, history_id, InstallHistory):
        return
    room = f"install-{history_id}"
    await ws_manager.connect(websocket, room)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket, room)
