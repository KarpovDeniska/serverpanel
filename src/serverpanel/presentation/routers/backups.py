"""Backup routes — configs CRUD, schedule install/uninstall, manual run, history, WS."""

from __future__ import annotations

import json

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

from serverpanel.application.services.backup_service import BackupService
from serverpanel.domain.backup import BackupPlan
from serverpanel.infrastructure.database.models import (
    BackupConfig,
    BackupHistory,
    Server,
    User,
)
from serverpanel.infrastructure.database.repositories.backups import (
    BackupConfigRepository,
    BackupHistoryRepository,
    StorageConfigRepository,
)
from serverpanel.infrastructure.database.repositories.servers import ServerRepository
from serverpanel.presentation.background import run_supervised
from serverpanel.presentation.dependencies import get_current_user, get_db
from serverpanel.presentation.progress import WsProgressReporter
from serverpanel.presentation.templates import templates
from serverpanel.presentation.websocket import ws_manager
from serverpanel.presentation.ws_auth import authorize_ws

router = APIRouter(tags=["backups"])

# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

async def _server_or_404(server_id: int, user: User, db: AsyncSession) -> Server:
    server = await ServerRepository(db).get_by_id_for_user(server_id, user.id)
    if not server:
        raise HTTPException(404, "Server not found")
    return server


async def _config_or_404(
    config_id: int, server: Server, db: AsyncSession
) -> BackupConfig:
    cfg = await BackupConfigRepository(db).get_with_server(config_id)
    if not cfg or cfg.server_id != server.id:
        raise HTTPException(404, "Backup config not found")
    return cfg


# ---------------------------------------------------------------------------
# List & detail (HTML)
# ---------------------------------------------------------------------------

