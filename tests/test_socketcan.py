"""SocketCAN — we check the frame format everywhere, the socket only on Linux."""

from __future__ import annotations

import pytest

from lagunhub.protocol.bus.socketcan import (
    CAN_EFF_FLAG,
    FRAME_SIZE,
    FRAME_STRUCT,
    HAS_SOCKETCAN,
    SocketCanBus,
    unpack,
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


# --- frames that are not LagunBus's (zadania/BUG-10.md) -------------------------

CAN_RTR_FLAG = 0x4000_0000
CAN_ERR_FLAG = 0x2000_0000


def _kernel(can_id: int, data: bytes = b"") -> bytes:
    return FRAME_STRUCT.pack(can_id, len(data), data.ljust(8, b"\x00"))


def test_an_extended_frame_is_ours() -> None:
    f = Frame(encode_id(0x1, 0x02A, 4, 0x03), b"\x11\x01")
    assert unpack(_kernel(f.can_id | CAN_EFF_FLAG, f.data)) == f


@pytest.mark.parametrize(
    ("can_id", "what"),
    [
        (CAN_EFF_FLAG | CAN_RTR_FLAG | 0x123, "a remote request"),
        (CAN_EFF_FLAG | CAN_ERR_FLAG | 0x004, "an error frame"),
        (0x123, "a standard 11-bit identifier"),
    ],
)
def test_a_frame_lagunbus_does_not_use_is_skipped(can_id: int, what: str) -> None:
    """One RTR frame from an analyser used to end the RX thread: its flag stayed in the
    identifier and `Frame` refused 30 bits. A standard frame was worse — read quietly as
    a LagunBus identifier."""
    assert unpack(_kernel(can_id)) is None, what


def test_a_short_read_is_skipped() -> None:
    assert unpack(b"\x00" * 8) is None
