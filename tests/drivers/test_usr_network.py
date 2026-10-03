"""Discovery and joining against fake adapters listening on the loopback."""

from __future__ import annotations

import socket
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field, replace

import pytest

from lagunhub.drivers.base import DriverError, Unreachable
from lagunhub.drivers.usr_can315 import (
    LAGUNBUS,
    Found,
    UsrSettings,
    commission,
    conflicts,
    discover,
)
from lagunhub.drivers.usr_can315 import commands as at

from .fakes import FACTORY


def search() -> tuple[Found, ...]:
    return discover(bind="127.0.0.1", broadcast="127.0.0.1", timeout=0.3)


def join(settings: UsrSettings, expect: str = "") -> Found:
    return commission(settings, expect=expect, bind="127.0.0.1", broadcast="127.0.0.1", timeout=0.3)


WANTED = replace(
    LAGUNBUS, address="192.168.1.31", gateway="192.168.1.1", socket_host="192.168.1.31"
)


@dataclass
class Adapters:
    """One UDP socket on 48899 speaking for several adapters at once."""

    macs: list[str]
    commands: list[str] = field(default_factory=list)
    _stop: threading.Event = field(default_factory=threading.Event)

    def serve(self, sock: socket.socket) -> None:
        while not self._stop.is_set():
            try:
                raw, sender = sock.recvfrom(1024)
            except (TimeoutError, OSError):
                continue
            text = raw.decode().strip()
            if text == at.AT_KEYWORD:
                # Every adapter answers the keyword, like the real ones on one segment.
                for mac in self.macs:
                    sock.sendto(f"127.0.0.1,{mac},USR-CAN315".encode(), sender)
                continue
            self.commands.append(text)
            sock.sendto(FACTORY.get(text, "\r\nOK\r\n").encode(), sender)


@pytest.fixture
def network() -> Iterator[Adapters]:
    adapters = Adapters([])
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", at.AT_PORT))
    sock.settimeout(0.05)
    thread = threading.Thread(target=adapters.serve, args=(sock,), daemon=True)
    thread.start()
    yield adapters
    adapters._stop.set()
    thread.join()
    sock.close()


def test_discover_deduplicates_by_mac(network: Adapters) -> None:
    network.macs = ["D8B04C000003", "D8B04C000004"]
    found = search()
    assert [device.mac for device in found] == ["D8B04C000003", "D8B04C000004"]


def test_two_factory_adapters_are_a_conflict() -> None:
    fresh = [Found("192.168.0.7", "D8B04C000003"), Found("192.168.0.7", "D8B04C000004")]
    ((first, second),) = conflicts(fresh)
    assert (first.mac, second.mac) == ("D8B04C000003", "D8B04C000004")


def test_one_device_answering_twice_is_not_a_conflict() -> None:
    device = Found("192.168.1.31", "D8B04C000003")
    assert conflicts([device, device]) == ()


def test_commission_one_adapter(network: Adapters) -> None:
    network.macs = ["D8B04C000003"]
    joined = join(WANTED)
    assert joined == Found("192.168.1.31", "D8B04C000003", "USR-CAN315")
    assert network.commands[-4:] == [
        "AT+WANN=STATIC,192.168.1.31,255.255.255.0,192.168.1.1",
        "AT+SOCKA=TCPS,192.168.1.31,20001",
        "AT+CFGTF",
        "AT+Z",
    ]


def test_commission_refuses_two_at_once(network: Adapters) -> None:
    network.macs = ["D8B04C000003", "D8B04C000004"]
    with pytest.raises(DriverError, match="one at a time"):
        join(WANTED)
    assert not any("=" in command for command in network.commands)


def test_commission_picks_by_mac(network: Adapters) -> None:
    network.macs = ["D8B04C000003", "D8B04C000004"]
    joined = join(WANTED, "d8:b0:4c:00:00:04")
    assert joined.mac == "D8B04C000004"


def test_commission_without_address_is_refused_before_the_network() -> None:
    with pytest.raises(DriverError, match=r"factory 192\.168\.0\.7"):
        join(LAGUNBUS)


def test_commission_over_dhcp(network: Adapters) -> None:
    network.macs = ["D8B04C000005"]
    joined = join(replace(LAGUNBUS, ip_mode=at.IpMode.DHCP))
    assert joined.address == ""
    assert "AT+WANN=DHCP" in network.commands


def test_silence_is_unreachable(network: Adapters) -> None:
    with pytest.raises(Unreachable, match="no adapter"):
        join(WANTED)
