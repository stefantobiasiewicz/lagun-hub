"""The CORE thread, the installation model and swapping the snapshot."""

from __future__ import annotations

import queue
import threading

import pytest

from lagunhub.core.hub import CoreThread
from lagunhub.core.model import (
    BindingSpec,
    Installation,
    NodeModel,
    NodeState,
    Snapshot,
    Uid,
)
from lagunhub.core.rx import ColdItem, Received
from lagunhub.core.schedule import FOREVER, Schedule
from lagunhub.protocol.endpoints import output as out
from lagunhub.protocol.frame import Frame, encode_header, encode_id
from lagunhub.protocol.registry import EndpointType, MessageKind
from lagunhub.protocol.values import encode
from tests.conftest import Clock

UID_KEY = Uid(bytes(range(8)))
UID_RELAY = Uid(bytes(range(8, 16)))


def setup() -> Installation:
    inst = Installation()
    inst.add(NodeModel(uid=UID_KEY, address=0x02A, bus=0, name="Key", state=NodeState.READY))
    inst.add(NodeModel(uid=UID_RELAY, address=0x040, bus=0, name="Lamp", state=NodeState.READY))
    inst.bindings.append(
        BindingSpec(
            source=(UID_KEY, 4, 0x03),
            target_uid=UID_RELAY,
            target_endpoint=4,
            target_element=out.TOGGLE,
            label="Toggle",
        )
    )
    return inst


def core_thread(inst: Installation, clock: Clock) -> tuple[CoreThread, list[Snapshot]]:
    published: list[Snapshot] = []
    cold: queue.Queue[ColdItem] = queue.Queue()
    core = CoreThread(inst, cold, published.append, clock=clock)
    return core, published


def report(address: int, endpoint: int, attribute: int, data: bytes) -> ColdItem:
    return Received(0, Frame(encode_id(MessageKind.REPORT, address, endpoint, attribute), data))


def received_frame(frame: Frame) -> ColdItem:
    return Received(0, frame)


class TestModel:
    def test_everything_is_keyed_by_uid(self) -> None:
        inst = setup()
        assert set(inst.nodes) == {UID_KEY, UID_RELAY}
        assert inst.by_address == {0x02A: UID_KEY, 0x040: UID_RELAY}

    def test_readdressing_rebuilds_the_target_frame(self) -> None:
        """The target frame is made from the CURRENT addresses, not once when the file
        was loaded."""
        inst = setup()
        before = inst.snapshot().bind[(UID_KEY, 4, 0x03)][0]

        inst.readdress(UID_RELAY, 0x041)
        po = inst.snapshot().bind[(UID_KEY, 4, 0x03)][0]

        assert before.can_id != po.can_id
        assert Frame(po.can_id).fields.address == 0x041
        assert 0x040 not in inst.by_address, "the old address is released"

    def test_a_target_without_an_address_yields_no_binding(self) -> None:
        """A binding to a node that has no address yet simply does nothing."""
        inst = setup()
        inst.readdress(UID_RELAY, 0x000)
        assert inst.snapshot().bind == {}

    def test_an_endpoint_nothing_is_known_about_is_still_an_endpoint(self) -> None:
        inst = setup()
        node = inst.nodes[UID_RELAY]
        assert node.endpoint(9).type_id is None
        assert 9 in node.endpoints

    def test_a_group_target_without_members_yields_no_frame(self) -> None:
        """A group nobody belongs to is zero frames — not an error."""
        inst = setup()
        inst.bindings.append(
            BindingSpec(
                source=(UID_KEY, 5, 0x03),
                group=1,
                target_endpoint=0,
                target_element=out.OFF,
                kind=MessageKind.GROUP_COMMAND,
            )
        )
        assert inst.snapshot().bind.get((UID_KEY, 5, 0x03)) is None


