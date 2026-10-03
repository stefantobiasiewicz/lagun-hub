"""Both drivers answer the same contract — the whole point of ``base.AdapterDriver``."""

from __future__ import annotations

from lagunhub.drivers.base import AdapterDriver
from lagunhub.drivers.socketcan import SocketCanDriver
from lagunhub.drivers.usr_can315 import UsrCan315

from .fakes import FACTORY, FakeTransport


def test_both_drivers_satisfy_the_contract() -> None:
    drivers: list[AdapterDriver] = [UsrCan315(FakeTransport(FACTORY)), SocketCanDriver("can0")]
    for driver in drivers:
        assert isinstance(driver, AdapterDriver)
    assert [driver.model for driver in drivers] == ["USR-CAN315", "SocketCAN"]
