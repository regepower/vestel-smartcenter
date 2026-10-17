"""FollowTV on/off switch: the only way (besides start_followtv) to allow FollowTV starts."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import VestelConfigEntry, VestelCoordinator
from .entity import VestelEntity

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant, entry: VestelConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([VestelFollowTVSwitch(entry.runtime_data)])


class VestelFollowTVSwitch(VestelEntity, SwitchEntity):
    """On = camera/VLC may start FollowTV (rate-limited). Off = stop and block. Off after every HA start."""

    _attr_translation_key = "followtv"
    _attr_icon = "mdi:television-play"

    def __init__(self, coordinator: VestelCoordinator) -> None:
        super().__init__(coordinator, "followtv_switch")

    @property
    def available(self) -> bool:
        return True

    @property
    def is_on(self) -> bool:
        return self.coordinator.followtv.enabled

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        mgr = self.coordinator.followtv
        return {"streaming": mgr.running, "last_error": mgr.last_error}

    async def async_turn_on(self, **kwargs: Any) -> None:
        self.coordinator.followtv.enabled = True
        self.coordinator.followtv.last_error = None
        self.coordinator.async_update_listeners()

    async def async_turn_off(self, **kwargs: Any) -> None:
        mgr = self.coordinator.followtv
        mgr.enabled = False
        await mgr.stop()  # its cleanup sends stopfollowtv to the TV
        self.coordinator.async_update_listeners()
