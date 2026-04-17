"""In-memory telemetry fan-out (session-scoped). Production would use Redis pub/sub."""

from __future__ import annotations

import json
from collections import defaultdict, deque
from typing import Any

from fastapi import WebSocket


class TelemetryHub:
    def __init__(self, history_max: int = 2000) -> None:
        self._subscribers: dict[str, list[WebSocket]] = defaultdict(list)
        self._history: dict[str, deque[dict[str, Any]]] = defaultdict(
            lambda: deque(maxlen=history_max)
        )

    def record(self, session_id: str, event: dict[str, Any]) -> None:
        self._history[session_id].append(event)

    def history(self, session_id: str, limit: int = 500) -> list[dict[str, Any]]:
        return list(self._history[session_id])[-limit:]

    async def connect(self, session_id: str, ws: WebSocket) -> None:
        await ws.accept()
        # Replay buffered events so dashboards are not blank until the next broadcast.
        for ev in self.history(session_id, limit=500):
            try:
                await ws.send_text(json.dumps(ev))
            except Exception:
                try:
                    await ws.close()
                except Exception:
                    pass
                return
        self._subscribers[session_id].append(ws)

    def disconnect(self, session_id: str, ws: WebSocket) -> None:
        subs = self._subscribers.get(session_id, [])
        if ws in subs:
            subs.remove(ws)

    async def broadcast(self, session_id: str, event: dict[str, Any]) -> None:
        self.record(session_id, event)
        dead: list[WebSocket] = []
        for ws in list(self._subscribers.get(session_id, [])):
            try:
                await ws.send_text(json.dumps(event))
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(session_id, ws)


hub = TelemetryHub()
