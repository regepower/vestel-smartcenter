"""Vestel / Telefunken SmartCenter TV integration."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets

import voluptuous as vol
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.const import CONF_HOST, CONF_MAC, CONF_NAME, Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from .const import (
    CONF_APPS_URL,
    CONF_DIAL_VERSION,
    CONF_LOCATION,
    CONF_MODEL,
    CONF_RTSP_HTTP_PORT,
    CONF_SW_VERSION,
    CONF_TIMER_TZ,
    CONF_TOKEN,
    DEFAULT_RTSP_HTTP_PORT,
    DOMAIN,
    SERVICE_SEND_RAW,
)
from .coordinator import VestelConfigEntry, VestelCoordinator
from .pyvestel import FollowTVManager, TVInfo, VestelTV, fetch_device_info, is_smartcenter, mac_from_arp

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BUTTON,
    Platform.CALENDAR,
    Platform.CAMERA,
    Platform.MEDIA_PLAYER,
    Platform.REMOTE,
    Platform.SENSOR,
    Platform.SWITCH,
]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    hass.http.register_view(FollowTVView())

    async def send_raw(call: ServiceCall) -> ServiceResponse:
        """Debug: raw request to the TV (no entity target needed; first/only TV by default)."""
        entries = [e for e in hass.config_entries.async_loaded_entries(DOMAIN)]
        if not entries:
            raise ServiceValidationError("No Vestel SmartCenter TV configured")
        wanted = call.data.get("host")
        entry = next((e for e in entries if e.data[CONF_HOST] == wanted), None) if wanted else entries[0]
        if entry is None:
            raise ServiceValidationError(f"No TV with host {wanted}")
        tv = entry.runtime_data.tv
        return await tv.send_raw(call.data["path"].lstrip("/"), call.data.get("body"))

    hass.services.async_register(
        DOMAIN,
        SERVICE_SEND_RAW,
        send_raw,
        schema=vol.Schema(
            {vol.Required("path"): cv.string, vol.Optional("body"): cv.string, vol.Optional("host"): cv.string}
        ),
        supports_response=SupportsResponse.ONLY,
    )
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
    info.mac = entry.options.get(CONF_MAC) or info.mac or d.get(CONF_MAC) or mac_from_arp(info.host)
    if info.mac and not entry.data.get(CONF_MAC):
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_MAC: info.mac})
    if CONF_TOKEN not in entry.data:
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_TOKEN: secrets.token_urlsafe(16)})

    tv = VestelTV(session, info)
    if entry.options.get(CONF_TIMER_TZ, "utc") == "local":
        tv.tz = dt_util.get_default_time_zone()
    port = entry.options.get(CONF_RTSP_HTTP_PORT, DEFAULT_RTSP_HTTP_PORT)
    followtv = FollowTVManager(tv, http_port=port or None, enabled=False)  # only via switch/service
    coordinator = VestelCoordinator(hass, entry, tv, followtv)
    coordinator.local_stream = LocalStreamServer(followtv, _ffmpeg_binary(hass))
    await coordinator.local_stream.start()
    await coordinator.async_load_cache()
    coordinator.options_snapshot = dict(entry.options)
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_reload))
    # Never block HA startup on the TV (it may be off or half-asleep): refresh in the background.
    entry.async_create_background_task(hass, _refresh_info(hass, entry, tv), f"{DOMAIN}-refresh-{entry.entry_id}")
    return True


def _ffmpeg_binary(hass: HomeAssistant) -> str | None:
    """ffmpeg is used to transcode DVB audio (MP2/AC3) to AAC, the only audio HA's stream keeps."""
    try:
        from homeassistant.components.ffmpeg import get_ffmpeg_manager  # noqa: PLC0415

        return get_ffmpeg_manager(hass).binary
    except Exception:  # noqa: BLE001 - ffmpeg integration not loaded
        return None


async def _refresh_info(hass: HomeAssistant, entry: VestelConfigEntry, tv: VestelTV) -> None:
    """Refresh dd.xml (apps URL/port may change); repair entries made from a non-SmartCenter DIAL server."""
    session = async_get_clientsession(hass)
    d = entry.data
    info = tv.info
    fresh: TVInfo | None = None
    with contextlib.suppress(Exception):
        async with asyncio.timeout(20):
            if info.location:
                cand = await fetch_device_info(session, info.host, info.location)
                fresh = cand if is_smartcenter(cand) and cand.apps_url else None
            if fresh is None:
                fresh = (await VestelTV.from_host(session, info.host)).info
    if fresh is not None:
        repaired = not d.get(CONF_MODEL)
        fresh.name = (fresh.name or info.name) if repaired else (info.name or fresh.name)
        fresh.mac = entry.options.get(CONF_MAC) or fresh.mac or info.mac
        fresh.protocol = info.protocol
        tv.info = fresh
        new_data = {
            **entry.data,
            CONF_NAME: fresh.name,
            CONF_LOCATION: fresh.location,
            CONF_APPS_URL: fresh.apps_url,
            CONF_DIAL_VERSION: fresh.dial_version,
            CONF_MODEL: fresh.model,
            CONF_SW_VERSION: fresh.software_version,
            CONF_MAC: fresh.mac,
        }
        if new_data != dict(entry.data):
            hass.config_entries.async_update_entry(
                entry, title=(fresh.name or entry.title) if repaired else entry.title, data=new_data
            )
    await entry.runtime_data.async_refresh()


