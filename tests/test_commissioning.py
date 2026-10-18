"""A node joining.

The asymmetry from 06-cykl-zycia-urzadzenia.md §2.1 matters most here: **a known device
gets its address always and without asking**, while the accepting mode protects only
against an unknown one getting in. Changing a fuse is not to require a ceremony.
"""

from __future__ import annotations

import queue

from lagunhub.core.commissioning import ACCEPT_WINDOW, Commissioning
from lagunhub.core.hub import CoreThread
from lagunhub.core.model import AddressPool, Installation, NodeState, Snapshot, Uid
from lagunhub.core.rx import ColdItem, Received
from lagunhub.core.schedule import Schedule
from lagunhub.protocol.bus import VirtualNetwork
from lagunhub.protocol.endpoints import device as dev
from lagunhub.protocol.frame import Frame, decode_join_id, encode_id, encode_join_id
from lagunhub.protocol.registry import NODE_CLASS_BLOCKS, MessageKind, NodeClass
from tests.conftest import Clock


def announcement(short_id: int, node_class: NodeClass = NodeClass.CONTROL) -> Frame:
    return Frame(
        encode_join_id(announce=True, short_id=short_id),
        short_id.to_bytes(4, "little") + bytes([int(node_class)]) + b"\x00" * 3,
    )


class Sender:
    def __init__(self) -> None:
        self.frames: list[Frame] = []

    def __call__(self, bus: int, frame: Frame) -> None:
        self.frames.append(frame)

    def assignments(self) -> list[tuple[int, int]]:
        """(ShortId, address) from every ASSIGNMENT."""
        return [
            (
                int.from_bytes(f.data[:4], "little"),
                int.from_bytes(f.data[4:6], "little"),
            )
            for f in self.frames
            if f.is_join and not decode_join_id(f.can_id).announce
        ]


def joining(clock: Clock) -> tuple[Commissioning, Installation, Sender, Schedule]:
    inst = Installation()
    sender = Sender()
    schedule = Schedule()
    return Commissioning(inst, sender, schedule, clock), inst, sender, schedule


class TestAddressPool:
    def test_the_block_follows_from_the_class(self) -> None:
        pool = AddressPool()
        assert pool.allocate(NodeClass.CONTROL) == 0x001
        assert pool.allocate(NodeClass.SENSOR) == 0x100
        assert pool.allocate(NodeClass.UNSPECIFIED) == 0x400

    def test_consecutive_addresses_from_the_same_block(self) -> None:
        pool = AddressPool()
        assert [pool.allocate(NodeClass.CONTROL) for _ in range(3)] == [0x001, 0x002, 0x003]

    def test_a_reserved_one_is_skipped(self) -> None:
        pool = AddressPool()
        pool.reserve(0x001)
        pool.reserve(0x002)
        assert pool.allocate(NodeClass.CONTROL) == 0x003

    def test_a_full_block_says_so_outright(self) -> None:
        pool = AddressPool()
        lo, hi = NODE_CLASS_BLOCKS[NodeClass.CONTROL]
        for address in range(lo, hi + 1):
            pool.reserve(address)
        assert pool.allocate(NodeClass.CONTROL) is None

    def test_the_blocks_do_not_mix(self) -> None:
        pool = AddressPool()
        for _ in range(10):
            pool.allocate(NodeClass.CONTROL)
        assert pool.allocate(NodeClass.SENSOR) == 0x100


