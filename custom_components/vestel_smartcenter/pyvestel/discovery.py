"""DIAL/SSDP discovery of Vestel SmartCenter TVs (MB100 family)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
from typing import Any

import aiohttp

from .models import TVInfo, parse_dd_xml

_LOGGER = logging.getLogger(__name__)

SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900
DIAL_ST = "urn:dial-multiscreen-org:service:dial:1"
M_SEARCH = (
    f'M-SEARCH * HTTP/1.1\r\nHOST: {SSDP_ADDR}:{SSDP_PORT}\r\nMAN: "ssdp:discover"\r\nMX: 3\r\nST: {DIAL_ST}\r\n\r\n'
).encode()


def _parse_ssdp(data: bytes) -> dict[str, str]:
    lines = data.decode(errors="replace").split("\r\n")
    hdr: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            hdr[k.strip().upper()] = v.strip()
    return hdr


class _Collector(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.found: dict[str, str] = {}  # ip -> LOCATION

    def datagram_received(self, data: bytes, addr: tuple[str | Any, int]) -> None:
        if not data.startswith(b"HTTP/1.1 200"):
            return
        hdr = _parse_ssdp(data)
        if hdr.get("ST") == DIAL_ST and "LOCATION" in hdr:
            self.found.setdefault(addr[0], hdr["LOCATION"])


async def ssdp_search(timeout: float = 4.0, target: str | None = None) -> dict[str, str]:
    """Return {ip: LOCATION} of DIAL devices. With target, also unicast to it."""
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.bind(("", 0))
    sock.setblocking(False)
    transport, proto = await loop.create_datagram_endpoint(_Collector, sock=sock)
    try:
        for _ in range(3):
            transport.sendto(M_SEARCH, (SSDP_ADDR, SSDP_PORT))
            if target:
                transport.sendto(M_SEARCH, (target, SSDP_PORT))
            await asyncio.sleep(timeout / 3)
            if target and target in proto.found:
                break
    finally:
        transport.close()
    return dict(proto.found)


async def fetch_device_info(session: aiohttp.ClientSession, host: str, location: str) -> TVInfo:
    """GET dd.xml; the Application-URL header is the SmartCenter base URL."""
    async with session.get(location, timeout=aiohttp.ClientTimeout(total=8)) as resp:
        resp.raise_for_status()
        apps_url = resp.headers.get("Application-URL")
        body = await resp.text()
    info = parse_dd_xml(body, host)
    info.location = location
    if apps_url:
        info.apps_url = apps_url if apps_url.endswith("/") else apps_url + "/"
    return info


async def discover(session: aiohttp.ClientSession, timeout: float = 4.0, host: str | None = None) -> list[TVInfo]:
    found = await ssdp_search(timeout, target=host)
    if host:
        found = {k: v for k, v in found.items() if k == host}
    out: list[TVInfo] = []
    for ip, loc in found.items():
        with contextlib.suppress(Exception):
            info = await fetch_device_info(session, ip, loc)
            if info.model or info.mac:  # only Vestel TVs carry <locale> data
                out.append(info)
                continue
            _LOGGER.debug("%s (%s) is DIAL but not a Vestel SmartCenter TV", ip, loc)
    return out
