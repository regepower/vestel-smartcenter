"""FollowTV: RTSP(-over-HTTP) client, stream decryption and fan-out.

Findings from the app's patched libVLC (modules/demux/mpeg/ts.c, Vestel build):
* The TV streams MPEG-TS in 192-byte packets (4-byte prefix + 188-byte TS packet).
* Encrypted streams: the URL is "<rtsp-url>|k1|k2|k3|k4" (4x 8 hex digits).
  key = ror2(k4) + ror2(k2) + ror2(k3) + ror2(k1)   (each 32-bit big endian, rotate right 2)
  -> AES-128. Every 192-byte packet is AES-ECB processed in 12 blocks and the first
  4 bytes are dropped. MB100: AES *decrypt* ("/decrypt" flag), MB90: AES *encrypt*.
* The app plays MB100 streams with ":rtsp-http :rtsp-http-port=8554" (RTSP tunnelled
  over HTTP, Apple/QuickTime style, port 8554).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

_LOGGER = logging.getLogger(__name__)

PKT = 192
TS = 188
SYNC = 0x47
USER_AGENT = "LibVLC/2.1.0 (LIVE555 Streaming Media v2014.01.21)"


# ------------------------------------------------------------------ key / url
def _ror2(word_hex: str) -> str:
    val = int.from_bytes(bytes.fromhex(word_hex.zfill(8)[:8]), "big")
    val = ((val >> 2) & 0x3FFFFFFF) | ((val & 3) << 30)
    return f"{val:08x}"


@dataclass(slots=True)
class StreamTarget:
    url: str
    key: bytes | None = None


def parse_stream_url(raw: str) -> StreamTarget:
    """Split '<url>|k1|k2|k3|k4' into url + AES key (as FollowmeTVFragment does)."""
    parts = raw.strip().split("|")
    if len(parts) == 5:
        key_hex = _ror2(parts[4]) + _ror2(parts[2]) + _ror2(parts[3]) + _ror2(parts[1])
        return StreamTarget(parts[0], bytes.fromhex(key_hex))
    return StreamTarget(parts[0], None)


# ----------------------------------------------------------------- decryptor
class TSRepacker:
    """Turns the TV payload (192-byte packets, optionally AES) into plain 188-byte TS."""

    def __init__(self, key: bytes | None = None, legacy_encrypt: bool = False) -> None:
        self._key = key
        self._buf = bytearray()
        self._aligned = False
        self._bad = 0
        self.mode: str | None = None  # "aes", "m2ts", "ts"
        if key is not None:
            cipher = Cipher(algorithms.AES(key), modes.ECB())  # noqa: S305 - protocol uses ECB
            self._ctx = cipher.encryptor() if legacy_encrypt else cipher.decryptor()
            self.mode = "aes"

    def _aes(self, data: bytes) -> bytes:
        return self._ctx.update(data)

    def _detect_plain(self) -> bool:
        b = self._buf
        for off in range(min(len(b), PKT)):
            if len(b) < off + 3 * PKT + 8:
                return False
            if all(b[off + 4 + i * PKT] == SYNC for i in range(3)):
                self.mode = "m2ts"
                del b[:off]
                return True
            if all(b[off + i * TS] == SYNC for i in range(4)):
                self.mode = "ts"
                del b[:off]
                return True
        return False

    def _align_aes(self) -> bool:
        b = self._buf
        if len(b) < 3 * PKT + PKT:
            return False
        for off in range(PKT):
            if all(self._aes(bytes(b[off + i * PKT : off + i * PKT + 16]))[4] == SYNC for i in range(3)):
                del b[:off]
                return True
        del b[: len(b) - 3 * PKT]  # keep tail, try again later
        return False

    def reset_alignment(self) -> None:
        """Call at RTP payload boundaries if you know a payload starts on a packet."""
        self._aligned = False

    def feed(self, data: bytes) -> bytes:
        self._buf += data
        out = bytearray()
        if self._key is None:
            if self.mode is None and not self._detect_plain():
                return b""
            if self.mode == "ts":
                n = len(self._buf) - len(self._buf) % TS
                out += self._buf[:n]
                del self._buf[:n]
                return bytes(out)
            n = len(self._buf) - len(self._buf) % PKT
            for i in range(0, n, PKT):
                out += self._buf[i + 4 : i + PKT]
            del self._buf[:n]
            return bytes(out)

        if not self._aligned:
            if not self._align_aes():
                return b""
            self._aligned = True
        n = len(self._buf) - len(self._buf) % PKT
        if n == 0:
            return b""
        plain = self._aes(bytes(self._buf[:n]))
        del self._buf[:n]
        for i in range(0, n, PKT):
            pkt = plain[i + 4 : i + PKT]
            if pkt[0] != SYNC:
                self._bad += 1
                if self._bad > 8:
                    _LOGGER.debug("lost AES packet alignment, resyncing")
                    self._aligned = False
                    self._bad = 0
                continue
            self._bad = 0
            out += pkt
        return bytes(out)


# --------------------------------------------------------------- RTSP client
class RtspError(Exception):
    pass


@dataclass(slots=True)
class _Resp:
    status: int
    headers: dict[str, str]
    body: bytes


class RtspSource:
    """Minimal RTSP client (RTP over TCP), optionally tunnelled over HTTP."""

    def __init__(self, url: str, http_port: int | None = 8554, timeout: float = 10.0) -> None:
        self.url = url
        self.http_port = http_port
        self.timeout = timeout
        u = urlsplit(url)
        if u.scheme not in ("rtsp", "rtspt"):
            raise RtspError(f"not an rtsp url: {url}")
        self._host = u.hostname or ""
        self._port = u.port or 554
        self._path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        self._cseq = 0
        self._session: str | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None  # where requests go
        self._extra_writers: list[asyncio.StreamWriter] = []
        self._tunnel = False
        self._keepalive: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[_Resp]] = {}
        self._control_url = url
        self._reading = False

    # transport -------------------------------------------------------------
    async def _open_http_tunnel(self) -> None:
        assert self.http_port is not None
        cookie = secrets.token_hex(11)
        common = (
            f"User-Agent: {USER_AGENT}\r\nx-sessioncookie: {cookie}\r\nPragma: no-cache\r\nCache-Control: no-cache\r\n"
        )
        r, w = await asyncio.wait_for(asyncio.open_connection(self._host, self.http_port), self.timeout)
        w.write(
            f"GET {self._path} HTTP/1.1\r\nCSeq: 1\r\n{common}Accept: application/x-rtsp-tunnelled\r\n\r\n".encode()
        )
        await w.drain()
        status = await asyncio.wait_for(r.readline(), self.timeout)
        if b" 200" not in status:
            w.close()
            raise RtspError(f"HTTP tunnel GET refused: {status!r}")
        while (await asyncio.wait_for(r.readline(), self.timeout)).strip():
            pass
        _, pw = await asyncio.wait_for(asyncio.open_connection(self._host, self.http_port), self.timeout)
        pw.write(
            f"POST {self._path} HTTP/1.1\r\nCSeq: 1\r\n{common}"
            "Content-Type: application/x-rtsp-tunnelled\r\nContent-Length: 32767\r\n"
            "Expires: Sun, 9 Jan 1972 00:00:00 GMT\r\n\r\n".encode()
        )
        await pw.drain()
        self._reader, self._writer = r, pw
        self._extra_writers = [w]
        self._tunnel = True

    async def _open_tcp(self) -> None:
        r, w = await asyncio.wait_for(asyncio.open_connection(self._host, self._port), self.timeout)
        self._reader, self._writer = r, w
        self._tunnel = False

    async def _send(self, method: str, url: str, headers: dict[str, str] | None = None) -> _Resp:
        assert self._writer is not None
        self._cseq += 1
        lines = [f"{method} {url} RTSP/1.0", f"CSeq: {self._cseq}", f"User-Agent: {USER_AGENT}"]
        if self._session:
            lines.append(f"Session: {self._session}")
        lines += [f"{k}: {v}" for k, v in (headers or {}).items()]
        msg = ("\r\n".join(lines) + "\r\n\r\n").encode()
        _LOGGER.debug("rtsp -> %s", msg)
        fut: asyncio.Future[_Resp] = asyncio.get_running_loop().create_future()
        self._pending[self._cseq] = fut
        self._writer.write(base64.b64encode(msg) if self._tunnel else msg)
        await self._writer.drain()
        if self._reading:
            return await asyncio.wait_for(fut, self.timeout)
        # before PLAY: read synchronously
        while not fut.done():
            item = await asyncio.wait_for(self._read_item(), self.timeout)
            if isinstance(item, _Resp):
                self._dispatch(item)
        return fut.result()

    def _dispatch(self, resp: _Resp) -> None:
        cseq = int(resp.headers.get("cseq", "0") or 0)
        fut = self._pending.pop(cseq, None)
        if fut and not fut.done():
            fut.set_result(resp)

    async def _read_item(self) -> _Resp | tuple[int, bytes]:
        r = self._reader
        assert r is not None
        first = await r.readexactly(1)
        if first == b"$":
            hdr = await r.readexactly(3)
            length = int.from_bytes(hdr[1:3], "big")
            return hdr[0], await r.readexactly(length)
        line = first + await r.readline()
        while not line.strip():  # stray CRLF
            line = await r.readline()
            if not line:
                raise RtspError("connection closed")
        if not line.startswith(b"RTSP/"):
            raise RtspError(f"unexpected data: {line[:60]!r}")
        status = int(line.split()[1])
        headers: dict[str, str] = {}
        while True:
            h = (await r.readline()).decode(errors="replace").strip()
            if not h:
                break
            k, _, v = h.partition(":")
            headers[k.strip().lower()] = v.strip()
        body = await r.readexactly(int(headers.get("content-length", "0") or 0))
        _LOGGER.debug("rtsp <- %s %s", status, headers)
        return _Resp(status, headers, body)

    # session ---------------------------------------------------------------
    async def start(self) -> None:
        errors: list[str] = []
        modes_: list[str] = (["http"] if self.http_port else []) + ["tcp"]
        for mode in modes_:
            try:
                await (self._open_http_tunnel() if mode == "http" else self._open_tcp())
                await self._handshake()
                _LOGGER.info("FollowTV RTSP via %s established", mode)
                return
            except (OSError, TimeoutError, RtspError, asyncio.IncompleteReadError) as err:
                errors.append(f"{mode}: {err!r}")
                await self.close()
        raise RtspError("; ".join(errors))

    async def _handshake(self) -> None:
        self._cseq = 0
        self._session = None
        await self._send("OPTIONS", self.url)
        d = await self._send("DESCRIBE", self.url, {"Accept": "application/sdp"})
        if d.status != 200:
            raise RtspError(f"DESCRIBE {d.status}")
        base = d.headers.get("content-base") or d.headers.get("content-location") or self.url
        self._control_url = self._pick_control(d.body.decode(errors="replace"), base)
        s = await self._send("SETUP", self._control_url, {"Transport": "RTP/AVP/TCP;unicast;interleaved=0-1"})
        if s.status != 200:
            raise RtspError(f"SETUP {s.status}")
        self._session = s.headers.get("session", "").split(";")[0] or None
        timeout = 60
        for part in s.headers.get("session", "").split(";")[1:]:
            if part.strip().startswith("timeout="):
                with contextlib.suppress(ValueError):
                    timeout = int(part.split("=")[1])
        p = await self._send("PLAY", base, {"Range": "npt=0.000-"})
        if p.status != 200:
            raise RtspError(f"PLAY {p.status}")
        self._reading = True
        self._keepalive = asyncio.create_task(self._keep_alive(max(10, timeout // 2)))

    @staticmethod
    def _pick_control(sdp: str, base: str) -> str:
        base_dir = base + "/" if not base.endswith("/") else base
        media_ctrl: str | None = None
        in_media = False
        for raw in sdp.splitlines():
            line = raw.strip()
            if line.startswith("m="):
                if media_ctrl is not None:
                    break
                in_media = True
            elif line.startswith("a=control:") and in_media:
                media_ctrl = line[10:].strip()
        if not media_ctrl or media_ctrl == "*":
            return base
        if media_ctrl.startswith("rtsp://"):
            return media_ctrl
        return urljoin(base_dir, media_ctrl)

    async def _keep_alive(self, interval: int) -> None:
        while True:
            await asyncio.sleep(interval)
            with contextlib.suppress(Exception):
                await self._send("OPTIONS", self.url)

    async def payloads(self) -> AsyncIterator[bytes]:
        """Yield RTP payloads of channel 0 until the connection ends."""
        while True:
            try:
                item = await self._read_item()
            except (asyncio.IncompleteReadError, ConnectionError, RtspError) as err:
                _LOGGER.info("FollowTV stream ended: %r", err)
                return
            if isinstance(item, _Resp):
                self._dispatch(item)
                continue
            ch, data = item
            if ch != 0 or len(data) < 12:
                continue
            yield rtp_payload(data)

    async def close(self) -> None:
        if self._keepalive:
            self._keepalive.cancel()
            self._keepalive = None
        if self._writer is not None and self._session:
            with contextlib.suppress(Exception):
                self._cseq += 1
                msg = (
                    f"TEARDOWN {self.url} RTSP/1.0\r\nCSeq: {self._cseq}\r\nSession: {self._session}\r\n\r\n"
                ).encode()
                self._writer.write(base64.b64encode(msg) if self._tunnel else msg)
                await asyncio.wait_for(self._writer.drain(), 2)
        for w in [self._writer, *self._extra_writers]:
            if w is not None:
                with contextlib.suppress(Exception):
                    w.close()
        self._writer = None
        self._reader = None
        self._extra_writers = []
        self._session = None
        self._reading = False


def rtp_payload(pkt: bytes) -> bytes:
    b0 = pkt[0]
    off = 12 + 4 * (b0 & 0x0F)
    if b0 & 0x10 and len(pkt) >= off + 4:
        off += 4 + 4 * int.from_bytes(pkt[off + 2 : off + 4], "big")
    end = len(pkt)
    if b0 & 0x20 and end > off:
        end -= pkt[-1]
    return pkt[off:end]


# ---------------------------------------------------------------- fan-out
class TSBroadcaster:
    """One upstream, many HTTP clients."""

    def __init__(self) -> None:
        self._subs: set[asyncio.Queue[bytes | None]] = set()

    def subscribe(self) -> asyncio.Queue[bytes | None]:
        q: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=512)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[bytes | None]) -> None:
        self._subs.discard(q)

    @property
    def count(self) -> int:
        return len(self._subs)

    def publish(self, chunk: bytes | None) -> None:
        for q in list(self._subs):
            if q.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
            q.put_nowait(chunk)


async def stream_ts(raw_url: str, http_port: int | None = 8554, legacy_encrypt: bool = False) -> AsyncIterator[bytes]:
    """Connect to a FollowTV URL (as returned by the TV) and yield plain TS bytes."""
    target = parse_stream_url(raw_url)
    if target.url.startswith("http"):
        import aiohttp  # noqa: PLC0415

        rep = TSRepacker(target.key, legacy_encrypt)
        async with aiohttp.ClientSession() as s, s.get(target.url) as r:
            async for chunk in r.content.iter_chunked(PKT * 64):
                if out := rep.feed(chunk):
                    yield out
        return
    src = RtspSource(target.url, http_port)
    await src.start()
    rep = TSRepacker(target.key, legacy_encrypt)
    try:
        async for payload in src.payloads():
            if out := rep.feed(payload):
                yield out
    finally:
        await src.close()