class TestJoiningMode:
    def test_an_unknown_one_waits_while_the_mode_is_off(self, clock: Clock) -> None:
        """The completion criterion: it lands as PENDING and gets no address."""
        comm, inst, sender, _ = joining(clock)
        comm.on_join(0, announcement(0xAABBCCDD))

        assert sender.assignments() == []
        assert inst.records[0xAABBCCDD].state == NodeState.PENDING
        assert inst.records[0xAABBCCDD].address == 0
        assert comm.deferred == 1

    def test_with_it_on_it_gets_an_address(self, clock: Clock) -> None:
        comm, inst, sender, _ = joining(clock)
        comm.accept_new()
        comm.on_join(0, announcement(0xAABBCCDD))

        assert sender.assignments() == [(0xAABBCCDD, 0x001)]
        assert inst.records[0xAABBCCDD].state == NodeState.JOINED

    def test_a_pending_one_is_taken_in_once_it_is_switched_on(self, clock: Clock) -> None:
        """A node announces itself without end, so once the mode is on it comes in by
        itself."""
        comm, _, sender, _ = joining(clock)
        comm.on_join(0, announcement(0xAABBCCDD))
        comm.accept_new()
        comm.on_join(0, announcement(0xAABBCCDD))

        assert sender.assignments() == [(0xAABBCCDD, 0x001)]

    def test_the_mode_expires_on_its_own(self, clock: Clock) -> None:
        comm, _, sender, schedule = joining(clock)
        comm.accept_new()
        opened = comm.accepting
        assert opened

        clock.advance(ACCEPT_WINDOW + 1.0)
        schedule.fire_due(clock.now)
        assert comm.accepting is False

        comm.on_join(0, announcement(0xAABBCCDD))
        assert sender.assignments() == []

    def test_a_known_one_gets_its_address_while_the_mode_is_off(self, clock: Clock) -> None:
        """The completion criterion — and the whole asymmetry of this mechanism."""
        comm, inst, sender, _ = joining(clock)
        comm.accept_new()
        comm.on_join(0, announcement(0xAABBCCDD))
        comm._close()

        comm.on_join(0, announcement(0xAABBCCDD))  # e.g. after power came back

        assert sender.assignments() == [(0xAABBCCDD, 0x001), (0xAABBCCDD, 0x001)]
        assert comm.readmitted == 1
        assert inst.records[0xAABBCCDD].address == 0x001

    def test_a_removed_one_needs_the_mode_like_an_unknown_one(self, clock: Clock) -> None:
        """Removing forgets the record, so a removed node is as unknown as one never
        seen (06-cykl-zycia-urzadzenia.md §6) — and gets in once the mode is open. The
        former `RETIRED` state kept it out even then."""
        comm, inst, sender, _ = joining(clock)
        comm.accept_new()
        comm.on_join(0, announcement(0xAABBCCDD))
        inst.record(0xAABBCCDD).uid = Uid(b"\x01" * 8)
        inst.forget(Uid(b"\x01" * 8))
        comm._close()

        comm.on_join(0, announcement(0xAABBCCDD))
        assert inst.records[0xAABBCCDD].state == NodeState.PENDING

        comm.accept_new()
        comm.on_join(0, announcement(0xAABBCCDD))
        assert sender.assignments()[-1] == (0xAABBCCDD, 0x001)

    def test_a_blocked_one_gets_nothing_even_with_the_mode_open(self, clock: Clock) -> None:
        """The one exception to "a known one always gets its address"
        (06-cykl-zycia-urzadzenia.md §8)."""
        comm, inst, sender, _ = joining(clock)
        comm.accept_new()
        comm.on_join(0, announcement(0xAABBCCDD))
        inst.record(0xAABBCCDD).uid = Uid(b"\x01" * 8)
        inst.blocked = frozenset({Uid(b"\x01" * 8)})

        comm.on_join(0, announcement(0xAABBCCDD))

        assert sender.assignments() == [(0xAABBCCDD, 0x001)]
        assert inst.records[0xAABBCCDD].state == NodeState.BLOCKED


class TestOneChosen:
    """One pending node without an address, admitted by the operator while the mode is
    off (06-cykl-zycia-urzadzenia.md §2.1; zadania/zrobione/FEAT-15.md)."""

    def test_it_gets_an_address_at_its_next_announcement(self, clock: Clock) -> None:
        comm, inst, sender, _ = joining(clock)
        comm.on_join(0, announcement(0xAABBCCDD))
        comm.admit(0xAABBCCDD)

        assert sender.assignments() == [], "no ASSIGNMENT without an announcement (N2)"
        comm.on_join(0, announcement(0xAABBCCDD))

        assert sender.assignments() == [(0xAABBCCDD, 0x001)]
        assert inst.records[0xAABBCCDD].state == NodeState.JOINED
        assert not comm.admitting(0xAABBCCDD), "the choice is spent"

    def test_the_others_stay_pending(self, clock: Clock) -> None:
        comm, inst, sender, _ = joining(clock)
        comm.on_join(0, announcement(0xAABBCCDD))
        comm.on_join(0, announcement(0x11223344))
        comm.admit(0xAABBCCDD)

        comm.on_join(0, announcement(0x11223344))
        comm.on_join(0, announcement(0xAABBCCDD))

        assert sender.assignments() == [(0xAABBCCDD, 0x001)]
        assert inst.records[0x11223344].state == NodeState.PENDING

    def test_the_choice_expires_like_the_mode(self, clock: Clock) -> None:
        comm, inst, sender, _ = joining(clock)
        comm.on_join(0, announcement(0xAABBCCDD))
        comm.admit(0xAABBCCDD)

        clock.advance(ACCEPT_WINDOW + 1.0)
        comm.on_join(0, announcement(0xAABBCCDD))

        assert sender.assignments() == []
        assert inst.records[0xAABBCCDD].state == NodeState.PENDING

    def test_a_full_block_keeps_the_choice_for_the_next_announcement(self, clock: Clock) -> None:
        comm, inst, sender, _ = joining(clock)
        comm.on_join(0, announcement(0xAABBCCDD))
        comm.admit(0xAABBCCDD)
        lo, hi = NODE_CLASS_BLOCKS[NodeClass.CONTROL]
        for address in range(lo, hi + 1):
            inst.addresses.reserve(address)

        comm.on_join(0, announcement(0xAABBCCDD))
        assert sender.assignments() == []
        assert comm.admitting(0xAABBCCDD)

        inst.addresses.release(lo)
        comm.on_join(0, announcement(0xAABBCCDD))
        assert sender.assignments() == [(0xAABBCCDD, lo)]

    def test_a_blocked_one_gets_nothing_even_when_chosen(self, clock: Clock) -> None:
        comm, inst, sender, _ = joining(clock)
        comm.on_join(0, announcement(0xAABBCCDD))
        inst.record(0xAABBCCDD).uid = Uid(b"\x01" * 8)
        inst.blocked = frozenset({Uid(b"\x01" * 8)})
        comm.admit(0xAABBCCDD)

        comm.on_join(0, announcement(0xAABBCCDD))

        assert sender.assignments() == []
        assert inst.records[0xAABBCCDD].state == NodeState.BLOCKED


