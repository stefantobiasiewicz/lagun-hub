"""UdpBus — the bus for running the hub with simulated nodes (zadania/zrobione/FEAT-02.md).

Every test takes a port of its own, so that it neither hears nor disturbs a simulator
running on the default port while the tests run.
"""

from __future__ import annotations

import random
import socket
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from lagunhub import cli
from lagunhub.protocol.bus import Bus, UdpBus
from lagunhub.protocol.bus.udp import (
    DATAGRAM_SIZE,
    DEFAULT_GROUP,
    DEFAULT_PORT,
    LOOPBACK,
    decode,
    encode,
    parse_address,
)
from lagunhub.protocol.endpoints import output as out
from lagunhub.protocol.frame import Frame, encode_id
from tests.test_e2e import build

SETTLE = 0.02
"""Loopback delivery is fast but not instant — the frame goes through the kernel."""


@pytest.fixture
def port() -> int:
    return random.randint(20_000, 60_000)


@pytest.fixture
def buses(port: int) -> Iterator[list[UdpBus]]:
    opened: list[UdpBus] = []
    yield opened
    for bus in opened:
        bus.close()


def receive(bus: UdpBus) -> list[Frame]:
    time.sleep(SETTLE)
    return list(iter(bus.recv, None))


# --- the datagram --------------------------------------------------------------------


def test_the_datagram_is_pinned_byte_for_byte() -> None:
    """The node side (`lagunbus_node`) is written separately and shares no code with the
    hub — these bytes are the contract between them. The same vector stands in the
    node library's tests; a change here without a change there splits the two."""
    datagram = encode(0x11223344, Frame(0x06108402, b"\x01"))
    assert datagram == bytes.fromhex(
        "4C425531"  # magic "LBU1"
        "44332211"  # sender, little-endian
        "02841086"  # can_id | CAN_EFF_FLAG, little-endian
        "01000000"  # length, three bytes of padding
        "0100000000000000"  # data, padded to 8
    )
    assert len(datagram) == DATAGRAM_SIZE == 24
    assert decode(datagram) == (0x11223344, Frame(0x06108402, b"\x01"))


def test_what_is_not_our_datagram_decodes_to_nothing() -> None:
    good = encode(1, Frame(0x06108402, b"\x01"))
    assert decode(good[:-1]) is None, "too short"
    assert decode(b"XXXX" + good[4:]) is None, "a foreign magic"
    assert decode(good[:12] + b"\x09" + good[13:]) is None, "a length past 8"


def test_an_empty_payload_survives() -> None:
    f = Frame(encode_id(0x6, 0x040, 4, 0x00), b"")
    assert decode(encode(7, f)) == (7, f)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", (DEFAULT_GROUP, DEFAULT_PORT)),
        ("239.1.2.3", ("239.1.2.3", DEFAULT_PORT)),
        ("239.1.2.3:9000", ("239.1.2.3", 9000)),
        (":9000", (DEFAULT_GROUP, 9000)),
    ],
)
def test_address_parsing(text: str, expected: tuple[str, int]) -> None:
    assert parse_address(text) == expected


@pytest.mark.parametrize("text", ["127.0.0.1", "239.1.2.3:0", "not-an-address", "239.1.2.3:x"])
def test_a_bad_address_is_refused(text: str) -> None:
    with pytest.raises(ValueError):
        parse_address(text)


# --- the bus -------------------------------------------------------------------------


def test_it_satisfies_the_interface(port: int, buses: list[UdpBus]) -> None:
    buses.append(UdpBus(DEFAULT_GROUP, port))
    assert isinstance(buses[0], Bus)


def test_every_other_port_hears_a_frame_and_the_sender_does_not(
    port: int, buses: list[UdpBus]
) -> None:
    """A cable: all the others hear it. The sender does not — as with SocketCAN, where
    a socket does not get its own frames back."""
    a, b, c = (UdpBus(DEFAULT_GROUP, port, name=n) for n in "abc")
    buses.extend((a, b, c))
    f = Frame(encode_id(0x1, 0x02A, 4, 0x03), b"\x00\x01")
    a.send(f)
    assert receive(b) == [f]
    assert receive(c) == [f]
    assert receive(a) == []


