"""Non-blocking telemetry: in-memory fan-out + optional HTTP push to platform API."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any, Callable, Coroutine

import httpx

from litebot.interfaces import MetricsEvent

logger = logging.getLogger(__name__)


class TelemetryHub:
    def __init__(self, buffer_max: int = 500) -> None:
        self._subs: list[Callable[[MetricsEvent], Coroutine[Any, Any, None]]] = []
        self._queue: asyncio.Queue[MetricsEvent] = asyncio.Queue(maxsize=buffer_max)
        self._recent: deque[MetricsEvent] = deque(maxlen=buffer_max)
        self._task: asyncio.Task[None] | None = None
        self._push_url: str | None = None
        self._push_token: str | None = None
        self._session_id: str | None = None

    def configure_push(
        self, url: str | None, token: str | None, session_id: str | None
    ) -> None:
        self._push_url = url
        self._push_token = token
        self._session_id = session_id

    def subscribe(self, cb: Callable[[MetricsEvent], Coroutine[Any, Any, None]]) -> None:
        self._subs.append(cb)

    async def emit(self, event: MetricsEvent) -> None:
        self._recent.append(event)
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            pass
        for cb in list(self._subs):
            asyncio.create_task(self._safe_cb(cb, event))

    async def _safe_cb(
        self, cb: Callable[[MetricsEvent], Coroutine[Any, Any, None]], event: MetricsEvent
    ) -> None:
        try:
            await cb(event)
        except Exception:
            pass

    def start_background(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._drain_loop())

    async def _drain_loop(self) -> None:
        async with httpx.AsyncClient(timeout=5.0) as client:
            while True:
                ev = await self._queue.get()
                if self._push_url:
                    await self._post_event(client, ev)

    async def _post_event(self, client: httpx.AsyncClient, ev: MetricsEvent) -> None:
        if not self._push_url:
            return
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._push_token:
            headers["Authorization"] = f"Bearer {self._push_token}"
        payload = {
            "kind": ev.kind,
            "ts": ev.ts,
            "session_id": self._session_id or ev.session_id,
            "symbol": ev.symbol,
            "data": ev.data,
        }
        try:
            r = await client.post(self._push_url, json=payload, headers=headers)
            if r.status_code >= 400:
                logger.warning(
                    "telemetry HTTP %s for %s: %s",
                    r.status_code,
                    self._push_url,
                    (r.text or "")[:500],
                )
        except Exception as e:
            logger.warning("telemetry push failed for %s: %s", self._push_url, e)

    def recent_snapshot(self, limit: int = 100) -> list[MetricsEvent]:
        return list(self._recent)[-limit:]


async def noop_handler(_: MetricsEvent) -> None:
    return
