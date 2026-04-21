"""Recovery routes — three scenarios (c_drive / d_drive / both), progress + WS."""

from __future__ import annotations

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from serverpanel.application.services.recovery_service import RecoveryService
from serverpanel.infrastructure.database.models import (
    RecoveryHistory,
    Server,
    StorageConfig,
    User,
)
from serverpanel.infrastructure.database.repositories.backups import (
    StorageConfigRepository,
)
from serverpanel.infrastructure.database.repositories.recovery import (
    RecoveryHistoryRepository,
)
from serverpanel.infrastructure.database.repositories.servers import ServerRepository
from serverpanel.presentation.background import run_supervised
from serverpanel.presentation.dependencies import get_current_user, get_db
from serverpanel.presentation.progress import WsProgressReporter
from serverpanel.presentation.templates import templates
from serverpanel.presentation.websocket import ws_manager
from serverpanel.presentation.ws_auth import authorize_ws

router = APIRouter(tags=["recovery"])
ALLOWED_SCENARIOS = ("c_drive", "d_drive", "both")


async def _server_or_404(server_id: int, user: User, db: AsyncSession) -> Server:
    server = await ServerRepository(db).get_by_id_for_user(server_id, user.id)
    if not server:
        raise HTTPException(404, "Server not found")
    return server


@router.get("", response_class=HTMLResponse)
async def recovery_index(
    server_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    server = await _server_or_404(server_id, user, db)
    history = await RecoveryHistoryRepository(db).list_for_server(server.id)
    return templates.TemplateResponse(request, "recovery/index.html", {
        "user": user,
        "server": server,
        "history": history,
    })


@router.get("/new", response_class=HTMLResponse)
async def recovery_wizard(
    server_id: int,
    request: Request,
    scenario: str = "c_drive",
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if scenario not in ALLOWED_SCENARIOS:
        raise HTTPException(400, f"Unknown scenario: {scenario}")
    server = await _server_or_404(server_id, user, db)
    storages = await StorageConfigRepository(db).list_for_server(server.id)
    if not storages:
        raise HTTPException(400, "Server has no StorageConfig — add a Storage Box first.")
    return templates.TemplateResponse(request, "recovery/wizard.html", {
        "user": user,
        "server": server,
        "scenario": scenario,
        "storages": storages,
    })


@router.post("/new")
async def recovery_start(
    server_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    server = await _server_or_404(server_id, user, db)

    running = await RecoveryHistoryRepository(db).get_running_for_server(server.id)
    if running:
        raise HTTPException(400, "Recovery already running for this server")

    form = await request.form()
    scenario = (form.get("scenario") or "").strip()
    if scenario not in ALLOWED_SCENARIOS:
        raise HTTPException(400, f"Unknown scenario: {scenario}")

    storage_id = form.get("storage_config_id")
    if not storage_id:
        raise HTTPException(400, "storage_config_id is required")
    storage = await StorageConfigRepository(db).get_by_id_for_user(
        int(storage_id), user.id
    )
    if not storage or storage.server_id != server.id:
        raise HTTPException(400, "StorageConfig not found for this server")

    software_flags = [
        k.removeprefix("sw_") for k in form.keys()
        if k.startswith("sw_") and form.get(k) == "on"
    ]
    software = dict.fromkeys(software_flags, True)

    cfg = {
        "storage_config_id": storage.id,
        "software": software,
        "hostname": (form.get("hostname") or "").strip() or None,
        "admin_password": (form.get("admin_password") or "").strip() or None,
        "product_key": (form.get("product_key") or "").strip() or None,
        "daily_folder": (form.get("daily_folder") or "latest").strip() or "latest",
        "iso_remote_path": (form.get("iso_remote_path") or "").strip()
            or "/backups/software/windows_server_2022.iso",
        "bcd_remote_path": (form.get("bcd_remote_path") or "").strip()
            or "/backups/software/BCD",
    }

    history = RecoveryHistory(
        server_id=server.id,
        scenario=scenario,
        status="pending",
        progress=0,
        log=[],
        config=cfg,
    )
    db.add(history)
    await db.flush()
    history_id = history.id
    await db.commit()

    server_db_id = server.id
    storage_db_id = storage.id

    async def _worker(bg_db, hist):
        from sqlalchemy import select as sa_select

        srv = (
            await bg_db.execute(
                sa_select(Server)
                .where(Server.id == server_db_id)
                .options(selectinload(Server.provider_config))
            )
        ).scalar_one()
        storage_row = await bg_db.get(StorageConfig, storage_db_id)
        reporter = WsProgressReporter(f"recovery-{hist.id}")
        await RecoveryService(bg_db, reporter=reporter).run(srv, hist, storage_row)

    run_supervised(RecoveryHistory, history_id, _worker, label="recovery")

    return RedirectResponse(
        f"/servers/{server_id}/recovery/{history_id}", status_code=302,
    )


@router.get("/{history_id}", response_class=HTMLResponse)
async def recovery_progress(
    server_id: int,
    history_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    server = await _server_or_404(server_id, user, db)
    hist = await db.get(RecoveryHistory, history_id)
    if not hist or hist.server_id != server.id:
        raise HTTPException(404, "Recovery run not found")
    return templates.TemplateResponse(request, "recovery/progress.html", {
        "user": user,
        "server": server,
        "history": hist,
    })


@router.websocket("/ws/{history_id}")
async def recovery_ws(server_id: int, history_id: int, websocket: WebSocket):
    if not await authorize_ws(websocket, server_id, history_id, RecoveryHistory):
        return
    room = f"recovery-{history_id}"
    await ws_manager.connect(websocket, room)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket, room)
