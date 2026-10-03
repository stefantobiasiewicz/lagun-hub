"""The SocketCAN driver against a fake /sys and a fake shell."""

from __future__ import annotations

import json
import shlex
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from lagunhub.drivers.base import (
    CanSettings,
    Change,
    DriverError,
    Mode,
    NotSupported,
    Rejected,
    Unreachable,
    configure,
)
from lagunhub.drivers.socketcan import LAGUNBUS, SocketCanDriver, SystemShell, interfaces

from .fakes import link_json


class FakeShell:
    """Answers ``ip -details`` from a string; records everything privileged."""

    def __init__(self, show: str) -> None:
        self.show = show
        self.privileged: list[str] = []

    def run(self, argv: Sequence[str], *, privileged: bool = False) -> str:
        if privileged:
            self.privileged.append(shlex.join(argv))
            return ""
        assert list(argv[:3]) == ["ip", "-details", "-json"]
        return self.show


def machine(
    root: Path,
    *,
    interface: str | None = "can0",
    device: str = "spi0.0",
    driver: str = "mcp251xfd",
) -> None:
    """A Linux machine in a directory: /sys/class/net with symlinks to the device."""
    net = root / "sys/class/net"
    net.mkdir(parents=True)
    if interface:
        hardware = root / "sys/devices/platform/soc/bus" / device
        (hardware.parent / "drivers" / driver).mkdir(parents=True)
        hardware.mkdir(parents=True)
        (hardware / "driver").symlink_to(hardware.parent / "drivers" / driver)
        (net / interface).mkdir()
        (net / interface / "type").write_text("280\n")
        (net / interface / "device").symlink_to(hardware)


def virtual(root: Path, name: str = "vcan0") -> None:
    """A ``vcan``: a CAN interface in /sys with no device behind it."""
    path = root / "sys/class/net" / name
    path.mkdir(parents=True)
    (path / "type").write_text("280\n")


def vcan_json(*, up: bool) -> str:
    flags = ["NOARP", "UP", "LOWER_UP"] if up else ["NOARP"]
    return json.dumps(
        [{"ifname": "vcan0", "flags": flags, "txqlen": 1000, "linkinfo": {"info_kind": "vcan"}}]
    )


def socketcan(root: Path, show: str = "", name: str = "can0") -> tuple[SocketCanDriver, FakeShell]:
    shell = FakeShell(show or link_json())
    return SocketCanDriver(name, shell=shell, root=root), shell


def hows(changes: Sequence[Change]) -> list[str]:
    return [change.how for change in changes]


def test_in_sync_has_an_empty_plan(tmp_path: Path) -> None:
    machine(tmp_path)
    driver, _ = socketcan(tmp_path)
    assert driver.plan(LAGUNBUS) == ()


def test_read(tmp_path: Path) -> None:
    machine(tmp_path)
    driver, _ = socketcan(tmp_path)
    assert driver.read() == LAGUNBUS
    assert driver.info().where == "spi0.0 · can0"
    assert driver.info().firmware == "mcp251xfd"


def test_after_a_reboot_the_hub_brings_it_up(tmp_path: Path) -> None:
    machine(tmp_path)
    down = link_json(restart_ms=0, bittiming={}, state="STOPPED")
    down = down.replace('"NOARP", "UP", "LOWER_UP", "ECHO"', '"NOARP", "ECHO"')
    driver, shell = socketcan(tmp_path, down)

    plan = driver.plan(LAGUNBUS)
    assert [(c.what, c.current, c.desired) for c in plan] == [
        ("bitrate", "unset", "500 kbit/s"),
        ("restart after bus-off", "0 ms", "100 ms"),
        ("link", "DOWN", "UP"),
    ]
    assert configure(driver, LAGUNBUS) == ()
    assert shell.privileged == [
        "ip link set can0 down",
        "ip link set can0 type can bitrate 500000",
        "ip link set can0 type can restart-ms 100",
        "ip link set can0 up",
        "ip link set can0 down",  # restart()
        "ip link set can0 up",
    ]


def test_fd_and_listen_only_are_switched_off(tmp_path: Path) -> None:
    machine(tmp_path)
    driver, _ = socketcan(tmp_path, link_json(ctrlmode=["FD", "LISTEN-ONLY"]))
    assert hows(driver.plan(LAGUNBUS)) == [
        "ip link set can0 type can fd off",
        "ip link set can0 type can listen-only off loopback off",
    ]