class TestAnnouncement:
    def test_the_block_follows_from_the_declared_class(self, clock: Clock) -> None:
        """The completion criterion: CONTROL gets an address from 0x001–0x0FF."""
        comm, _, sender, _ = joining(clock)
        comm.accept_new()
        comm.on_join(0, announcement(0x11111111, NodeClass.CONTROL))
        comm.on_join(0, announcement(0x22222222, NodeClass.SENSOR))
        comm.on_join(0, announcement(0x33333333, NodeClass.UNSPECIFIED))

        addresses = [address for _, address in sender.assignments()]
        assert addresses == [0x001, 0x100, 0x400]

    def test_an_unknown_class_lands_in_the_reserve(self, clock: Clock) -> None:
        """The class is a hint for bring-up, not a truth about the device
        (05-mapowanie-can.md §4.1)."""
        comm, _, sender, _ = joining(clock)
        comm.accept_new()
        frame = Frame(
            encode_join_id(announce=True, short_id=0xAABBCCDD),
            (0xAABBCCDD).to_bytes(4, "little") + b"\x63" + b"\x00" * 3,
        )
        comm.on_join(0, frame)
        assert sender.assignments() == [(0xAABBCCDD, 0x400)]

    def test_it_compares_the_full_thirty_two_bits(self, clock: Clock) -> None:
        """05-mapowanie-can.md §4.3 — the redundancy is deliberate and the payload
        settles it."""
        comm, _, sender, _ = joining(clock)
        comm.accept_new()
        contradictory = Frame(
            encode_join_id(announce=True, short_id=0xAABBCCDD),
            (0x11223344).to_bytes(4, "little") + b"\x01" + b"\x00" * 3,
        )
        comm.on_join(0, contradictory)
        assert sender.assignments() == [], "the payload contradicts the identifier"

    def test_an_assignment_has_a_lower_identifier_than_an_announcement(self, clock: Clock) -> None:
        """05-mapowanie-can.md §4.4 — assignments push through the crowd of announcers."""
        comm, _, sender, _ = joining(clock)
        comm.accept_new()
        ann = announcement(0xAABBCCDD)
        comm.on_join(0, ann)
        assert sender.frames[0].can_id < ann.can_id

    def test_a_short_payload_is_skipped(self, clock: Clock) -> None:
        comm, _, sender, _ = joining(clock)
        comm.accept_new()
        comm.on_join(0, Frame(encode_join_id(announce=True, short_id=0x1), b"\x01\x00"))
        assert sender.assignments() == []


class TestConflict:
    def test_the_same_address_on_two_buses(self, clock: Clock) -> None:
        """It is a conflict also when the nodes sit on different buses — and only the hub
        is able to see it, because they will never hear each other
        (03-adresacja.md §6)."""
        comm, inst, _, _ = joining(clock)
        comm.accept_new()
        comm.on_join(0, announcement(0x11111111))

        # Booted from the same bus the node got its address on — all is well.
        comm.on_booted(0, 0x001)
        assert comm.conflicts == []
        assert inst.issues == []

        # The same address, but the frame came from the OTHER bus.
        comm.on_booted(1, 0x001)
        assert comm.conflicts == [(0x001, 0, 1)]
        assert len(inst.issues) == 1
        assert "0x001" in inst.issues[0]

    def test_the_issue_does_not_multiply_on_every_restart(self, clock: Clock) -> None:
        """A node restarting in circles sends Booted in circles. The issue is to stay one
        — a list that grows without end stops being a list anybody reads."""
        comm, inst, _, _ = joining(clock)
        comm.accept_new()
        comm.on_join(0, announcement(0x11111111))

        for _ in range(5):
            comm.on_booted(1, 0x001)

        assert comm.conflicts == [(0x001, 0, 1)]
        assert len(inst.issues) == 1

    def test_a_free_address_on_another_bus_is_not_a_conflict(self, clock: Clock) -> None:
        """An address nobody holds is in conflict with nothing."""
        comm, inst, _, _ = joining(clock)
        comm.accept_new()
        comm.on_join(0, announcement(0x11111111))

        comm.on_booted(1, 0x0FF)
        assert comm.conflicts == []
        assert inst.issues == []


