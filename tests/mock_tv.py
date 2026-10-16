"""Mock Vestel SmartCenter TV for offline tests (models the reverse-engineered protocol)."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import struct

from aiohttp import web
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

CHANNELS = (
    "<active_list network='Astra'>"
    "<service rsn='1' name='Das Erste HD' handle='11' onid='1' tsid='1019' sid='10301' type='1'/>"
    "<service rsn='2' name='ZDF HD' handle='12' onid='1' tsid='1011' sid='11110' type='1'/>"
    "<service rsn='3' name='Rock & Pop' handle='13' onid='1' tsid='1' sid='5' type='2'/>"
    "</active_list>"
)
TIMERS = (
    "<timerlist><timer id='12345678' event_name='Tatort's Finale' start='2026/10/18-18:15' "
    "end='2026/10/18-19:45' repeat='o' service_name='Das Erste HD'/></timerlist>"
)
RECORDS = "<recordlist><record id='7' event_name='heute' start='2026/10/17-17:00' end='2026/10/17-17:20' repeat='w'/></recordlist>"
KEY_WORDS = ["0a1b2c3d", "11223344", "55667788", "99aabbcc"]


def derive_key(words: list[str]) -> bytes:
    def ror2(h: str) -> str:
        v = int(h, 16)
        return f"{((v >> 2) & 0x3FFFFFFF) | ((v & 3) << 30):08x}"

    k1, k2, k3, k4 = words
    return bytes.fromhex(ror2(k4) + ror2(k2) + ror2(k3) + ror2(k1))


def make_ts(n: int, start_cc: int = 0) -> list[bytes]:
    pkts = []
    for i in range(n):
        cc = (start_cc + i) & 0x0F
        hdr = bytes([0x47, 0x01, 0x00, 0x10 | cc])
        pkts.append(hdr + bytes([i & 0xFF]) * 184)
    return pkts


class MockTV:
    def __init__(self, v2: bool, encrypted: bool, http_port: int, ws_port: int, rtsp_port: int, tunnel_port: int):
        self.v2 = v2
        self.encrypted = encrypted
        self.http_port = http_port
        self.ws_port = ws_port
        self.rtsp_port = rtsp_port
        self.tunnel_port = tunnel_port
        self.keys: list[str] = []
        self.posts: list[str] = []
        self.volume = 20
        self.ws_clients: set[web.WebSocketResponse] = set()
        self._runners: list[web.AppRunner] = []
        self._servers: list[asyncio.base_events.Server] = []
        self._tunnels: dict[str, asyncio.StreamWriter] = {}
        self.rtsp_methods: list[str] = []

    # ---------------------------------------------------------------- answers
    def answer(self, cmd: str) -> str | None:
        follow = f"rtsp://127.0.0.1:{self.rtsp_port}/followtv"
        if self.encrypted:
            follow += "|" + "|".join(KEY_WORDS)
        return {
            "activechannellist": CHANNELS,
            "program": "<service rsn='2' name='ZDF HD' handle='12' onid='1' tsid='1011' sid='11110' type='1'/>",
            "volume": f"<volume level={self.volume}",
            "tvstate": "<tv_state value='EPG'/>",
            "timerlist": TIMERS,
            "recordlist": RECORDS,
            "specialportalapps": "Netflix:1|PVR:1|Skype:0",
            "startfollowtv": f"followtv_started_status:0_url:{follow}",
        }.get(cmd)

    def handle_body(self, body: str) -> str | None:
        self.posts.append(body)
        if "<remote>" in body:
            self.keys.append(body.split("code=")[1][1:5])
            return None
        if "<setvolume>" in body:
            self.volume = int(body.split("level='")[1].split("'", maxsplit=1)[0])
            return None
        if "<addtimer>" in body or "<addrecord>" in body:
            return "<timer id='87654321'/>"
        if "<deletetimer>" in body or "<deleterecord>" in body:
            return "ok"
        if "<browserseturl>" in body:
            return "open_browser_status:1"
        return None

    # ---------------------------------------------------------------- http
    async def dd(self, request: web.Request) -> web.Response:
        xml = (
            "<root><locale><name>Telefunken Mock</name><tv_version>mb100</tv_version>"
            "<software_version>V1.2.3</software_version><mac>00:11:22:33:44:55</mac>"
            f"<dial_version>{'2.0' if self.v2 else '1.0'}</dial_version></locale></root>"
        )
        return web.Response(text=xml, headers={"Application-URL": f"http://127.0.0.1:{self.http_port}/apps/"})

    async def v1_get(self, request: web.Request) -> web.Response:
        cmd = request.match_info["cmd"]
        if cmd == "getwebsocketlink":
            return web.Response(text=f"ws://127.0.0.1:{self.ws_port}/")
        ans = self.answer(cmd)
        return web.Response(text=ans or "", status=200 if ans is not None or cmd == "stopfollowtv" else 404)

    async def v1_post(self, request: web.Request) -> web.Response:
        return web.Response(text=self.handle_body(await request.text()) or "OK")

    async def v2_post(self, request: web.Request) -> web.Response:
        body = await request.text()
        ans = self.answer(body[9:-10]) if body.startswith("<command>") else self.handle_body(body)
        if ans is not None:
            for ws in list(self.ws_clients):
                await ws.send_str(ans)
        return web.Response(text="")

    async def ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.ws_clients.add(ws)
        await ws.send_str("<event><tv_state value='DEFAULT'/></event>" if self.v2 else "keyboard_open_status:0")
        try:
            async for _ in ws:
                pass
        finally:
            self.ws_clients.discard(ws)
        return ws

    async def push_event(self, text: str) -> None:
        for ws in list(self.ws_clients):
            await ws.send_str(f"<event>{text}</event>" if self.v2 else text)

    # ---------------------------------------------------------------- rtsp
    def payloads(self, n_rtp: int) -> list[bytes]:
        cipher = Cipher(algorithms.AES(derive_key(KEY_WORDS)), modes.ECB())
        out, cc, seq = [], 0, 0
        for _ in range(n_rtp):
            ts = make_ts(7, cc)
            cc += 7
            data = b"".join(struct.pack(">I", 0x12345678) + p for p in ts)  # 192-byte packets
            if self.encrypted:
                data = cipher.encryptor().update(data)
            rtp = struct.pack(">BBHII", 0x80, 33, seq & 0xFFFF, seq * 3000, 0xDEADBEEF) + data
            seq += 1
            out.append(b"$\x00" + struct.pack(">H", len(rtp)) + rtp)
        return out

    async def _rtsp_dialog(self, reqs: asyncio.StreamReader | None, read_req, write) -> None:
        while True:
            req = await read_req()
            if req is None:
                return
            lines = req.split("\r\n")
            method = lines[0].split()[0]
            self.rtsp_methods.append(method)
            cseq = next(x.split(":", 1)[1].strip() for x in lines if x.lower().startswith("cseq"))
            hdr = f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\n"
            if method == "DESCRIBE":
                sdp = "v=0\r\ns=FollowTV\r\nm=video 0 RTP/AVP 33\r\na=control:track1\r\n"
                write(
                    (
                        hdr + f"Content-Base: rtsp://127.0.0.1:{self.rtsp_port}/followtv/\r\n"
                        f"Content-Type: application/sdp\r\nContent-Length: {len(sdp)}\r\n\r\n{sdp}"
                    ).encode()
                )
            elif method == "SETUP":
                write((hdr + "Session: ABCD;timeout=60\r\nTransport: RTP/AVP/TCP;interleaved=0-1\r\n\r\n").encode())
            elif method == "PLAY":
                write((hdr + "Session: ABCD\r\n\r\n").encode())
                for p in self.payloads(40):
                    write(p)
                    await asyncio.sleep(0)
            else:
                write((hdr + "\r\n").encode())

    async def _rtsp_tcp(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        async def read_req() -> str | None:
            data = await r.readuntil(b"\r\n\r\n") if not r.at_eof() else b""
            return data.decode() if data else None

        with contextlib.suppress(Exception):
            await self._rtsp_dialog(None, read_req, w.write)
        w.close()

    async def _tunnel(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        head = (await r.readuntil(b"\r\n\r\n")).decode()
        cookie = next(x.split(":", 1)[1].strip() for x in head.split("\r\n") if x.lower().startswith("x-sessioncookie"))
        if head.startswith("GET"):
            w.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/x-rtsp-tunnelled\r\n\r\n")
            self._tunnels[cookie] = w
            return
        out = self._tunnels[cookie]
        buf = b""

        async def read_req() -> str | None:
            nonlocal buf
            while True:
                try:
                    dec = base64.b64decode(buf, validate=True).decode()
                    if dec.endswith("\r\n\r\n"):
                        buf = b""
                        return dec
                except Exception:  # noqa: BLE001
                    pass
                chunk = await r.read(4096)
                if not chunk:
                    return None
                buf += chunk

        with contextlib.suppress(Exception):
            await self._rtsp_dialog(None, read_req, out.write)

    # ---------------------------------------------------------------- run
    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/dd.xml", self.dd)
        app.router.add_get("/apps/vr/{cmd}", self.v1_get)
        app.router.add_post("/apps/vr/{cmd}", self.v1_post)
        app.router.add_post("/apps/SmartCenter", self.v2_post)
        wsapp = web.Application()
        wsapp.router.add_get("/", self.ws)
        for a, port in ((app, self.http_port), (wsapp, self.ws_port)):
            runner = web.AppRunner(a)
            await runner.setup()
            await web.TCPSite(runner, "127.0.0.1", port).start()
            self._runners.append(runner)
        self._servers.append(await asyncio.start_server(self._rtsp_tcp, "127.0.0.1", self.rtsp_port))
        self._servers.append(await asyncio.start_server(self._tunnel, "127.0.0.1", self.tunnel_port))

    async def stop(self) -> None:
        for ws in list(self.ws_clients):
            await ws.close()
        for s in self._servers:
            s.close()
        for w in self._tunnels.values():
            w.close()
        for r in self._runners:
            await r.cleanup()
