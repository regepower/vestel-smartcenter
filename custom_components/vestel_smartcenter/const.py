"""Constants for Vestel SmartCenter."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "vestel_smartcenter"

CONF_LOCATION: Final = "location"
CONF_APPS_URL: Final = "apps_url"
CONF_DIAL_VERSION: Final = "dial_version"
CONF_MODEL: Final = "model"
CONF_SW_VERSION: Final = "software_version"
CONF_TOKEN: Final = "stream_token"  # noqa: S105 - config key, not a secret
CONF_RTSP_HTTP_PORT: Final = "rtsp_http_port"
CONF_TIMER_TZ: Final = "timer_timezone"  # "utc" | "local"

DEFAULT_RTSP_HTTP_PORT: Final = 8554
SCAN_INTERVAL: Final = timedelta(seconds=15)
CHANNELS_INTERVAL: Final = timedelta(minutes=30)
TIMERS_INTERVAL: Final = timedelta(minutes=5)

STREAM_PATH: Final = "/api/vestel_smartcenter/followtv/{entry_id}.ts"

SERVICE_ADD_TIMER: Final = "add_timer"
SERVICE_DELETE_TIMER: Final = "delete_timer"
SERVICE_OPEN_URL: Final = "open_url"
SERVICE_SEND_TEXT: Final = "send_text"
SERVICE_SET_ECO_MODE: Final = "set_eco_mode"
SERVICE_START_FOLLOWTV: Final = "start_followtv"
SERVICE_STOP_FOLLOWTV: Final = "stop_followtv"
SERVICE_REFRESH_CHANNELS: Final = "refresh_channels"
SERVICE_SEND_RAW: Final = "send_raw"
