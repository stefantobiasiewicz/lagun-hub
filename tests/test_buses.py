"""Setting buses up at start — the hub's side of zadania/zrobione/FEAT-01.md, R3 and R5."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from lagunhub import buses
from lagunhub.core.config import BusConfig
from lagunhub.drivers.base import AdapterInfo, CanSettings, Change, Rejected, Unreachable
from lagunhub.drivers.socketcan import SocketCanSettings
from lagunhub.protocol.bus import VirtualNetwork
from lagunhub.protocol.bus.base import Bus

CAN0 = BusConfig(name="ground-floor", channel="can0", bitrate=250_000)

BITRATE = Change("bitrate", "unset", "250 kbit/s", "ip link set can0 type can bitrate 250000")
LINK = Change("link", "DOWN", "UP", "ip link set can0 up")
MISSING = Change("interface", "missing", "can0", "the kernel has no can0: `dmesg`", manual=True)


class FakeDriver:
    """A plan given up front; records what was asked of it. What ``apply`` carried out
    drops out of the next plan, as it would on the hardware."""

    model = "fake"

    def __init__(
        self,
        plan: Sequence[Change] = (),
        *,
        fails_plan: Exception | None = None,
        fails_apply: Exception | None = None,
    ) -> None:
        self._plan = tuple(plan)
        self.fails_plan = fails_plan
        self.fails_apply = fails_apply
        self.desired: CanSettings | None = None
        self.applied: list[Change] = []
        self.calls: list[str] = []

    def plan(self, desired: CanSettings) -> tuple[Change, ...]:
        self.calls.append("plan")
        self.desired = desired
        if self.fails_plan:
            raise self.fails_plan
        return tuple(change for change in self._plan if change not in self.applied)

    def apply(self, changes: Sequence[Change]) -> None:
        self.calls.append("apply")
        if self.fails_apply:
            raise self.fails_apply
        self.applied += changes

    def open(self) -> None:
        self.calls.append("open")

    def close(self) -> None:
        self.calls.append("close")

    def info(self) -> AdapterInfo:
        return AdapterInfo(model="fake", firmware="mcp251xfd", where="spi0.0 · can0")

    def read(self) -> CanSettings:
        return CanSettings()

    def persist(self) -> None:
        self.calls.append("persist")

    def restart(self) -> None:
        self.calls.append("restart")


def test_the_file_decides_the_bitrate() -> None:
    driver = FakeDriver()
    buses.bring_up(CAN0, driver)
    assert driver.desired == SocketCanSettings(bitrate=250_000)


def test_in_sync_touches_nothing() -> None:
    driver = FakeDriver()
    outcome = buses.bring_up(CAN0, driver)
    assert (outcome.applied, outcome.issue) == ((), "")
    assert driver.calls == ["plan"]
    assert "as configured" in outcome.line()


def test_after_a_reboot_the_hub_sets_it_up() -> None:
    """R3: one command — the hub brings the interface to the file by itself."""
    driver = FakeDriver([BITRATE, LINK])
    outcome = buses.bring_up(CAN0, driver)
    assert driver.applied == [BITRATE, LINK]
    assert outcome.issue == ""
    assert "set: bitrate, link" in outcome.line()


def test_apply_alone_no_second_bounce() -> None:
    """``apply`` already takes the interface down and up; ``restart`` would do it again,
    and ``persist`` is empty for SocketCAN by design."""
    driver = FakeDriver([BITRATE])
    buses.bring_up(CAN0, driver)
    assert driver.calls == ["plan", "apply"]


def test_what_a_person_must_do_is_an_issue() -> None:
    driver = FakeDriver([MISSING])
    outcome = buses.bring_up(CAN0, driver)
    assert driver.applied == []
    assert "ground-floor (can0)" in outcome.issue
    assert "dmesg" in outcome.issue


def test_without_the_sudo_rule_the_issue_says_what_to_add() -> None:
    """R5: the hub still starts; the issue carries the line for sudoers."""
    refused = Rejected("ip link set can0 down", "sudo: a password is required")
    outcome = buses.bring_up(CAN0, FakeDriver([BITRATE], fails_apply=refused))
    assert "NOPASSWD: " in outcome.issue
    assert "ip link set can*" in outcome.issue
    assert "setup-rpi.sh" in outcome.issue


def test_a_driver_error_is_an_issue_not_a_crash() -> None:
    outcome = buses.bring_up(CAN0, FakeDriver(fails_plan=Unreachable("ip not found")))
    assert "ip not found" in outcome.issue
    assert outcome.issue in outcome.line()


def test_a_bus_that_does_not_open_is_left_out() -> None:
    """The hub starts without it; the operator sees why in the issues."""
    can1 = BusConfig(name="first-floor", channel="can1")
    opened = VirtualNetwork().attach("can0")

    def opener(bus: BusConfig) -> Bus:
        if bus.channel == "can1":
            raise OSError(19, "No such device")
        return opened

    got, issues = buses.open_buses([CAN0, can1], opener)
    assert got == [opened]
    (issue,) = issues
    assert "first-floor (can1)" in issue
    assert "No such device" in issue


def test_no_socketcan_at_all_still_stops_the_start() -> None:
    """On a machine without AF_CAN no bus will ever open — that is not one missing bus
    but the wrong machine, and `run` says so with a hint about --udp."""

    def opener(bus: BusConfig) -> Bus:
        raise RuntimeError("SocketCAN is available on Linux only")

    with pytest.raises(RuntimeError):
        buses.open_buses([CAN0], opener)
