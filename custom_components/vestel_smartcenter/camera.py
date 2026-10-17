"""FollowTV as a camera entity: HA's stream component turns the TS into HLS for dashboards."""

from __future__ import annotations

import asyncio
from functools import cache
from pathlib import Path

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.components.ffmpeg import async_get_image
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import VestelConfigEntry, VestelCoordinator
from .entity import VestelEntity

PARALLEL_UPDATES = 0
IDLE_IMAGE = Path(__file__).with_name("followtv_idle.jpg")


@cache
def _idle_image() -> bytes:
    return IDLE_IMAGE.read_bytes()


async def async_setup_entry(
    hass: HomeAssistant, entry: VestelConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([VestelFollowTVCamera(entry.runtime_data)])


class VestelFollowTVCamera(VestelEntity, Camera):
    _attr_translation_key = "followtv"
    _attr_supported_features = CameraEntityFeature.STREAM
    _attr_icon = "mdi:television-play"

    def __init__(self, coordinator: VestelCoordinator) -> None:
        VestelEntity.__init__(self, coordinator, "followtv")
        Camera.__init__(self)

    @property
    def available(self) -> bool:
        d = self.coordinator.data
        return bool(d and d.is_on)

    @property
    def is_streaming(self) -> bool:
        return self.coordinator.followtv.running

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        mgr = self.coordinator.followtv
        return {
            "encrypted": mgr.encrypted,
            "last_error": mgr.last_error or mgr.stats.error,
            "tv_response": mgr.raw_response,
            "transport": mgr.stats.transport,
            "bytes_in": mgr.stats.bytes_in,
            "bytes_out": mgr.stats.bytes_out,
        }

    async def stream_source(self) -> str | None:
        """Pulled by HA's stream worker; the first client starts FollowTV on the TV."""
        ls = self.coordinator.local_stream
        return ls.url if ls else None

    async def async_camera_image(self, width: int | None = None, height: int | None = None) -> bytes | None:
        """Snapshot. Only grabs a real frame while FollowTV is already running: HA requests
        thumbnails every few seconds, and starting FollowTV for each of them would block the TV."""
        ls = self.coordinator.local_stream
        if ls and ls.url and self.coordinator.followtv.running:
            try:
                async with asyncio.timeout(20):
                    img = await async_get_image(self.hass, ls.raw_url or ls.url, width=width, height=height)
                if img:
                    return img
            except TimeoutError:
                pass
        return await self.hass.async_add_executor_job(_idle_image)
