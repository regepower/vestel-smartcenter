"""Tests for pyvestel against the mock TV."""

from __future__ import annotations

import asyncio
import socket
import sys
from datetime import UTC, datetime
from pathlib import Path

import aiohttp
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "custom_components" / "vestel_smartcenter"))
sys.path.insert(0, str(Path(__file__).parent))

from mock_tv import KEY_WORDS, MockTV, derive_key

import pyvestel.api as api_mod
from pyvestel import FollowTVManager, TSRepacker, VestelTV, parse_stream_url, resolve_key
from pyvestel.followtv import rtp_payload, stream_ts
from pyvestel.models import (
    Channel,
    FollowTVError,
    TVState,
    build_timed_xml,
    parse_channel_list,
    parse_followtv,
    parse_state,
    parse_timed_list,
    parse_volume,
)
import itertools


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ------------------------------------------------------------------- unit
def test_keys() -> None:
    assert resolve_key("POWER") == 1012
    assert resolve_key("BUTTON_VOL_UP") == 1016
    assert resolve_key("7") == 1007
    assert resolve_key("1053") == 1053
    with pytest.raises(ValueError):
        resolve_key("FOO")


def test_key_derivation_matches_app() -> None:
    raw = "rtsp://1.2.3.4:554/x|" + "|".join(KEY_WORDS)
    t = parse_stream_url(raw)
    assert t.url == "rtsp://1.2.3.4:554/x"
    assert t.key == derive_key(KEY_WORDS)
    # ror2(0x99aabbcc) computed by hand: 0x99aabbcc >> 2 = 0x266aaef3, low bits 00 -> 0x266aaef3
    assert t.key[:4].hex() == "266aaef3"
    assert parse_stream_url("http://a/b").key is None


def test_parsers() -> None:
    from mock_tv import CHANNELS, RECORDS, TIMERS

    ch = parse_channel_list(CHANNELS)
    assert [c.name for c in ch] == ["Das Erste HD", "ZDF HD", "Rock & Pop"]
    assert ch[0].network == "Astra" and ch[1].sid == 11110
    assert parse_channel_list("channel_list_size = 0") == []
    t = parse_timed_list(TIMERS, "timer")
    assert t[0].id == 12345678 and t[0].name == "Tatort s Finale"
    assert t[0].start == datetime(2026, 10, 18, 18, 15, tzinfo=UTC)
    assert parse_timed_list(RECORDS, "record")[0].repeat == "w"
    assert parse_volume("<volume level=37") == 37
    assert parse_state("<tv_state value='STANDBY'/>") is TVState.STANDBY
    r = parse_followtv("followtv_started_status:6")
    assert r is not None and r.url is None and r.error == FollowTVError.CHANNEL_ENCRYPTED
    r = parse_followtv("followtv_started_status:0_url:rtsp://x/y|a|b|c|d")
    assert r is not None and r.url == "rtsp://x/y|a|b|c|d"


def test_build_timed_xml() -> None:
    ch = Channel(rsn=2, name="ZDF", handle=12, onid=1, tsid=1011, sid=11110)
    xml = build_timed_xml(
        "addtimer",
        ch,
        "Tätort's",
        datetime(2026, 10, 18, 20, 15, tzinfo=UTC),
        datetime(2026, 10, 18, 21, 45, tzinfo=UTC),
    )
    assert xml == (
        "<addtimer><service handle='12' rsn='2' event_name='Tatort s' onid='1' tsid='1011' sid='11110'/>"
        "<start y='2026' m='10' d='18' h='20' min='15'/><end y='2026' m='10' d='18' h='21' min='45'/>"
        "<pref repeat='o' change='a' playback='p'/></addtimer>"
    )


