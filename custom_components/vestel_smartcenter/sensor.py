"""Sensors: current channel, reminders, recordings, TV UI state."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import TVData, VestelConfigEntry, VestelCoordinator
from .entity import VestelEntity
from .pyvestel import TimedObject, TVState

PARALLEL_UPDATES = 0


def _items(items: list[TimedObject]) -> list[dict[str, Any]]:
    return [
        {
            "id": t.id,
            "name": t.name,
            "start": t.start.isoformat() if t.start else None,
            "end": t.end.isoformat() if t.end else None,
            "repeat": t.repeat,
            **t.extra,
        }
        for t in sorted(items, key=lambda x: x.start.timestamp() if x.start else 0)
    ]


@dataclass(frozen=True, kw_only=True)
class VestelSensorDescription(SensorEntityDescription):
    value_fn: Callable[[TVData], Any]
    attrs_fn: Callable[[TVData], dict[str, Any]] | None = None


SENSORS: tuple[VestelSensorDescription, ...] = (
    VestelSensorDescription(
        key="channel",
        translation_key="channel",
        value_fn=lambda d: d.channel.name if d.is_on and d.channel else None,
        attrs_fn=lambda d: (
            {
                "rsn": d.channel.rsn,
                "onid": d.channel.onid,
                "tsid": d.channel.tsid,
                "sid": d.channel.sid,
                "handle": d.channel.handle,
            }
            if d.channel
            else {}
        ),
    ),
    VestelSensorDescription(
        key="reminders",
        translation_key="reminders",
        value_fn=lambda d: len(d.reminders),
        attrs_fn=lambda d: {"items": _items(d.reminders)},
    ),
    VestelSensorDescription(
        key="recordings",
        translation_key="recordings",
        value_fn=lambda d: len(d.recordings),
        attrs_fn=lambda d: {"items": _items(d.recordings)},
    ),
    VestelSensorDescription(
        key="tv_state",
        translation_key="tv_state",
        device_class=SensorDeviceClass.ENUM,
        options=[s.value.lower() for s in TVState] + ["off"],
        value_fn=lambda d: (d.state.value.lower() if d.state else "default") if d.online else "off",
    ),
    VestelSensorDescription(
        key="channel_count",
        translation_key="channel_count",
        entity_registry_enabled_default=False,
        value_fn=lambda d: len(d.channels),
        attrs_fn=lambda d: {"channels": {c.rsn: c.name for c in d.channels}},
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: VestelConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities(VestelSensor(entry.runtime_data, desc) for desc in SENSORS)


class VestelSensor(VestelEntity, SensorEntity):
    entity_description: VestelSensorDescription

    def __init__(self, coordinator: VestelCoordinator, desc: VestelSensorDescription) -> None:
        super().__init__(coordinator, desc.key)
        self.entity_description = desc

    @property
    def available(self) -> bool:
        return self.coordinator.data is not None

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        fn = self.entity_description.attrs_fn
        return fn(self.coordinator.data) if fn else None
