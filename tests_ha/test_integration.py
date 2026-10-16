# mypy: ignore-errors
"""Home Assistant integration tests against the mock TV."""

from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from mock_tv import MockTV

from custom_components.vestel_smartcenter.const import CONF_TOKEN, DOMAIN
from custom_components.vestel_smartcenter.pyvestel import api as api_mod


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    return


@pytest.fixture
async def mock_tv(monkeypatch: pytest.MonkeyPatch, socket_enabled: None):
    ports = {k: free_port() for k in ("http_port", "ws_port", "rtsp_port", "tunnel_port")}
    monkeypatch.setattr(api_mod, "WS_PORT_V2", ports["ws_port"])
    tv = MockTV(True, True, **ports)
    await tv.start()
    yield tv
    await tv.stop()


async def _setup(hass: HomeAssistant, tv: MockTV) -> str:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"host": "127.0.0.1", "location": f"http://127.0.0.1:{tv.http_port}/dd.xml"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    assert result["title"] == "Telefunken Mock"
    await hass.async_block_till_done()
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    hass.config_entries.async_update_entry(entry, options={"rtsp_http_port": tv.tunnel_port})
    await hass.async_block_till_done()
    return entry.entry_id


async def test_full(hass: HomeAssistant, mock_tv: MockTV, hass_client_no_auth) -> None:
    assert await async_setup_component(hass, "http", {})
    entry_id = await _setup(hass, mock_tv)
    entry = hass.config_entries.async_get_entry(entry_id)
    assert entry is not None and entry.unique_id == "00:11:22:33:44:55"

    mp = hass.states.get("media_player.telefunken_mock")
    assert mp is not None, hass.states.async_entity_ids()
    assert mp.state == "on"
    assert mp.attributes["source"] == "ZDF HD"
    assert mp.attributes["source_list"] == ["Das Erste HD", "ZDF HD", "Rock & Pop"]
    assert mp.attributes["volume_level"] == 0.2
    assert hass.states.get("sensor.telefunken_mock_channel").state == "ZDF HD"
    assert hass.states.get("sensor.telefunken_mock_reminders").state == "1"
    assert hass.states.get("sensor.telefunken_mock_tv_state").state == "epg"

    await hass.services.async_call(
        "media_player", "select_source", {"entity_id": mp.entity_id, "source": "Das Erste HD"}, blocking=True
    )
    assert any("<channel rsn='1'/>" in p for p in mock_tv.posts)
    await hass.services.async_call(
        "media_player", "volume_set", {"entity_id": mp.entity_id, "volume_level": 0.42}, blocking=True
    )
    assert mock_tv.volume == 42
    await hass.services.async_call(
        "remote",
        "send_command",
        {"entity_id": "remote.telefunken_mock_remote", "command": ["MENU", "OK"]},
        blocking=True,
    )
    assert mock_tv.keys[-2:] == ["1048", "1053"]

    resp = await hass.services.async_call(
        DOMAIN,
        "add_timer",
        {
            "entity_id": mp.entity_id,
            "channel": "ZDF HD",
            "start": "2026-10-18 20:15:00",
            "end": "2026-10-18 21:45:00",
            "name": "Krimi",
        },
        blocking=True,
        return_response=True,
    )
    assert resp[mp.entity_id]["id"] == "87654321"
    body = next(p for p in mock_tv.posts if "<addtimer>" in p)
    assert "event_name='Krimi'" in body and "rsn='2'" in body

    resp = await hass.services.async_call(
        DOMAIN, "start_followtv", {"entity_id": mp.entity_id}, blocking=True, return_response=True
    )
    r = resp[mp.entity_id]
    assert r["encrypted"] is True and r["direct_url"] is None
    assert f"/api/vestel_smartcenter/followtv/{entry_id}.ts?token=" in r["proxy_url"]

    client = await hass_client_no_auth()
    bad = await client.get(f"/api/vestel_smartcenter/followtv/{entry_id}.ts?token=nope")
    assert bad.status == 401
    ok = await client.get(f"/api/vestel_smartcenter/followtv/{entry_id}.ts?token={entry.data[CONF_TOKEN]}")
    assert ok.status == 200 and ok.headers["Content-Type"] == "video/mp2t"
    data = await ok.content.read(188 * 50)
    assert data[0] == 0x47 and data[188] == 0x47
    ok.close()

    assert await hass.config_entries.async_unload(entry_id)
    await hass.async_block_till_done()


async def test_flow_rejects_vewd(hass: HomeAssistant, mock_tv: MockTV) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "127.0.0.1", "location": f"http://127.0.0.1:{mock_tv.http_port}/vewd.xml"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "not_smartcenter"}
    assert "Vewd" in result["description_placeholders"]["seen"]


async def test_buttons_raw_diagnostics(hass: HomeAssistant, mock_tv: MockTV) -> None:
    from custom_components.vestel_smartcenter.diagnostics import async_get_config_entry_diagnostics

    entry_id = await _setup(hass, mock_tv)
    await hass.services.async_call("button", "press", {"entity_id": "button.telefunken_mock_menu"}, blocking=True)
    assert mock_tv.keys[-1] == "1048"
    resp = await hass.services.async_call(
        DOMAIN,
        "send_raw",
        {"entity_id": "media_player.telefunken_mock", "path": "SmartCenter", "body": "<command>volume</command>"},
        blocking=True,
        return_response=True,
    )
    r = resp["media_player.telefunken_mock"]
    assert r["status"] == 200 and r["ws"] == ["<volume level=20"] and r["protocol"] == "v2"
    entry = hass.config_entries.async_get_entry(entry_id)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["entry"]["stream_token"] == "**REDACTED**"
    assert any(h["method"] == "POST" for h in diag["history"])
    assert await hass.config_entries.async_unload(entry_id)


async def test_camera_and_calendar(hass: HomeAssistant, mock_tv: MockTV, socket_enabled: None) -> None:
    import aiohttp
    from homeassistant.components.camera import async_get_stream_source

    entry_id = await _setup(hass, mock_tv)
    cam = hass.states.get("camera.telefunken_mock_followtv")
    assert cam is not None and cam.state != "unavailable"
    url = await async_get_stream_source(hass, cam.entity_id)
    assert url is not None and url.startswith("http://127.0.0.1:")
    async with aiohttp.ClientSession() as s, s.get(url) as r:
        data = await r.content.read(188 * 20)
    assert r.status == 200 and data[0] == 0x47 and data[188] == 0x47

    cal = hass.states.get("calendar.telefunken_mock_tv_timers")
    assert cal is not None
    resp = await hass.services.async_call(
        "calendar",
        "get_events",
        {"entity_id": cal.entity_id, "start_date_time": "2026-10-17 00:00:00", "end_date_time": "2026-10-31 00:00:00"},
        blocking=True,
        return_response=True,
    )
    events = resp[cal.entity_id]["events"]
    summaries = [e["summary"] for e in events]
    assert any("Tatort" in x for x in summaries)
    # weekly recording 'heute' expands to 2 occurrences in the window
    assert sum("heute" in x for x in summaries) == 2
    assert await hass.config_entries.async_unload(entry_id)
    await hass.async_block_till_done()
