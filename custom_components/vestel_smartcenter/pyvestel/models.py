"""Data models and XML parsing/building for the SmartCenter protocol."""

from __future__ import annotations

import re
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import UTC, datetime, tzinfo
from enum import StrEnum

TIME_FMT = "%Y/%m/%d-%H:%M"  # times from the TV are UTC


class TVState(StrEnum):
    """TV UI state (vr/tvstate, <tv_state value='..'/>)."""

    DEFAULT = "DEFAULT"
    PORTAL = "PORTAL"
    MEDIA_BROWSER = "MEDIA_BROWSER"
    EPG = "EPG"
    BACKLIGHT_OFF = "BACKLIGHT_OFF"
    STANDBY = "STANDBY"


class FollowTVError(StrEnum):
    """followtv_started_status:<n> error codes."""

    START_FAILED = "3"
    ACTION_NOT_ALLOWED = "4"
    RECORDING_ENCRYPTED = "5"
    CHANNEL_ENCRYPTED = "6"
    WEB_BROWSER_RUNNING = "7"
    MEDIA_PLAYER_RUNNING = "8"
    UNSUPPORTED_SOURCE = "9"
    FOLLOWTV_DISABLED = "10"
    RTSP_HD_CONTENT = "12"


@dataclass(slots=True)
class TVInfo:
    """Device description (dd.xml <locale>) plus discovery data."""

    host: str
    name: str | None = None
    model: str | None = None  # tv_version, e.g. "mb100"
    software_version: str | None = None
    mac: str | None = None
    dial_version: str = "1"
    apps_url: str | None = None
    location: str | None = None
    browser_type: str | None = None
    alexa_enabled: bool = False
    qui_ui: str | None = None
    protocol: str | None = None  # "v1" | "v2"; None = derive from dial_version / auto-probe

    @property
    def is_v2(self) -> bool:
        if self.protocol:
            return self.protocol == "v2"
        try:
            return float(self.dial_version.strip().split()[0]) >= 2  # app: == "2.0"; newer TVs report higher
        except (ValueError, IndexError):
            return False


@dataclass(slots=True)
class Channel:
    rsn: int
    name: str
    handle: int = 0
    onid: int = 0
    tsid: int = 0
    sid: int = 0
    type: int = 0
    network: str | None = None


@dataclass(slots=True)
class TimedObject:
    """Reminder (<timer>) or recording (<record>) on the TV."""

    id: int
    name: str
    start: datetime | None
    end: datetime | None
    repeat: str | None = None  # o=once, d=daily, w=weekly
    kind: str = "timer"  # "timer" (reminder) or "record"
    extra: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class FollowTVResult:
    raw: str
    url: str | None = None
    error: FollowTVError | str | None = None


def _int(val: str | None) -> int:
    try:
        return int(val) if val is not None else 0
    except ValueError:
        return 0


def _safe_xml(text: str) -> str:
    """The TV sends unescaped '&' (the app does the same replace)."""
    return re.sub(r"&(?!(amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)", "&amp;", text)


def _strip_decl(text: str) -> str:
    return re.sub(r"<\?xml[^>]*\?>", "", text).strip()


def _clean(text: str) -> str:
    """MB230 appends junk like '</com' (truncated '</command>') after the payload."""
    text = re.sub(r"<\/?[A-Za-z_]*\s*$", "", text.strip())  # dangling partial tag at the end
    return re.sub(r"</command>\s*$", "", text)


def _parse_fragment(text: str) -> ET.Element:
    text = _clean(text)
    return ET.fromstring(f"<root>{_strip_decl(_safe_xml(text))}</root>")  # noqa: S314 - LAN device, stdlib parser has no XXE


def _channel_from(el: ET.Element, network: str | None = None) -> Channel:
    a = {k.lower(): v for k, v in el.attrib.items()}
    return Channel(
        rsn=_int(a.get("rsn")),
        name=a.get("name", ""),
        handle=_int(a.get("handle")),
        onid=_int(a.get("onid")),
        tsid=_int(a.get("tsid")),
        sid=_int(a.get("sid")),
        type=_int(a.get("type")),
        network=network,
    )


def parse_channel_list(text: str | None) -> list[Channel]:
    if not text or text.strip().startswith("channel_list_size = 0"):
        return []
    root = _parse_fragment(text)
    out: list[Channel] = []
    for lst in root.iter():
        if lst.tag.lower() == "active_list":
            net = {k.lower(): v for k, v in lst.attrib.items()}.get("network")
            out.extend(_channel_from(s, net) for s in lst if s.tag.lower() == "service")
    if not out:  # flat list without <active_list>
        out = [_channel_from(s) for s in root.iter() if s.tag.lower() == "service"]
    return out


def parse_program(text: str | None) -> Channel | None:
    """vr/program -> currently tuned service."""
    if not text:
        return None
    try:
        root = _parse_fragment(text)
    except ET.ParseError:
        m = re.search(r"<service\b[^>]*/>", text)
        if not m:
            return None
        root = _parse_fragment(m.group(0))
    for el in root.iter():
        if el.tag.lower() == "service":
            return _channel_from(el)
    return None


