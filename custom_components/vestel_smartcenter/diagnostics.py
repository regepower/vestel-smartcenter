"""Diagnostics download: device info + last raw exchanges with the TV."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_TOKEN
from .coordinator import VestelConfigEntry


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: VestelConfigEntry) -> dict[str, Any]:
    coord = entry.runtime_data
    tv = coord.tv
    d = coord.data
    return {
        "entry": async_redact_data(dict(entry.data), {CONF_TOKEN}),
        "options": dict(entry.options),
        "tv_info": asdict(tv.info),
        "is_v2": tv.info.is_v2,
        "ws_connected": tv.connected,
        "data": {
            "online": d.online if d else None,
            "state": d.state if d else None,
            "volume": d.volume if d else None,
            "channel": asdict(d.channel) if d and d.channel else None,
            "channel_count": len(d.channels) if d else 0,
        },
        "followtv": {
            "running": coord.followtv.running,
            "last_error": coord.followtv.last_error,
            "tv_response": coord.followtv.raw_response,
            "local_url": coord.local_stream.url if coord.local_stream else None,
            "stats": asdict(coord.followtv.stats),
        },
        "history": list(tv.history),
    }