def test_order_of_sending_is_kept(port: int, buses: list[UdpBus]) -> None:
    """No arbitration between processes — order of sending, which is what is claimed."""
    a, b = UdpBus(DEFAULT_GROUP, port), UdpBus(DEFAULT_GROUP, port)
    buses.extend((a, b))
    frames = [Frame(encode_id(0x8, 0x200 + i, 4, 0x00), bytes([i])) for i in range(20)]
    for f in frames:
        a.send(f)
    assert receive(b) == frames


def test_garbage_in_the_group_is_dropped(port: int, buses: list[UdpBus]) -> None:
    """The group is open to every program on the machine."""
    b = UdpBus(DEFAULT_GROUP, port)
    buses.append(b)
    stranger = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    stranger.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(LOOPBACK))
    stranger.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 0)
    try:
        stranger.sendto(b"hello", (DEFAULT_GROUP, port))
        stranger.sendto(b"X" * DATAGRAM_SIZE, (DEFAULT_GROUP, port))
        good = Frame(encode_id(0x1, 0x02A, 4, 0x03), b"\x00\x01")
        stranger.sendto(encode(99, good), (DEFAULT_GROUP, port))
    finally:
        stranger.close()
    assert receive(b) == [good]


def test_a_key_toggles_a_relay_over_udp(tmp_path: Path, port: int, buses: list[UdpBus]) -> None:
    """The first vertical path of test_e2e.py, with every node and the hub on a socket of
    its own — as the hub and `lagun-sim` processes are."""

    def attach(name: str) -> Bus:
        bus = UdpBus(DEFAULT_GROUP, port, name=name)
        buses.append(bus)
        return bus

    rig = build(tmp_path, attach=attach, settle=0.001)
    assert rig.output.values[out.ON_OFF] is False
    rig.click()
    assert rig.output.values[out.ON_OFF] is True
    rig.click()
    assert rig.output.values[out.ON_OFF] is False
    assert rig.engine.matched == 2


# --- the command line ----------------------------------------------------------------


def test_ephemeral_without_a_source_is_clean_and_goes_away() -> None:
    temporary, where = cli._ephemeral_config(None)
    assert (where / "hub.yaml").exists()
    temporary.cleanup()
    assert not where.exists()


def test_ephemeral_copies_a_directory_and_leaves_it_untouched(tmp_path: Path) -> None:
    source = tmp_path / "config"
    source.mkdir()
    (source / "hub.yaml").write_text("buses: [can0]\n", encoding="utf-8")
    (source / "hub.yaml.tmp").write_text("half a write\n", encoding="utf-8")

    temporary, where = cli._ephemeral_config(source)
    try:
        assert (where / "hub.yaml").read_text(encoding="utf-8") == "buses: [can0]\n"
        assert not (where / "hub.yaml.tmp").exists(), "a half-written file is not copied"
        (where / "hub.yaml").write_text("changed\n", encoding="utf-8")
        assert (source / "hub.yaml").read_text(encoding="utf-8") == "buses: [can0]\n"
    finally:
        temporary.cleanup()


def test_ephemeral_copies_a_single_file(tmp_path: Path) -> None:
    source = tmp_path / "house.yaml"
    source.write_text("buses: [can0]\n", encoding="utf-8")
    temporary, where = cli._ephemeral_config(source)
    try:
        assert where.name == "house.yaml"
        assert where.read_text(encoding="utf-8") == "buses: [can0]\n"
    finally:
        temporary.cleanup()


def test_udp_and_bus_exclude_each_other(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.main(["run", "--bus", "can0", "--udp"])
    assert "not allowed with" in capsys.readouterr().err