def test_core_settings_are_enough(tmp_path: Path) -> None:
    machine(tmp_path)
    driver, _ = socketcan(tmp_path, link_json(bittiming={"bitrate": 125000}))
    assert hows(driver.plan(CanSettings())) == ["ip link set can0 type can bitrate 500000"]


@pytest.mark.parametrize("kernel_driver", ["mcp251xfd", "mcp251x", "gs_usb"])
def test_any_can_controller_is_ours(tmp_path: Path, kernel_driver: str) -> None:
    """The route is the kind, not the chip: ``ip link`` is the same for all of them."""
    machine(tmp_path, device="1-1:1.0", driver=kernel_driver)
    driver, _ = socketcan(tmp_path, link_json(bittiming={"bitrate": 125000}))
    assert hows(driver.plan(LAGUNBUS)) == ["ip link set can0 type can bitrate 500000"]
    assert driver.info().firmware == kernel_driver


def test_missing_interface_is_for_a_person(tmp_path: Path) -> None:
    """The hub cannot conjure a controller; what is under the kernel is installation."""
    machine(tmp_path, interface=None)
    driver, shell = socketcan(tmp_path)
    (change,) = driver.plan(LAGUNBUS)
    assert (change.what, change.current, change.manual) == ("interface", "missing", True)
    assert "dmesg" in change.how
    assert configure(driver, LAGUNBUS) == (change,)
    assert shell.privileged == []
    with pytest.raises(Unreachable, match="does not exist"):
        driver.read()


def test_virtual_interface_that_is_up_is_in_sync(tmp_path: Path) -> None:
    """A ``vcan`` has no timing: whatever the bitrate in the file, there is nothing to set."""
    virtual(tmp_path)
    driver, _ = socketcan(tmp_path, vcan_json(up=True), "vcan0")
    assert driver.plan(LAGUNBUS) == ()
    assert driver.plan(replace(LAGUNBUS, bitrate=125_000)) == ()


def test_virtual_interface_is_never_touched(tmp_path: Path) -> None:
    """Bringing a ``vcan`` up is left to a person — the hub's sudo rule covers ``can*`` only."""
    virtual(tmp_path)
    driver, shell = socketcan(tmp_path, vcan_json(up=False), "vcan0")
    (change,) = driver.plan(LAGUNBUS)
    assert (change.what, change.current, change.desired, change.manual) == (
        "link",
        "DOWN",
        "UP",
        True,
    )
    assert "can-up.sh vcan0" in change.how
    assert configure(driver, LAGUNBUS) == (change,)
    assert shell.privileged == []


def test_fd_is_not_supported(tmp_path: Path) -> None:
    machine(tmp_path)
    driver, _ = socketcan(tmp_path)
    with pytest.raises(NotSupported, match=r"CAN 2\.0B"):
        driver.plan(replace(LAGUNBUS, fd=True))


def test_apply_runs_nothing_foreign(tmp_path: Path) -> None:
    machine(tmp_path)
    driver, shell = socketcan(tmp_path)
    with pytest.raises(DriverError, match="refusing"):
        driver.apply([Change("x", "a", "b", "rm -rf /")])
    with pytest.raises(DriverError, match="refusing"):
        driver.apply([Change("x", "a", "b", "ip link set can1 down")])
    assert shell.privileged == []


def test_interfaces_lists_everything_can(tmp_path: Path) -> None:
    machine(tmp_path)
    virtual(tmp_path)
    eth = tmp_path / "sys/class/net/eth0"
    eth.mkdir()
    (eth / "type").write_text("1\n")
    assert [(i.name, i.device, i.driver) for i in interfaces(tmp_path)] == [
        ("can0", "spi0.0", "mcp251xfd"),
        ("vcan0", "", ""),
    ]


def test_not_linux(tmp_path: Path) -> None:
    with pytest.raises(Unreachable, match="not Linux"):
        SocketCanDriver(root=tmp_path).open()


def test_system_shell() -> None:
    shell = SystemShell(sudo=False)
    assert shell.run(["echo", "ok"]).strip() == "ok"
    with pytest.raises(Rejected):
        shell.run(["false"])
    with pytest.raises(Unreachable, match="not found"):
        shell.run(["no-such-binary-anywhere"])


def test_listen_only_is_a_mode(tmp_path: Path) -> None:
    machine(tmp_path)
    driver, _ = socketcan(tmp_path)
    assert hows(driver.plan(replace(LAGUNBUS, mode=Mode.LISTEN_ONLY))) == [
        "ip link set can0 type can listen-only on loopback off"
    ]
