"""WebSocket connection manager for real-time updates."""

from __future__ import annotations

import logging
from collections import defaultdict

from fastapi import WebSocket

log = logging.getLogger(__name__)


class ConnectionManager:
    """Manages WebSocket connections grouped by room (e.g., recovery run ID)."""

    def __init__(self):
        self._rooms: dict[str, list[WebSocket]] = defaultdict(list)

    async def connect(self, websocket: WebSocket, room: str) -> None:
        await websocket.accept()
        self._rooms[room].append(websocket)
        log.info("WS connected to room %s (%d clients)", room, len(self._rooms[room]))

    def disconnect(self, websocket: WebSocket, room: str) -> None:
        if room in self._rooms:
            self._rooms[room] = [ws for ws in self._rooms[room] if ws is not websocket]
            if not self._rooms[room]:
                del self._rooms[room]

    async def send_to_room(self, room: str, data: dict) -> None:
        """Send JSON message to all clients in a room."""
        if room not in self._rooms:
            return
        dead = []
        for ws in self._rooms[room]:
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws, room)

    async def send_log(self, room: str, message: str, level: str = "info") -> None:
        """Send a log-style message."""
        await self.send_to_room(room, {
            "type": "log",
            "message": message,
            "level": level,
        })

    async def send_progress(
        self, room: str, step: str, progress: int, total: int
    ) -> None:
        """Send progress update."""
        await self.send_to_room(room, {
            "type": "progress",
            "step": step,
            "progress": progress,
            "total": total,
        })

    async def send_status(self, room: str, status: str) -> None:
        """Send status change."""
        await self.send_to_room(room, {
            "type": "status",
            "status": status,
        })


# Singleton
ws_manager = ConnectionManager()
