"""Vestel / Telefunken SmartCenter TV integration."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.const import CONF_HOST, CONF_MAC, CONF_NAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_APPS_URL,
    CONF_DIAL_VERSION,
    CONF_LOCATION,
    CONF_MODEL,
    CONF_RTSP_HTTP_PORT,
    CONF_SW_VERSION,
    CONF_TOKEN,
    DEFAULT_RTSP_HTTP_PORT,
    DOMAIN,
)
from .coordinator import VestelConfigEntry, VestelCoordinator
from .pyvestel import FollowTVManager, TVInfo, VestelTV, fetch_device_info, is_smartcenter, mac_from_arp

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.MEDIA_PLAYER, Platform.REMOTE, Platform.SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    hass.http.register_view(FollowTVView())
    return True


async def async_setup_entry(hass: HomeAssistant, entry: VestelConfigEntry) -> bool:
    session = async_get_clientsession(hass)
    d = entry.data
    info = TVInfo(
        host=d[CONF_HOST],
        name=d.get(CONF_NAME),
        model=d.get(CONF_MODEL),
        software_version=d.get(CONF_SW_VERSION),
        mac=d.get(CONF_MAC),
        dial_version=d.get(CONF_DIAL_VERSION, "1"),
        apps_url=d[CONF_APPS_URL],
        location=d.get(CONF_LOCATION),
    )
    # Refresh dd.xml if the TV is on (apps URL / port may change). If the stored server is not the
    # SmartCenter one (e.g. the Vewd browser's DIAL server), re-discover all DIAL servers of the host.
    fresh: TVInfo | None = None
    with contextlib.suppress(Exception):
        if info.location:
            cand = await fetch_device_info(session, info.host, info.location)
            fresh = cand if is_smartcenter(cand) and cand.apps_url else None
        if fresh is None:
            fresh = (await VestelTV.from_host(session, info.host)).info
    if fresh is not None:
        repaired = not d.get(CONF_MODEL)  # entry was created from a non-SmartCenter DIAL server
        if not repaired:
            fresh.name = info.name or fresh.name
        info = fresh
        hass.config_entries.async_update_entry(
            entry,
            title=(fresh.name or entry.title) if repaired else entry.title,
            data={
                **({CONF_NAME: fresh.name} if repaired and fresh.name else {}),
                **entry.data,
                CONF_LOCATION: fresh.location,
                CONF_APPS_URL: fresh.apps_url,
                CONF_DIAL_VERSION: fresh.dial_version,
                CONF_MODEL: fresh.model,
                CONF_SW_VERSION: fresh.software_version,
                CONF_MAC: fresh.mac or d.get(CONF_MAC),
            },
        )
    info.mac = entry.options.get(CONF_MAC) or info.mac or d.get(CONF_MAC) or mac_from_arp(info.host)
    if info.mac and not entry.data.get(CONF_MAC):
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_MAC: info.mac})
    if CONF_TOKEN not in entry.data:
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_TOKEN: secrets.token_urlsafe(16)})

    tv = VestelTV(session, info)
    port = entry.options.get(CONF_RTSP_HTTP_PORT, DEFAULT_RTSP_HTTP_PORT)
    followtv = FollowTVManager(tv, http_port=port or None)
    coordinator = VestelCoordinator(hass, entry, tv, followtv)
    await coordinator.async_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_reload))
    return True


async def _reload(hass: HomeAssistant, entry: VestelConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: VestelConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        coord = entry.runtime_data
        await coord.followtv.stop()
        await coord.tv.disconnect()
    return ok


class FollowTVView(HomeAssistantView):
    """Serves the decrypted FollowTV stream as MPEG-TS (open it in VLC)."""

    url = "/api/vestel_smartcenter/followtv/{entry_id}.ts"
    name = "api:vestel_smartcenter:followtv"
    requires_auth = False  # protected by a per-entry token (VLC cannot send HA auth headers)

    async def get(self, request: web.Request, entry_id: str) -> web.StreamResponse:
        hass: HomeAssistant = request.app["hass"]
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN or not hasattr(entry, "runtime_data"):
            return web.Response(status=404)
        token = entry.data.get(CONF_TOKEN)
        if not token or not secrets.compare_digest(request.query.get("token", ""), token):
            return web.Response(status=401)
        mgr: FollowTVManager = entry.runtime_data.followtv
        q = mgr.subscribe()
        first = await _first_chunk(q)
        if first is None:
            mgr.unsubscribe(q)
            return web.Response(status=503, text=f"FollowTV not available: {mgr.last_error}")
        resp = web.StreamResponse(headers={"Content-Type": "video/mp2t", "Cache-Control": "no-cache"})
        await resp.prepare(request)
        try:
            await resp.write(first)
            while (chunk := await q.get()) is not None:
                await resp.write(chunk)
        except ConnectionResetError:
            pass
        finally:
            mgr.unsubscribe(q)
        return resp


async def _first_chunk(q: asyncio.Queue[bytes | None]) -> bytes | None:
    try:
        return await asyncio.wait_for(q.get(), 40)
    except TimeoutError:
        return None
