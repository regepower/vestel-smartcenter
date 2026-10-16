"""Calendar with the TV's reminders and scheduled recordings."""

from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .coordinator import VestelConfigEntry, VestelCoordinator
from .entity import VestelEntity
from .pyvestel import TimedObject

PARALLEL_UPDATES = 0
REPEAT = {"o": None, "d": timedelta(days=1), "w": timedelta(weeks=1)}
REPEAT_TEXT = {"o": "einmalig", "d": "täglich", "w": "wöchentlich"}


async def async_setup_entry(
    hass: HomeAssistant, entry: VestelConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([VestelTimerCalendar(entry.runtime_data)])


def _events(item: TimedObject, start: datetime, end: datetime) -> list[CalendarEvent]:
    if item.start is None:
        return []
    s = item.start
    e = item.end if item.end and item.end > s else s + timedelta(minutes=1)
    step = REPEAT.get(item.repeat or "o")
    prefix = "⏺ " if item.kind == "record" else "🔔 "
    desc_parts = [
        "Aufnahme" if item.kind == "record" else "Erinnerung",
        REPEAT_TEXT.get(item.repeat or "o", item.repeat or ""),
        f"ID {item.id}",
        *(f"{k}: {v}" for k, v in item.extra.items()),
    ]
    out: list[CalendarEvent] = []
    for _ in range(400):  # bounded expansion of repeating timers
        if s >= end:
            break
        if e > start:
            out.append(
                CalendarEvent(
                    start=dt_util.as_local(s),
                    end=dt_util.as_local(e),
                    summary=prefix + (item.name or "?"),
                    description=" · ".join(p for p in desc_parts if p),
                    uid=f"{item.kind}-{item.id}-{int(s.timestamp())}",
                )
            )
        if step is None:
            break
        s, e = s + step, e + step
    return out


class VestelTimerCalendar(VestelEntity, CalendarEntity):
    _attr_translation_key = "timers"

    def __init__(self, coordinator: VestelCoordinator) -> None:
        super().__init__(coordinator, "timers_calendar")

    def _all(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        d = self.coordinator.data
        if d is None:
            return []
        evs = [ev for t in (*d.recordings, *d.reminders) for ev in _events(t, start, end)]
        return sorted(evs, key=lambda ev: ev.start_datetime_local)

    @property
    def event(self) -> CalendarEvent | None:
        now = dt_util.utcnow()
        evs = self._all(now, now + timedelta(days=60))
        return evs[0] if evs else None

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        return self._all(dt_util.as_utc(start_date), dt_util.as_utc(end_date))
