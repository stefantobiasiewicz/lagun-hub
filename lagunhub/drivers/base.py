"""Driver interface for a CAN adapter — the thing that **connects** the hub to a bus.

An adapter is not a bus. ``Bus`` answers "how do I send a frame"; an adapter driver
answers **"is this piece of hardware set up the way the installation needs it"** —
bitrate, extended frames, CAN FD off. Two different questions, so two interfaces:
``Bus`` lives in the flow of frames, the driver in a configuration that is moved once
and checked afterwards.

The same interface has to serve two devices that have nothing in common:

* **USR-CAN315** — an Ethernet↔CAN box configured with AT commands over the network;
* **SocketCAN** — any CAN controller the kernel sees (an MCP2518FD on the Raspberry
  Pi's SPI, a USB stick), configured with ``ip link``.

What they share is the **question**, not the way of answering it. So the interface
speaks of intent ("500 kbit/s, extended frames, no FD") and each driver knows on its
own whether that means ``AT+CAN=500,1,EDTF`` or ``ip link set can0 type can bitrate 500000``.

**The ``plan`` / ``apply`` split is deliberate.** An adapter can take the bus down for
several seconds, and an address change can make it disappear for good. People have the
right to see the list of changes before any of them goes out — first what is wrong,
and only on request the fix.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable


class DriverError(Exception):
    """Anything that stopped the conversation with the adapter from finishing."""


class Unreachable(DriverError):
    """The adapter does not answer.

    Its own exception, because this is the one failure that **must not be confused with
    a refusal**: silence means a wrong address, a pulled cable or a device in reset —
    not "bad parameter".
    """


class Rejected(DriverError):
    """The adapter answered, and the answer was no."""

    def __init__(self, command: str, detail: str) -> None:
        super().__init__(f"{command}: {detail}")
        self.command = command
        self.detail = detail


class NotSupported(DriverError):
    """This adapter does not have that parameter and never will.

    Raised **instead of silently skipping it**. A configuration that "was applied" but
    did not take effect costs more than an error on the first command.
    """


class Mode(Enum):
    """Operating mode of the CAN controller."""

    NORMAL = "normal"
    LISTEN_ONLY = "listen-only"
    """Does not acknowledge frames. For watching someone else's bus — and for testing
    an adapter before there is a second node on ours."""
    LOOPBACK = "loopback"


@dataclass(frozen=True, slots=True)
class CanSettings:
    """The core of the configuration — what matters to **every** adapter.

    The defaults are LagunBus values, not the manufacturer's: CAN 2.0B, 500 kbit/s,
    extended frames (``05-mapowanie-can.md``). An adapter out of the box has different
    ones, and that is the whole reason this module exists.
    """

    bitrate: int = 500_000
    extended: bool = True
    """Extended frames (29-bit identifier). LagunBus uses nothing else."""
    fd: bool = False
    """CAN FD. **Always off.** An FD frame on this bus is a frame nobody understands —
    and adapters like to ship with FD enabled."""
    mode: Mode = Mode.NORMAL


@dataclass(frozen=True, slots=True)
class AdapterInfo:
    """Who this is and where it stands. For printing to a person, not for logic."""

    model: str
    firmware: str = ""
    serial: str = ""
    where: str = ""
    """IP address, device path, interface name — whatever the driver has."""


@dataclass(frozen=True, slots=True)
class Change:
    """One difference between what is in the adapter and what should be.

    ``how`` is a complete command, not a delta: AT commands set a whole triple of
    parameters at once, so "change only the bitrate" does not exist. A change covers as
    much as one command covers — and shows the person exactly that much.
    """

    what: str
    current: str
    desired: str
    how: str
    manual: bool = False
    """``True``: the driver **will not do this**. An interface the kernel does not have,
    a virtual one that is down, a termination jumper — ``how`` then tells the person what
    to do."""


@runtime_checkable
class AdapterDriver(Protocol):
    """A driver for one adapter. One configuration session, not a frame path."""

    model: str

    def open(self) -> None:
        """Make contact and enter configuration mode. Raises ``Unreachable``."""
        ...

    def close(self) -> None:
        """Leave configuration mode and release resources. Safe to call repeatedly."""
        ...

    def info(self) -> AdapterInfo: ...

    def read(self) -> CanSettings:
        """What the adapter has **now**. Drivers return their own richer subtypes."""
        ...

    def plan(self, desired: CanSettings) -> tuple[Change, ...]:
        """What needs changing. **Changes nothing** — an empty tuple means "in sync"."""
        ...

    def apply(self, changes: Sequence[Change]) -> None:
        """Carry out changes from ``plan``. Skips the ``manual`` ones."""
        ...

    def persist(self) -> None:
        """Save so that it survives a power cut."""
        ...

    def restart(self) -> None:
        """Restart the adapter so the changes take effect."""
        ...


def configure(
    driver: AdapterDriver, desired: CanSettings, *, dry_run: bool = False
) -> tuple[Change, ...]:
    """Bring the adapter to ``desired``. Returns what is left to be done by hand.

    The order is the only sensible one: change → save permanently → restart. The other
    way round gives an adapter that works until the first power cut and then returns to
    factory settings — a fault nobody finds, because it shows up weeks later.

    ``dry_run`` returns the plan alone. For showing it to a person.
    """
    plan = driver.plan(desired)
    by_hand = tuple(change for change in plan if change.manual)
    automatic = [change for change in plan if not change.manual]
    if dry_run:
        return plan
    if not automatic:
        return by_hand
    driver.apply(automatic)
    driver.persist()
    driver.restart()
    return by_hand
