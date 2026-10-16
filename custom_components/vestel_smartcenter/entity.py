"""Base entity."""

from __future__ import annotations

from homeassistant.const import CONF_HOST, CONF_MAC, CONF_NAME
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo, format_mac
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_MODEL, CONF_SW_VERSION, DOMAIN
from .coordinator import VestelCoordinator
from .pyvestel import VestelTV


class VestelEntity(CoordinatorEntity[VestelCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: VestelCoordinator, key: str) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        d = entry.data
        uid = entry.unique_id or entry.entry_id
        self._attr_unique_id = f"{uid}_{key}"
        mac = d.get(CONF_MAC)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, uid)},
            connections={(CONNECTION_NETWORK_MAC, format_mac(mac))} if mac else set(),
            manufacturer="Vestel",
            model=(d.get(CONF_MODEL) or "SmartCenter TV").upper(),
            name=d.get(CONF_NAME) or f"Smart TV {d[CONF_HOST]}",
            sw_version=d.get(CONF_SW_VERSION),
            configuration_url=None,
        )

    @property
    def tv(self) -> VestelTV:
        return self.coordinator.tv
