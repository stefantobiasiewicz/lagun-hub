"""`lagun-hub doctor` — what is ready on this machine and what is not.

It came about for a reason worth writing down: **the hub is built on macOS and works on
a Raspberry Pi**, and nobody sits at both at once. When "it does not work" on the RPi,
the answer has to come from the RPi — not from guesswork.

The checks are deliberately **without dependencies and without privileges**: they read
`/sys`, try to open a socket and load the configuration. None of them changes anything,
so `doctor` may be run at any moment, the hub running included.
"""

from __future__ import annotations

import socket
import stat
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Final

from lagunhub import __version__
from lagunhub.adapters.http.auth import TOKEN_FILE
from lagunhub.core.config import ConfigError, load
from lagunhub.protocol.bus.socketcan import HAS_SOCKETCAN


def how_many(count: int, singular: str, plural: str | None = None) -> str:
    """`1 node`, `2 nodes`, `0 nodes`.

    This is the only place in the hub where a number goes straight into a sentence read
    by a human, so it is the only place that has to care how the noun after it looks.
    """
    if count == 1:
        return f"{count} {singular}"
    return f"{count} {plural or singular + 's'}"


ARPHRD_CAN: Final = 280
"""The interface kind in `/sys/class/net/*/type`. This is how a CAN is recognised
without `ip`."""

SYS_NET: Final = Path("/sys/class/net")


class Level(Enum):
    OK = "✓"
    WARNING = "!"
    ERROR = "✖"
    IRRELEVANT = "·"


@dataclass(frozen=True, slots=True)
class Check:
    state: Level
    what: str
    detail: str = ""
    advice: str = ""
    """What to do about it. Empty means "nothing, this is how it should be"."""


def check(config: Path) -> list[Check]:
    """Every check, one after another. None of them stops the ones that follow."""
    return [
        *_environment(),
        *_socketcan(),
        *_interfaces(),
        *_configuration(config),
    ]


# --- environment ------------------------------------------------------------------


def _environment() -> list[Check]:
    import platform
    import sys

    return [
        Check(Level.OK, "hub", f"lagun-hub {__version__}"),
        Check(
            Level.OK if sys.version_info >= (3, 12) else Level.ERROR,
            "python",
            platform.python_version(),
            "" if sys.version_info >= (3, 12) else "3.12 or newer required",
        ),
        Check(Level.OK, "system", f"{platform.system()} {platform.release()} {platform.machine()}"),
    ]


def _socketcan() -> list[Check]:
    if HAS_SOCKETCAN:
        return [Check(Level.OK, "SocketCAN", "AF_CAN available")]
    return [
        Check(
            Level.WARNING,
            "SocketCAN",
            "no AF_CAN — this machine is not a Linux",
            "the hub will only start on a virtual bus",
        )
    ]


# --- interfaces -------------------------------------------------------------------


def _interfaces() -> list[Check]:
    if not SYS_NET.is_dir():
        return []
    found_ones = sorted(_can_interfaces())
    if not found_ones:
        return [
            Check(
                Level.WARNING,
                "CAN interfaces",
                "none",
                "vcan: `sudo modprobe vcan && sudo ip link add dev vcan0 type vcan"
                " && sudo ip link set up vcan0`",
            )
        ]
    return [_interface(name) for name in found_ones]


def _can_interfaces() -> list[str]:
    out: list[str] = []
    for path in SYS_NET.iterdir():
        try:
            if int((path / "type").read_text()) == ARPHRD_CAN:
                out.append(path.name)
        except (OSError, ValueError):
            continue
    return out


def _interface(name: str) -> Check:
    """The interface state straight from `/sys` — no `ip`, no `subprocess`."""
    directory = SYS_NET / name
    operstate = _read(directory / "operstate")
    bitrate = _read(directory / "can_bittiming" / "bitrate")
    details = [operstate or "?"]
    if bitrate:
        details.append(f"{int(bitrate) // 1000} kbit/s")
    elif not name.startswith("vcan"):
        details.append("no timing")

    if operstate not in ("up", "unknown"):
        return Check(
            Level.ERROR,
            f"interface {name}",
            " · ".join(details),
            f"`sudo ip link set {name} up type can bitrate 500000`",
        )
    if not bitrate and not name.startswith("vcan"):
        return Check(
            Level.WARNING,
            f"interface {name}",
            " · ".join(details),
            "the interface is up but has no timing — check whether the bitrate is set",
        )

    reason = _open(name)
    if reason:
        return Check(
            Level.ERROR,
            f"interface {name}",
            f"{' · '.join(details)} · {reason}",
            "the hub will not open this bus — check which user the service runs as",
        )
    details.append("the socket opens")
    return Check(Level.OK, f"interface {name}", " · ".join(details))


