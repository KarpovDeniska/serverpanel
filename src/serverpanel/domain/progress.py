"""Progress-reporting protocol used by long-running application services.

Application services (install/backup/recovery) must not know about HTTP or
WebSocket. They depend on this `ProgressReporter` Protocol; a concrete
WebSocket-backed implementation lives in the presentation layer.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ProgressReporter(Protocol):
    async def log(self, message: str, level: str = "info") -> None: ...

    async def progress(self, step: str, num: int, total: int) -> None: ...

    async def status(self, status: str) -> None: ...


class NullProgressReporter:
    """No-op reporter used when a service runs without live progress (e.g. CLI)."""

    async def log(self, message: str, level: str = "info") -> None:  # noqa: D401
        return None

    async def progress(self, step: str, num: int, total: int) -> None:
        return None

    async def status(self, status: str) -> None:
        return None