class TestSlowPath:
    def test_a_report_restores_state_without_polling(self, clock: Clock) -> None:
        """01-model-danych.md §6 — that is what the periodic refresh is for."""
        inst = setup()
        inst.nodes[UID_RELAY].endpoint(4).type_id = EndpointType.Output
        core, _ = core_thread(inst, clock)

        clock.advance(5.0)
        core.cold.put(report(0x040, 4, out.ON_OFF, encode("bool", True)))
        core.pump()

        state = inst.nodes[UID_RELAY].endpoints[4].attributes[out.ON_OFF]
        assert state.value is True
        assert state.raw == b"\x01"
        assert state.at == 5.0

    def test_an_unknown_type_keeps_the_bytes(self, clock: Clock) -> None:
        """Better to keep bytes we cannot interpret than to pretend they are absent."""
        inst = setup()
        core, _ = core_thread(inst, clock)

        core.cold.put(report(0x040, 9, 0x00, b"\x2c\x01"))
        core.pump()

        state = inst.nodes[UID_RELAY].endpoints[9].attributes[0x00]
        assert state.value is None
        assert state.raw == b"\x2c\x01"

    def test_every_frame_moves_last_heard_forward(self, clock: Clock) -> None:
        """06-cykl-zycia-urzadzenia.md §5 — of any kind, not only a report."""
        inst = setup()
        core, _ = core_thread(inst, clock)

        clock.advance(3.0)
        core.cold.put(
            received_frame(Frame(encode_id(MessageKind.EVENT, 0x02A, 4, 0x03), b"\x00\x01"))
        )
        core.pump()
        assert inst.nodes[UID_KEY].last_heard == 3.0

        clock.advance(2.0)
        core.cold.put(
            received_frame(
                Frame(encode_id(MessageKind.RESPONSE, 0x040, 4, 0x02), bytes([encode_header(1)]))
            )
        )
        core.pump()
        assert inst.nodes[UID_RELAY].last_heard == 5.0

    def test_a_frame_from_an_unknown_address_breaks_nothing(self, clock: Clock) -> None:
        inst = setup()
        core, _ = core_thread(inst, clock)
        core.cold.put(report(0x111, 4, 0x00, b"\x01"))
        core.pump()
        assert core.handled == 1
        assert len(inst.nodes) == 2

    def test_an_announcement_reaches_joining(self, clock: Clock) -> None:
        from lagunhub.protocol.frame import encode_join_id

        inst = setup()
        core, _ = core_thread(inst, clock)
        short_id = 0x0000_DEAD
        core.cold.put(
            received_frame(
                Frame(
                    encode_join_id(announce=True, short_id=short_id),
                    short_id.to_bytes(4, "little") + b"\x01",
                )
            )
        )
        core.pump()

        assert core.ignored == 0
        assert short_id in inst.records


class TestSnapshotSwap:
    def test_it_publishes_at_start(self, clock: Clock) -> None:
        core, published = core_thread(setup(), clock)
        core.pump()
        assert len(published) == 1
        assert published[0].addr == {0x02A: UID_KEY, 0x040: UID_RELAY}

    def test_it_does_not_publish_without_a_change(self, clock: Clock) -> None:
        core, published = core_thread(setup(), clock)
        core.pump()
        core.cold.put(report(0x040, 4, out.ON_OFF, b"\x01"))
        core.pump()
        assert len(published) == 1, "a report changes state, not structure"

    def test_it_publishes_after_the_model_changes(self, clock: Clock) -> None:
        inst = setup()
        core, published = core_thread(inst, clock)
        core.pump()
        core.call(lambda i: i.readdress(UID_RELAY, 0x041))
        core.pump()
        assert len(published) == 2
        assert published[-1].addr[0x041] == UID_RELAY

    def test_the_swap_never_yields_a_mixed_image(self) -> None:
        """The completion criterion for the snapshot.

        A reader takes the reference ONCE per frame. It either sees the old snapshot
        whole or the new one whole — there is no state in between.
        """
        inst = setup()
        held: list[Snapshot] = [inst.snapshot()]
        stop = threading.Event()
        errors: list[str] = []

        def writer() -> None:
            for i in range(2000):
                inst.readdress(UID_RELAY, 0x040 + (i % 16))
                held[0] = inst.snapshot()
            stop.set()

        def reader() -> None:
            while not stop.is_set():
                snapshot = held[0]  # one read of the reference
                targets = snapshot.bind.get((UID_KEY, 4, 0x03))
                if targets is None:
                    errors.append("a snapshot without the binding")
                    continue
                address = Frame(targets[0].can_id).fields.address
                if snapshot.addr.get(address) != UID_RELAY:
                    errors.append(f"a mixed image: target 0x{address:03X}")

        threads = [
            threading.Thread(target=writer),
            *[threading.Thread(target=reader) for _ in range(3)],
        ]
        for w in threads:
            w.start()
        for w in threads:
            w.join(timeout=10)

        assert errors == []


