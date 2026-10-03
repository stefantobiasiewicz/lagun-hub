"""AT commands of the USR-CAN315/316 converter — **text only, no I/O**.

The text layer: it builds command lines and takes replies apart. It does not know
whether they travel over UDP, over TCP or down a serial cable — which is why it can be
checked without an adapter on the desk, and that matters when the box is in another room.

Source: ``docs/usr-can315/at-commands.pdf`` ("[AT Command] USR-CAN315&CAN316"). Every departure
from the documentation is marked with a comment; there are a few, and each one cost
reading the table twice.

Question format: ``AT+<CMD>[=para-1,para-2,…]<CR>``
Answer format:   ``<CR><LF>+<CMD>:<para…><CR><LF><CR><LF>OK<CR><LF>``
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Final

from ..base import Rejected

CR: Final = "\r"
LF: Final = "\n"

AT_PORT: Final = 48899
"""Port for network AT commands. 48899 from the factory, changed by ``AT+SEARCH``."""

AT_KEYWORD: Final = "www.usr.cn"
"""The keyword that opens network AT mode, and the payload of the search broadcast."""

ENTER_COMMAND: Final = "+++"
"""Entering AT mode over a serial cable. The device answers ``A``, within 3 s you must
send ``a`` back, and it replies ``+OK``. Nothing may be sent for one packing interval
before ``+++`` — otherwise it lands inside the data and is passed through as payload."""

ENTER_ACK: Final = "a"

ERRORS: Final = {
    1: "malformed command",
    2: "unknown command",
    3: "wrong operator: the command takes no query, or takes no assignment",
    4: "bad parameter: out of range, or wrong count",
}

_ERROR = re.compile(r"ERROR:(\d+)")


class FrameType(Enum):
    """Frame type in the USR vocabulary."""

    STANDARD = "NDTF"
    EXTENDED = "EDTF"


class Conversion(Enum):
    """Conversion mode (``AT+TMODE``) — what the adapter does with bytes from the network."""

    TRANS = "TRANS"
    """Transparent. Bytes travel in frames with **one fixed** identifier."""
    TPRTL = "TPRTL"
    """Transparent, with the identifier cut out of the payload (``AT+MARK``)."""
    PROTOL = "PROTOL"
    """Standard: every frame carries its own identifier and type in the header.
    **This is the mode for LagunBus** — see ``usr_can315.LAGUNBUS``."""
    MODBUS = "MODBUS"
    USER = "USER"


class Direction(Enum):
    """Conversion direction (``AT+TRDIR``). The documented names speak of the serial port
    even on the network model — "UART" here means "the other side"."""

    BOTH = "BOTHWAY"
    FROM_CAN = "CAN-UART"
    TO_CAN = "UART-CAN"


class CanMode(Enum):
    """Controller mode (``AT+CANMODE``, CAN315 only)."""

    NORMAL = "NORMAL"
    SILENT = "SILENT"
    LOOPBACK = "LOOPBACK"


class FilterMode(Enum):
    """Filter mode (``AT+CANLT``)."""

    OFF = "OFF"
    STANDARD = "NDTF"
    EXTENDED = "EDTF"
    USER = "USER"


class IpMode(Enum):
    """How the address is obtained (``AT+WANN``)."""

    STATIC = "STATIC"
    DHCP = "DHCP"


class SocketMode(Enum):
    """Socket role (``AT+SOCKA``/``AT+SOCKB``). Socket B only does the client modes."""

    TCP_SERVER = "TCPS"
    TCP_CLIENT = "TCPC"
    UDP_SERVER = "UDPS"
    UDP_CLIENT = "UDPC"


# Command names as constants, not literals at the call site: the name comes back in the
# reply as `+NAME:`, so the same string builds the question and parses the answer.
# A typo split across the two would read as "no reply" instead of "unknown command".

TEST: Final = ""
ECHO: Final = "E"
RESTART: Final = "Z"
EXIT: Final = "ENTM"
VERSION: Final = "VER"
SAVE: Final = "CFGTF"
RELOAD: Final = "RELD"
CLEAR: Final = "CLEAR"
NAME: Final = "MID"
SERIAL: Final = "SN"

CAN: Final = "CAN"
CANFD: Final = "CANFD"
CANBAUD: Final = "CANBAUD"
CANFDBAUD: Final = "CANFDBAUD"
FILTER: Final = "CANLT"
FILTER_ADD: Final = "ADDLIST"
FILTER_DEL: Final = "DELLIST"
DIRECTION: Final = "TRDIR"
CONVERSION: Final = "TMODE"
MESSAGE: Final = "MSG"
MARK: Final = "MARK"
CAN_MODE: Final = "CANMODE"
CAN_PACK: Final = "CANPACK"

WAN: Final = "WANN"
DHCP: Final = "DHCPEN"
SOCKET_A: Final = "SOCKA"
SOCKET_B: Final = "SOCKB"
SOCKET_A_ENABLE: Final = "SOCKAEN"
SOCKET_B_ENABLE: Final = "SOCKBEN"
SOCKET_A_PORT: Final = "SOCKPORT"
SOCKET_B_PORT: Final = "SOCKBPORT"
SOCKET_A_LINK: Final = "SOCKLK"
MAX_CLIENTS: Final = "MAXSK"
FULL_POLICY: Final = "TCPSE"
SEARCH: Final = "SEARCH"
HEARTBEAT: Final = "HEARTEN"
REGISTRATION: Final = "REGEN"
IDLE_RESTART: Final = "RSTIM"
PING: Final = "PING"

BITRATES: Final = (5, 10, 20, 50, 100, 120, 125, 150, 200, 250, 400, 500, 600, 750, 1000)
"""Bitrates from the device's own list, in kbit/s. Anything else has to be computed by
hand with ``AT+CANBAUD`` — and a wrong sample point gives a bus that "almost" works."""

_CUSTOM = re.compile(r"custom\(([\d.]+)K\)", re.IGNORECASE)


# --- building the question -------------------------------------------------------


def query(name: str) -> str:
    """``AT+VER``. An empty ``name`` gives bare ``AT`` — the command that tests the link."""
    return f"AT+{name}" if name else "AT"


def setting(name: str, *params: object) -> str:
    """``AT+CAN=500,1,EDTF``. Parameters go through ``str`` — enums yield their value."""
    fields = ",".join(_field(param) for param in params)
    return f"AT+{name}={fields}"


def _field(param: object) -> str:
    if isinstance(param, Enum):
        return str(param.value)
    if isinstance(param, bool):
        return "1" if param else "0"
    return str(param)


def line(command: str) -> bytes:
    """A command onto the wire. Ends with ``CR`` — a bare ``LF`` the device ignores."""
    return (command + CR).encode("ascii")


# --- taking the answer apart -----------------------------------------------------


def parse(raw: bytes, name: str, *, command: str = "") -> tuple[str, ...]:
    """Parameters from the reply to ``AT+<name>``. Raises ``Rejected`` on ``ERROR:n``.

    Tolerates the **command echo**: with ``AT+E=ON`` the device first sends back what it
    received. So we look for the ``+NAME:`` line, not for the first line.

    A missing ``OK`` is a refusal too. A device that answered with something other than
    ``OK`` and other than ``ERROR`` is in a state this driver does not understand — and
    it is better that it says so out loud.
    """
    text = raw.decode("ascii", errors="replace")
    failure = _ERROR.search(text)
    if failure:
        code = int(failure.group(1))
        raise Rejected(command or query(name), ERRORS.get(code, f"ERROR:{code}"))

    lines = [entry.strip() for entry in text.splitlines() if entry.strip()]
    prefix = f"+{name}:"
    for entry in lines:
        if entry.startswith(prefix):
            return tuple(field.strip() for field in entry[len(prefix) :].split(","))

    if any(entry.startswith("OK") or entry == "+OK" for entry in lines):
        return ()
    raise Rejected(command or query(name), f"reply without OK: {text.strip()!r}")


# --- value translation -----------------------------------------------------------


def bitrate_field(bitrate: int) -> str:
    """Bitrate in bit/s → command field (kbit/s). Off the device's list — ``ValueError``."""
    kbps, remainder = divmod(bitrate, 1000)
    if remainder or kbps not in BITRATES:
        allowed = ", ".join(str(value) for value in BITRATES)
        raise ValueError(f"{bitrate} bit/s is not on the device list; allowed: {allowed} kbit/s")
    return str(kbps)