def test_repacker_plain_m2ts_and_ts() -> None:
    from mock_tv import make_ts

    pk = make_ts(10)
    m2ts = b"".join(b"\0\0\0\0" + p for p in pk)
    rep = TSRepacker()
    out = rep.feed(b"\x00" * 5 + m2ts[:500]) + rep.feed(m2ts[500:])
    assert rep.mode == "m2ts" and out == b"".join(pk[: len(out) // 188])
    rep2 = TSRepacker()
    assert rep2.feed(b"".join(pk)) == b"".join(pk) and rep2.mode == "ts"


def test_rtp_payload_ext_and_padding() -> None:
    pkt = bytes([0xB1, 33, 0, 1]) + b"\0" * 8 + b"CSRC" + b"\x00\x00\x00\x01" + b"EXTX" + b"DATA" + b"\0\0\x02"
    assert rtp_payload(pkt) == b"DATA\0"


# ------------------------------------------------------------- end to end
@pytest.fixture
def ports() -> dict[str, int]:
    return {k: free_port() for k in ("http_port", "ws_port", "rtsp_port", "tunnel_port")}


async def _setup(v2: bool, encrypted: bool, ports: dict[str, int], monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(api_mod, "WS_PORT_V2", ports["ws_port"])
    mock = MockTV(v2, encrypted, **ports)
    await mock.start()
    session = aiohttp.ClientSession()
    tv = await VestelTV.from_host(session, "127.0.0.1", location=f"http://127.0.0.1:{ports['http_port']}/dd.xml")
    return mock, session, tv


@pytest.mark.parametrize("v2", [False, True])
async def test_commands(v2: bool, ports: dict[str, int], monkeypatch: pytest.MonkeyPatch) -> None:
    mock, session, tv = await _setup(v2, False, ports, monkeypatch)
    events: list[str] = []
    tv.add_listener(events.append)
    try:
        assert tv.info.is_v2 is v2 and tv.info.mac == "00:11:22:33:44:55"
        await tv.connect()
        assert len(await tv.get_channels()) == 3
        cur = await tv.get_current_channel()
        assert cur is not None and cur.name == "ZDF HD"
        assert await tv.get_state() is TVState.EPG
        assert (await tv.get_reminders())[0].id == 12345678
        assert (await tv.get_recordings())[0].id == 7
        assert (await tv.get_special_apps())["Netflix"] is True
        await tv.set_volume(33)
        assert await tv.get_volume() == 33
        await tv.send_key("POWER")
        assert mock.keys == ["1012"]
        new_id = await tv.add_timed("timer", Channel(rsn=1, name="x"), "Test", datetime.now(UTC), datetime.now(UTC))
        assert new_id == "87654321"
        assert await tv.delete_timed("timer", 87654321)
        await mock.push_event("<channel_list_changed/>")
        await asyncio.sleep(0.2)
        assert "<channel_list_changed/>" in events
        ok_quotes = "'" if v2 else '"'
        assert any(p.startswith(f"<?xml version={ok_quotes}1.0") for p in mock.posts)
    finally:
        await tv.disconnect()
        await session.close()
        await mock.stop()


@pytest.mark.parametrize(("encrypted", "tunnel"), [(True, True), (False, True), (True, False)])
async def test_followtv(encrypted: bool, tunnel: bool, ports: dict[str, int], monkeypatch: pytest.MonkeyPatch) -> None:
    mock, session, tv = await _setup(True, encrypted, ports, monkeypatch)
    try:
        await tv.connect()
        res = await tv.start_followtv()
        assert res is not None and res.url is not None
        assert ("|" in res.url) is encrypted
        data = b""
        port = ports["tunnel_port"] if tunnel else None
        async for chunk in stream_ts(res.url, port):
            data += chunk
            if len(data) >= 188 * 7 * 30:
                break
        assert len(data) % 188 == 0
        pk = [data[i : i + 188] for i in range(0, len(data), 188)]
        assert all(p[0] == 0x47 for p in pk)
        ccs = [p[3] & 0x0F for p in pk]
        assert all((b - a) & 0x0F == 1 for a, b in itertools.pairwise(ccs))
        assert ("GET" not in mock.rtsp_methods) and "PLAY" in mock.rtsp_methods
    finally:
        await tv.disconnect()
        await session.close()
        await mock.stop()


async def test_manager_fanout(ports: dict[str, int], monkeypatch: pytest.MonkeyPatch) -> None:
    mock, session, tv = await _setup(True, True, ports, monkeypatch)
    try:
        await tv.connect()
        mgr = FollowTVManager(tv, http_port=ports["tunnel_port"], idle_stop=0.1)
        q1, q2 = mgr.subscribe(), mgr.subscribe()
        c1 = await asyncio.wait_for(q1.get(), 5)
        c2 = await asyncio.wait_for(q2.get(), 5)
        assert c1 and c2 and c1[0] == 0x47
        assert mgr.encrypted is True
        mgr.unsubscribe(q1)
        mgr.unsubscribe(q2)
        await asyncio.sleep(0.4)
        assert not mgr.running
    finally:
        await tv.disconnect()
        await session.close()
        await mock.stop()


async def test_pick_smartcenter_ignores_vewd(ports: dict[str, int], monkeypatch: pytest.MonkeyPatch) -> None:
    from pyvestel import NotSmartCenterError, pick_smartcenter

    mock, session, _tv = await _setup(True, False, ports, monkeypatch)
    base = f"http://127.0.0.1:{ports['http_port']}"
    try:
        best, infos = await pick_smartcenter(session, "127.0.0.1", [f"{base}/vewd.xml", f"{base}/dd.xml"])
        assert best is not None and best.model == "mb100" and len(infos) == 2
        with pytest.raises(NotSmartCenterError) as exc:
            await VestelTV.from_host(session, "127.0.0.1", location=f"{base}/vewd.xml")
        assert "Vewd" in str(exc.value)
    finally:
        await session.close()
        await mock.stop()
