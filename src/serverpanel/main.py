"""FastAPI application factory."""

from contextlib import asynccontextmanager
from importlib.resources import files as _pkg_files

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from serverpanel.config import get_settings
from serverpanel.infrastructure.database.engine import (
    cleanup_stale_runs,
    dispose_db,
    init_db,
)
from serverpanel.presentation.csrf import CSRFMiddleware
from serverpanel.presentation.middleware import AuthRedirectMiddleware

_PKG_ROOT = _pkg_files("serverpanel")
STATIC_DIR = str(_PKG_ROOT / "static")
TEMPLATES_DIR = str(_PKG_ROOT / "templates")


async def _backup_sync_loop(interval_seconds: int) -> None:
    """Every `interval_seconds`, walk every Server with a configured
    ProviderConfig + SSH creds and pull scheduled-run reports from its
    `C:\\ProgramData\\serverpanel\\configs\\*\\last_report.json` into
    BackupHistory. Lets the UI reflect nightly Task Scheduler runs even
    though serverpanel itself didn't initiate them.
    """
    import asyncio
    import logging

    from sqlalchemy import select as _select
    from sqlalchemy.orm import selectinload

    from serverpanel.application.services.backup_service import BackupService
    from serverpanel.infrastructure.database.engine import get_session_factory
    from serverpanel.infrastructure.database.models import Server

    log = logging.getLogger("serverpanel.sync")

    while True:
        try:
            factory = get_session_factory()
            async with factory() as db:
                servers = (await db.execute(
                    _select(Server).options(selectinload(Server.provider_config))
                )).scalars().all()
                for srv in servers:
                    if not srv.ssh_key_encrypted:
                        continue
                    try:
                        svc = BackupService(db)
                        created = await svc.sync_reports_from_server(srv)
                        if created:
                            log.info("backup sync: server=%s created=%d", srv.name, created)
                    except Exception:
                        log.exception("backup sync failed for server %s", srv.id)
        except Exception:
            log.exception("backup sync loop iteration crashed")
        await asyncio.sleep(interval_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown."""
    import asyncio

    # Import providers to auto-register
    import serverpanel.infrastructure.providers.hetzner  # noqa: F401
    import serverpanel.infrastructure.providers.storage.hetzner_storagebox  # noqa: F401

    await init_db()
    await cleanup_stale_runs()

    cfg = get_settings()
    sync_task: asyncio.Task | None = None
    if cfg.backup_sync_interval_seconds > 0:
        sync_task = asyncio.create_task(
            _backup_sync_loop(cfg.backup_sync_interval_seconds),
            name="backup-sync-loop",
        )

    yield

    if sync_task is not None:
        sync_task.cancel()
        try:
            await sync_task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            import logging as _logging

            _logging.getLogger("serverpanel.sync").warning("sync loop crashed on shutdown: %s", e)
    await dispose_db()


def create_app() -> FastAPI:
    cfg = get_settings()

    app = FastAPI(
        title="ServerPanel",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/api/docs" if cfg.debug else None,
        redoc_url=None,
    )

    # Middleware (order matters: last added = first executed).
    # Request flow: Session → CSRF → AuthRedirect → handler.
    app.add_middleware(AuthRedirectMiddleware)
    app.add_middleware(CSRFMiddleware)
    app.add_middleware(
        SessionMiddleware,
        secret_key=cfg.secret_key,
        max_age=cfg.session_lifetime_hours * 3600,
        https_only=cfg.session_cookie_secure,
        same_site=cfg.session_cookie_samesite,
    )

    # Static files
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # Health endpoint — no auth, checks DB connectivity
    @app.get("/health", include_in_schema=False)
    async def health():
        from sqlalchemy import text

        from serverpanel.infrastructure.database.engine import get_engine

        try:
            async with get_engine().connect() as conn:
                await conn.execute(text("SELECT 1"))
            return {"status": "ok"}
        except Exception as e:
            return JSONResponse({"status": "error", "detail": str(e)}, status_code=503)

    # Routers
    from serverpanel.presentation.routers import auth, dashboard, servers, settings

    app.include_router(auth.router)
    app.include_router(dashboard.router)
    app.include_router(servers.router)
    app.include_router(settings.router)

    return app


# Entry point for uvicorn
app = create_app()


def cli():
    """CLI entry point.

    Subcommands:
      serverpanel                                — run server (default)
      serverpanel serve                          — run server (explicit)
      serverpanel seed ...                       — populate DB with user + server + storage
      serverpanel import-hetzner-recovery <yaml> — port a legacy hetzner-recovery config.yaml
    """
    import argparse
    import asyncio
    import sys
    from pathlib import Path

    parser = argparse.ArgumentParser(prog="serverpanel")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("serve", help="Run the web server")

    seed_cmd = sub.add_parser(
        "seed",
        help="Create/update User + ProviderConfig + Server + StorageConfig in one shot",
    )
    seed_cmd.add_argument("--admin-email", required=True)
    seed_cmd.add_argument("--admin-password", help="Required only if the user doesn't exist yet")
    seed_cmd.add_argument("--server-ip", required=True)
    seed_cmd.add_argument("--server-ssh-username", default="Administrator")
    seed_cmd.add_argument("--server-ssh-key", help="Path to Windows-server SSH private key")
    seed_cmd.add_argument("--server-ssh-password", help="Alternative to SSH key")
    seed_cmd.add_argument("--sb-host", required=True)
    seed_cmd.add_argument("--sb-user", required=True)
    seed_cmd.add_argument("--sb-port", type=int, default=23)
    seed_cmd.add_argument("--sb-ssh-key", help="Path to Storage Box SSH private key")
    seed_cmd.add_argument("--sb-password", help="Alternative to SSH key")
    seed_cmd.add_argument("--robot-user", help="Hetzner Robot webservice login (starts with #ws+)")
    seed_cmd.add_argument("--robot-password", help="Hetzner Robot webservice password")
    seed_cmd.add_argument("--provider-name", default="hetzner-dedicated")
    seed_cmd.add_argument("--server-name", default="hetzner-windows")
    seed_cmd.add_argument("--storage-name", default="hetzner-storagebox")

    imp = sub.add_parser(
        "import-hetzner-recovery",
        help="Import a legacy hetzner-recovery config.yaml",
    )
    imp.add_argument("yaml_path", help="Path to the legacy config.yaml")
    imp.add_argument("--user-email", required=True, help="Owner user email (must already exist)")
    imp.add_argument("--sb-private-key", help="Path to Storage Box SSH private key (contents will be encrypted and stored)")
    imp.add_argument("--server-private-key", help="Path to target Windows server SSH private key")

    legacy = sub.add_parser(
        "seed-legacy-backups",
        help="Create the 'legacy-daily' + 'legacy-weekly-iis' BackupConfigs for an existing server+storage",
    )
    legacy.add_argument("--server-name", default="hetzner-windows")
    legacy.add_argument("--storage-name",
                        help="StorageConfig name (or --storage-id). If there's only one for the server — picked automatically.")
    legacy.add_argument("--storage-id", type=int, help="Exact StorageConfig.id to use")

    ek = sub.add_parser(
        "export-keys",
        help="Materialize SSH private keys from the DB to disk files "
             "(server keys + Storage Box keys). The DB is Fernet-encrypted "
             "with ENCRYPTION_KEY; losing .env alone would lock you out of "
             "restore even if serverpanel.db survives. Keep a copy outside.",
    )
    ek.add_argument("--out", default="~/.ssh/serverpanel-seed",
                    help="Target directory (created if missing). Default: ~/.ssh/serverpanel-seed")

    rc = sub.add_parser(
        "set-robot-creds",
        help="Attach Hetzner Robot webservice credentials (login starts with '#ws+') "
             "to the ProviderConfig of a given server. Unlocks the Recovery flow "
             "and API status panel in the UI.",
    )
    rc.add_argument("--server-name", default="hetzner-windows")
    rc.add_argument("--robot-user", required=True, help="e.g. '#ws+y9zKyNA7'")
    rc.add_argument("--robot-password", required=True)

    sr = sub.add_parser(
        "sync-from-robot",
        help="Refresh Server.provider_server_id from Hetzner Robot API — fix the "
             "'requires numeric server id, got <IP>' UI error that happens when a "
             "server was seeded without Robot creds and its id was set to the IP.",
    )
    sr.add_argument("--server-name", default="hetzner-windows")

    args = parser.parse_args()

    if args.command == "import-hetzner-recovery":
        from serverpanel.application.importers.hetzner_recovery import import_legacy_config
        from serverpanel.infrastructure.database.engine import (
            dispose_db as _dispose,
        )
        from serverpanel.infrastructure.database.engine import (
            get_session_factory,
        )
        from serverpanel.infrastructure.database.engine import (
            init_db as _init,
        )

        sb_key_text = Path(args.sb_private_key).read_text() if args.sb_private_key else None
        srv_key_text = Path(args.server_private_key).read_text() if args.server_private_key else None

        async def _run():
            await _init()
            async with get_session_factory()() as db:
                result = await import_legacy_config(
                    db,
                    Path(args.yaml_path),
                    user_email=args.user_email,
                    private_key_text=sb_key_text,
                    rescue_private_key_text=srv_key_text,
                )
                print("Imported:")
                for k, v in result.items():
                    print(f"  {k}: {v}")
            await _dispose()

        asyncio.run(_run())
        sys.exit(0)

    if args.command == "seed":
        from serverpanel.application.importers.seed import seed as _seed
        from serverpanel.infrastructure.database.engine import (
            dispose_db as _dispose,
        )
        from serverpanel.infrastructure.database.engine import (
            get_session_factory,
        )
        from serverpanel.infrastructure.database.engine import (
            init_db as _init,
        )

        async def _run():
            await _init()
            async with get_session_factory()() as db:
                result = await _seed(
                    db,
                    admin_email=args.admin_email,
                    admin_password=args.admin_password,
                    server_ip=args.server_ip,
                    server_ssh_username=args.server_ssh_username,
                    server_ssh_key_path=args.server_ssh_key,
                    server_ssh_password=args.server_ssh_password,
                    sb_host=args.sb_host,
                    sb_user=args.sb_user,
                    sb_port=args.sb_port,
                    sb_ssh_key_path=args.sb_ssh_key,
                    sb_password=args.sb_password,
                    robot_user=args.robot_user,
                    robot_password=args.robot_password,
                    provider_name=args.provider_name,
                    server_name=args.server_name,
                    storage_name=args.storage_name,
                )
                print("Seeded:")
                for k, v in result.items():
                    print(f"  {k}: {v}")
            await _dispose()

        asyncio.run(_run())
        sys.exit(0)

    if args.command == "seed-legacy-backups":
        from sqlalchemy import select

        from serverpanel.application.importers.hetzner_recovery import (
            _upsert_daily_backup,
            _upsert_monthly_backup,
            _upsert_weekly_backup,
        )
        from serverpanel.infrastructure.database.engine import (
            dispose_db as _dispose,
        )
        from serverpanel.infrastructure.database.engine import (
            get_session_factory,
        )
        from serverpanel.infrastructure.database.engine import (
            init_db as _init,
        )
        from serverpanel.infrastructure.database.models import Server, StorageConfig

        async def _run():
            await _init()
            async with get_session_factory()() as db:
                srv = (await db.execute(
                    select(Server).where(Server.name == args.server_name)
                )).scalar_one_or_none()
                if srv is None:
                    sys.stderr.write(f"Server name={args.server_name!r} not found\n")
                    sys.exit(2)

                storages = (await db.execute(
                    select(StorageConfig).where(StorageConfig.server_id == srv.id)
                )).scalars().all()
                if not storages:
                    sys.stderr.write(f"No StorageConfig attached to server {srv.id}\n")
                    sys.exit(2)

                if args.storage_id:
                    stor = next((s for s in storages if s.id == args.storage_id), None)
                elif args.storage_name:
                    stor = next((s for s in storages if s.name == args.storage_name), None)
                elif len(storages) == 1:
                    stor = storages[0]
                else:
                    sys.stderr.write(
                        f"Server has {len(storages)} storages; pass --storage-id or --storage-name:\n"
                    )
                    for s in storages:
                        sys.stderr.write(f"  id={s.id} name={s.name!r} type={s.storage_type}\n")
                    sys.exit(2)

                if stor is None:
                    sys.stderr.write("Storage matching the given filter not found.\n")
                    sys.exit(2)

                daily_id = await _upsert_daily_backup(db, srv.id, stor.id)
                weekly_id = await _upsert_weekly_backup(db, srv.id, stor.id)
                monthly_id = await _upsert_monthly_backup(db, srv.id, stor.id)
                await db.commit()
                print(f"server_id={srv.id} storage_id={stor.id}")
                print(f"backup_daily_id={daily_id} (name=legacy-daily)")
                print(f"backup_weekly_id={weekly_id} (name=legacy-weekly-iis)")
                print(f"backup_monthly_id={monthly_id} (name=legacy-monthly)")
            await _dispose()

        asyncio.run(_run())
        sys.exit(0)

    if args.command == "export-keys":
        import os
        import stat

        from sqlalchemy import select

        from serverpanel.infrastructure.crypto import decrypt_json
        from serverpanel.infrastructure.database.engine import (
            dispose_db as _dispose,
        )
        from serverpanel.infrastructure.database.engine import (
            get_session_factory,
        )
        from serverpanel.infrastructure.database.engine import (
            init_db as _init,
        )
        from serverpanel.infrastructure.database.models import Server, StorageConfig

        out_dir = Path(os.path.expanduser(args.out)).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)

        async def _run():
            await _init()
            written: list[tuple[str, str]] = []  # (label, path)
            async with get_session_factory()() as db:
                for srv in (await db.execute(select(Server))).scalars().all():
                    if not srv.ssh_key_encrypted:
                        continue
                    try:
                        creds = decrypt_json(srv.ssh_key_encrypted)
                    except Exception as e:
                        sys.stderr.write(f"WARN: cannot decrypt server {srv.name!r}: {e}\n")
                        continue
                    pk = creds.get("private_key")
                    if not pk:
                        continue
                    p = out_dir / f"{srv.name}_id_ed25519"
                    p.write_text(pk, encoding="utf-8")
                    try:
                        p.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600 — ssh refuses world-readable
                    except OSError as e:
                        # Windows NTFS can reject POSIX chmod bits; ssh on
                        # Windows doesn't enforce 0600 anyway.
                        sys.stderr.write(f"WARN: chmod 0600 on {p} failed: {e}\n")
                    written.append((f"server {srv.name}", str(p)))

                for stor in (await db.execute(select(StorageConfig))).scalars().all():
                    if not stor.connection_encrypted:
                        continue
                    try:
                        conn = decrypt_json(stor.connection_encrypted)
                    except Exception as e:
                        sys.stderr.write(f"WARN: cannot decrypt storage {stor.name!r}: {e}\n")
                        continue
                    pk = conn.get("private_key")
                    if not pk:
                        continue
                    p = out_dir / f"{stor.name}_id_ed25519"
                    p.write_text(pk, encoding="utf-8")
                    try:
                        p.chmod(stat.S_IRUSR | stat.S_IWUSR)
                    except OSError as e:
                        sys.stderr.write(f"WARN: chmod 0600 on {p} failed: {e}\n")
                    written.append((f"storage {stor.name}", str(p)))
            await _dispose()
            if not written:
                print(f"No keys found in DB (checked {out_dir})")
                return
            print(f"Exported {len(written)} key(s) to {out_dir}:")
            for label, path in written:
                print(f"  {label} -> {path}")

        asyncio.run(_run())
        sys.exit(0)

    if args.command == "sync-from-robot":
        from sqlalchemy import select

        from serverpanel.infrastructure.crypto import decrypt_json
        from serverpanel.infrastructure.database.engine import (
            dispose_db as _dispose,
        )
        from serverpanel.infrastructure.database.engine import (
            get_session_factory,
        )
        from serverpanel.infrastructure.database.engine import (
            init_db as _init,
        )
        from serverpanel.infrastructure.database.models import (
            ProviderConfig,
            Server,
        )
        from serverpanel.infrastructure.providers.hetzner.robot_api import (
            HetznerRobotAPI,
        )

        async def _run():
            await _init()
            async with get_session_factory()() as db:
                srv = (await db.execute(
                    select(Server).where(Server.name == args.server_name)
                )).scalar_one_or_none()
                if srv is None:
                    sys.stderr.write(f"Server name={args.server_name!r} not found\n")
                    sys.exit(2)
                pc = await db.get(ProviderConfig, srv.provider_config_id)
                if pc is None or not pc.credentials_encrypted:
                    sys.stderr.write(
                        "ProviderConfig has no Robot credentials — run "
                        "`serverpanel set-robot-creds` first\n"
                    )
                    sys.exit(2)
                creds = decrypt_json(pc.credentials_encrypted)
                if not creds.get("robot_user") or not creds.get("robot_password"):
                    sys.stderr.write("ProviderConfig.credentials_encrypted missing robot_user/robot_password\n")
                    sys.exit(2)
                api = HetznerRobotAPI(creds["robot_user"], creds["robot_password"])
                servers = await api.get_servers()
                match = None
                for entry in servers:
                    s = entry.get("server", {})
                    if s.get("server_ip") == srv.ip_address:
                        match = s
                        break
                if match is None:
                    sys.stderr.write(
                        f"No server with IP {srv.ip_address} in Robot account "
                        f"(got {len(servers)} servers total)\n"
                    )
                    sys.exit(2)

                old = srv.provider_server_id
                srv.provider_server_id = str(match["server_number"])
                # pick up server_name + product as useful metadata if empty
                if not (srv.extra or {}).get("robot"):
                    extra = dict(srv.extra or {})
                    extra["robot"] = {
                        "server_name": match.get("server_name"),
                        "product": match.get("product"),
                        "dc": match.get("dc"),
                        "status": match.get("status"),
                    }
                    srv.extra = extra
                db.add(srv)
                await db.commit()
                print(
                    f"server {srv.name}: provider_server_id {old!r} -> "
                    f"{srv.provider_server_id!r} (product={match.get('product')}, "
                    f"dc={match.get('dc')})"
                )
            await _dispose()

        asyncio.run(_run())
        sys.exit(0)

    if args.command == "set-robot-creds":
        from sqlalchemy import select

        from serverpanel.infrastructure.crypto import encrypt_json
        from serverpanel.infrastructure.database.engine import (
            dispose_db as _dispose,
        )
        from serverpanel.infrastructure.database.engine import (
            get_session_factory,
        )
        from serverpanel.infrastructure.database.engine import (
            init_db as _init,
        )
        from serverpanel.infrastructure.database.models import (
            ProviderConfig,
            Server,
        )

        async def _run():
            await _init()
            async with get_session_factory()() as db:
                srv = (await db.execute(
                    select(Server).where(Server.name == args.server_name)
                )).scalar_one_or_none()
                if srv is None:
                    sys.stderr.write(f"Server name={args.server_name!r} not found\n")
                    sys.exit(2)
                pc = await db.get(ProviderConfig, srv.provider_config_id)
                if pc is None:
                    sys.stderr.write(
                        f"ProviderConfig {srv.provider_config_id} not found for server {srv.id}\n"
                    )
                    sys.exit(2)
                pc.credentials_encrypted = encrypt_json({
                    "robot_user": args.robot_user,
                    "robot_password": args.robot_password,
                })
                db.add(pc)
                await db.commit()
                print(
                    f"Robot credentials set on provider {pc.id} ({pc.name}), "
                    f"server {srv.name} (id={srv.id})"
                )
            await _dispose()

        asyncio.run(_run())
        sys.exit(0)

    # default: run server
    import uvicorn

    cfg = get_settings()
    uvicorn.run(
        "serverpanel.main:app",
        host=cfg.host,
        port=cfg.port,
        reload=cfg.debug,
    )
