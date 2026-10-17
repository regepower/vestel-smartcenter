"""One button entity per common remote key (usable without YAML)."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import VestelConfigEntry, VestelCoordinator
from .entity import VestelEntity
from .pyvestel import VestelError

PARALLEL_UPDATES = 1

# key, icon, enabled by default
BUTTONS: tuple[tuple[str, str, bool], ...] = (
    ("POWER", "mdi:power", True),
    ("HOME", "mdi:home", True),
    ("MENU", "mdi:menu", True),
    ("BACK", "mdi:arrow-u-left-top", True),
    ("EXIT", "mdi:exit-to-app", True),
    ("OK", "mdi:checkbox-blank-circle-outline", True),
    ("UP", "mdi:chevron-up", True),
    ("DOWN", "mdi:chevron-down", True),
    ("LEFT", "mdi:chevron-left", True),
    ("RIGHT", "mdi:chevron-right", True),
    ("VOL_UP", "mdi:volume-plus", True),
    ("VOL_DOWN", "mdi:volume-minus", True),
    ("MUTE", "mdi:volume-mute", True),
    ("PROG_UP", "mdi:menu-up", True),
    ("PROG_DOWN", "mdi:menu-down", True),
    ("SOURCE", "mdi:import", True),
    ("EPG", "mdi:television-guide", True),
    ("INFO", "mdi:information-outline", True),
    ("TEXT", "mdi:text-box-outline", False),
    ("QMENU", "mdi:dots-horizontal", False),
    ("RED", "mdi:square-rounded", False),
    ("GREEN", "mdi:square-rounded", False),
    ("YELLOW", "mdi:square-rounded", False),
    ("BLUE", "mdi:square-rounded", False),
    ("PLAY", "mdi:play", False),
    ("PAUSE", "mdi:pause", False),
    ("STOP", "mdi:stop", False),
    ("RECORD", "mdi:record-rec", False),
    *((str(i), f"mdi:numeric-{i}", False) for i in range(10)),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: VestelConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coord = entry.runtime_data
    async_add_entities([*(VestelKeyButton(coord, *b) for b in BUTTONS), VestelFollowTVStopButton(coord)])


class VestelKeyButton(VestelEntity, ButtonEntity):
    def __init__(self, coordinator: VestelCoordinator, key: str, icon: str, enabled: bool) -> None:
        super().__init__(coordinator, f"key_{key.lower()}")
        self._key = key
        self._attr_translation_key = f"key_{key.lower()}"
        self._attr_icon = icon
        self._attr_entity_registry_enabled_default = enabled

    @property
    def available(self) -> bool:
        return True

    async def async_press(self) -> None:
        try:
            await self.tv.send_key(self._key)
        except VestelError as err:
            raise HomeAssistantError(f"{self._key}: {err}") from err


class VestelFollowTVStopButton(VestelEntity, ButtonEntity):
    """Ends FollowTV on the TV and in HA (it also stops by itself shortly after the last viewer leaves)."""

    _attr_translation_key = "followtv_stop"
    _attr_icon = "mdi:television-off"

    def __init__(self, coordinator: VestelCoordinator) -> None:
        super().__init__(coordinator, "followtv_stop")

    @property
    def available(self) -> bool:
        return True

    async def async_press(self) -> None:
        self.coordinator.followtv.enabled = False
        await self.coordinator.followtv.stop()
        self.coordinator.async_update_listeners()
        try:
            await self.tv.stop_followtv()
        except VestelError as err:
            raise HomeAssistantError(f"FollowTV stop: {err}") from err
