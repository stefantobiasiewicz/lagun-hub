"""The watch over SocketCAN buses: a bus that goes down is a failure, unless the hub took
it down itself (zadania/zrobione/BUG-04.md).

**Who took it down decides.** The hub takes an interface down only when it reconfigures
it ("apply" in the `Buses` view) — that is a planned break, said to the watch beforehand
with `planned()`, and nothing is reported. Every other way down is a **failure**: the
controller's driver gave up, somebody ran ``ip link set can0 down`` in another terminal,
the USB adapter was pulled out, the interface was down when the hub started. For a
failure the hub says so at once and brings the bus back up by itself — the same
`buses.bring_up` the start uses, so a stick plugged back in without a bitrate gets one.

**How hard it tries** (BUG-04, D1, D2):

- three attempts, 1 s, 5 s and 30 s after the failure is seen;
- none when sudo asks for a password — a fourth try will not find one;
- more than three failures in ten minutes and the hub stops repairing and says it is the
  hardware: bringing a bus up again and again only hides a fault that keeps coming back.

Having given up, the hub still **looks** — once a second, changing nothing. A bus someone
brings up by hand, or an interface that comes back, is picked up without a restart; an
interface that comes back still down starts a new round of attempts.

**Knowing it was down at all.** RX sees ``ENETDOWN`` only if it was waiting on the socket
at that moment, and never sees an interface that was down before the hub started. So the
watch does not rely on RX: it reads the link flag and the interface index from
``/sys/class/net`` every second — two small files, no process, no privileges. The index
also catches an adapter pulled out and plugged back between two looks: same name, new
index, and the socket bound to the old one hears nothing (`SocketCanBus.reopen`).

**A thread of its own, not CORE.** Bringing a bus up runs ``sudo ip``, which can take
seconds; CORE answers nodes in milliseconds. Reports reach the model the way every
change does — through CORE's queue (`report`).

Only SocketCAN: other kinds of bus are not watched and only "do not die" applies to them
(BUG-04, Z). Next to `buses.py`, outside `core/`, for the same reason: this is where the
hub reaches under the bus into the machine.
"""

from __future__ import annotations

import datetime
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol

from lagunhub.buses import Outcome

IFF_UP: Final = 0x1
"""The administrative "up" bit in ``/sys/class/net/*/flags`` — what ``ip link set … up``
sets and ``down`` clears."""

DELAYS: Final = (1.0, 5.0, 30.0)
"""Seconds before each attempt to bring a failed bus up — before the first one counted
from when the failure was seen, then from the previous attempt (D1)."""

FUSE: Final = 3
FUSE_WINDOW: Final = 600.0
"""More than `FUSE` failures within `FUSE_WINDOW` seconds — the hub stops repairing (D2)."""

TICK: Final = 1.0
"""How often the watch looks. A failure is seen within a second; the cost is reading
two small files per bus."""


class Link(StrEnum):
    UP = "up"
    PLANNED = "planned"
    """Down because the hub took it down — reconfiguring it. Not reported."""
    FAILURE = "failure"
    """Down, not by the hub; the hub is bringing it up."""
    GAVE_UP = "gave-up"
    """Down, and the hub has stopped trying — `fault` says why. It still looks."""


@dataclass(frozen=True, slots=True)
class LinkState:
    link: Link = Link.UP
    fault: str = ""
    """One sentence for a person: what is wrong, what the hub is doing about it."""


@dataclass(frozen=True, slots=True)
class Seen:
    """One look at an interface."""

    up: bool
    ifindex: int


Probe = Callable[[str], Seen | None]
"""Look at an interface by name; ``None`` when the kernel has no such interface."""


def sysfs(root: Path = Path("/")) -> Probe:
    """`Probe` over ``/sys/class/net`` — ``root`` is for tests, as in the driver."""

    def probe(channel: str) -> Seen | None:
        path = root / "sys/class/net" / channel
        try:
            flags = int((path / "flags").read_text().strip(), 16)
            ifindex = int((path / "ifindex").read_text().strip())
        except (OSError, ValueError):
            return None
        return Seen(up=bool(flags & IFF_UP), ifindex=ifindex)

    return probe


class Reopenable(Protocol):
    """What the watch needs of a bus: `SocketCanBus`."""

    ifindex: int

    def reopen(self) -> None: ...


@dataclass
class _Watched:
    name: str
    channel: str
    bus: Reopenable
    repair: Callable[[], Outcome]
    state: LinkState = LinkState()
    attempts: int = 0
    next_at: float = 0.0
    failures: deque[float] = field(default_factory=deque)
    planned: int = 0
    present: bool = True
    """Whether the last look found the interface at all."""

    @property
    def label(self) -> str:
        return f"bus {self.name} ({self.channel})"


