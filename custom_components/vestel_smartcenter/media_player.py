"""Media player entity + entity services."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import voluptuous as vol
from homeassistant.components.media_player import (
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
)
from homeassistant.core import HomeAssistant, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_platform
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.util import dt as dt_util

from .const import (
    CONF_TOKEN,
    SERVICE_ADD_TIMER,
    SERVICE_DELETE_TIMER,
    SERVICE_OPEN_URL,
    SERVICE_REFRESH_CHANNELS,
    SERVICE_SEND_TEXT,
    SERVICE_SET_ECO_MODE,
    SERVICE_START_FOLLOWTV,
    SERVICE_STOP_FOLLOWTV,
    STREAM_PATH,
)
from .coordinator import VestelConfigEntry
from .entity import VestelEntity
from .pyvestel import VestelError

PARALLEL_UPDATES = 1

FEATURES = (
    MediaPlayerEntityFeature.TURN_OFF
    | MediaPlayerEntityFeature.VOLUME_SET
    | MediaPlayerEntityFeature.VOLUME_STEP
    | MediaPlayerEntityFeature.VOLUME_MUTE
    | MediaPlayerEntityFeature.SELECT_SOURCE
    | MediaPlayerEntityFeature.PLAY
    | MediaPlayerEntityFeature.PAUSE
    | MediaPlayerEntityFeature.STOP
    | MediaPlayerEntityFeature.NEXT_TRACK
    | MediaPlayerEntityFeature.PREVIOUS_TRACK
    | MediaPlayerEntityFeature.PLAY_MEDIA
)
REPEAT = {"once": "o", "daily": "d", "weekly": "w"}


async def async_setup_entry(
    hass: HomeAssistant, entry: VestelConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([VestelMediaPlayer(entry.runtime_data)])
    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_ADD_TIMER,
        {
            vol.Required("channel"): cv.string,
            vol.Required("start"): cv.datetime,
            vol.Required("end"): cv.datetime,
            vol.Optional("name", default=""): cv.string,
            vol.Optional("kind", default="reminder"): vol.In(["reminder", "record"]),
            vol.Optional("repeat", default="once"): vol.In(list(REPEAT)),
        },
        "async_add_timer",
        supports_response=SupportsResponse.OPTIONAL,
    )
    platform.async_register_entity_service(
        SERVICE_DELETE_TIMER,
        {
            vol.Required("timer_id"): cv.positive_int,
            vol.Optional("kind", default="reminder"): vol.In(["reminder", "record"]),
        },
        "async_delete_timer",
    )
    platform.async_register_entity_service(SERVICE_OPEN_URL, {vol.Required("url"): cv.url}, "async_open_url")
    platform.async_register_entity_service(SERVICE_SEND_TEXT, {vol.Required("text"): cv.string}, "async_send_text")
    platform.async_register_entity_service(
        SERVICE_SET_ECO_MODE,
        {vol.Required("mode"): vol.In(["low", "medium", "high", "auto", "off", "eco"])},
        "async_set_eco_mode",
    )
    platform.async_register_entity_service(
        SERVICE_START_FOLLOWTV, {}, "async_start_followtv", supports_response=SupportsResponse.ONLY
    )
    platform.async_register_entity_service(SERVICE_STOP_FOLLOWTV, {}, "async_stop_followtv")
    platform.async_register_entity_service(SERVICE_REFRESH_CHANNELS, {}, "async_refresh_channels")


class VestelMediaPlayer(VestelEntity, MediaPlayerEntity):
    _attr_name = None
    _attr_device_class = MediaPlayerDeviceClass.TV
    _attr_media_content_type = MediaType.CHANNEL
    _attr_volume_step = 0.02

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "media_player")

    # ------------------------------------------------------------ state
    @property
    def available(self) -> bool:
        return True  # an "off" TV is still controllable (WOL)

    @property
    def supported_features(self) -> MediaPlayerEntityFeature:
        f = FEATURES
        if self.tv.info.mac:
            f |= MediaPlayerEntityFeature.TURN_ON
        return f

    @property
    def state(self) -> MediaPlayerState:
        d = self.coordinator.data
        return MediaPlayerState.ON if d and d.is_on else MediaPlayerState.OFF

    @property
    def volume_level(self) -> float | None:
        d = self.coordinator.data
        return d.volume / 100 if d and d.is_on and d.volume is not None else None

    @property
    def source_list(self) -> list[str] | None:
        d = self.coordinator.data
        return [c.name for c in d.channels] if d and d.channels else None

    @property
    def source(self) -> str | None:
        d = self.coordinator.data
        return d.channel.name if d and d.is_on and d.channel else None

    @property
    def media_channel(self) -> str | None:
        return self.source

    @property
    def media_title(self) -> str | None:
        return self.source

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        d = self.coordinator.data
        mgr = self.coordinator.followtv
        return {
            "tv_state": d.state.value if d and d.state else None,
            "channel_rsn": d.channel.rsn if d and d.channel else None,
            "followtv_active": mgr.running,
            "followtv_enabled": mgr.enabled,
            "followtv_error": mgr.last_error,
        }

    # ------------------------------------------------------------ control
    async def _call(self, coro) -> Any:
        try:
            return await coro
        except VestelError as err:
            raise HomeAssistantError(str(err)) from err

    async def async_turn_on(self) -> None:
        await self.hass.async_add_executor_job(self.tv.wake_on_lan)
        await asyncio.sleep(1)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self) -> None:
        await self._call(self.tv.send_key("POWER"))
        if self.coordinator.data:
            self.coordinator.data.online = False
            self.coordinator.async_update_listeners()

    async def async_set_volume_level(self, volume: float) -> None:
        await self._call(self.tv.set_volume(round(volume * 100)))
        if self.coordinator.data:
            self.coordinator.data.volume = round(volume * 100)
            self.async_write_ha_state()

    async def async_volume_up(self) -> None:
        await self._call(self.tv.send_key("VOL_UP"))

    async def async_volume_down(self) -> None:
        await self._call(self.tv.send_key("VOL_DOWN"))

    async def async_mute_volume(self, mute: bool) -> None:
        await self._call(self.tv.send_key("MUTE"))  # toggle only, TV does not report mute

    async def async_media_play(self) -> None:
        await self._call(self.tv.send_key("PLAY"))

    async def async_media_pause(self) -> None:
        await self._call(self.tv.send_key("PAUSE"))

    async def async_media_stop(self) -> None:
        await self._call(self.tv.send_key("STOP"))

    async def async_media_next_track(self) -> None:
        await self._call(self.tv.send_key("PROG_UP"))
        await self.coordinator.async_request_refresh()

    async def async_media_previous_track(self) -> None:
        await self._call(self.tv.send_key("PROG_DOWN"))
        await self.coordinator.async_request_refresh()

    async def async_select_source(self, source: str) -> None:
        ch = self.coordinator.find_channel(source)
        if ch is None:
            raise ServiceValidationError(f"Unknown channel: {source}")
        await self._call(self.tv.set_channel(ch.rsn))
        await self.coordinator.async_request_refresh()

    async def async_play_media(self, media_type: MediaType | str, media_id: str, **kwargs: Any) -> None:
        if media_type == MediaType.URL or media_id.startswith("http"):
            await self._call(self.tv.open_url(media_id))
            return
        ch = self.coordinator.find_channel(media_id)
        if ch is not None:
            await self._call(self.tv.set_channel(ch.rsn))
        elif media_id.isdigit():  # channel number typed on the remote
            for digit in media_id:
                await self._call(self.tv.send_key(digit))
                await asyncio.sleep(0.3)
        else:
            raise ServiceValidationError(f"Unknown channel: {media_id}")
        await self.coordinator.async_request_refresh()

    # ------------------------------------------------------------ services
    async def async_add_timer(
        self, channel: str, start: datetime, end: datetime, name: str, kind: str, repeat: str
    ) -> ServiceResponse:
        ch = self.coordinator.find_channel(channel)
        if ch is None:
            raise ServiceValidationError(f"Unknown channel: {channel}")
        s = dt_util.as_utc(start if start.tzinfo else start.replace(tzinfo=dt_util.get_default_time_zone()))
        e = dt_util.as_utc(end if end.tzinfo else end.replace(tzinfo=dt_util.get_default_time_zone()))
        if e <= s:
            raise ServiceValidationError("end must be after start")
        new_id = await self._call(
            self.tv.add_timed("timer" if kind == "reminder" else "record", ch, name or ch.name, s, e, REPEAT[repeat])
        )
        await self._refresh_timers()
        return {"id": new_id}

    async def async_delete_timer(self, timer_id: int, kind: str) -> None:
        await self._call(self.tv.delete_timed("timer" if kind == "reminder" else "record", timer_id))
        await self._refresh_timers()

    async def _refresh_timers(self) -> None:
        self.coordinator.invalidate(timers=True)
        await self.coordinator.async_request_refresh()

    async def async_open_url(self, url: str) -> None:
        await self._call(self.tv.open_url(url))

    async def async_send_text(self, text: str) -> None:
        await self._call(self.tv.send_text(text))

    async def async_set_eco_mode(self, mode: str) -> None:
        await self._call(self.tv.set_eco_mode(mode))

    async def async_refresh_channels(self) -> None:
        self.coordinator.invalidate(channels=True)
        await self.coordinator.async_request_refresh()

    async def async_start_followtv(self) -> ServiceResponse:
        """Ask the TV for a stream; return direct URL (plain) and the HA proxy URL (always works)."""
        mgr = self.coordinator.followtv
        entry = self.coordinator.config_entry
        try:
            base = get_url(self.hass, prefer_external=False)
        except NoURLAvailableError:
            base = ""
        proxy = f"{base}{STREAM_PATH.format(entry_id=entry.entry_id)}?token={entry.data[CONF_TOKEN]}"
        # No extra start on the TV here: the stream is started once, by the first client of proxy_url.
        mgr.enabled = True
        self.coordinator.async_update_listeners()
        direct = mgr.raw_url if mgr.raw_url and not mgr.encrypted else None
        return {
            "proxy_url": proxy,
            "direct_url": direct,
            "encrypted": mgr.encrypted,
            "vlc_command": f'vlc "{proxy}"',
            "running": mgr.running,
        }

    async def async_stop_followtv(self) -> None:
        self.coordinator.followtv.enabled = False
        await self.coordinator.followtv.stop()
        self.coordinator.async_update_listeners()
        await self._call(self.tv.stop_followtv())
