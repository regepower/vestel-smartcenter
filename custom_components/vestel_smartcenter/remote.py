"""Remote entity: send any key (see pyvestel.keys.KEYS)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Any

from homeassistant.components.remote import ATTR_DELAY_SECS, ATTR_NUM_REPEATS, RemoteEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import VestelConfigEntry, VestelCoordinator
from .entity import VestelEntity
from .pyvestel import KEYS, VestelError, resolve_key

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant, entry: VestelConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([VestelRemote(entry.runtime_data)])


class VestelRemote(VestelEntity, RemoteEntity):
    """HA remote entity: its switch = TV on/off; keys via remote.send_command (buttons cover the UI)."""

    _attr_translation_key = "remote"
    _attr_entity_registry_visible_default = False

    def __init__(self, coordinator: VestelCoordinator) -> None:
        super().__init__(coordinator, "remote")

    @property
    def available(self) -> bool:
        return True

    @property
    def is_on(self) -> bool:
        d = self.coordinator.data
        return bool(d and d.is_on)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"keys": sorted(KEYS)}

    async def async_turn_on(self, **kwargs: Any) -> None:
        try:
            await self.hass.async_add_executor_job(self.tv.wake_on_lan)
        except VestelError as err:
            raise HomeAssistantError(f"{err} - set the TV MAC in the integration options") from err

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.async_send_command(["POWER"])

    async def async_send_command(self, command: Iterable[str], **kwargs: Any) -> None:
        repeats = int(kwargs.get(ATTR_NUM_REPEATS, 1))
        delay = float(kwargs.get(ATTR_DELAY_SECS, 0.3))
        cmds = list(command)
        try:
            codes = [resolve_key(c) for c in cmds]
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err
        try:
            for _ in range(repeats):
                for code in codes:
                    await self.tv.send_key(code)
                    await asyncio.sleep(delay)
        except VestelError as err:
            raise HomeAssistantError(str(err)) from err
