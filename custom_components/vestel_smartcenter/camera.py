"""FollowTV as a camera entity: HA's stream component turns the TS into HLS for dashboards."""

from __future__ import annotations

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import VestelConfigEntry, VestelCoordinator
from .entity import VestelEntity

PARALLEL_UPDATES = 0


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
        return {"encrypted": mgr.encrypted, "last_error": mgr.last_error}

    async def stream_source(self) -> str | None:
        """Pulled by HA's stream worker; the first client starts FollowTV on the TV."""
        ls = self.coordinator.local_stream
        return ls.url if ls else None

    async def async_camera_image(self, width: int | None = None, height: int | None = None) -> bytes | None:
        return None  # no still images; the live stream is shown instead
