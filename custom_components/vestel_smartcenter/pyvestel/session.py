"""FollowTV session: start on TV, pull + decrypt stream, fan out to clients."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from .api import VestelError, VestelTV
from .followtv import StreamStats, TSBroadcaster, stream_ts

_LOGGER = logging.getLogger(__name__)


class FollowTVManager:
    def __init__(self, tv: VestelTV, http_port: int | None = 8554, idle_stop: float = 5.0) -> None:
        self.tv = tv
        self.http_port = http_port
        self.idle_stop = idle_stop
        self.bus = TSBroadcaster()
        self.raw_url: str | None = None
        self.last_error: str | None = None
        self.encrypted: bool | None = None
        self.raw_response: str | None = None
        self.stats = StreamStats()
        self._task: asyncio.Task[None] | None = None
        self._stop_handle: asyncio.TimerHandle | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def subscribe(self) -> asyncio.Queue[bytes | None]:
        if self._stop_handle:
            self._stop_handle.cancel()
            self._stop_handle = None
        q = self.bus.subscribe()
        if not self.running:
            self._task = asyncio.create_task(self._run(), name="vestel-followtv")
        return q

    def unsubscribe(self, q: asyncio.Queue[bytes | None]) -> None:
        self.bus.unsubscribe(q)
        if self.bus.count == 0 and self.running:
            loop = asyncio.get_running_loop()
            self._stop_handle = loop.call_later(self.idle_stop, lambda: asyncio.create_task(self.stop()))

    async def start_on_tv(self) -> str | None:
        """Only ask the TV for a stream URL (for direct VLC use)."""
        res = await self.tv.start_followtv()
        self.raw_response = res.raw if res else None
        if res is None:
            self.last_error = "no answer"
            return None
        self.last_error = None if res.url else str(res.error or res.raw)
        self.raw_url = res.url
        self.encrypted = bool(res.url and "|" in res.url)
        return res.url

    async def _run(self) -> None:
        try:
            url = await self.start_on_tv()
            if not url:
                _LOGGER.warning("FollowTV not started: %s", self.last_error)
                return
            legacy = (self.tv.info.model or "").lower() == "mb90"
            self.stats = StreamStats()
            async for chunk in stream_ts(url, self.http_port, legacy, self.stats):
                self.bus.publish(chunk)
        except (VestelError, OSError, TimeoutError, Exception) as err:  # noqa: BLE001
            self.last_error = repr(err)
            _LOGGER.warning("FollowTV stream error: %r", err)
        finally:
            self.bus.publish(None)
            with contextlib.suppress(Exception):
                await self.tv.stop_followtv()

    async def stop(self) -> None:
        if self._stop_handle:
            self._stop_handle.cancel()
            self._stop_handle = None
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        self._task = None