class TestFortyAtOnce:
    def test_a_wave_of_announcements_ends_without_collisions(self, clock: Clock) -> None:
        """The completion criterion for joining — 03-adresacja.md §5.5."""
        comm, inst, sender, _ = joining(clock)
        comm.accept_new()
        for i in range(40):
            comm.on_join(0, announcement(0x1000_0000 + i))

        assignments = sender.assignments()
        assert len(assignments) == 40
        addresses = [address for _, address in assignments]
        assert len(set(addresses)) == 40, "not one address twice"
        assert all(0x001 <= a <= 0x0FF for a in addresses), "all in the CONTROL block"
        assert len(inst.addresses) == 40

    def test_each_one_sends_a_different_announcement_identifier(self) -> None:
        """The condition for collision-free arbitration (05-mapowanie-can.md §4.2)."""
        identifiers = {announcement(0x1000_0000 + i).can_id for i in range(40)}
        assert len(identifiers) == 40


def test_core_routes_an_announcement_to_joining(clock: Clock) -> None:
    inst = Installation()
    cold: queue.Queue[ColdItem] = queue.Queue()
    net = VirtualNetwork()
    hub = net.attach("hub")
    preview = net.attach("preview_port")
    published: list[Snapshot] = []
    core = CoreThread(inst, cold, published.append, buses=[hub], clock=clock)
    core.commissioning.accept_new()

    cold.put(Received(0, announcement(0xAABBCCDD)))
    core.pump()

    assert core.commissioning.assigned == 1
    assignment = preview.recv()
    assert assignment is not None and assignment.is_join
    assert not decode_join_id(assignment.can_id).announce


def booted(address: int, firmware: int = 0x0001_0000) -> Frame:
    """The `Booted` event as a node sends it: resetReason + firmwareVersion."""
    return Frame(
        encode_id(MessageKind.EVENT, address, 0, dev.BOOTED),
        bytes([0x00, 0x00]) + firmware.to_bytes(4, "little"),
    )


def test_core_sees_the_same_address_on_two_buses(clock: Clock) -> None:
    """Wiring up the rule of 03-adresacja.md §6 — not the rule itself.

    Until this change `Commissioning.on_booted` existed and NOBODY called it except a
    test. This test watches exactly that road: a frame from the bus enters CORE and an
    issue for the operator comes out. Without it the rule can be unhooked again unnoticed.
    """
    inst = Installation()
    cold: queue.Queue[ColdItem] = queue.Queue()
    net = VirtualNetwork()
    hub = net.attach("hub")
    second = net.attach("second")
    published: list[Snapshot] = []
    core = CoreThread(inst, cold, published.append, buses=[hub, second], clock=clock)
    core.commissioning.accept_new()

    # The node joins on bus 0 and gets an address.
    cold.put(Received(0, announcement(0xAABBCCDD)))
    core.pump()
    record = inst.record(0xAABBCCDD)
    assert record.address and record.bus == 0

    # The same address speaks up from bus 1 — the nodes will never hear each other, so
    # the self-yielding of §6 will not save them. Only the hub sees this.
    cold.put(Received(1, booted(record.address)))
    core.pump()

    assert len(inst.issues) == 1
    assert f"0x{record.address:03X}" in inst.issues[0]


def test_core_keeps_quiet_when_booted_comes_from_its_own_bus(clock: Clock) -> None:
    """An ordinary node restart is not a conflict and is to report nothing."""
    inst = Installation()
    cold: queue.Queue[ColdItem] = queue.Queue()
    net = VirtualNetwork()
    hub = net.attach("hub")
    published: list[Snapshot] = []
    core = CoreThread(inst, cold, published.append, buses=[hub], clock=clock)
    core.commissioning.accept_new()

    cold.put(Received(0, announcement(0xAABBCCDD)))
    core.pump()
    record = inst.record(0xAABBCCDD)

    cold.put(Received(0, booted(record.address)))
    core.pump()

    assert inst.issues == []