class TestDeadlines:
    def test_the_wait_follows_from_the_nearest_deadline(self) -> None:
        s = Schedule()
        assert s.next_deadline(0.0) == FOREVER
        s.after(0.0, 0.1, lambda: None)
        s.after(0.0, 0.5, lambda: None)
        assert s.next_deadline(0.0) == pytest.approx(0.1)

    def test_never_negative(self) -> None:
        s = Schedule()
        s.at(1.0, lambda: None)
        assert s.next_deadline(5.0) == 0.0

    def test_they_fire_in_order(self) -> None:
        s = Schedule()
        log: list[str] = []
        s.at(2.0, lambda: log.append("b"))
        s.at(1.0, lambda: log.append("a"))
        s.at(3.0, lambda: log.append("c"))
        assert s.fire_due(2.5) == 2
        assert log == ["a", "b"]

    def test_a_cancelled_one_does_not_fire(self) -> None:
        s = Schedule()
        log: list[str] = []
        handle = s.at(1.0, lambda: log.append("a"))
        s.at(1.0, lambda: log.append("b"))
        handle.cancel()
        s.fire_due(2.0)
        assert log == ["b"]
        assert len(s) == 0


class TestModelDump:
    """`format_installation` is today the only way to see the model."""

    def test_an_empty_model_says_so_outright(self) -> None:
        from lagunhub.monitor import format_installation

        assert "empty" in format_installation(Installation(), 0.0)

    def test_a_node_without_reports_says_nothing_is_known_about_it(self, clock: Clock) -> None:
        """The key has no reported attributes, so the hub will learn nothing about it."""
        from lagunhub.monitor import format_installation

        inst = setup()
        core, _ = core_thread(inst, clock)
        core.cold.put(
            received_frame(Frame(encode_id(MessageKind.EVENT, 0x02A, 4, 0x03), b"\x00\x01"))
        )
        core.pump()

        dump = format_installation(inst, clock.now)
        assert "Key" in dump
        assert "reported nothing" in dump

    def test_a_report_shows_up_named(self, clock: Clock) -> None:
        from lagunhub.monitor import format_installation

        inst = setup()
        inst.nodes[UID_RELAY].endpoint(4).type_id = EndpointType.Output
        core, _ = core_thread(inst, clock)
        core.cold.put(report(0x040, 4, out.ON_OFF, encode("bool", True)))
        core.pump()

        dump = format_installation(inst, clock.now)
        assert "Output" in dump
        assert "OnOff = True" in dump

    def test_an_unknown_type_shows_the_bytes(self, clock: Clock) -> None:
        from lagunhub.monitor import format_installation

        inst = setup()
        core, _ = core_thread(inst, clock)
        core.cold.put(report(0x040, 9, 0x00, b"\x2c\x01"))
        core.pump()

        dump = format_installation(inst, clock.now)
        assert "unknown type" in dump
        assert "[2c 01]" in dump
