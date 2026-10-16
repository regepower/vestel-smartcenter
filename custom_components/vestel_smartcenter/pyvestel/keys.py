"""Remote key codes (from com.vestel.supertvcommunicator.Mapper)."""

from __future__ import annotations

KEYS: dict[str, int] = {
    "POWER": 1012,
    "HOME": 1046,
    "MENU": 1048,
    "QMENU": 1043,
    "BACK": 1010,
    "EXIT": 1037,
    "OK": 1053,
    "UP": 1020,
    "DOWN": 1019,
    "LEFT": 1021,
    "RIGHT": 1022,
    "VOL_UP": 1016,
    "VOL_DOWN": 1017,
    "MUTE": 1013,
    "PROG_UP": 1032,
    "PROG_DOWN": 1033,
    "CHAN": 1045,
    "SWAP": 1034,
    "SOURCE": 1056,
    "MMEDIA": 1057,
    "INFO": 1018,
    "EPG": 1047,
    "TEXT": 1255,
    "SUBTITLE": 1031,
    "LANG": 1015,
    "SCREEN": 1011,
    "PRESETS": 1014,
    "FAV": 1040,
    "SLEEP": 1042,
    "RED": 1055,
    "GREEN": 1054,
    "YELLOW": 1050,
    "BLUE": 1052,
    "PLAY": 1025,
    "PAUSE": 1049,
    "STOP": 1024,
    "RECORD": 1051,
    "REWIND": 1027,
    "FORWARD": 1028,
    "PREVIOUS": 1034,
    "NEXT": 1255,
    **{str(i): 1000 + i for i in range(10)},
}


def resolve_key(key: str | int) -> int:
    """Map a key name ("VOL_UP", "BUTTON_VOL_UP", "5") or raw code to the TV code."""
    if isinstance(key, int):
        return key
    name = key.strip().upper()
    if name.startswith("BUTTON_"):
        name = name[7:]
    if name in KEYS:
        return KEYS[name]
    if name.isdigit():
        return int(name)
    raise ValueError(f"Unknown key: {key}")
