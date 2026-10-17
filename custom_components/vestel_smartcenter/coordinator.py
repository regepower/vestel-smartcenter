"""Coordinator: polling + websocket push for one TV."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import CHANNELS_INTERVAL, DOMAIN, SCAN_INTERVAL, TIMERS_INTERVAL
from .pyvestel import Channel, FollowTVManager, TimedObject, TVState, VestelError, VestelTV

if TYPE_CHECKING:
    from . import LocalStreamServer

_LOGGER = logging.getLogger(__name__)


@dataclass
class TVData:
    online: bool = False
    state: TVState | None = None
    volume: int | None = None
    channel: Channel | None = None
    channels: list[Channel] = field(default_factory=list)
    reminders: list[TimedObject] = field(default_factory=list)
    recordings: list[TimedObject] = field(default_factory=list)

    @property
    def is_on(self) -> bool:
        return self.online and self.state is not TVState.STANDBY


type VestelConfigEntry = ConfigEntry[VestelCoordinator]


class VestelCoordinator(DataUpdateCoordinator[TVData]):
    config_entry: VestelConfigEntry

    def __init__(self, hass: HomeAssistant, entry: VestelConfigEntry, tv: VestelTV, followtv: FollowTVManager) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=f"{DOMAIN} {tv.host}", update_interval=SCAN_INTERVAL)
        self.tv = tv
        self.followtv = followtv
        self.local_stream: LocalStreamServer | None = None
        self.options_snapshot: dict[str, Any] = {}
        self._channels_at: datetime | None = None
        self._timers_at: datetime | None = None
        # Timers/channels survive HA restarts while the TV (or its power socket) is off.
        self._store: Store[dict[str, Any]] = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}")
        self._saved: dict[str, Any] = {}
        tv.add_listener(self._on_event)

    async def async_load_cache(self) -> None:
        raw = await self._store.async_load() or {}
        self._saved = raw
        data = TVData(
            channels=[Channel(**c) for c in raw.get("channels", [])],
            reminders=[_timed_from(t) for t in raw.get("reminders", [])],
            recordings=[_timed_from(t) for t in raw.get("recordings", [])],
        )
        self.async_set_updated_data(data)

    def _save_cache(self, data: TVData) -> None:
        payload = {
            "channels": [asdict(c) for c in data.channels],
            "reminders": [_timed_to(t) for t in data.reminders],
            "recordings": [_timed_to(t) for t in data.recordings],
        }
        if payload != self._saved:  # write only on change
            self._saved = payload
            self.hass.async_create_task(self._store.async_save(payload))

    @callback
    def _on_event(self, event: str) -> None:
        data = self.data or TVData()
        if event in ("tv_status:0", "__disconnected__"):
            data.online = False
            self.async_set_updated_data(data)
            return
        if "<tv_state value=" in event or event.startswith("tv_state"):
            val = event.split("'")[1] if "'" in event else event.rsplit(":", maxsplit=1)[-1]
            try:
                data.state = TVState(val)
            except ValueError:
                data.state = TVState.DEFAULT
            self.async_set_updated_data(data)
            return
        if "<channel_list_changed" in event:
            self._channels_at = None
        elif "<timer id=" in event or "<record id=" in event:
            self._timers_at = None
        else:
            return
        self.hass.async_create_task(self.async_request_refresh())

    async def _async_update_data(self) -> TVData:
        data = TVData() if self.data is None else self.data
        online = await self.tv.ping()
        if not online:
            if self.tv.connected:
                await self.tv.disconnect()
            data.online = False
            return data
        try:
            async with asyncio.timeout(120):  # never let one poll hang
                data.online = await self._poll(data)
        except (VestelError, TimeoutError) as err:
            _LOGGER.debug("update failed: %s", err)
            data.online = False
        return data

    async def _poll(self, data: TVData) -> bool:
        """Returns True if the TV UI answers (on). Timer lists are tried even in standby."""
        if not self.tv.connected:
            await self.tv.connect()
        state = await self.tv.get_state()
        now = dt_util.utcnow()
        if state is not None and self.followtv.running:
            data.state = state  # keep the TV's load low while it streams
            return True
        if state is not None:
            data.state = state
            data.volume = await self.tv.get_volume()
            data.channel = await self.tv.get_current_channel()
        if state is not None and (
            self._channels_at is None or now - self._channels_at > CHANNELS_INTERVAL or not data.channels
        ):
            chans = await self.tv.get_channels()
            if chans:
                data.channels = chans
                self._channels_at = now
                self._save_cache(data)
        if self._timers_at is None or now - self._timers_at > TIMERS_INTERVAL:
            try:
                data.reminders = await self.tv.get_reminders()
                data.recordings = await self.tv.get_recordings()
                self._timers_at = now
                self._save_cache(data)
            except VestelError as err:  # keep the cached lists
                _LOGGER.debug("timer list update failed: %s", err)
        return state is not None

    def invalidate(self, channels: bool = False, timers: bool = False) -> None:
        if channels:
            self._channels_at = None
        if timers:
            self._timers_at = None

    def find_channel(self, value: str | int) -> Channel | None:
        chans = self.data.channels if self.data else []
        s = str(value).strip()
        for ch in chans:
            if str(ch.rsn) == s:
                return ch
        low = s.lower()
        return next((c for c in chans if c.name.lower() == low), None)


def _timed_to(t: TimedObject) -> dict[str, Any]:
    return {
        "id": t.id,
        "name": t.name,
        "start": t.start.isoformat() if t.start else None,
        "end": t.end.isoformat() if t.end else None,
        "repeat": t.repeat,
        "kind": t.kind,
        "extra": t.extra,
    }


def _timed_from(d: dict[str, Any]) -> TimedObject:
    return TimedObject(
        id=d["id"],
        name=d["name"],
        start=dt_util.parse_datetime(d["start"]) if d.get("start") else None,
        end=dt_util.parse_datetime(d["end"]) if d.get("end") else None,
        repeat=d.get("repeat"),
        kind=d.get("kind", "timer"),
        extra=d.get("extra") or {},
    )
