"""WebSocket-backed ProgressReporter adapter.

Injected into application services so they never import `ws_manager`
directly.
"""

from __future__ import annotations

from serverpanel.domain.progress import ProgressReporter
from serverpanel.presentation.websocket import ws_manager


class WsProgressReporter(ProgressReporter):
    def __init__(self, room: str) -> None:
        self.room = room

    async def log(self, message: str, level: str = "info") -> None:
        await ws_manager.send_log(self.room, message, level)

    async def progress(self, step: str, num: int, total: int) -> None:
        await ws_manager.send_progress(self.room, step, num, total)

    async def status(self, status: str) -> None:
        await ws_manager.send_status(self.room, status)