def fd_bitrate_field(bitrate: int) -> str:
    """CAN FD data-phase bitrate → command field (kbit/s).

    Separate from ``bitrate_field`` because **the bitrate list does not apply here**: the
    data phase runs from 100 kbit/s to 5 Mbit/s and takes values the arbitration list
    does not have.
    """
    kbps, remainder = divmod(bitrate, 1000)
    if remainder or not 100 <= kbps <= 5000:
        raise ValueError(f"{bitrate} bit/s is outside the CAN FD data range 100k..5M")
    return str(kbps)


def read_bitrate(field: str) -> int:
    """A field from a reply → bit/s.

    The device answers ``custom(666.66K)`` when the bitrate was computed with
    ``AT+CANBAUD``. The word ``custom`` **exists only in replies** — it cannot be set.
    """
    computed = _CUSTOM.search(field)
    if computed:
        return round(float(computed.group(1)) * 1000)
    return round(float(field) * 1000)


def show_bitrate(bitrate: int) -> str:
    """A bitrate **read** from the device, for showing in a plan.

    Separate from ``bitrate_field`` because a reading is not bound by the list: an
    adapter can sit on a bitrate computed with ``AT+CANBAUD``, and then ``666.66`` is
    the fact of the matter, not an error. The list constrains only what is **set**.
    """
    return f"{bitrate / 1000:g}"


def frame_type(extended: bool) -> FrameType:
    return FrameType.EXTENDED if extended else FrameType.STANDARD


def can_id_field(can_id: int) -> str:
    """The identifier in hex and **without** ``0x`` — the way the device wants it."""
    return format(can_id, "X")
