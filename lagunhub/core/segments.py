"""Values longer than a frame — lagunbus-spec/05-mapowanie-can.md §6.

**Segmenting is not an interaction of its own.** It is a way of carrying a value that
does not fit into eight bytes — a read stays a read and a write stays a write. To the
hub a `Uid` arriving in three frames is still **one read**; all that changes is the
number of frames the result comes back in.

A segment's payload:

    [0]   bit 7      LAST — the last segment of the value
          bits 6…3   reserve, send 0
          bits 2…0   Tid
    [1]   segment number, 0…255
    [2...7] data — 6 bytes per segment
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final

from lagunhub.core.schedule import Handle, Schedule
from lagunhub.protocol.frame import TID_MASK, Frame

DATA_PER_SEGMENT: Final = 6
LAST_FLAG: Final = 0x80
MAX_SEGMENTS: Final = 256
CAPACITY: Final = MAX_SEGMENTS * DATA_PER_SEGMENT
"""1536 bytes per value. It will not carry a firmware update and is not to pretend it
can (05-mapowanie-can.md §6.9)."""

TIMEOUT: Final = 0.100
"""No next segment for 100 ms — we discard the value entirely
(05-mapowanie-can.md §6.6)."""


@dataclass(frozen=True, slots=True)
class Segment:
    tid: int
    number: int
    data: bytes
    last: bool


def encode_segment(tid: int, number: int, data: bytes, *, last: bool) -> bytes:
    if not 0 <= tid <= TID_MASK:
        raise ValueError(f"Tid outside 0..7: {tid}")
    if not 0 <= number < MAX_SEGMENTS:
        raise ValueError(f"segment number outside 0..255: {number}")
    if len(data) > DATA_PER_SEGMENT:
        raise ValueError(f"a segment carries at most {DATA_PER_SEGMENT} B, got {len(data)}")
    return bytes([(LAST_FLAG if last else 0) | tid, number]) + data


def decode_segment(payload: bytes) -> Segment:
    if len(payload) < 2:
        raise ValueError("a segment has at least two header bytes")
    return Segment(
        tid=payload[0] & TID_MASK,
        number=payload[1],
        data=payload[2:],
        last=bool(payload[0] & LAST_FLAG),
    )


def split(value: bytes, tid: int) -> list[bytes]:
    """Slice a value into segments. The last has `LAST` set, even if it is empty."""
    if len(value) > CAPACITY:
        raise ValueError(f"value longer than {CAPACITY} B: {len(value)}")
    pieces = [value[i : i + DATA_PER_SEGMENT] for i in range(0, len(value), DATA_PER_SEGMENT)] or [
        b""
    ]
    return [
        encode_segment(tid, number, chunk, last=number == len(pieces) - 1)
        for number, chunk in enumerate(pieces)
    ]


@dataclass
class Assembly:
    """One value under assembly."""

    can_id: int
    tid: int
    chunks: list[bytes] = field(default_factory=list)
    timer: Handle | None = None

    @property
    def expected(self) -> int:
        return len(self.chunks)

    def value(self) -> bytes:
        return b"".join(self.chunks)


class SegmentReassembler:
    """Assembles segments into values. Belongs solely to the CORE thread.

    The key is the **node address**, because a node assembles at most one value in each
    direction at a time (05-mapowanie-can.md §6.7). A new value from the same node
    invalidates the previous one — just as segment zero invalidates whatever was
    collected before it (§6.5).
    """

    def __init__(
        self,
        schedule: Schedule,
        clock: Callable[[], float],
        on_value: Callable[[int, int, bytes], None],
        on_lost: Callable[[int, int, str], None],
    ) -> None:
        self._schedule = schedule
        self._clock = clock
        self._on_value = on_value
        self._on_lost = on_lost
        self._building: dict[int, Assembly] = {}

        self.completed = 0
        self.dropped = 0

    def __len__(self) -> int:
        return len(self._building)

    def feed(self, frame: Frame) -> None:
        address = frame.fields.address
        try:
            segment = decode_segment(frame.data)
        except ValueError:
            self._drop(address, "segment shorter than its header")
            return

        assembly = self._building.get(address)

        if segment.number == 0:
            # Segment zero invalidates everything received earlier for this value
            # (§6.5). Because ordering is guaranteed, late segments of an abandoned
            # attempt arrive BEFORE it and die along with it.
            if assembly is not None:
                self._forget(address, count=False)
            assembly = Assembly(can_id=frame.can_id, tid=segment.tid)
            self._building[address] = assembly
        elif assembly is None:
            return  # a segment with no beginning — there is nothing to add it to
        elif frame.can_id != assembly.can_id or segment.tid != assembly.tid:
            self._drop(address, "segment from another value")
            return
        elif segment.number != assembly.expected:
            # A hole in the numbering discards the value ENTIRELY (§6.6). There is no
            # partial retransmission — the sender repeats the whole request.
            self._drop(address, f"hole: expected {assembly.expected}, got {segment.number}")
            return

        assembly.chunks.append(segment.data)
        if assembly.timer is not None:
            assembly.timer.cancel()

        if segment.last:
            self._forget(address, count=False)
            self.completed += 1
            self._on_value(assembly.can_id, assembly.tid, assembly.value())
            return

        assembly.timer = self._schedule.after(
            self._clock(), TIMEOUT, lambda: self._drop(address, "no next segment")
        )

    def _drop(self, address: int, reason: str) -> None:
        assembly = self._building.get(address)
        if assembly is None:
            return
        self._forget(address, count=True)
        self._on_lost(assembly.can_id, assembly.tid, reason)

    def _forget(self, address: int, *, count: bool) -> None:
        assembly = self._building.pop(address, None)
        if assembly is None:
            return
        if assembly.timer is not None:
            assembly.timer.cancel()
        if count:
            self.dropped += 1
