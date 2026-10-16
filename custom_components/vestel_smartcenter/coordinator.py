"""Coordinator: polling + websocket push for one TV."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import CHANNELS_INTERVAL, DOMAIN, SCAN_INTERVAL, TIMERS_INTERVAL
from .pyvestel import Channel, FollowTVManager, TimedObject, TVState, VestelError, VestelTV

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
        self._channels_at: datetime | None = None
        self._timers_at: datetime | None = None
        tv.add_listener(self._on_event)

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
            if not self.tv.connected:
                await self.tv.connect()
            data.state = await self.tv.get_state()
            data.volume = await self.tv.get_volume()
            data.channel = await self.tv.get_current_channel()
            now = dt_util.utcnow()
            if self._channels_at is None or now - self._channels_at > CHANNELS_INTERVAL or not data.channels:
                chans = await self.tv.get_channels()
                if chans:
                    data.channels = chans
                    self._channels_at = now
            if self._timers_at is None or now - self._timers_at > TIMERS_INTERVAL:
                data.reminders = await self.tv.get_reminders()
                data.recordings = await self.tv.get_recordings()
                self._timers_at = now
            data.online = True
        except VestelError as err:
            _LOGGER.debug("update failed: %s", err)
            data.online = False
        return data

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
