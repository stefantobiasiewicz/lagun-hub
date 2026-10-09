"""The narrowest vertical path: key → hub → relay.

This is the only test that checks the whole thing at once, and that is why it is the most
important one in this directory. Everything else examines pieces.
"""

from __future__ import annotations

import queue
import time
from collections.abc import Callable
from dataclasses import dataclass

import pytest

from lagunhub.core.bindings import BindingEngine
from lagunhub.core.config import load
from lagunhub.core.config.apply import apply
from lagunhub.core.hub import CoreThread
from lagunhub.core.model import EndpointModel, Installation, NodeModel, NodeState, Uid
from lagunhub.core.rx import ColdItem, RxThread, Sent
from lagunhub.core.traffic import Traffic
from lagunhub.core.transactions import Outcome
from lagunhub.protocol.bus import Bus, VirtualBus, VirtualNetwork
from lagunhub.protocol.endpoints import output as out
from lagunhub.protocol.frame import Frame, encode_id
from lagunhub.protocol.registry import EndpointType, MessageKind
from tests.conftest import Clock
from tests.node.devices import key1, relay1
from tests.node.endpoints.key_input import KeyInput
from tests.node.endpoints.output import Output
from tests.node.node import NodeCore

CONFIG = """
buses: [vcan0]
nodes:
  "0001020304050607":
    alias: kitchen-switch
    name: "The kitchen key"
  "08090a0b0c0d0e0f":
    alias: kitchen-relay
    name: "The lamp over the table"
bindings:
  - from: { node: kitchen-switch, endpoint: 4, type: KeyInput, event: Click }
    to:   { node: kitchen-relay,  endpoint: 4, type: Output,   command: Toggle }
"""


@dataclass
class Rig:
    """Two nodes and a hub on one bus. A clock driven from the test."""

    clock: Clock
    net: VirtualNetwork
    key_sim: NodeCore
    relay: NodeCore
    rx: RxThread
    engine: BindingEngine
    core: CoreThread
    preview: Bus
    settle: float = 0.0
    """Real time to wait after the nodes send — for a bus that goes through the kernel
    (UDP), where a frame is not in the other socket the instant `send` returns."""

    @property
    def key(self) -> KeyInput:
        return self.key_sim.endpoint(4, KeyInput)

    @property
    def output(self) -> Output:
        return self.relay.endpoint(4, Output)

    def run(self, seconds: float, step: float = 0.005) -> None:
        """Wind the time on. Each step: the nodes send, the hub listens, the nodes
        receive."""
        end = self.clock.now + seconds
        while self.clock.now < end:
            self.clock.advance(step)
            for node in (self.key_sim, self.relay):
                node.poll()
                node.tick(self.clock.now)
            if self.settle:
                time.sleep(self.settle)
            self.rx.pump()
            self.core.pump(self.clock.now)
            for node in (self.key_sim, self.relay):
                node.poll()

    def click(self) -> None:
        self.key.press(self.clock.now)
        self.run(0.080)
        self.key.release(self.clock.now)
        self.run(0.050)


def build(  # type: ignore[no-untyped-def]
    tmp_path, attach: Callable[[str], Bus] | None = None, settle: float = 0.0
) -> Rig:
    """The rig on a virtual bus — or on any other, when `attach` makes the ports."""
    path = tmp_path / "poc.yaml"
    path.write_text(CONFIG, encoding="utf-8")
    config = load(path)

    clock = Clock()
    net = VirtualNetwork()
    port = attach if attach is not None else net.attach
    key_sim = key1(uid=bytes(range(8)), address=0x02A, clock=clock, name="key")
    relay = relay1(uid=bytes(range(8, 16)), address=0x040, clock=clock, name="relay")
    key_sim.start(port("key"))
    relay.start(port("relay"))

    hub_port = port("hub")
    installation = Installation()
    apply(installation, config)
    for uid, address in ((bytes(range(8)), 0x02A), (bytes(range(8, 16)), 0x040)):
        installation.add(NodeModel(uid=Uid(uid), address=address, bus=0, state=NodeState.READY))
    cold: queue.Queue[ColdItem] = queue.Queue()
    engine = BindingEngine([hub_port], installation.snapshot())
    rx = RxThread([hub_port], engine, cold)
    engine.on_sent = lambda bus, frame: rx.offer(Sent(bus, frame, clock.now))
    core = CoreThread(
        installation,
        cold,
        lambda s: setattr(engine, "snapshot", s),
        buses=[hub_port],
        clock=clock,
        tids=engine.tids,
    )
    preview = port("preview_port")
    if settle:
        time.sleep(settle)
    while preview.recv() is not None:
        pass
    rx.pump()
    core.pump(clock.now)
    return Rig(clock, net, key_sim, relay, rx, engine, core, preview, settle)


