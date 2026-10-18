"""Buses at start: set the hardware up as the file says, then open the sockets.

Starting the hub is **one command** and the hub copes by itself (zadania/zrobione/FEAT-01.md,
R3): after a reboot of the Raspberry Pi ``can0`` is down and has no bitrate, and it used
to take ``sudo ./tools/can-up.sh can0 500000`` before ``lagun-hub run``. Now the hub
asks the driver for the difference between the interface and ``hub.yaml`` and carries
it out before it opens the socket.

**What it cannot do does not stop it.** A missing sudo rule, an interface the kernel
does not have, a ``vcan`` that is down — each becomes an issue with the sentence that
fixes it, and the hub starts anyway: the API is up, the view shows the issue, and the
other buses work (R5). A bus whose socket will not open at all is left out of this run.
The one exception is a machine without SocketCAN at all — that is not one missing bus
but the wrong machine, and ``run`` stops with its hint about ``--udp``.

Kept out of ``core/`` on purpose: this runs once, before the threads exist, and is the
only place where the hub reaches under the bus into the machine.
"""

from __future__ import annotations

import getpass
import shutil
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

from lagunhub.core.config import BusConfig
from lagunhub.drivers.base import CanSettings, Change, DriverError, Rejected
from lagunhub.drivers.socketcan import SocketCanSettings
from lagunhub.protocol.bus.base import Bus


class Driver(Protocol):
    """The part of ``AdapterDriver`` the start uses: no ``open``, no ``persist``."""

    def plan(self, desired: CanSettings) -> tuple[Change, ...]: ...

    def apply(self, changes: Sequence[Change]) -> None: ...


@dataclass(frozen=True, slots=True)
class Outcome:
    """What happened to one bus at start."""

    bus: BusConfig
    applied: tuple[Change, ...] = ()
    issue: str = ""
    """Empty when the bus is as the file says. Otherwise one sentence for the issue
    list — what is wrong and what to do about it."""
    hopeless: bool = False
    """Trying again will not help: sudo asked for a password. The watch over the buses
    gives up at once instead of spending its attempts (zadania/zrobione/BUG-04.md, D1)."""

    def line(self) -> str:
        """One line for the console."""
        head = f"bus {self.bus.name} ({self.bus.channel}) · {self.bus.bitrate // 1000} kbit/s"
        if self.issue:
            return f"!! {self.issue}"
        if self.applied:
            return f"{head} · set: {', '.join(change.what for change in self.applied)}"
        return f"{head} · as configured"


def settings(bus: BusConfig) -> SocketCanSettings:
    """The file's entry → what the driver is asked for. Only the bitrate is in the file;
    restart-ms, FD off and the queue are LagunBus values, not a per-house choice."""
    return SocketCanSettings(bitrate=bus.bitrate)


def bring_up(bus: BusConfig, driver: Driver) -> Outcome:
    """Plan, then carry out what the driver may do by itself.

    ``plan`` + ``apply`` and not ``base.configure``: ``configure`` follows with
    ``persist`` and ``restart``, which for SocketCAN means nothing and a second
    down-and-up of an interface ``apply`` has just brought up.
    """
    label = f"bus {bus.name} ({bus.channel})"
    try:
        plan = driver.plan(settings(bus))
        automatic = tuple(change for change in plan if not change.manual)
        by_hand = [change for change in plan if change.manual]
        if automatic:
            driver.apply(automatic)
    except Rejected as exc:
        hint = sudo_hint(exc)
        return Outcome(bus, issue=f"{label}: {exc}{hint}", hopeless=bool(hint))
    except (DriverError, ValueError) as exc:
        return Outcome(bus, issue=f"{label}: {exc}")
    if by_hand:
        todo = "; ".join(f"{change.what} — {change.how}" for change in by_hand)
        return Outcome(bus, automatic, issue=f"{label} needs a person: {todo}")
    return Outcome(bus, automatic)


def open_buses(
    configs: Iterable[BusConfig], opener: Callable[[BusConfig], Bus]
) -> tuple[list[Bus], list[str]]:
    """Open every bus that will open. Returns the buses and an issue per one left out.

    ``RuntimeError`` (no SocketCAN on this machine) is let through — see the module
    description.
    """
    opened: list[Bus] = []
    issues: list[str] = []
    for config in configs:
        try:
            opened.append(opener(config))
        except OSError as exc:
            issues.append(
                f"bus {config.name} ({config.channel}) did not open: {exc} — the hub runs "
                "without it; restart the hub once the interface is there"
            )
    return opened, issues


def sudo_hint(exc: Rejected) -> str:
    """The exact sudoers line when sudo asked for a password (R5)."""
    if "password is required" not in exc.detail:
        return ""
    ip = shutil.which("ip") or "/usr/sbin/ip"
    return (
        f" — run tools/setup-rpi.sh, or add to /etc/sudoers.d/lagun-hub: "
        f"`{getpass.getuser()} ALL=(root) NOPASSWD: {ip} link set can*`"
    )
