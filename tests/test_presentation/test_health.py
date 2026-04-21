"""/health endpoint smoke test."""


import pytest
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_health_returns_ok(tmp_path, monkeypatch):
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
            r = await client.get("/health")
            assert r.status_code == 200
            assert r.json() == {"status": "ok"}
