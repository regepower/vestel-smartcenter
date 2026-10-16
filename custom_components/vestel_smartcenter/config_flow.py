"""Config flow for Vestel SmartCenter."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlsplit

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_MAC, CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import format_mac

from .const import (
    CONF_APPS_URL,
    CONF_DIAL_VERSION,
    CONF_LOCATION,
    CONF_MODEL,
    CONF_RTSP_HTTP_PORT,
    CONF_SW_VERSION,
    CONF_TIMER_TZ,
    DEFAULT_RTSP_HTTP_PORT,
    DOMAIN,
)
from .coordinator import VestelConfigEntry
from .pyvestel import NotSmartCenterError, TVInfo, VestelError, VestelTV, discover, mac_from_arp, pick_smartcenter

_LOGGER = logging.getLogger(__name__)


def _entry_data(info: TVInfo) -> dict[str, Any]:
    return {
        CONF_HOST: info.host,
        CONF_NAME: info.name or "Smart TV",
        CONF_MAC: info.mac,
        CONF_LOCATION: info.location,
        CONF_APPS_URL: info.apps_url,
        CONF_DIAL_VERSION: info.dial_version,
        CONF_MODEL: info.model,
        CONF_SW_VERSION: info.software_version,
    }


class VestelConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._found: dict[str, TVInfo] = {}

    async def _finish(self, info: TVInfo) -> ConfigFlowResult:
        uid = format_mac(info.mac) if info.mac else info.host
        await self.async_set_unique_id(uid)
        self._abort_if_unique_id_configured(updates=_entry_data(info))
        return self.async_create_entry(title=info.name or f"Smart TV {info.host}", data=_entry_data(info))

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {"seen": "-"}
        session = async_get_clientsession(self.hass)
        if user_input is not None:
            host = user_input.get(CONF_HOST, "").strip()
            location = user_input.get(CONF_LOCATION, "").strip() or None
            if not host and not location:
                tvs = await discover(session)
                self._found = {t.host: t for t in tvs}
                if not self._found:
                    errors["base"] = "no_devices_found"
                else:
                    return await self.async_step_pick()
            else:
                try:
                    if location:
                        host = host or urlsplit(location).hostname or ""
                        info, infos = await pick_smartcenter(session, host, [location])
                        if info is None:
                            raise NotSmartCenterError(host, [f"{i.name or '?'} @ {i.location}" for i in infos])
                    else:
                        info = (await VestelTV.from_host(session, host)).info
                except NotSmartCenterError as err:
                    errors["base"] = "not_smartcenter"
                    placeholders["seen"] = "; ".join(err.seen) or "-"
                except (VestelError, OSError, TimeoutError) as err:
                    _LOGGER.debug("connect failed: %s", err)
                    errors["base"] = "cannot_connect"
                else:
                    if not info.mac:
                        info.mac = mac_from_arp(info.host)
                    return await self._finish(info)
        schema = vol.Schema(
            {
                vol.Optional(CONF_HOST, default=""): str,
                vol.Optional(CONF_LOCATION, default=""): str,
            }
        )
        return self.async_show_form(
            step_id="user", data_schema=schema, errors=errors, description_placeholders=placeholders
        )

    async def async_step_pick(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return await self._finish(self._found[user_input[CONF_HOST]])
        options = {h: f"{t.name or 'Smart TV'} ({h}, {t.model or '?'})" for h, t in self._found.items()}
        return self.async_show_form(step_id="pick", data_schema=vol.Schema({vol.Required(CONF_HOST): vol.In(options)}))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: VestelConfigEntry) -> OptionsFlow:
        return VestelOptionsFlow()


class VestelOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        opts = self.config_entry.options
        cur = opts.get(CONF_RTSP_HTTP_PORT, DEFAULT_RTSP_HTTP_PORT)
        mac = opts.get(CONF_MAC) or self.config_entry.data.get(CONF_MAC) or ""
        schema = vol.Schema(
            {
                vol.Optional(CONF_MAC, default=mac): str,
                vol.Required(CONF_RTSP_HTTP_PORT, default=cur): vol.All(int, vol.Range(0, 65535)),
                vol.Required(CONF_TIMER_TZ, default=opts.get(CONF_TIMER_TZ, "utc")): vol.In(["utc", "local"]),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