def _fix_event_names(text: str) -> str:
    """Replace stray single quotes inside event_name='..' (as the app does)."""

    def repl(m: re.Match[str]) -> str:
        return "event_name='" + m.group(1).replace("'", " ") + "' start="

    return re.sub(r"event_name='(.*?)' start=", repl, text)


def _parse_time(val: str | None, tz: tzinfo = UTC) -> datetime | None:
    if not val:
        return None
    try:
        return datetime.strptime(val.strip(), TIME_FMT).replace(tzinfo=tz)
    except ValueError:
        return None


def parse_timed_list(text: str | None, kind: str, tz: tzinfo = UTC) -> list[TimedObject]:
    """kind: 'timer' (vr/timerlist) or 'record' (vr/recordlist)."""
    if not text:
        return []
    root = _parse_fragment(_fix_event_names(text))
    out: list[TimedObject] = []
    for el in root.iter():
        if el.tag.lower() != kind:
            continue
        a = {k.lower(): v for k, v in el.attrib.items()}
        out.append(
            TimedObject(
                id=_int(a.pop("id", None)),
                name=a.pop("event_name", ""),
                start=_parse_time(a.pop("start", None), tz),
                end=_parse_time(a.pop("end", None), tz),
                repeat=a.pop("repeat", None),
                kind=kind,
                extra=a,
            )
        )
    return out


def parse_volume(text: str | None) -> int | None:
    if not text:
        return None
    digits = re.sub(r"\D+", "", text[text.rfind("=") + 1 :])
    return int(digits) if digits else None


def parse_state(text: str | None) -> TVState | None:
    if not text:
        return None
    parts = text.split("'")
    val = parts[1] if len(parts) > 1 else ""
    try:
        return TVState(val)
    except ValueError:
        return TVState.DEFAULT


def parse_followtv(text: str | None) -> FollowTVResult | None:
    if not text:
        return None
    text = text.strip()
    url = text[text.rfind("_url:") + 5 :] if "_url:" in text else text
    if not url.startswith(("http", "rtsp")):
        url = None  # type: ignore[assignment]
    err: FollowTVError | str | None = None
    m = re.fullmatch(r"followtv_started_status:(\d+)", text)
    if m:
        try:
            err = FollowTVError(m.group(1))
        except ValueError:
            err = m.group(1)
    return FollowTVResult(raw=text, url=url, error=err)


def parse_special_apps(text: str | None) -> dict[str, bool]:
    out: dict[str, bool] = {}
    if not text or "|" not in text:
        return out
    for part in text.replace("\n", "").split("|"):
        if ":" in part:
            k, v = part.split(":", 1)
            out[k.strip()] = v.strip() == "1"
    return out


def parse_dd_xml(xml_text: str, host: str) -> TVInfo:
    """Parse the DIAL device description; Vestel puts its data in <locale>."""
    root = ET.fromstring(_safe_xml(xml_text))  # noqa: S314 - LAN device, stdlib parser has no XXE
    info = TVInfo(host=host)

    def local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    for el in root.iter():
        if local(el.tag) != "locale":
            continue
        vals = {local(c.tag): (c.text or "").strip() for c in el}
        info.name = vals.get("name") or None
        info.model = vals.get("tv_version") or None
        info.software_version = vals.get("software_version") or None
        info.mac = vals.get("mac") or None
        info.dial_version = vals.get("dial_version") or "1"
        info.browser_type = vals.get("browser_type") or None
        info.alexa_enabled = vals.get("alexa_enabled") == "1"
        info.qui_ui = vals.get("quiUI") or None
        break
    if info.name is None:
        for el in root.iter():
            if local(el.tag) == "friendlyName" and el.text:
                info.name = el.text.strip()
                break
    return info


def deaccent(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")


def build_timed_xml(
    tag: str,
    channel: Channel,
    name: str,
    start_utc: datetime,
    end_utc: datetime,
    repeat: str = "o",
    change: str = "a",
    playback: str = "p",
    tz: tzinfo = UTC,
) -> str:
    """Body for addtimer/addrecord (tag = 'addtimer' | 'addrecord'). Times in UTC."""
    ev = deaccent(name).replace("'", " ").replace("&", "+").replace("<", " ").replace(">", " ")
    s, e = start_utc.astimezone(tz), end_utc.astimezone(tz)
    return (
        f"<{tag}><service handle='{channel.handle}' rsn='{channel.rsn}' event_name='{ev}' "
        f"onid='{channel.onid}' tsid='{channel.tsid}' sid='{channel.sid}'/>"
        f"<start y='{s:%Y}' m='{s:%m}' d='{s:%d}' h='{s.hour}' min='{s:%M}'/>"
        f"<end y='{e:%Y}' m='{e:%m}' d='{e:%d}' h='{e.hour}' min='{e:%M}'/>"
        f"<pref repeat='{repeat}' change='{change}' playback='{playback}'/></{tag}>"
    )


def parse_added_id(text: str | None) -> str | None:
    if not text or "'" not in text:
        return None
    i = text.index("'") + 1
    return text[i : i + 8].rstrip("'")
