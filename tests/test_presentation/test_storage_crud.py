"""Smoke test: StorageConfig CRUD routes require auth and scope to the user."""

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_storage_new_requires_auth(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")

    import importlib

    import serverpanel.infrastructure.database.engine as engine
    importlib.reload(engine)
    import serverpanel.main as main
    importlib.reload(main)

    app = main.create_app()
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            r = await client.get(
                "/servers/1/storages/new",
                headers={"accept": "text/html"},
                follow_redirects=False,
            )
            # Unauthenticated HTML → redirect to /login by AuthRedirectMiddleware
            assert r.status_code in (302, 401)
