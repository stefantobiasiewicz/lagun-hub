"""The binding engine — the hub’s fast path.

Everything in this file happens **on the RX thread, between recv() and send()**. The
four prohibitions from ARCHITEKTURA.md §4.5 apply here, and the first matters most: no
I/O beyond ``send()``. Not because microseconds would run out, but because a file, a
database and a network can stall for seconds.

A frame the hub sent **never comes back into this engine**, because the engine is fed
only by what arrived from the socket. A chain of bindings is therefore impossible to
build, not merely forbidden (07-hub.md §4.1).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Hashable, Sequence
from typing import Final

from lagunhub.core.busload import BusCounts
from lagunhub.core.metrics import Latency
from lagunhub.core.model import Snapshot
from lagunhub.core.transactions import TidAllocator
from lagunhub.protocol.bus.base import Bus
from lagunhub.protocol.frame import (
    ELEMENT_MASK,
    ENDPOINT_MASK,
    ENDPOINT_SHIFT,
    KIND_MASK,
    KIND_SHIFT,
    Frame,
    encode_header,
)
from lagunhub.protocol.registry import MessageKind

ADDRESS_SHIFT: Final = 14
ADDRESS_MASK: Final = 0x7FF

FAST_KINDS: Final = frozenset({MessageKind.ALARM, MessageKind.EVENT})
"""Kinds handled at once, on the RX thread. The rest go to the queue."""


class BindingEngine:
    """The match ``(Uid, endpoint, element)`` → the target's finished frames."""

    def __init__(
        self,
        buses: Sequence[Bus],
        snapshot: Snapshot,
        latency: Latency | None = None,
        tids: TidAllocator | None = None,
        on_sent: Callable[[int, Frame, Hashable | None], None] | None = None,
    ) -> None:
        self.buses = list(buses)
        self.snapshot = snapshot
        self.latency = latency or Latency()
        self.tids = tids or TidAllocator()
        self.on_sent = on_sent
        """Reporting a sent frame to the slow path. CORE minds the transaction. The third
        argument is the key under which a newer command supersedes an older one still
        unanswered — set only for a target with arguments from the event (B2)."""
        self.matched = 0
        self.unmatched = 0
        self.sent = 0
        self.dropped = 0
        self.counts = BusCounts(len(self.buses))
        """Per bus, written only here on the RX thread; CORE reads them
        (`core/busload.py`). `sent` and `dropped` above are their totals."""

    def handle(self, frame: Frame) -> int:
        """Returns the number of commands sent. Zero is not an error.

        An event nobody listens to is a normal state of an installation — a key may
        simply not be configured yet (07-hub.md §4).
        """
        can_id = frame.can_id
        if (can_id >> KIND_SHIFT) & KIND_MASK not in FAST_KINDS:
            return 0

        started = time.perf_counter_ns()
        snapshot = self.snapshot  # one read of the reference per frame
        uid = snapshot.addr.get((can_id >> ADDRESS_SHIFT) & ADDRESS_MASK)
        if uid is None:
            self.unmatched += 1
            return 0

        source = (
            uid,
            (can_id >> ENDPOINT_SHIFT) & ENDPOINT_MASK,
            can_id & ELEMENT_MASK,
        )
        targets = snapshot.bind.get(source)
        if not targets:
            self.unmatched += 1
            return 0

        sent_now = 0
        for target in targets:
            payload = target.payload
            supersedes: Hashable | None = None
            if target.splices:
                # Arguments from the event, byte for byte (07-hub.md §3.2): offsets and
                # types were settled at load, so here it is a copy of a few bytes and no
                # decoding (zadania/zrobione/DEVICE-02.md N1). An event too short for its
                # own type is a broken node — no command rather than a wrong one.
                data = frame.data
                if any(len(data) < start + size for start, _, size in target.splices):
                    self.dropped += 1
                    continue
                filled = bytearray(payload)
                for start, at, size in target.splices:
                    filled[at : at + size] = data[start : start + size]
                payload = bytes(filled)
                supersedes = (source, target.can_id)
            if target.transactional:
                # Allocation is shared with CORE and covers the full pool of eight
                # values. Half the pool would give a limit of four commands per 300 ms
                # where the specification budgets six (04-interakcje.md §2.2) — and
                # dimming by holding a key would stop half way.
                payload = bytes([encode_header(self.tids.next(target.can_id))]) + payload[1:]
            sent_one = Frame(target.can_id, payload)
            try:
                self.buses[target.bus].send(sent_one)
                sent_now += 1
                self.counts.count(target.bus, sent_one)
                if self.on_sent is not None:
                    self.on_sent(target.bus, sent_one, supersedes)
            except OSError:
                # A full send queue (`TransmitQueueFull`) or a bus that is down
                # (`ENETDOWN`, zadania/zrobione/BUG-04.md) — either way the command did not go
                # out, and a late command is worse than a missing one: we do not retry.
                # Caught here and not in RX, which would count it against the bus the
                # event came from and skip the event's slow path.
                self.dropped += 1
                self.counts.drop(target.bus)

        self.matched += 1
        self.sent += sent_now
        self.latency.record(time.perf_counter_ns() - started)
        return sent_now