def _open(name: str) -> str:
    """Really try to open the socket. Returns the reason for failure, or an empty name.

    The only check that **does the same thing the hub will do** — and therefore the only
    one that answers the question about privileges. Guesswork in this matter is
    worthless: it depends on the kernel, on the distribution, and on whether someone
    gave the service a different user.
    """
    if not HAS_SOCKETCAN:
        return ""
    from lagunhub.protocol.bus import SocketCanBus

    try:
        bus = SocketCanBus(name)
    except OSError as exc:
        return str(exc)
    bus.close()
    return ""


def _read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


# --- configuration ----------------------------------------------------------------


def _configuration(config: Path) -> list[Check]:
    if not config.exists():
        return [
            Check(
                Level.ERROR,
                "configuration",
                f"{config} does not exist",
                "point at a directory with --config, or copy the example from the repository",
            )
        ]
    try:
        loaded = load(config)
    except (ConfigError, OSError) as exc:
        return [
            Check(
                Level.ERROR,
                "configuration",
                str(exc),
                "the hub will not start with a configuration like this",
            )
        ]

    out = [
        Check(
            Level.OK,
            "configuration",
            how_many(len(loaded.nodes), "node") + ", " + how_many(len(loaded.bindings), "binding"),
        )
    ]
    out += [_bus(bus.channel, bus.bitrate) for bus in loaded.buses]
    out.append(_listen(loaded.api.host, loaded.api.port))
    out.append(_token(config))
    return out


def _bus(channel: str, bitrate: int) -> Check:
    """Whether the interface from the configuration exists at all — the commonest
    mistake after a move."""
    if not SYS_NET.is_dir():
        return Check(Level.IRRELEVANT, f"bus {channel}", "not this system")
    if not (SYS_NET / channel).exists():
        return Check(
            Level.ERROR,
            f"bus {channel}",
            "there is no such interface",
            f"the config points at `{channel}` — either fix the file or bring the interface up",
        )
    configured = _read(SYS_NET / channel / "can_bittiming" / "bitrate")
    if configured and int(configured) != bitrate:
        return Check(
            Level.WARNING,
            f"bus {channel}",
            f"the kernel has {int(configured) // 1000} kbit/s, the file says {bitrate // 1000}",
            "the speed is not set by the hub but by the kernel — a drift means someone forgot",
        )
    return Check(Level.OK, f"bus {channel}", f"{bitrate // 1000} kbit/s")


def _listen(host: str, port: int) -> Check:
    """Whether the port is free — and, along the way, whether the hub is not already
    running next door."""
    exposed = host not in ("127.0.0.1", "localhost", "::1")
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError as exc:
            return Check(
                Level.ERROR,
                f"API {host}:{port}",
                str(exc),
                "the port is taken — is the hub not running already? `systemctl status lagun-hub`",
            )
    if exposed:
        return Check(
            Level.WARNING,
            f"API {host}:{port}",
            "exposed to the network",
            "the token is then the ONLY protection — check who you gave it to",
        )
    return Check(Level.OK, f"API {host}:{port}", "loopback")


def _token(config: Path) -> Check:
    file_ = config / TOKEN_FILE if config.is_dir() else config.parent / TOKEN_FILE
    if not file_.exists():
        return Check(
            Level.OK,
            "token",
            "not there yet",
            "it will be drawn at the first start and printed once",
        )
    mode = stat.S_IMODE(file_.stat().st_mode)
    if mode & 0o077:
        return Check(
            Level.ERROR,
            "token",
            f"{file_} has mode {mode:04o}",
            f"`chmod 600 {file_}` — otherwise everyone on the machine can read it",
        )
    return Check(Level.OK, "token", f"{file_}, mode {mode:04o}")


# --- printout ---------------------------------------------------------------------


def report(results: list[Check]) -> str:
    width = max((len(c.what) for c in results), default=0)
    lines: list[str] = []
    for c in results:
        lines.append(f" {c.state.value}  {c.what.ljust(width)}  {c.detail}".rstrip())
        if c.advice:
            lines.append(f"    {' ' * width}  → {c.advice}")
    return "\n".join(lines)


def summary(results: list[Check]) -> tuple[str, int]:
    """The closing sentence and the exit code. An error means the hub will not start,
    or will start wrong."""
    errors = sum(1 for c in results if c.state is Level.ERROR)
    warnings = sum(1 for c in results if c.state is Level.WARNING)
    if errors:
        return how_many(errors, "thing") + " to fix before starting", 1
    if warnings:
        return f"everything works; {how_many(warnings, 'thing')} worth a look", 0
    return "everything ready", 0


__all__ = ["Check", "Level", "check", "how_many", "report", "summary"]
