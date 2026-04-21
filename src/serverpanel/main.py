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


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown."""
    # Import providers to auto-register
    import serverpanel.infrastructure.providers.hetzner  # noqa: F401
    import serverpanel.infrastructure.providers.storage.hetzner_storagebox  # noqa: F401

    await init_db()
    await cleanup_stale_runs()
    yield
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
      serverpanel import-hetzner-recovery <yaml> — port a legacy hetzner-recovery config.yaml
    """
    import argparse
    import asyncio
    import sys
    from pathlib import Path

    parser = argparse.ArgumentParser(prog="serverpanel")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("serve", help="Run the web server")

    imp = sub.add_parser(
        "import-hetzner-recovery",
        help="Import a legacy hetzner-recovery config.yaml",
    )
    imp.add_argument("yaml_path", help="Path to the legacy config.yaml")
    imp.add_argument("--user-email", required=True, help="Owner user email (must already exist)")
    imp.add_argument("--sb-private-key", help="Path to Storage Box SSH private key (contents will be encrypted and stored)")
    imp.add_argument("--server-private-key", help="Path to target Windows server SSH private key")

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

    # default: run server
    import uvicorn

    cfg = get_settings()
    uvicorn.run(
        "serverpanel.main:app",
        host=cfg.host,
        port=cfg.port,
        reload=cfg.debug,
    )
