"""Joining a node — lagunbus-spec/03-adresacja.md §5,
06-cykl-zycia-urzadzenia.md §2.

Two frames with **an identifier layout of their own**, because the node does not know
its address yet and a node without an address cannot be addressed. The identifier
carries a `ShortId` instead, so every announcing node sends a different one — and
arbitration queues them up in hardware, with no software involved
(05-mapowanie-can.md §4.2).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

from lagunhub.core.model import Installation, JoinRecord, NodeState
from lagunhub.core.schedule import Handle, Schedule
from lagunhub.protocol.frame import Frame, decode_join_id, encode_join_id
from lagunhub.protocol.registry import NodeClass

ACCEPT_WINDOW: Final = 900.0
"""Fifteen minutes. A mode left open permanently protects against nothing
(06-cykl-zycia-urzadzenia.md §2.1)."""

ANNOUNCE_PAYLOAD: Final = 5
"""`ShortId` (32 b) and `NodeClass`. The rest of the payload is reserved."""


class Commissioning:
    """Address assignment. Belongs solely to the CORE thread."""

    def __init__(
        self,
        installation: Installation,
        send: Callable[[int, Frame], None],
        schedule: Schedule,
        clock: Callable[[], float],
        on_assigned: Callable[[JoinRecord], None] | None = None,
    ) -> None:
        self.installation = installation
        self._on_assigned = on_assigned
        self._send = send
        self._schedule = schedule
        self._clock = clock
        self._accept_until: float = 0.0
        self._timer: Handle | None = None

        self.assigned = 0
        self.readmitted = 0
        self.deferred = 0
        self.conflicts: list[tuple[int, int, int]] = []
        """``(address, bus, bus)`` — address conflicts already reported.

        Kept so that a node restarting in circles does not append the same issue on
        every `Booted`."""

    # --- the accepting mode ---------------------------------------------------

    @property
    def accepting(self) -> bool:
        return self._clock() < self._accept_until

    def accept_new(self, duration: float = ACCEPT_WINDOW) -> None:
        """Open joining mode. It expires on its own."""
        now = self._clock()
        self._accept_until = now + duration
        if self._timer is not None:
            self._timer.cancel()
        self._timer = self._schedule.after(now, duration, self._close)

    def _close(self) -> None:
        self._accept_until = 0.0
        self._timer = None

    # --- announcement ---------------------------------------------------------

    def on_join(self, bus: int, frame: Frame) -> None:
        fields = decode_join_id(frame.can_id)
        if not fields.announce:
            return  # the assignment is ours; one coming back does not concern us
        if len(frame.data) < ANNOUNCE_PAYLOAD:
            return

        # The hub compares the FULL 32 bits from the payload, not the 24 from the
        # identifier (05-mapowanie-can.md §4.3). These are raw join-frame fields, not
        # attribute values: they have no "absent" value and do not go through the type
        # codec.
        short_id = int.from_bytes(frame.data[:4], "little")
        if short_id & 0x00FF_FFFF != fields.short_id_low:
            return  # the payload contradicts the identifier — the frame is not credible

        node_class = _node_class(frame.data[4])
        record = self.installation.record(short_id)
        record.node_class = node_class
        record.bus = bus
        record.last_seen = self._clock()

        known = record.state in (NodeState.JOINED, NodeState.READY, NodeState.ABSENT)
        if known and record.address:
            # A known device gets its address ALWAYS and without asking about the mode.
            # Changing a fuse must not require a ceremony
            # (06-cykl-zycia-urzadzenia.md §2.1).
            self.readmitted += 1
            self._assign(bus, record, record.address)
            return

        if record.state == NodeState.RETIRED or not self.accepting:
            # A retired one is treated like an unknown one: it may still speak up, so
            # it needs joining mode (06-cykl-zycia-urzadzenia.md §6).
            if record.state != NodeState.RETIRED:
                record.state = NodeState.PENDING
            self.deferred += 1
            return

        address = self.installation.addresses.allocate(node_class)
        if address is None:
            self.deferred += 1
            return
        self.assigned += 1
        self._assign(bus, record, address)

    def _assign(self, bus: int, record: JoinRecord, address: int) -> None:
        if record.address and record.address != address:
            # A pending node that came back without its address — reset by hand, say —
            # no longer holds the old one.
            self.installation.addresses.release(record.address)
        record.address = address
        record.state = NodeState.JOINED
        self.installation.addresses.reserve(address)
        if self._on_assigned is not None:
            self._on_assigned(record)
        self._send(
            bus,
            Frame(
                encode_join_id(announce=False, short_id=record.short_id),
                record.short_id.to_bytes(4, "little") + address.to_bytes(2, "little"),
            ),
        )

    # --- address conflict -----------------------------------------------------

    def on_booted(self, bus: int, address: int) -> None:
        """The same address on two buses is a conflict (03-adresacja.md §6).

        Only the hub can detect it: nodes on separate buses never hear each other. The
        hub cannot settle it — it does not know which node is the right one — so it
        reports an issue needing human attention (07-hub.md §5.3).

        **We recognise it by the interface, not by `ShortId`.** §6 spoke of "two
        `Booted` announcements with different `ShortId`s", but `Booted` carries only
        `resetReason` and `firmwareVersion` — there is no `ShortId` in it and never was.
        The bus number, on the other hand, the hub has with every frame, and under
        global addressing (03-adresacja.md §2.1) the same address on two interfaces is
        **in itself** a conflict. The data the rule asked for is therefore unnecessary —
        what arrives anyway suffices.
        """
        foreign = self.installation.foreign_record_at(bus, address)
        if foreign is None:
            return
        if (address, foreign.bus, bus) in self.conflicts:
            return
        self.conflicts.append((address, foreign.bus, bus))
        self.installation.issues.append(
            f"address 0x{address:03X} is in use on two buses "
            f"({foreign.bus} and {bus}) — the hub cannot settle which node is the right "
            f"one"
        )


def _node_class(value: int) -> NodeClass:
    try:
        return NodeClass(value)
    except ValueError:
        # The class is a hint for bring-up, not a truth about the device
        # (05-mapowanie-can.md §4.1). An unknown value lands in the reserve, like
        # UNSPECIFIED.
        return NodeClass.UNSPECIFIED