@router.get("", response_class=HTMLResponse)
async def list_backups(
    server_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    server = await _server_or_404(server_id, user, db)
    configs = await BackupConfigRepository(db).list_for_server(server.id)
    storages = await StorageConfigRepository(db).list_for_server(server.id)

    # Latest BackupHistory per config → {config_id: history_row}. Single
    # query (subquery on MAX(id) grouped by config_id) instead of N+1.
    latest_by_config: dict[int, BackupHistory] = {}
    if configs:
        from sqlalchemy import func, select as _select
        config_ids = [c.id for c in configs]
        latest_ids_subq = (
            _select(func.max(BackupHistory.id))
            .where(BackupHistory.backup_config_id.in_(config_ids))
            .group_by(BackupHistory.backup_config_id)
        )
        rows = (await db.execute(
            _select(BackupHistory).where(BackupHistory.id.in_(latest_ids_subq))
        )).scalars().all()
        latest_by_config = {r.backup_config_id: r for r in rows}

    return templates.TemplateResponse(request, "backups/index.html", {
        "user": user,
        "server": server,
        "configs": configs,
        "storages": storages,
        "latest_by_config": latest_by_config,
    })


@router.get("/new", response_class=HTMLResponse)
async def new_backup_form(
    server_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    server = await _server_or_404(server_id, user, db)
    storages = await StorageConfigRepository(db).list_for_server(server.id)
    return templates.TemplateResponse(request, "backups/edit.html", {
        "user": user,
        "server": server,
        "config": None,
        "storages": storages,
    })


@router.get("/{config_id}", response_class=HTMLResponse)
async def backup_detail(
    server_id: int,
    config_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    server = await _server_or_404(server_id, user, db)
    cfg = await _config_or_404(config_id, server, db)
    history = await BackupHistoryRepository(db).list_for_config(cfg.id)
    return templates.TemplateResponse(request, "backups/detail.html", {
        "user": user,
        "server": server,
        "config": cfg,
        "history": history,
    })


@router.get("/{config_id}/edit", response_class=HTMLResponse)
async def edit_backup_form(
    server_id: int,
    config_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    server = await _server_or_404(server_id, user, db)
    cfg = await _config_or_404(config_id, server, db)
    storages = await StorageConfigRepository(db).list_for_server(server.id)
    return templates.TemplateResponse(request, "backups/edit.html", {
        "user": user,
        "server": server,
        "config": cfg,
        "storages": storages,
    })


# ---------------------------------------------------------------------------
# CRUD (form POSTs)
# ---------------------------------------------------------------------------

def _parse_form_json(value: str, field_name: str):
    try:
        return json.loads(value)
    except json.JSONDecodeError as e:
        raise HTTPException(400, f"{field_name}: invalid JSON — {e}") from e


@router.post("")
async def create_backup(
    server_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    server = await _server_or_404(server_id, user, db)
    form = await request.form()
    name = (form.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name is required")
    sources_raw = form.get("sources") or "[]"
    destinations_raw = form.get("destinations") or "[]"
    sources = _parse_form_json(sources_raw, "sources")
    destinations = _parse_form_json(destinations_raw, "destinations")
    # Validate via pydantic
    try:
        BackupPlan.model_validate({"sources": sources, "destinations": destinations})
    except Exception as e:
        raise HTTPException(400, f"plan validation: {e}") from e

    rotation = int(form.get("rotation_days") or 14)
    schedule = (form.get("schedule") or "").strip() or None

    cfg = BackupConfig(
        server_id=server.id,
        name=name,
        sources=sources,
        destinations=destinations,
        schedule=schedule,
        rotation_days=rotation,
    )
    db.add(cfg)
    await db.flush()
    cfg_id = cfg.id
    return RedirectResponse(f"/servers/{server_id}/backups/{cfg_id}", status_code=302)


@router.post("/{config_id}/edit")
async def update_backup(
    server_id: int,
    config_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    server = await _server_or_404(server_id, user, db)
    cfg = await _config_or_404(config_id, server, db)
    form = await request.form()
    cfg.name = (form.get("name") or cfg.name).strip()
    sources = _parse_form_json(form.get("sources") or "[]", "sources")
    destinations = _parse_form_json(form.get("destinations") or "[]", "destinations")
    try:
        BackupPlan.model_validate({"sources": sources, "destinations": destinations})
    except Exception as e:
        raise HTTPException(400, f"plan validation: {e}") from e
    cfg.sources = sources
    cfg.destinations = destinations
    cfg.rotation_days = int(form.get("rotation_days") or cfg.rotation_days)
    schedule = (form.get("schedule") or "").strip()
    cfg.schedule = schedule or None
    db.add(cfg)
    return RedirectResponse(f"/servers/{server_id}/backups/{cfg.id}", status_code=302)


@router.post("/{config_id}/delete")
async def delete_backup(
    server_id: int,
    config_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    server = await _server_or_404(server_id, user, db)
    cfg = await _config_or_404(config_id, server, db)
    # Best-effort uninstall on server; ignore failures so stale DB rows can still be deleted.
    try:
        await BackupService(db).uninstall_schedule(cfg)
    except Exception:
        import logging
        logging.getLogger(__name__).warning(
            "uninstall_schedule failed for backup config %s, deleting DB row anyway",
            cfg.id, exc_info=True,
        )
    await db.delete(cfg)
    return RedirectResponse(f"/servers/{server_id}/backups", status_code=302)


# ---------------------------------------------------------------------------
# Schedule install / uninstall
# ---------------------------------------------------------------------------

@router.post("/{config_id}/install")
async def install_schedule(
    server_id: int,
    config_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    server = await _server_or_404(server_id, user, db)
    cfg = await _config_or_404(config_id, server, db)
    base = f"/servers/{server_id}/backups/{cfg.id}"
    try:
        await BackupService(db).install_schedule(cfg)
    except Exception as e:
        # Don't blow up the UI with a raw JSON 500; redirect back with a
        # toast so the user sees WHAT failed in the same page they started on.
        msg = str(e).replace("\n", " ").replace("\r", " ")[:500]
        return RedirectResponse(f"{base}?toast=err:{msg}", status_code=302)
    return RedirectResponse(
        f"{base}?toast=ok:Schedule installed on target server", status_code=302
    )


@router.post("/{config_id}/uninstall")
async def uninstall_schedule(
    server_id: int,
    config_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    server = await _server_or_404(server_id, user, db)
    cfg = await _config_or_404(config_id, server, db)
    base = f"/servers/{server_id}/backups/{cfg.id}"
    try:
        await BackupService(db).uninstall_schedule(cfg)
    except Exception as e:
        msg = str(e).replace("\n", " ").replace("\r", " ")[:500]
        return RedirectResponse(f"{base}?toast=err:{msg}", status_code=302)
    return RedirectResponse(f"{base}?toast=ok:Schedule removed from target server", status_code=302)


# ---------------------------------------------------------------------------
# Manual run + progress
# ---------------------------------------------------------------------------

@router.post("/{config_id}/run")
async def run_backup(
    server_id: int,
    config_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    server = await _server_or_404(server_id, user, db)
    cfg = await _config_or_404(config_id, server, db)

    history = BackupHistory(
        backup_config_id=cfg.id,
        status="pending",
        details={"log": [], "destinations": []},
    )
    db.add(history)
    await db.flush()
    history_id = history.id
    await db.commit()  # release row before background task opens its own session

    async def _worker(bg_db, bg_hist):
        bg_cfg = await BackupConfigRepository(bg_db).get_with_server(config_id)
        reporter = WsProgressReporter(f"backup-{bg_hist.id}")
        await BackupService(bg_db, reporter=reporter).run(bg_cfg, bg_hist)

    run_supervised(BackupHistory, history_id, _worker, label="backup")
    return RedirectResponse(
        f"/servers/{server_id}/backups/{cfg.id}/runs/{history_id}",
        status_code=302,
    )


@router.get("/{config_id}/runs/{history_id}", response_class=HTMLResponse)
async def run_progress(
    server_id: int,
    config_id: int,
    history_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    server = await _server_or_404(server_id, user, db)
    cfg = await _config_or_404(config_id, server, db)
    history = await db.get(BackupHistory, history_id)
    if not history or history.backup_config_id != cfg.id:
        raise HTTPException(404, "Run not found")
    return templates.TemplateResponse(request, "backups/run.html", {
        "user": user,
        "server": server,
        "config": cfg,
        "history": history,
    })


@router.websocket("/ws/{history_id}")
async def backup_ws(server_id: int, history_id: int, websocket: WebSocket):
    if not await authorize_ws(websocket, server_id, history_id, BackupHistory):
        return
    room = f"backup-{history_id}"
    await ws_manager.connect(websocket, room)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket, room)