class BusWatch(threading.Thread):
    def __init__(
        self,
        *,
        probe: Probe | None = None,
        clock: Callable[[], float] = time.monotonic,
        report: Callable[[str], None] | None = None,
        wall: Callable[[], str] | None = None,
    ) -> None:
        super().__init__(name="buswatch", daemon=True)
        self.probe = probe or sysfs()
        self.clock = clock
        self.report: Callable[[str], None] = report or (lambda line: None)
        """Where a line for a person goes — the issues, through CORE's queue."""
        self.wall = wall or (lambda: datetime.datetime.now().strftime("%H:%M:%S"))
        """The time of day in front of each report: a failure that mended itself is
        still worth knowing *when*."""
        self._buses: dict[str, _Watched] = {}
        self._lock = threading.RLock()
        """Held for a whole look, repairs included — so `planned()` waits for a repair
        in progress instead of running ``ip link`` alongside it."""
        self._halt = threading.Event()

    # --- set-up ---

    def add(
        self,
        name: str,
        channel: str,
        bus: Reopenable,
        repair: Callable[[], Outcome],
        start: Outcome | None = None,
    ) -> None:
        """Watch a bus. ``start`` is what the hub's start did to it: when that failed,
        it was the first attempt and its issue is already reported — the watch carries
        on from there instead of saying it again."""
        now = self.clock()
        watched = _Watched(name, channel, bus, repair)
        seen = self.probe(channel)
        watched.present = seen is not None
        if not self._healthy(watched, seen) and start is not None and start.issue:
            watched.failures.append(now)
            if start.hopeless:
                watched.state = LinkState(Link.GAVE_UP, start.issue)
            else:
                watched.attempts = 1
                watched.next_at = now + DELAYS[1]
                watched.state = LinkState(Link.FAILURE, start.issue)
        with self._lock:
            self._buses[channel] = watched

    # --- what others ask ---

    def state(self, channel: str) -> LinkState | None:
        """`None` for a bus not watched. No lock: one reference, read whole."""
        watched = self._buses.get(channel)
        return watched.state if watched is not None else None

    @contextmanager
    def planned(self, channel: str) -> Iterator[None]:
        """A break the hub makes itself: while inside, a bus that is down is not a
        failure. Back outside, the next look judges it as usual — a bus the
        reconfiguring left down is a failure from then on."""
        with self._lock:
            watched = self._buses.get(channel)
            if watched is not None:
                watched.planned += 1
        try:
            yield
        finally:
            if watched is not None:
                with self._lock:
                    watched.planned -= 1

    # --- the thread ---

    def run(self) -> None:
        while not self._halt.wait(TICK):
            self.tick()

    def stop(self) -> None:
        self._halt.set()

    def tick(self, now: float | None = None) -> None:
        """One look at every bus. Tests call this instead of start()."""
        now = self.clock() if now is None else now
        with self._lock:
            for watched in self._buses.values():
                self._check(watched, now)

    # --- the rules ---

    def _check(self, watched: _Watched, now: float) -> None:
        seen = self.probe(watched.channel)
        came_back = seen is not None and not watched.present
        watched.present = seen is not None
        if watched.planned:
            watched.state = LinkState(Link.PLANNED)
            return
        # Up, but not the interface the socket is bound to: it was gone and came back
        # between two looks, or was not there when the hub started.
        stale = seen is not None and seen.up and seen.ifindex != watched.bus.ifindex
        if stale and self._reopen(watched) and watched.state.link is Link.UP:
            self._say(f"{watched.label} came back as a new interface — the hub reopened it")
        link = watched.state.link
        if self._healthy(watched, seen):
            if link in (Link.FAILURE, Link.GAVE_UP):
                self._say(f"{watched.label} is working again")
            watched.state = LinkState(Link.UP)
            return
        why = "the interface is gone" if seen is None else "it is down"
        if link in (Link.UP, Link.PLANNED):
            self._begin(watched, now, f"{watched.label} failed — {why}, and not by the hub")
        elif link is Link.FAILURE and now >= watched.next_at:
            self._attempt(watched, now)
        elif link is Link.GAVE_UP and came_back:
            self._begin(watched, now, f"{watched.label} is back, but {why}")

    def _begin(self, watched: _Watched, now: float, what: str) -> None:
        """A new failure: count it against the fuse, then schedule the first attempt."""
        watched.failures.append(now)
        while watched.failures and now - watched.failures[0] > FUSE_WINDOW:
            watched.failures.popleft()
        if len(watched.failures) > FUSE:
            self._give_up(
                watched,
                f"{what} — {len(watched.failures)} failures in "
                f"{FUSE_WINDOW / 60:.0f} min, the hub stops bringing it up: look at the "
                "hardware (cable, termination, power, the adapter, `dmesg`)",
            )
            return
        watched.attempts = 0
        watched.next_at = now + DELAYS[0]
        fault = f"{what} — the hub is bringing it up"
        watched.state = LinkState(Link.FAILURE, fault)
        self._say(fault)

    def _attempt(self, watched: _Watched, now: float) -> None:
        outcome = watched.repair()
        watched.attempts += 1
        seen = self.probe(watched.channel)
        if seen is not None and seen.up and seen.ifindex != watched.bus.ifindex:
            self._reopen(watched)
            seen = self.probe(watched.channel)
        if not outcome.issue and self._healthy(watched, seen):
            watched.state = LinkState(Link.UP)
            self._say(f"{watched.label} is working again — brought up by the hub")
            return
        reason = outcome.issue or "it stays down"
        if outcome.hopeless:
            self._give_up(watched, reason)
        elif watched.attempts >= len(DELAYS):
            self._give_up(
                watched,
                f"the hub could not bring {watched.label} up in {watched.attempts} attempts "
                f"— {reason}",
            )
        else:
            watched.next_at = now + DELAYS[watched.attempts]
            watched.state = LinkState(
                Link.FAILURE,
                f"{watched.label}: attempt {watched.attempts} of {len(DELAYS)} failed — {reason}",
            )

    def _give_up(self, watched: _Watched, fault: str) -> None:
        watched.state = LinkState(Link.GAVE_UP, fault)
        self._say(fault)

    def _reopen(self, watched: _Watched) -> bool:
        try:
            watched.bus.reopen()
        except OSError:
            return False
        return True

    @staticmethod
    def _healthy(watched: _Watched, seen: Seen | None) -> bool:
        return seen is not None and seen.up and seen.ifindex == watched.bus.ifindex

    def _say(self, text: str) -> None:
        self.report(f"{self.wall()} {text}")
