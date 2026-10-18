"""What ``ip`` says about a CAN interface, and the ``ip`` commands that change it.

Text only, no processes — the same split as ``usr_can315.commands``. The kernel exposes
CAN timing over netlink and nowhere else, so ``ip -details -json link show`` is the one
place to read it from; ``/sys/class/net`` has the link state but not the bitrate.

The JSON shape follows iproute2's CAN printer (``ip/iplink_can.c``). Keys that are
missing — an interface that was never given a bitrate has no ``bittiming`` at all — read
as "unset" rather than as an error, because "unset" is exactly the state after a reboot.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Final

from ..base import DriverError, Mode

IP: Final = "ip"

LISTEN_ONLY: Final = "LISTEN-ONLY"
LOOPBACK: Final = "LOOPBACK"
FD: Final = "FD"


@dataclass(frozen=True, slots=True)
class Link:
    """One CAN interface as the kernel sees it right now."""

    name: str
    up: bool
    bitrate: int = 0
    """Zero means the interface was never given a bitrate — the state after a reboot."""
    restart_ms: int = 0
    ctrlmode: frozenset[str] = frozenset()
    clock: int = 0
    """The controller's clock in Hz — for a person reading ``info``; nothing compares it.
    Whether it matches the crystal is a question about ``config.txt``, which is not the
    hub's (zadania/zrobione/FEAT-01.md, R4)."""
    txqueuelen: int = 0
    state: str = ""
    """``ERROR-ACTIVE``, ``ERROR-PASSIVE``, ``BUS-OFF``, ``STOPPED``…"""
    virtual: bool = False
    """A ``vcan``: a CAN interface with no controller, so no timing and no ``ctrlmode``.
    Only its link state means anything."""

    @property
    def fd(self) -> bool:
        return FD in self.ctrlmode

    @property
    def mode(self) -> Mode:
        if LOOPBACK in self.ctrlmode:
            return Mode.LOOPBACK
        if LISTEN_ONLY in self.ctrlmode:
            return Mode.LISTEN_ONLY
        return Mode.NORMAL


def show(name: str) -> list[str]:
    """``ip -details -json link show can0`` — the read that needs no privileges."""
    return [IP, "-details", "-json", "link", "show", name]


def parse(text: str, name: str) -> Link:
    """The JSON from ``show`` → ``Link``. Raises ``DriverError`` when it is not CAN."""
    try:
        entries: Any = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DriverError(f"ip gave output that is not JSON: {exc}") from exc
    if not isinstance(entries, list) or not entries:
        raise DriverError(f"ip knows nothing about {name}")
    entry: dict[str, Any] = entries[0]

    linkinfo: dict[str, Any] = entry.get("linkinfo", {})
    up = "UP" in entry.get("flags", ())
    if linkinfo.get("info_kind") == "vcan":
        return Link(name=name, up=up, txqueuelen=int(entry.get("txqlen", 0)), virtual=True)
    if linkinfo.get("info_kind") != "can":
        kind = linkinfo.get("info_kind") or entry.get("link_type", "?")
        raise DriverError(f"{name} is not a CAN controller (kind: {kind})")
    data: dict[str, Any] = linkinfo.get("info_data", {})
    timing: dict[str, Any] = data.get("bittiming", {})

    return Link(
        name=name,
        up=up,
        bitrate=int(timing.get("bitrate", 0)),
        restart_ms=int(data.get("restart_ms", 0)),
        ctrlmode=frozenset(data.get("ctrlmode", ())),
        clock=int(data.get("clock", 0)),
        txqueuelen=int(entry.get("txqlen", 0)),
        state=str(data.get("state", "")),
    )


# --- commands that change things (all need CAP_NET_ADMIN) ------------------------


def down(name: str) -> list[str]:
    return [IP, "link", "set", name, "down"]


def up(name: str) -> list[str]:
    return [IP, "link", "set", name, "up"]


def can(name: str, *options: str) -> list[str]:
    """``ip link set can0 type can bitrate 500000``. Timing changes only while down."""
    return [IP, "link", "set", name, "type", "can", *options]


def txqueuelen(name: str, length: int) -> list[str]:
    return [IP, "link", "set", name, "txqueuelen", str(length)]


def on_off(flag: bool) -> str:
    return "on" if flag else "off"


def mode_options(mode: Mode) -> tuple[str, ...]:
    """Both flags every time: setting one does not clear the other."""
    return (
        "listen-only",
        on_off(mode is Mode.LISTEN_ONLY),
        "loopback",
        on_off(mode is Mode.LOOPBACK),
    )
