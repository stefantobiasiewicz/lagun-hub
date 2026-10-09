"""The watch over SocketCAN buses (zadania/zrobione/BUG-04.md) — on a fake interface and a clock
driven from the test."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from lagunhub.buses import Outcome
from lagunhub.buswatch import DELAYS, FUSE, BusWatch, Link, Seen, sysfs
from lagunhub.core.config import BusConfig
from tests.conftest import Clock

CONFIG = BusConfig(name="main", channel="can0")


@dataclass
class Kernel:
    """One interface as the kernel holds it."""

    up: bool = True
    ifindex: int = 3
    present: bool = True

    def look(self, channel: str) -> Seen | None:
        assert channel == "can0"
        return Seen(self.up, self.ifindex) if self.present else None

    def unplug(self) -> None:
        self.present = False

    def plug(self, *, up: bool) -> None:
        self.present = True
        self.up = up
        self.ifindex += 1


@dataclass
class Socket:
    """`SocketCanBus` as the watch sees it: bound to an index, reopened onto the current."""

    kernel: Kernel
    ifindex: int = 3
    reopened: int = 0

    def reopen(self) -> None:
        if not self.kernel.present:
            raise OSError(19, "ENODEV")
        self.ifindex = self.kernel.ifindex
        self.reopened += 1


@dataclass
class Repair:
    """`buses.bring_up` as the watch sees it."""

    kernel: Kernel
    works: bool = True
    hopeless: bool = False
    calls: list[float] = field(default_factory=list)
    clock: Clock | None = None

    def __call__(self) -> Outcome:
        self.calls.append(self.clock.now if self.clock else 0.0)
        if self.hopeless:
            return Outcome(CONFIG, issue="sudo: a password is required", hopeless=True)
        if not self.works or not self.kernel.present:
            return Outcome(CONFIG, issue="RTNETLINK answers: Device or resource busy")
        self.kernel.up = True
        return Outcome(CONFIG)


@dataclass
class World:
    clock: Clock
    kernel: Kernel
    socket: Socket
    repair: Repair
    watch: BusWatch
    said: list[str]

    def run(self, seconds: float) -> None:
        """Look once a second, as the thread does."""
        end = self.clock.now + seconds
        while self.clock.now < end:
            self.clock.advance(1.0)
            self.watch.tick()

    def link(self) -> Link:
        state = self.watch.state("can0")
        assert state is not None
        return state.link


def world(start: Outcome | None = None, *, up: bool = True) -> World:
    clock = Clock()
    kernel = Kernel(up=up)
    socket = Socket(kernel)
    repair = Repair(kernel, clock=clock)
    said: list[str] = []
    watch = BusWatch(probe=kernel.look, clock=clock, report=said.append, wall=lambda: "12:00:00")
    watch.add("main", "can0", socket, repair, start)
    return World(clock, kernel, socket, repair, watch, said)


def test_a_bus_that_stays_up_says_nothing() -> None:
    w = world()
    w.run(10)
    assert w.link() is Link.UP
    assert w.said == []
    assert w.repair.calls == []


def test_a_bus_going_down_is_a_failure_said_at_once_and_mended() -> None:
    w = world()
    w.kernel.up = False
    w.run(1)
    assert w.link() is Link.FAILURE
    assert w.said == [
        "12:00:00 bus main (can0) failed — it is down, and not by the hub — the hub is "
        "bringing it up"
    ]

    w.run(1)
    assert w.repair.calls == [1.0 + DELAYS[0]]
    assert w.link() is Link.UP
    assert w.said[-1] == "12:00:00 bus main (can0) is working again — brought up by the hub"


def test_three_attempts_then_the_hub_gives_up() -> None:
    w = world()
    w.repair.works = False
    w.kernel.up = False
    w.run(1)  # seen at 1 s

    w.run(60)
    first = 1.0 + DELAYS[0]
    assert w.repair.calls == [first, first + DELAYS[1], first + DELAYS[1] + DELAYS[2]]
    assert w.link() is Link.GAVE_UP
    assert "could not bring bus main (can0) up in 3 attempts" in w.said[-1]

    w.run(600)
    assert len(w.repair.calls) == 3, "having given up it only looks"


def test_no_password_for_sudo_gives_up_at_once() -> None:
    w = world()
    w.repair.hopeless = True
    w.kernel.up = False
    w.run(5)
    assert len(w.repair.calls) == 1
    assert w.link() is Link.GAVE_UP
    assert "password is required" in w.said[-1]


def test_a_bus_brought_up_by_hand_after_the_hub_gave_up_works_again() -> None:
    w = world()
    w.repair.hopeless = True
    w.kernel.up = False
    w.run(5)

    w.kernel.up = True  # somebody, by hand
    w.run(1)
    assert w.link() is Link.UP
    assert w.said[-1] == "12:00:00 bus main (can0) is working again"


def test_a_planned_break_is_not_a_failure() -> None:
    w = world()
    with w.watch.planned("can0"):
        w.kernel.up = False
        w.run(3)
        assert w.link() is Link.PLANNED
        w.kernel.up = True
    w.run(1)
    assert w.link() is Link.UP
    assert w.said == []
    assert w.repair.calls == []


def test_a_bus_the_planned_break_left_down_is_a_failure() -> None:
    w = world()
    with w.watch.planned("can0"):
        w.kernel.up = False
        w.run(1)
    w.run(1)
    assert w.link() is Link.FAILURE
    assert "not by the hub" in w.said[0]


def test_a_failure_that_keeps_coming_back_is_the_hardware() -> None:
    w = world()
    for _ in range(FUSE):
        w.kernel.up = False
        w.run(3)
        assert w.link() is Link.UP, "mended"
    w.kernel.up = False
    w.run(1)

    assert w.link() is Link.GAVE_UP
    assert f"{FUSE + 1} failures in 10 min" in w.said[-1]
    assert "hardware" in w.said[-1]
    assert len(w.repair.calls) == FUSE


def test_failures_ten_minutes_apart_do_not_blow_the_fuse() -> None:
    w = world()
    for _ in range(FUSE + 2):
        w.kernel.up = False
        w.run(3)
        assert w.link() is Link.UP
        w.run(300)


def test_an_adapter_pulled_out_and_plugged_back_is_reopened() -> None:
    w = world()
    w.kernel.unplug()
    w.run(1)
    assert w.link() is Link.FAILURE
    assert "the interface is gone" in w.said[-1]

    w.kernel.plug(up=False)  # a USB stick comes back down, without a bitrate
    w.run(2)
    assert w.link() is Link.UP
    assert w.socket.ifindex == w.kernel.ifindex, "the socket is on the new interface"
    assert w.socket.reopened == 1


def test_an_adapter_swapped_between_two_looks_is_reopened() -> None:
    """Gone and back up in under a second: only the index tells."""
    w = world()
    w.kernel.plug(up=True)
    w.run(1)
    assert w.link() is Link.UP
    assert w.socket.ifindex == w.kernel.ifindex
    assert w.said == ["12:00:00 bus main (can0) came back as a new interface — the hub reopened it"]


def test_an_interface_coming_back_after_the_hub_gave_up_starts_a_new_round() -> None:
    w = world()
    w.kernel.unplug()
    w.run(60)
    assert w.link() is Link.GAVE_UP
    calls = len(w.repair.calls)

    w.kernel.plug(up=False)
    w.run(1)
    assert w.link() is Link.FAILURE
    assert "is back, but it is down" in w.said[-1]
    w.run(1)
    assert len(w.repair.calls) == calls + 1
    assert w.link() is Link.UP


def test_a_failed_start_carries_on_without_saying_it_again() -> None:
    """The start was the first attempt and its issue is already in the list."""
    w = world(Outcome(CONFIG, issue="bus main (can0): busy"), up=False)
    assert w.link() is Link.FAILURE

    w.run(DELAYS[1] - 1)
    assert w.repair.calls == []
    w.run(1)
    assert w.repair.calls == [DELAYS[1]]
    assert w.link() is Link.UP
    assert w.said == ["12:00:00 bus main (can0) is working again — brought up by the hub"]


def test_a_start_without_sudo_has_given_up_already() -> None:
    w = world(Outcome(CONFIG, issue="sudo: a password is required", hopeless=True), up=False)
    w.run(60)
    assert w.link() is Link.GAVE_UP
    assert w.repair.calls == []
    assert w.said == []


def test_an_issue_at_start_on_a_working_bus_is_not_a_failure() -> None:
    """A change only a person can make, on a bus that is up — the bus works."""
    w = world(Outcome(CONFIG, issue="bus main (can0) needs a person: bitrate"))
    w.run(10)
    assert w.link() is Link.UP
    assert w.said == []


def test_a_bus_not_watched_has_no_state() -> None:
    assert world().watch.state("can1") is None


@pytest.mark.parametrize(("flags", "up"), [("0x40080", False), ("0x40081", True)])
def test_sysfs_reads_the_up_flag_and_the_index(tmp_path: Path, flags: str, up: bool) -> None:
    net = tmp_path / "sys/class/net/can0"
    net.mkdir(parents=True)
    (net / "flags").write_text(f"{flags}\n")
    (net / "ifindex").write_text("5\n")
    assert sysfs(tmp_path)("can0") == Seen(up=up, ifindex=5)
    assert sysfs(tmp_path)("can1") is None
