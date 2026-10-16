"""DIAL/SSDP discovery of Vestel SmartCenter TVs (MB100 family)."""

from __future__ import annotations

import asyncio
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
        # ip -> all LOCATIONs. A TV can run several DIAL servers (e.g. Vewd browser + Vestel SmartCenter).
        self.found: dict[str, list[str]] = {}

    def datagram_received(self, data: bytes, addr: tuple[str | Any, int]) -> None:
        if not data.startswith(b"HTTP/1.1 200"):
            return
        hdr = _parse_ssdp(data)
        if hdr.get("ST") == DIAL_ST and "LOCATION" in hdr:
            locs = self.found.setdefault(addr[0], [])
            if hdr["LOCATION"] not in locs:
                locs.append(hdr["LOCATION"])


async def ssdp_search(timeout: float = 4.0, target: str | None = None) -> dict[str, list[str]]:
    """Return {ip: [LOCATION, ...]} of DIAL devices. With target, also unicast to it."""
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
    finally:
        transport.close()
    return {k: list(v) for k, v in proto.found.items()}


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


def is_smartcenter(info: TVInfo) -> bool:
    """Only the Vestel SmartCenter DIAL server carries <locale> data (like the app checks)."""
    return bool(info.model or info.mac)


async def pick_smartcenter(
    session: aiohttp.ClientSession, host: str, locations: list[str]
) -> tuple[TVInfo | None, list[TVInfo]]:
    """Fetch all dd.xml of one IP; return (SmartCenter info or None, all infos)."""
    infos: list[TVInfo] = []
    for loc in locations:
        try:
            infos.append(await fetch_device_info(session, host, loc))
        except (aiohttp.ClientError, TimeoutError, ValueError, Exception) as err:  # noqa: BLE001
            _LOGGER.debug("%s: %s", loc, err)
    best = next((i for i in infos if is_smartcenter(i) and i.apps_url), None)
    return best, infos


def mac_from_arp(host: str) -> str | None:
    """Linux ARP cache lookup (HA OS / Docker host network)."""
    try:
        with open("/proc/net/arp", encoding="ascii") as fh:
            for line in fh.read().splitlines()[1:]:
                cols = line.split()
                if len(cols) >= 4 and cols[0] == host and cols[3] != "00:00:00:00:00:00":
                    return cols[3].lower()
    except OSError:
        return None
    return None


async def discover(session: aiohttp.ClientSession, timeout: float = 4.0, host: str | None = None) -> list[TVInfo]:
    found = await ssdp_search(timeout, target=host)
    if host:
        found = {k: v for k, v in found.items() if k == host}
    out: list[TVInfo] = []
    for ip, locs in found.items():
        best, _ = await pick_smartcenter(session, ip, locs)
        if best is not None:
            out.append(best)
        else:
            _LOGGER.debug("%s (%s) is DIAL but not a Vestel SmartCenter TV", ip, locs)
    return out
