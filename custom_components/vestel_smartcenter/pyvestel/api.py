"""Async client for Vestel/Telefunken SmartCenter TVs (MB100 DIAL v1 and v2)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
from collections.abc import Callable
from datetime import datetime

import aiohttp

from .discovery import fetch_device_info, ssdp_search
from .keys import resolve_key
from .models import (
    Channel,
    FollowTVResult,
    TimedObject,
    TVInfo,
    TVState,
    build_timed_xml,
    parse_added_id,
    parse_channel_list,
    parse_followtv,
    parse_program,
    parse_special_apps,
    parse_state,
    parse_timed_list,
    parse_volume,
)

_LOGGER = logging.getLogger(__name__)

WS_PORT_V2 = 7681
DEFAULT_TIMEOUT = 10.0
FOLLOWTV_TIMEOUT = 30.0

EventCallback = Callable[[str], None]


class VestelError(Exception):
    """Communication error."""


class VestelTV:
    """One TV. Keep a single instance per TV (v2 answers arrive on the websocket)."""

    def __init__(self, session: aiohttp.ClientSession, info: TVInfo) -> None:
        if not info.apps_url:
            raise ValueError("TVInfo.apps_url is required")
        self._session = session
        self.info = info
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._ws_task: asyncio.Task[None] | None = None
        self._responses: asyncio.Queue[str] = asyncio.Queue()
        self._lock = asyncio.Lock()
        self._listeners: list[EventCallback] = []
        self._closing = False

    # ------------------------------------------------------------------ setup
    @classmethod
    async def from_host(
        cls,
        session: aiohttp.ClientSession,
        host: str,
        location: str | None = None,
        timeout: float = 4.0,
    ) -> VestelTV:
        if location is None:
            found = await ssdp_search(timeout, target=host)
            location = found.get(host)
            if location is None:
                raise VestelError(f"No DIAL answer from {host} (TV off or not a SmartCenter TV?)")
        try:
            info = await fetch_device_info(session, host, location)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise VestelError(f"dd.xml not reachable: {err}") from err
        if not info.apps_url:
            raise VestelError("TV did not send an Application-URL header")
        return cls(session, info)

    @property
    def host(self) -> str:
        return self.info.host

    @property
    def connected(self) -> bool:
        return self._ws is not None and not self._ws.closed

    def add_listener(self, cb: EventCallback) -> Callable[[], None]:
        self._listeners.append(cb)
        return lambda: self._listeners.remove(cb)

    async def connect(self) -> None:
        """Open the websocket (status events; for v2 also command responses)."""
        if self.connected:
            return
        self._closing = False
        if self.info.is_v2:
            url = f"ws://{self.host}:{WS_PORT_V2}/"
        else:
            url = (await self._get("vr/getwebsocketlink") or "").strip()
            if not url.startswith("ws"):
                raise VestelError("vr/getwebsocketlink returned no websocket URL")
        try:
            self._ws = await self._session.ws_connect(
                url,
                headers={
                    "Origin": f"http://{self.host}",
                    "Pragma": "no-cache",
                    "Cache-Control": "no-cache",
                },
                timeout=aiohttp.ClientWSTimeout(ws_close=5),
                heartbeat=None,
                autoping=True,
            )
        except (aiohttp.ClientError, TimeoutError) as err:
            raise VestelError(f"websocket {url}: {err}") from err
        self._ws_task = asyncio.create_task(self._ws_reader(self._ws), name=f"vestel-ws-{self.host}")

    async def disconnect(self) -> None:
        self._closing = True
        if self._ws is not None:
            await self._ws.close()
        if self._ws_task is not None:
            self._ws_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._ws_task
        self._ws = None
        self._ws_task = None

    async def _ws_reader(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        try:
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                text: str = msg.data
                _LOGGER.debug("ws <- %s", text)
                if "<event>" in text:
                    self._emit(text.replace("<event>", "").replace("</event>", "").strip())
                elif self.info.is_v2:
                    self._responses.put_nowait(text)
                else:
                    self._emit(text.strip())
        except Exception:
            _LOGGER.debug("websocket reader stopped", exc_info=True)
        finally:
            if not self._closing:
                self._emit("__disconnected__")

    def _emit(self, event: str) -> None:
        for cb in list(self._listeners):
            try:
                cb(event)
            except Exception:
                _LOGGER.exception("listener failed")

    # -------------------------------------------------------------- transport
    async def _get(self, path: str, timeout: float = DEFAULT_TIMEOUT) -> str | None:
        url = f"{self.info.apps_url}{path}"
        try:
            async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                return (await r.text()).strip() if r.status == 200 else None
        except (aiohttp.ClientError, TimeoutError) as err:
            raise VestelError(f"GET {url}: {err}") from err

    async def _post(self, path: str, body: str, timeout: float = DEFAULT_TIMEOUT) -> str | None:
        url = f"{self.info.apps_url}{path}"
        try:
            async with self._session.post(
                url,
                data=body.encode("utf-8"),
                headers={"Content-Type": "text/plain; charset=ISO-8859-1"},
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as r:
                return (await r.text()).strip() if r.status in (200, 201) else None
        except (aiohttp.ClientError, TimeoutError) as err:
            raise VestelError(f"POST {url}: {err}") from err

    async def _v2(self, body: str, expect: bool, timeout: float) -> str | None:
        async with self._lock:
            while not self._responses.empty():
                self._responses.get_nowait()
            http_body = await self._post("SmartCenter", body)
            if not expect:
                return http_body
            if not self.connected:
                await self.connect()
            try:
                return await asyncio.wait_for(self._responses.get(), timeout)
            except TimeoutError:
                return http_body or None

    async def _query(self, v1_path: str, v2_cmd: str, timeout: float = DEFAULT_TIMEOUT) -> str | None:
        if self.info.is_v2:
            return await self._v2(f"<command>{v2_cmd}</command>", True, timeout)
        return await self._get(v1_path, timeout)

    async def _action(
        self, v1_path: str, xml: str, expect: bool = False, timeout: float = DEFAULT_TIMEOUT
    ) -> str | None:
        """xml without declaration; v1 uses '<?xml version="1.0" ?>', v2 single quotes."""
        if self.info.is_v2:
            return await self._v2(f"<?xml version='1.0' ?>{xml}", expect, timeout)
        return await self._post(v1_path, f'<?xml version="1.0" ?>{xml}', timeout)

    # ---------------------------------------------------------------- queries
    async def get_channels(self) -> list[Channel]:
        return parse_channel_list(await self._query("vr/activechannellist", "activechannellist", 45))

    async def get_current_channel(self) -> Channel | None:
        return parse_program(await self._query("vr/program", "program"))

    async def get_volume(self) -> int | None:
        return parse_volume(await self._query("vr/volume", "volume"))

    async def get_state(self) -> TVState | None:
        return parse_state(await self._query("vr/tvstate", "tvstate"))

    async def get_reminders(self) -> list[TimedObject]:
        return parse_timed_list(await self._query("vr/timerlist", "timerlist", 20), "timer")

    async def get_recordings(self) -> list[TimedObject]:
        return parse_timed_list(await self._query("vr/recordlist", "recordlist", 20), "record")

    async def get_special_apps(self) -> dict[str, bool]:
        return parse_special_apps(await self._query("vr/specialportalapps", "specialportalapps"))

    async def get_portal_url(self) -> str | None:
        if not self.info.is_v2:
            return None
        return await self._v2("<?xml version='1.0' ?><command>getportalurl</command>", True, 10)

    async def ping(self) -> bool:
        """True if the TV's DIAL server answers (TV on or in networked standby)."""
        if not self.info.location:
            return self.connected
        try:
            async with self._session.get(self.info.location, timeout=aiohttp.ClientTimeout(total=4)) as r:
                return r.status == 200
        except (aiohttp.ClientError, TimeoutError):
            return False

    # ---------------------------------------------------------------- actions
    async def send_key(self, key: str | int) -> None:
        code = resolve_key(key)
        if self.info.is_v2:
            await self._v2(f"<?xml version='1.0' ?><remote><key code='{code}'/></remote>", False, 5)
        else:
            await self._post("vr/remote", f'<?xml version="1.0" ?><remote><key code="{code}"/></remote>')

    async def send_text(self, text: str) -> None:
        for ch in text:
            code = {"\n": 10, "\b": 8}.get(ch, ord(ch))
            await self._action("vr/keyboard", f"<keyboard><key value='{code}'/></keyboard>")

    async def set_volume(self, level: int) -> None:
        level = max(0, min(100, int(level)))
        await self._action("vr/setvolume", f"<setvolume><volume level='{level}' /></setvolume>")

    async def set_channel(self, rsn: int) -> None:
        await self._action(
            "vr/setcurrentchannel", f"<setcurrentchannel><channel rsn='{int(rsn)}'/></setcurrentchannel>"
        )

    async def open_url(self, url: str) -> str | None:
        safe = url.replace("'", "%27")
        return await self._action(
            "vr/browserseturl", f"<browserseturl><load url='{safe}'/></browserseturl>", expect=True
        )

    async def set_eco_mode(self, mode: str) -> None:
        if mode not in ("low", "medium", "high", "auto", "off", "eco"):
            raise ValueError("mode must be low|medium|high|auto|off|eco")
        await self._action("vr/setdimmode", f"<setdimmode><dim_mode dim_value='{mode}'/></setdimmode>")

    async def mouse(self, dx: int, dy: int, button: int = 0) -> None:
        await self._action(
            "vr/mouseevent",
            f"<mouseevent><event_data dx='{int(dx)}' dy='{int(dy)}' button='{int(button)}'/></mouseevent>",
        )

    async def rename_channel(self, old_rsn: int, old_name: str, new_rsn: int, new_name: str) -> None:
        await self._action(
            "vr/changechannelinfo",
            f"<changechannelinfo><old_service rsn='{old_rsn}' channel_name='{old_name}'/>"
            f"<new_service rsn='{new_rsn}' channel_name='{new_name}'/></changechannelinfo>",
            expect=True,
        )

    async def show_pin(self, timeout_s: int = 30) -> None:
        if self.info.is_v2:
            await self._v2(
                f"<?xml version='1.0' ?><smartpindisplay><key timeout='{int(timeout_s)}'/></smartpindisplay>",
                False,
                5,
            )

    async def add_timed(
        self,
        kind: str,
        channel: Channel,
        name: str,
        start: datetime,
        end: datetime,
        repeat: str = "o",
    ) -> str | None:
        """kind: 'timer' (reminder) or 'record'. Returns the new id (if reported)."""
        tag = "addtimer" if kind == "timer" else "addrecord"
        body = build_timed_xml(tag, channel, name, start, end, repeat)
        if self.info.is_v2:
            resp = await self._v2(body, True, 20)
        else:
            resp = await self._post(f"vr/{tag}", body, 20)
        return parse_added_id(resp)

    async def delete_timed(self, kind: str, item_id: int) -> bool:
        if kind == "timer":
            xml = f"<deletetimer><timer id='{int(item_id)}'/></deletetimer>"
            path = "vr/deletetimer"
        else:
            xml = f"<deleterecord><record id='{int(item_id)}'/></deleterecord>"
            path = "vr/deleterecord"
        return await self._action(path, xml, expect=True) is not None

    async def start_followtv(self) -> FollowTVResult | None:
        raw = await self._query("vr/startfollowtv", "startfollowtv", FOLLOWTV_TIMEOUT)
        return parse_followtv(raw)

    async def stop_followtv(self) -> None:
        if self.info.is_v2:
            # The app sends nothing here (bug); this mirrors the v1 command name.
            await self._v2("<command>stopfollowtv</command>", False, 5)
        else:
            await self._get("vr/stopfollowtv")

    # -------------------------------------------------------------- power on
    def wake_on_lan(self, broadcast: str = "255.255.255.255") -> None:
        mac = (self.info.mac or "").replace(":", "").replace("-", "")
        if len(mac) != 12:
            raise VestelError("MAC unknown, cannot send WOL")
        packet = b"\xff" * 6 + bytes.fromhex(mac) * 16
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            for target in ((broadcast, 9), (self.host, 50), (self.host, 9)):
                with contextlib.suppress(OSError):
                    s.sendto(packet, target)
