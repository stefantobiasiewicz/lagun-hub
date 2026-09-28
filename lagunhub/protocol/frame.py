"""The LagunBus frame and the codec of the 29-bit identifier.

The field layout and every constant come from ``lagunbus-spec/05-mapowanie-can.md``.
Nothing in this module knows about a socket or a bus — this is a pure codec.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, NamedTuple

# --- identifier layout, 05-mapowanie-can.md §1 -----------------------------------
#
#  28 27 26 25 │ 24 … 14 │ 13 … 8 │ 7 … 0
#    RODZAJ    │  ADRES  │   EP   │ ELEM
#      (4)     │   (11)  │   (6)  │  (8)

KIND_SHIFT: Final = 25
KIND_MASK: Final = 0x0F
ADDRESS_SHIFT: Final = 14
ADDRESS_MASK: Final = 0x7FF
ENDPOINT_SHIFT: Final = 8
ENDPOINT_MASK: Final = 0x3F
ELEMENT_MASK: Final = 0xFF

CAN_ID_MASK: Final = 0x1FFF_FFFF
"""29 bits, without a single spare one."""

MAX_PAYLOAD: Final = 8

# --- addresses, 03-adresacja.md §2 -----------------------------------------------

ADDRESS_UNASSIGNED: Final = 0x000
ADDRESS_NODE_MIN: Final = 0x001
ADDRESS_NODE_MAX: Final = 0x7FD
ADDRESS_HUB: Final = 0x7FE
ADDRESS_BROADCAST: Final = 0x7FF

ENDPOINT_MAX: Final = 63

# --- the join frame has a layout of its own, 05-mapowanie-can.md §4 --------------
#
#  28 27 26 25 │ 24 │ 23 … 0
#     0xF      │ K  │ ShortId[23:0]

JOIN_KIND: Final = 0x0F
JOIN_DIRECTION_SHIFT: Final = 24
JOIN_SHORT_ID_MASK: Final = 0x00FF_FFFF

# --- header byte of transactional messages, 05-mapowanie-can.md §5.1 -------------
#
#  bit 7 6 5 4 3 │ 2 1 0
#      WYNIK     │  Tid

TID_MASK: Final = 0x07
RESULT_SHIFT: Final = 3
RESULT_MASK: Final = 0x1F


class Fields(NamedTuple):
    """The four fields of an ordinary identifier."""

    kind: int
    address: int
    endpoint: int
    element: int


class JoinFields(NamedTuple):
    """The two fields of a join frame identifier.

    ``short_id`` is the **low 24 bits**. The full 32 travel in the payload, and those
    are what the hub compares before assigning an address (05-mapowanie-can.md §4.3).
    """

    announce: bool
    short_id_low: int


def encode_id(kind: int, address: int, endpoint: int, element: int) -> int:
    """Build an identifier from four fields. The ranges are checked."""
    if not 0 <= kind <= KIND_MASK:
        raise ValueError(f"kind outside 0..15: {kind}")
    if not 0 <= address <= ADDRESS_MASK:
        raise ValueError(f"address outside 0..0x7FF: {address:#x}")
    if not 0 <= endpoint <= ENDPOINT_MASK:
        raise ValueError(f"endpoint outside 0..63: {endpoint}")
    if not 0 <= element <= ELEMENT_MASK:
        raise ValueError(f"element outside 0..255: {element}")
    return (
        (kind << KIND_SHIFT) | (address << ADDRESS_SHIFT) | (endpoint << ENDPOINT_SHIFT) | element
    )


def decode_id(can_id: int) -> Fields:
    """Take an identifier apart into fields. No checking — this is the fast path."""
    return Fields(
        kind=(can_id >> KIND_SHIFT) & KIND_MASK,
        address=(can_id >> ADDRESS_SHIFT) & ADDRESS_MASK,
        endpoint=(can_id >> ENDPOINT_SHIFT) & ENDPOINT_MASK,
        element=can_id & ELEMENT_MASK,
    )


def encode_join_id(*, announce: bool, short_id: int) -> int:
    """The identifier of a join frame.

    ``announce=True`` is an ANNOUNCEMENT (node -> hub, K=1), ``False`` an ASSIGNMENT
    (hub -> node, K=0). An assignment has the lower identifier, so it goes ahead of
    further announcements in arbitration (05-mapowanie-can.md §4.4).
    """
    if not 0 <= short_id <= 0xFFFF_FFFF:
        raise ValueError(f"ShortId outside u32: {short_id:#x}")
    return (
        (JOIN_KIND << KIND_SHIFT)
        | (int(announce) << JOIN_DIRECTION_SHIFT)
        | (short_id & JOIN_SHORT_ID_MASK)
    )


def decode_join_id(can_id: int) -> JoinFields:
    return JoinFields(
        announce=bool((can_id >> JOIN_DIRECTION_SHIFT) & 0x01),
        short_id_low=can_id & JOIN_SHORT_ID_MASK,
    )


def encode_header(tid: int, result: int = 0) -> int:
    """Byte ``[0]`` of a transactional message.

    In a request the result field is reserved and sent as zero; only the reply fills
    it in.
    """
    if not 0 <= tid <= TID_MASK:
        raise ValueError(f"Tid outside 0..7: {tid}")
    if not 0 <= result <= RESULT_MASK:
        raise ValueError(f"result code outside 0..31: {result}")
    return (result << RESULT_SHIFT) | tid


def decode_header(byte: int) -> tuple[int, int]:
    """Returns ``(result, tid)``."""
    return (byte >> RESULT_SHIFT) & RESULT_MASK, byte & TID_MASK


@dataclass(frozen=True, slots=True)
class Frame:
    """One CAN 2.0B frame: an extended identifier and up to eight payload bytes.

    The frame length follows from the payload length — the specification does not
    regulate DLC, because there is nothing to regulate: the message kind settles the
    payload layout.
    """

    can_id: int
    data: bytes = b""

    def __post_init__(self) -> None:
        if not 0 <= self.can_id <= CAN_ID_MASK:
            raise ValueError(f"identifier does not fit into 29 bits: {self.can_id:#x}")
        if len(self.data) > MAX_PAYLOAD:
            raise ValueError(f"payload longer than 8 bytes: {len(self.data)}")

    @property
    def kind(self) -> int:
        return (self.can_id >> KIND_SHIFT) & KIND_MASK

    @property
    def is_join(self) -> bool:
        return self.kind == JOIN_KIND

    @property
    def fields(self) -> Fields:
        """The fields of an ordinary frame. For a join frame they mean nothing — see ``join``."""
        return decode_id(self.can_id)

    @property
    def join(self) -> JoinFields:
        return decode_join_id(self.can_id)

    def __str__(self) -> str:
        payload = self.data.hex(" ") if self.data else "—"
        if self.is_join:
            j = self.join
            direction = "ANNOUNCEMENT" if j.announce else "ASSIGNMENT"
            return f"{self.can_id:08X} {direction} short={j.short_id_low:06X} [{payload}]"
        f = self.fields
        return (
            f"{self.can_id:08X} kind={f.kind:X} addr={f.address:#05x} "
            f"ep={f.endpoint} el={f.element:#04x} [{payload}]"
        )
