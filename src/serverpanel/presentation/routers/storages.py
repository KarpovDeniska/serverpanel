"""StorageConfig CRUD — nested under servers.

Form-driven creation of Hetzner Storage Box / SFTP / S3 storage configs
that backup and recovery flows consume.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from serverpanel.infrastructure.crypto import encrypt_json
from serverpanel.infrastructure.database.models import Server, StorageConfig, User
from serverpanel.infrastructure.database.repositories.backups import (
    StorageConfigRepository,
)
from serverpanel.infrastructure.database.repositories.servers import ServerRepository
from serverpanel.infrastructure.providers.storage import list_storage_types
from serverpanel.presentation.dependencies import get_current_user, get_db
from serverpanel.presentation.templates import templates

log = logging.getLogger(__name__)

router = APIRouter(tags=["storages"])


async def _server_or_404(server_id: int, user: User, db: AsyncSession) -> Server:
    server = await ServerRepository(db).get_by_id_for_user(server_id, user.id)
    if not server:
        raise HTTPException(404, "Server not found")
    return server


@router.get("/new", response_class=HTMLResponse)
async def new_storage_form(
    server_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    server = await _server_or_404(server_id, user, db)
    return templates.TemplateResponse(request, "storages/edit.html", {
        "user": user,
        "server": server,
        "storage_types": list_storage_types(),
    })


@router.post("/new")
async def create_storage(
    server_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    server = await _server_or_404(server_id, user, db)
    form = await request.form()

    storage_type = (form.get("storage_type") or "").strip()
    name = (form.get("name") or "").strip()
    host = (form.get("host") or "").strip()
    username = (form.get("username") or "").strip()
    port_raw = (form.get("port") or "").strip()
    password = form.get("password") or ""
    private_key = form.get("private_key") or ""
    base_path = (form.get("base_path") or "/").strip() or "/"

    if not all([storage_type, name, host, username]):
        raise HTTPException(400, "storage_type, name, host, username обязательны")
    try:
        port = int(port_raw) if port_raw else (23 if storage_type == "hetzner_storagebox" else 22)
    except ValueError as e:
        raise HTTPException(400, f"port: {e}") from e

    connection = {
        "host": host,
        "user": username,
        "port": port,
    }
    if password:
        connection["password"] = password
    if private_key:
        connection["private_key"] = private_key

    cfg = StorageConfig(
        server_id=server.id,
        storage_type=storage_type,
        name=name,
        connection_encrypted=encrypt_json(connection),
        base_path=base_path,
    )
    db.add(cfg)
    await db.flush()
    return RedirectResponse(f"/servers/{server_id}", status_code=302)


@router.post("/{storage_id}/delete")
async def delete_storage(
    server_id: int,
    storage_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    server = await _server_or_404(server_id, user, db)
    storage = await StorageConfigRepository(db).get_by_id_for_user(storage_id, user.id)
    if not storage or storage.server_id != server.id:
        raise HTTPException(404, "Storage not found")
    await db.delete(storage)
    return RedirectResponse(f"/servers/{server_id}", status_code=302)