def test_a_key_toggles_a_relay(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The completion criterion of the first vertical path."""
    inst = build(tmp_path)
    assert inst.output.values[out.ON_OFF] is False

    inst.click()
    assert inst.output.values[out.ON_OFF] is True, "the click switched it on"

    inst.click()
    assert inst.output.values[out.ON_OFF] is False, "the second click switched it off"

    assert inst.engine.matched == 2
    assert inst.engine.sent == 2


def test_a_binding_and_core_draw_tids_from_one_counter(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A click through the binding, then `Toggle` from CORE to the same output within
    300 ms — two commands, two toggles (BUG-06).

    With a counter each, both started at 0: the node saw the same `Tid` + endpoint +
    command inside its deduplication window, sent back the remembered `OK` and did not
    switch. The hub reported success and the light stayed as it was.
    """
    inst = build(tmp_path)
    inst.click()
    assert inst.output.values[out.ON_OFF] is True

    results: list[Outcome] = []
    inst.core.request(0x040, 4, out.TOGGLE, kind=MessageKind.COMMAND, on_done=results.append)
    inst.run(0.050)

    assert [r.ok for r in results] == [True]
    assert inst.output.values[out.ON_OFF] is False, "the second Toggle was carried out"


def test_the_whole_exchange_is_visible_on_the_bus(tmp_path) -> None:  # type: ignore[no-untyped-def]
    inst = build(tmp_path)
    while inst.preview.recv() is not None:
        pass

    inst.click()

    exchange = []
    while (frame := inst.preview.recv()) is not None:
        f = frame.fields
        exchange.append((MessageKind(f.kind).name, f.address, f.endpoint, f.element))

    assert exchange == [
        ("EVENT", 0x02A, 4, 0x03),  # Click from the key
        ("COMMAND", 0x040, 4, 0x02),  # Toggle from the hub
        ("RESPONSE", 0x040, 4, 0x02),  # I took it and carried it out
        ("REPORT", 0x040, 4, 0x00),  # and here is the physical effect
    ]


def test_a_hold_does_not_toggle(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A hold gives no Click, so the relay is not to move."""
    inst = build(tmp_path)
    inst.key.press(inst.clock.now)
    inst.run(1.200)
    inst.key.release(inst.clock.now)
    inst.run(0.050)

    assert inst.output.values[out.ON_OFF] is False
    assert inst.engine.matched == 0
    assert inst.engine.unmatched > 0, "HoldStart and HoldTick arrived, but nobody listens"


def test_the_hub_does_not_repeat_frames_between_buses(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The hub is an actor, not a repeater (03-adresacja.md §4.3) — it sends a command
    OF ITS OWN."""
    inst = build(tmp_path)
    while inst.preview.recv() is not None:
        pass
    inst.click()

    sent_by_hub = [f for f in iter(inst.preview.recv, None) if f.fields.kind == MessageKind.COMMAND]
    assert len(sent_by_hub) == 1
    assert sent_by_hub[0].fields.address == 0x040, "the RECIPIENT's address, not the event's sender"


def test_load_does_not_spoil_the_latency(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Traffic unrelated to bindings goes through the engine and costs nothing."""
    inst = build(tmp_path)
    noise = VirtualBus("noise", inst.net)
    inst.net._ports.append(noise)

    for i in range(500):
        noise.send(Frame(encode_id(MessageKind.REPORT, 0x200 + (i % 50), 4, 0x00), b"\x01"))
        if i % 10 == 0:
            noise.send(Frame(encode_id(MessageKind.EVENT, 0x02A, 4, 0x03), bytes([i & 0xFF, 1])))
    inst.rx.pump()

    stats = inst.engine.latency.percentiles()
    assert stats["n"] == 50
    assert stats["p999"] < 50_000, "p99.9 below the 50 ms alarm threshold"
    assert inst.rx.overflowed == 0


def test_the_rx_thread_works_on_a_real_select(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """This one test runs the thread for real — the rest call pump()."""
    inst = build(tmp_path)
    inst.rx.start()
    try:
        for _ in range(20):
            inst.key.force_click()
            inst.key_sim.flush()
            time.sleep(0.002)
        deadline = time.monotonic() + 1.0
        while inst.engine.matched < 20 and time.monotonic() < deadline:
            time.sleep(0.005)
    finally:
        inst.rx.stop()

    assert inst.engine.matched == 20
    stats = inst.engine.latency.percentiles()
    assert stats["p99"] < 50_000


class TestRetriesLive:
    """The completion criterion for transactions — a repetition has no right to carry a command out
    twice."""

    def test_a_lost_reply_does_not_toggle_twice(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        inst = build(tmp_path)
        inst.relay.faults.drop_responses = 1

        inst.click()
        assert inst.output.values[out.ON_OFF] is True, "the command was carried out"

        inst.run(0.150)  # the first repetition falls due after 100 ms

        assert inst.output.values[out.ON_OFF] is True, (
            "the repetition landed in the deduplication buffer rather than switching back"
        )
        assert inst.core.transactions.retried == 1
        assert inst.core.transactions.answered == 1
        assert inst.core.transactions.lost == 0

    def test_a_silent_node_ends_in_silence_after_three_attempts(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        inst = build(tmp_path)
        inst.relay.faults.drop_responses = 9

        inst.click()
        inst.run(0.400)

        tx = inst.core.transactions
        assert tx.retried == 2, "the first attempt plus two repetitions"
        assert tx.lost == 1
        assert tx.answered == 0

    def test_busy_is_retried_and_eventually_gets_through(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        inst = build(tmp_path)
        inst.relay.faults.busy_responses = 1

        inst.click()
        inst.run(0.050)

        assert inst.output.values[out.ON_OFF] is True, "the second time it was carried out"
        assert inst.core.transactions.retried == 1

    def test_the_hub_sees_its_own_commands_on_the_slow_path(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """On a real SocketCAN a socket does not receive its own frames."""
        inst = build(tmp_path)
        inst.click()
        assert inst.core.transactions.opened == 1, (
            "a command sent by the fast path came under CORE's care"
        )


class TestLateRepliesInTheFeed:
    """A reply after its transaction has closed is still named after the REQUEST
    (BUG-03).

    `0x02` is the `Toggle` command or the `Red` attribute, and the reply alone does not
    say which. Once the first reply closed the transaction, the second one was named from
    the attribute table — "Red OK", an answer to something nobody sent.
    """

    def feed(self, inst: Rig, act: Callable[[], None]) -> list[Traffic]:
        sub = inst.core.traffic.subscribe()
        try:
            act()
            return [t for t in iter(lambda: sub.get(0), None)]
        finally:
            inst.core.traffic.unsubscribe(sub)

    def known_relay(self, inst: Rig) -> None:
        """Names come from the type catalogue, so the hub has to know the endpoint — as it
        would after enumeration, which this rig skips."""
        relay = inst.core.installation.nodes[Uid(bytes(range(8, 16)))]
        relay.endpoints[4] = EndpointModel(4, EndpointType.Output)

    def test_both_replies_to_a_repeated_toggle_are_named_toggle(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        inst = build(tmp_path)
        self.known_relay(inst)
        # The node carries the command out (at ≈85 ms, the press) but holds its transmit
        # queue until 250 ms — the hub repeats at ≈185 ms, and both replies leave
        # together, as with the interrupt serviced every 100 ms (FEAT-11).
        inst.relay.faults.silent_until = inst.clock.now + 0.250

        def click() -> None:
            inst.click()
            inst.run(0.200)

        replies = [r for r in self.feed(inst, click) if r.kind == "RESPONSE"]

        assert [r.name for r in replies] == ["Toggle", "Toggle"]
        assert [r.late for r in replies] == [False, True]
        first, second = replies
        assert first.elapsed is not None and second.elapsed is not None
        assert second.elapsed >= first.elapsed, "counted from the first attempt, too"

    def test_a_retry_is_marked_as_one(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """On the wire a retry is the same frame with the same `Tid` — without a mark the
        feed showed two identical `Toggle` rows, and the retry was found only on the
        board."""
        inst = build(tmp_path)
        self.known_relay(inst)
        inst.relay.faults.silent_until = inst.clock.now + 0.250

        def click() -> None:
            inst.click()
            inst.run(0.200)

        commands = [r for r in self.feed(inst, click) if r.kind == "COMMAND"]

        assert [r.retry for r in commands] == [0, 1]
        assert commands[0].elapsed is None, "the first attempt has nothing to count from"
        assert commands[1].elapsed == pytest.approx(0.100, abs=0.010)

    def test_a_reply_nobody_asked_for_is_not_named(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """Without the request the namespace is unknown — the number, not a guess."""
        inst = build(tmp_path)
        self.known_relay(inst)

        def stray() -> None:
            inst.preview.send(Frame(encode_id(MessageKind.RESPONSE, 0x040, 4, out.TOGGLE), b"\x05"))
            inst.run(0.010)

        (reply,) = [r for r in self.feed(inst, stray) if r.kind == "RESPONSE"]
        assert reply.name == ""
        assert reply.elapsed is None and reply.late is False


class TestSegmentsLive:
    """Identifying a device: the hub reads a Uid that does not fit in a frame."""

    def read_uid(self, inst: Rig) -> list[Outcome]:
        from lagunhub.protocol.endpoints import device as dev

        results: list[Outcome] = []
        inst.core.request(0x040, 0, dev.UID, on_done=results.append)
        inst.run(0.050)
        return results

    def test_a_uid_arrives_in_segments(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        from lagunhub.protocol.values import decode_blob

        inst = build(tmp_path)
        while inst.preview.recv() is not None:
            pass

        results = self.read_uid(inst)

        assert results[0].ok
        assert decode_blob(results[0].data) == inst.relay.uid
        assert inst.core.segments.completed == 1

        kinds = [
            MessageKind(f.fields.kind).name for f in iter(inst.preview.recv, None) if not f.is_join
        ]
        assert kinds == ["READ", "RESPONSE", "LONG_RESPONSE", "LONG_RESPONSE"], (
            "the read, the SEGMENTED announcement and two segments of an eight-byte Uid"
        )

    def test_to_the_hub_this_is_still_one_read(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        inst = build(tmp_path)
        self.read_uid(inst)

        tx = inst.core.transactions
        assert tx.opened == 1
        assert tx.answered == 1
        assert tx.segmented == 1
        assert tx.retried == 0

    def test_a_lost_segment_repeats_the_whole_request(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """05-mapowanie-can.md §6.6 — there is no partial retransmission."""
        from lagunhub.protocol.values import decode_blob

        inst = build(tmp_path)
        inst.relay.faults.drop_segment = 1

        results = self.read_uid(inst)
        assert results == [], "the last segment was lost, the value is not whole yet"
        assert inst.core.segments.dropped == 0, "the hole is invisible — simply nothing arrived"

        inst.run(0.120)  # 100 ms with no next segment
        assert inst.core.segments.dropped == 1
        assert inst.core.transactions.retried == 1
        assert results[0].ok, "the second attempt went through whole"
        assert decode_blob(results[0].data) == inst.relay.uid

    def test_a_second_long_read_mid_way_gets_busy(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """A node assembles at most one value in each direction at a time
        (05-mapowanie-can.md §6.7)."""
        from lagunhub.protocol.endpoints import device as dev
        from lagunhub.protocol.frame import decode_header, encode_header, encode_id
        from lagunhub.protocol.registry import ResultCode

        inst = build(tmp_path)
        relay = inst.relay
        poller = inst.net.attach("poller")

        def read_yaml(tid: int) -> None:
            poller.send(
                Frame(encode_id(MessageKind.READ, 0x040, 0, dev.UID), bytes([encode_header(tid)]))
            )
            relay.poll()

        relay.faults.silent_until = inst.clock.now + 10.0
        read_yaml(1)
        assert relay._segments_pending > 0, "the segments wait in the transmit queue"

        read_yaml(2)
        relay.faults.silent_until = 0.0
        relay.flush()

        results = [
            decode_header(f.data[0])
            for f in iter(poller.recv, None)
            if not f.is_join and f.fields.kind == MessageKind.RESPONSE
        ]
        assert ResultCode.SEGMENTED in [r for r, _ in results]
        assert ResultCode.BUSY in [r for r, _ in results], "the second read was turned away"
