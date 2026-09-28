"""SocketCAN — we check the frame format everywhere, the socket only on Linux."""

from __future__ import annotations

import pytest

from lagunhub.protocol.bus.socketcan import (
    CAN_EFF_FLAG,
    FRAME_SIZE,
    FRAME_STRUCT,
    HAS_SOCKETCAN,
    SocketCanBus,
)
from lagunhub.protocol.frame import Frame, encode_id


def test_the_package_imports_without_af_can() -> None:
    """Development happens on macOS — the import has no right to fall over."""
    import lagunhub.protocol.bus  # noqa: F401


def test_a_kernel_frame_is_16_bytes() -> None:
    assert FRAME_SIZE == 16


def test_frame_format_round_trips() -> None:
    f = Frame(encode_id(0x1, 0x02A, 4, 0x03), b"\x11\x01")
    raw = FRAME_STRUCT.pack(f.can_id | CAN_EFF_FLAG, len(f.data), f.data.ljust(8, b"\x00"))
    assert len(raw) == FRAME_SIZE

    can_id, length, payload = FRAME_STRUCT.unpack(raw)
    assert can_id & CAN_EFF_FLAG, "LagunBus uses nothing but the extended identifier"
    assert Frame(can_id & ~CAN_EFF_FLAG, payload[:length]) == f


def test_an_empty_payload_survives_padding() -> None:
    """A read and an argumentless command have one byte or none — padding does not
    count."""
    f = Frame(encode_id(0x6, 0x040, 4, 0x00), b"\x03")
    raw = FRAME_STRUCT.pack(f.can_id | CAN_EFF_FLAG, len(f.data), f.data.ljust(8, b"\x00"))
    _, length, payload = FRAME_STRUCT.unpack(raw)
    assert payload[:length] == b"\x03"


@pytest.mark.skipif(HAS_SOCKETCAN, reason="this test is about machines without SocketCAN")
def test_without_socketcan_it_says_outright_what_to_do() -> None:
    with pytest.raises(RuntimeError, match="VirtualBus"):
        SocketCanBus("can0")