async def _reload(hass: HomeAssistant, entry: VestelConfigEntry) -> None:
    # Only option changes need a reload; data updates (refreshed dd.xml) are applied live.
    if dict(entry.options) != getattr(entry.runtime_data, "options_snapshot", None):
        await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: VestelConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        coord = entry.runtime_data
        await coord.followtv.stop()
        if coord.local_stream is not None:
            await coord.local_stream.stop()
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
        return await stream_to_response(request, entry.runtime_data.followtv)


async def stream_to_response(request: web.Request, mgr: FollowTVManager) -> web.StreamResponse:
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


class LocalStreamServer:
    """Loopback-only HTTP server for HA's stream worker (avoids HA auth/SSL/port issues).

    /followtv.ts is the plain decrypted TS, /followtv_aac.ts the same with audio as AAC
    (HA's stream drops MP2/AC3, which DVB channels use -> no sound otherwise)."""

    def __init__(self, mgr: FollowTVManager, ffmpeg: str | None = None) -> None:
        self._mgr = mgr
        self._ffmpeg = ffmpeg
        self._runner: web.AppRunner | None = None
        self.url: str | None = None
        self.raw_url: str | None = None

    async def start(self) -> None:
        app = web.Application()

        async def handle(request: web.Request) -> web.StreamResponse:
            return await stream_to_response(request, self._mgr)

        async def handle_aac(request: web.Request) -> web.StreamResponse:
            return await stream_aac_to_response(request, self._mgr, self._ffmpeg or "ffmpeg")

        app.router.add_get("/followtv.ts", handle)
        app.router.add_get("/followtv_aac.ts", handle_aac)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        self.raw_url = f"http://127.0.0.1:{port}/followtv.ts"
        self.url = f"http://127.0.0.1:{port}/followtv_aac.ts" if self._ffmpeg else self.raw_url

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None


FFMPEG_AAC_ARGS = (
    "-hide_banner", "-loglevel", "error", "-fflags", "+genpts+discardcorrupt",
    "-i", "pipe:0", "-map", "0:v:0", "-map", "0:a:0?",
    "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-ac", "2",
    "-f", "mpegts", "pipe:1",
)  # fmt: skip


async def stream_aac_to_response(request: web.Request, mgr: FollowTVManager, ffmpeg: str) -> web.StreamResponse:
    """Pipe the decrypted TS through ffmpeg: video copied, audio -> AAC."""
    q = mgr.subscribe()
    first = await _first_chunk(q)
    if first is None:
        mgr.unsubscribe(q)
        return web.Response(status=503, text=f"FollowTV not available: {mgr.last_error}")
    proc = await asyncio.create_subprocess_exec(
        ffmpeg, *FFMPEG_AAC_ARGS,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )  # fmt: skip
    assert proc.stdin is not None and proc.stdout is not None

    async def feed() -> None:
        stdin = proc.stdin
        assert stdin is not None
        try:
            chunk: bytes | None = first
            while chunk is not None:
                stdin.write(chunk)
                await stdin.drain()
                chunk = await q.get()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with contextlib.suppress(Exception):
                stdin.close()

    feeder = asyncio.create_task(feed())
    resp = web.StreamResponse(headers={"Content-Type": "video/mp2t", "Cache-Control": "no-cache"})
    try:
        await resp.prepare(request)
        while data := await proc.stdout.read(65536):
            await resp.write(data)
    except ConnectionResetError:
        pass
    finally:
        feeder.cancel()
        mgr.unsubscribe(q)
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        err = b""
        with contextlib.suppress(Exception):
            async with asyncio.timeout(2):
                err = (await proc.communicate())[1]
        if err:
            _LOGGER.debug("ffmpeg (FollowTV audio): %s", err.decode(errors="replace")[-500:])
    return resp


async def _first_chunk(q: asyncio.Queue[bytes | None]) -> bytes | None:
    try:
        return await asyncio.wait_for(q.get(), 40)
    except TimeoutError:
        return None
