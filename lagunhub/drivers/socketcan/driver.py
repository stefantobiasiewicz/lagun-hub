"""Driver for a SocketCAN interface — ``can0``, ``can1``… whatever controller is behind it.

The same contract as the USR-CAN315, answered from a different world: the bitrate, the
restart delay, FD and listen-only live in **the kernel**, are set with ``ip link`` and
are lost at every reboot. The hub sets them at start (zadania/zrobione/FEAT-01.md, R3).

**The route is the kind, not the chip.** ``ip link set can0 type can bitrate 500000`` is
the same command for an ``mcp251xfd`` on SPI, an ``mcp251x`` and a ``gs_usb`` stick, so
one driver serves them all. It started as an MCP2518FD driver that also read the device
tree overlay in ``config.txt`` — the SPI device, the interrupt pin, the crystal — and
refused any other chip. That part was dropped, not moved: what sits **under** the kernel
is the installation of the machine, done once by a person with a keyboard and a reboot
(``hub/README.md``, Raspberry Pi), not something the hub manages (FEAT-01, R4). When the
interface is not there at all, the plan says so and points at ``dmesg``, nothing more.

**Virtual interfaces are only checked.** A ``vcan`` has no timing, and creating or
raising one takes ``modprobe`` and ``ip link add`` — more than the hub's sudo rule
allows, for a bus that exists only for trials (FEAT-01, R7). A ``vcan`` that is down
comes out of ``plan`` as a change for a person.

**Nothing is persisted.** ``ip link`` settings die with every reboot and that is
accepted: the hub brings the interface up at start by calling ``apply``, so the file
the hub reads stays the single place where the bitrate is written down. ``persist`` is
therefore empty, on purpose.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol, runtime_checkable

from ..base import (
    AdapterInfo,
    CanSettings,
    Change,
    DriverError,
    NotSupported,
    Rejected,
    Unreachable,
)
from . import iproute

MODEL: Final = "SocketCAN"
ARPHRD_CAN: Final = 280
"""Interface type in ``/sys/class/net/*/type`` — a CAN interface recognised without ``ip``."""

MAX_BITRATE: Final = 1_000_000


# --- running commands ------------------------------------------------------------


@runtime_checkable
class Shell(Protocol):
    """A way to run a command. Separate from the driver so tests need no Linux."""

    def run(self, argv: Sequence[str], *, privileged: bool = False) -> str:
        """Run ``argv``, return its stdout. Raises ``Rejected`` on a non-zero exit."""
        ...


class SystemShell:
    """``subprocess`` with ``sudo -n`` in front of what needs privileges.

    ``-n`` because a hub waiting for a password nobody will type is worse than a hub
    that fails at once with a sentence saying why. Root does not get ``sudo`` at all.
    """

    def __init__(self, *, sudo: bool = True, timeout: float = 10.0) -> None:
        self.sudo = sudo
        self.timeout = timeout

    def run(self, argv: Sequence[str], *, privileged: bool = False) -> str:
        command = list(argv)
        if privileged and self.sudo and os.geteuid() != 0:
            command = ["sudo", "-n", *command]
        try:
            done = subprocess.run(
                command, capture_output=True, text=True, timeout=self.timeout, check=False
            )
        except FileNotFoundError as exc:
            raise Unreachable(f"{command[0]} not found: not Linux, or iproute2 missing") from exc
        except subprocess.TimeoutExpired as exc:
            raise Unreachable(f"{shlex.join(command)} did not finish in {self.timeout} s") from exc
        if done.returncode != 0:
            detail = done.stderr.strip() or f"exit status {done.returncode}"
            if "password is required" in detail:
                detail += "; give the hub's user a NOPASSWD sudo rule for ip, or run as root"
            raise Rejected(shlex.join(argv), detail)
        return done.stdout


# --- settings --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SocketCanSettings(CanSettings):
    """SocketCAN configuration: the ``CanSettings`` core plus what the kernel adds.

    ``extended`` from the core has nothing to set here: SocketCAN passes both frame types
    and the hub decides what it sends. It is accepted and left alone.
    """

    restart_ms: int = 100
    """Automatic recovery from bus-off. Zero leaves the interface dead until someone
    takes it down and up by hand."""
    txqueuelen: int = 10
    """The kernel's transmit queue. The hub's fast path drops frames when it is full
    rather than wait, so its length is a real parameter, not a tuning knob."""


LAGUNBUS: Final = SocketCanSettings()
"""LagunBus on a SocketCAN interface: CAN 2.0B at 500 kbit/s, FD off, ``restart-ms 100``
as in ``hub/tools/can-up.sh``."""


@dataclass(frozen=True, slots=True)
class Interface:
    """A CAN interface found in ``/sys``, with what is behind it."""

    name: str
    device: str = ""
    """``spi0.0`` for an SPI chip, a USB path for a stick; empty for virtual interfaces."""
    driver: str = ""
    """Kernel driver: ``mcp251xfd``, ``mcp251x``, ``gs_usb``…; empty for ``vcan``."""


def interfaces(root: Path = Path("/")) -> tuple[Interface, ...]:
    """Every CAN interface on this machine, virtual ones included."""
    net = root / "sys/class/net"
    if not net.is_dir():
        return ()
    found: list[Interface] = []
    for path in sorted(net.iterdir()):
        if _read(path / "type") != str(ARPHRD_CAN):
            continue
        found.append(Interface(path.name, _link_name(path / "device"), _driver(path)))
    return tuple(found)


# --- driver ----------------------------------------------------------------------


class SocketCanDriver:
    """A driver for one SocketCAN interface. Satisfies ``AdapterDriver``."""

    model = MODEL

    def __init__(
        self, interface: str = "can0", *, shell: Shell | None = None, root: Path = Path("/")
    ) -> None:
        self.interface = interface
        self._shell = shell or SystemShell()
        self._root = root

    def open(self) -> None:
        """Nothing to connect to — only a check that this is a machine with ``/sys``."""
        if not self._net.is_dir():
            raise Unreachable("no /sys/class/net: this is not Linux")

    def close(self) -> None:
        pass

    def __enter__(self) -> SocketCanDriver:
        self.open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def info(self) -> AdapterInfo:
        path = self._net / self.interface
        device = _link_name(path / "device")
        return AdapterInfo(
            model=MODEL,
            firmware=_driver(path),
            where=f"{device} · {self.interface}" if device else self.interface,
        )

    def read(self) -> SocketCanSettings:
        """What the kernel holds for this interface now.

        Raises ``Unreachable`` when the interface does not exist — ``plan`` still works
        then and says why.
        """
        link = self._link()
        if link is None:
            raise Unreachable(f"{self.interface} does not exist")
        return SocketCanSettings(
            bitrate=link.bitrate,
            fd=link.fd,
            mode=link.mode,
            restart_ms=link.restart_ms,
            txqueuelen=link.txqueuelen,
        )

    def plan(self, desired: CanSettings) -> tuple[Change, ...]:
        """One ``ip link set`` per parameter that differs — or one change for a person,
        when there is no interface to set anything on, or it is a virtual one."""
        wanted = _as_socketcan(desired)
        if wanted.fd:
            raise NotSupported(
                "CAN FD needs a data bitrate this driver does not set; LagunBus is CAN 2.0B"
            )
        if not 0 < wanted.bitrate <= MAX_BITRATE:
            raise ValueError(f"{wanted.bitrate} bit/s is outside 1..{MAX_BITRATE}")

        link = self._link()
        if link is None:
            return (
                Change(
                    what="interface",
                    current="missing",
                    desired=self.interface,
                    how=f"the kernel has no {self.interface}: the controller did not come up "
                    "or is not described to the kernel — `dmesg`, and for a chip on SPI "
                    "its overlay in config.txt (hub/README.md)",
                    manual=True,
                ),
            )
        if link.virtual:
            if link.up:
                return ()
            return (
                Change(
                    what="link",
                    current="DOWN",
                    desired="UP",
                    how=f"sudo ./tools/can-up.sh {self.interface} — the hub does not touch "
                    "virtual interfaces",
                    manual=True,
                ),
            )
        changes: list[Change] = []
        self._plan_kernel(changes, wanted, link)
        return tuple(changes)

    def apply(self, changes: Sequence[Change]) -> None:
        """Down, every ``ip link set`` from the plan, up.

        Timing changes only while the interface is down, so it goes down even for a
        single change — and always comes back up, so a plan that only said "bring it up"
        is carried out by the same three steps.

        Only commands aimed at this interface are run. ``Change.how`` is text that a
        person may have seen and edited on the way; the driver does not run whatever
        text arrives.
        """
        commands = [shlex.split(change.how) for change in changes if not change.manual]
        if not commands:
            return
        prefix = [iproute.IP, "link", "set", self.interface]
        for argv in commands:
            if argv[:4] != prefix:
                raise DriverError(f"refusing to run {shlex.join(argv)!r} for {self.interface}")
        self._shell.run(iproute.down(self.interface), privileged=True)
        for argv in commands:
            if argv != iproute.up(self.interface):
                self._shell.run(argv, privileged=True)
        self._shell.run(iproute.up(self.interface), privileged=True)

    def persist(self) -> None:
        """Nothing: the hub brings the interface up at start with ``apply``.

        A decision, not an omission — see the module description. A systemd-networkd
        file would keep a second copy of the bitrate outside the hub's configuration,
        and two copies drift.
        """

    def restart(self) -> None:
        """Down and up. Clears bus-off at once instead of waiting for ``restart-ms``."""
        self._shell.run(iproute.down(self.interface), privileged=True)
        self._shell.run(iproute.up(self.interface), privileged=True)

    # --- kernel ---

    def _plan_kernel(
        self, changes: list[Change], wanted: SocketCanSettings, link: iproute.Link
    ) -> None:
        """One change per parameter, each a complete ``ip`` command of its own."""
        name = self.interface
        self._compare(
            changes,
            "bitrate",
            _bps(link.bitrate),
            _bps(wanted.bitrate),
            iproute.can(name, "bitrate", str(wanted.bitrate)),
        )
        self._compare(
            changes,
            "restart after bus-off",
            f"{link.restart_ms} ms",
            f"{wanted.restart_ms} ms",
            iproute.can(name, "restart-ms", str(wanted.restart_ms)),
        )
        self._compare(
            changes,
            "CAN FD",
            iproute.on_off(link.fd),
            iproute.on_off(wanted.fd),
            iproute.can(name, "fd", "off"),
        )
        self._compare(
            changes,
            "mode",
            link.mode.value,
            wanted.mode.value,
            iproute.can(name, *iproute.mode_options(wanted.mode)),
        )
        self._compare(
            changes,
            "transmit queue",
            str(link.txqueuelen),
            str(wanted.txqueuelen),
            iproute.txqueuelen(name, wanted.txqueuelen),
        )
        self._compare(changes, "link", "UP" if link.up else "DOWN", "UP", iproute.up(name))

    @staticmethod
    def _compare(
        changes: list[Change], what: str, current: str, desired: str, argv: list[str]
    ) -> None:
        if current != desired:
            changes.append(Change(what, current, desired, shlex.join(argv)))

    # --- reading ---

    @property
    def _net(self) -> Path:
        return self._root / "sys/class/net"

    def _link(self) -> iproute.Link | None:
        if not (self._net / self.interface).exists():
            return None
        return iproute.parse(self._shell.run(iproute.show(self.interface)), self.interface)


# --- helpers ---------------------------------------------------------------------


def _as_socketcan(desired: CanSettings) -> SocketCanSettings:
    """The ``CanSettings`` core → full SocketCAN settings, the rest from ``LAGUNBUS``."""
    if isinstance(desired, SocketCanSettings):
        return desired
    return SocketCanSettings(
        bitrate=desired.bitrate, extended=desired.extended, fd=desired.fd, mode=desired.mode
    )


def _driver(interface: Path) -> str:
    return _link_name(interface / "device" / "driver")


def _link_name(path: Path) -> str:
    """Last part of where a sysfs symlink points. Empty when there is no such link."""
    try:
        return path.resolve(strict=True).name
    except OSError:
        return ""


def _read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def _bps(bitrate: int) -> str:
    return f"{bitrate // 1000} kbit/s" if bitrate else "unset"
