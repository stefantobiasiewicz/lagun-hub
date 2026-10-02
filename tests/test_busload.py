"""Traffic per bus — counters, the rate read from them, the load (zadania/zrobione/FEAT-05.md).

The counters are what a later collector of metrics will stand on (zadania/FEAT-08.md), so
the tests pin both halves: a counter **only grows** and is split by bus, and the rate is
the counters' difference over the window — nothing kept on the side.
"""

from __future__ import annotations

import queue
from concurrent.futures import Future
from pathlib import Path
from typing import Any

import pytest

from lagunhub.api import HubApi
from lagunhub.core.bindings import BindingEngine
from lagunhub.core.busload import FRAME_BITS, SAMPLE, WINDOW, BusCounts, BusMeter
from lagunhub.core.config import load
from lagunhub.core.hub import CoreThread
from lagunhub.core.model import Installation, NodeModel, NodeState, Snapshot, Target, Uid
from lagunhub.core.rx import ColdItem, RxThread
from lagunhub.protocol.bus import VirtualBus, VirtualNetwork
from lagunhub.protocol.bus.base import TransmitQueueFull
from lagunhub.protocol.frame import Frame, encode_header, encode_id
from lagunhub.protocol.registry import MessageKind
from tests.conftest import Clock

UID_KEY = Uid(bytes(range(8)))
UID_RELAY = Uid(bytes(range(8, 16)))


def report(address: int, payload: bytes = b"\x01") -> Frame:
    return Frame(encode_id(MessageKind.REPORT, address, 4, 0x00), payload)


def click(address: int) -> Frame:
    return Frame(encode_id(MessageKind.EVENT, address, 4, 0x03), b"\x00\x01")


class Full(VirtualBus):
    """A bus whose kernel send queue is always full."""

    def send(self, frame: Frame) -> None:
        raise TransmitQueueFull("full")


class TestFrameBits:
    def test_an_eight_byte_extended_frame_is_160_bits_at_worst(self) -> None:
        """67 bits of frame and gap + 64 of data + 29 stuff bits — the figure every CAN
        load calculator quotes for the worst case."""
        assert FRAME_BITS[8] == 160

    def test_an_empty_frame(self) -> None:
        assert FRAME_BITS[0] == 67 + 13

    def test_every_payload_length_has_a_figure(self) -> None:
        assert len(FRAME_BITS) == 9
        assert list(FRAME_BITS) == sorted(FRAME_BITS)


class TestMeter:
    def meter(self, buses: int = 1) -> tuple[BusMeter, BusCounts, BusCounts]:
        rx, tx = BusCounts(buses), BusCounts(buses)
        meter = BusMeter(buses)
        meter.watch(received=[rx], sent=[tx])
        return meter, rx, tx

    def test_n_frames_in_t_seconds_is_n_over_t(self) -> None:
        meter, rx, _ = self.meter()
        now = 0.0
        meter.sample(now)
        for _ in range(int(WINDOW / SAMPLE)):
            for _ in range(20):
                rx.count(0, report(0x020))
            now += SAMPLE
            meter.sample(now)
        assert meter.reading(0).rx_rate == pytest.approx(20.0 / SAMPLE)

    def test_the_rate_follows_the_window_not_the_whole_life(self) -> None:
        """A burst long ago must not hold the rate up — that is what the window is for."""
        meter, rx, _ = self.meter()
        meter.sample(0.0)
        for _ in range(300):
            rx.count(0, report(0x020))
        meter.sample(1.0)
        now = 1.0
        for _ in range(int(WINDOW / SAMPLE)):
            rx.count(0, report(0x020))
            now += SAMPLE
            meter.sample(now)
        reading = meter.reading(0)
        assert reading.rx_rate == pytest.approx(1.0 / SAMPLE)
        assert reading.rx == 303, "the counter keeps everything since the start"

    def test_one_sample_gives_no_rate_yet(self) -> None:
        meter, rx, _ = self.meter()
        rx.count(0, report(0x020))
        meter.sample(0.0)
        assert meter.reading(0).rx_rate == 0.0
        assert meter.reading(0).rx == 1

    def test_buses_are_counted_apart(self) -> None:
        meter, rx, tx = self.meter(buses=2)
        meter.sample(0.0)
        for _ in range(4):
            rx.count(1, report(0x020))
        tx.count(0, report(0x001))
        tx.drop(1)
        meter.sample(1.0)
        first, second = meter.reading(0), meter.reading(1)
        assert (first.rx, first.tx, first.dropped) == (0, 1, 0)
        assert (second.rx, second.tx, second.dropped) == (4, 0, 1)
        assert second.rx_rate == pytest.approx(4.0)
        assert first.tx_rate == pytest.approx(1.0)

    def test_sent_frames_add_up_from_every_sender(self) -> None:
        """Two threads send — the fast path and CORE — each into its own counters."""
        meter, _, fast = self.meter()
        core = BusCounts(1)
        meter.watch(sent=[core])
        fast.count(0, report(0x001))
        core.count(0, report(0x001))
        core.count(0, report(0x001))
        assert meter.reading(0).tx == 3

    def test_bits_count_both_directions(self) -> None:
        """Both directions take the same wire, so the load is their sum."""
        meter, rx, tx = self.meter()
        meter.sample(0.0)
        rx.count(0, Frame(encode_id(MessageKind.REPORT, 0x20, 4, 0), bytes(8)))
        tx.count(0, Frame(encode_id(MessageKind.COMMAND, 0x40, 4, 2), b""))
        meter.sample(1.0)
        assert meter.reading(0).bits_rate == pytest.approx(FRAME_BITS[8] + FRAME_BITS[0])


class TestCountingOnTheWire:
    """Each counter sits where the frame is handled — not on the far end of a queue that
    may overflow (zadania/zrobione/FEAT-05.md, what the code showed)."""

    def test_rx_counts_per_bus(self) -> None:
        net_a, net_b = VirtualNetwork(), VirtualNetwork()
        hub_a, hub_b = net_a.attach("a"), net_b.attach("b")
        node_a, node_b = net_a.attach("node_a"), net_b.attach("node_b")
        rx = RxThread([hub_a, hub_b], BindingEngine([hub_a, hub_b], Snapshot()), queue.Queue())

        node_a.send(report(0x020))
        for _ in range(3):
            node_b.send(report(0x021))
        rx.pump()

        assert rx.counts.frames == [1, 3]
        assert rx.received == 4, "the old total stays"

    def test_rx_counts_even_when_the_slow_queue_is_full(self) -> None:
        net = VirtualNetwork()
        hub, node = net.attach("hub"), net.attach("node")
        rx = RxThread([hub], BindingEngine([hub], Snapshot()), queue.Queue(maxsize=1))
        for _ in range(5):
            node.send(report(0x020))
        rx.pump()
        assert rx.overflowed == 4
        assert rx.counts.frames == [5]

    def binding(self, bus: int) -> Snapshot:
        target = Target(
            bus=bus,
            can_id=encode_id(MessageKind.COMMAND, 0x040, 4, 0x02),
            payload=bytes([encode_header(0)]),
            transactional=True,
        )
        return Snapshot(
            addr={0x02A: UID_KEY, 0x040: UID_RELAY}, bind={(UID_KEY, 4, 0x03): (target,)}
        )

    def test_the_fast_path_counts_on_the_target_bus(self) -> None:
        net_a, net_b = VirtualNetwork(), VirtualNetwork()
        engine = BindingEngine([net_a.attach("a"), net_b.attach("b")], self.binding(bus=1))
        engine.handle(click(0x02A))
        assert engine.counts.frames == [0, 1]
        assert engine.counts.bits == [0, FRAME_BITS[1]]

    def test_the_fast_path_counts_a_drop_apart_from_a_send(self) -> None:
        net = VirtualNetwork()
        engine = BindingEngine([Full("full", net)], self.binding(bus=0))
        engine.handle(click(0x02A))
        assert engine.counts.frames == [0]
        assert engine.counts.dropped == [1]
        assert engine.dropped == 1, "the old total stays"

    def core(self, *buses: VirtualBus) -> CoreThread:
        cold: queue.Queue[ColdItem] = queue.Queue()
        return CoreThread(Installation(), cold, lambda s: None, buses=buses, clock=Clock())

    def test_core_counts_what_it_sends(self) -> None:
        net = VirtualNetwork()
        core = self.core(net.attach("a"), net.attach("b"))
        core._send(1, report(0x001))
        assert core.counts.frames == [0, 1]

    def test_core_counts_a_drop(self) -> None:
        net = VirtualNetwork()
        core = self.core(Full("full", net))
        core._send(0, report(0x001))
        assert core.counts.frames == [0]
        assert core.counts.dropped == [1]
        assert core.dropped_sends == 1, "the old total stays"

    def test_core_survives_a_bus_it_does_not_have(self) -> None:
        core = self.core(VirtualNetwork().attach("a"))
        core._send(5, report(0x001))
        assert core.dropped_sends == 1
        assert core.counts.dropped == [0], "no bus to put it on"

    def test_core_samples_every_second_on_its_own(self) -> None:
        """The rate needs nobody to ask — CORE's own deadline takes the samples."""
        net = VirtualNetwork()
        clock = Clock()
        cold: queue.Queue[ColdItem] = queue.Queue()
        core = CoreThread(
            Installation(), cold, lambda s: None, buses=[net.attach("a")], clock=clock
        )
        for _ in range(int(WINDOW / SAMPLE)):
            for _ in range(10):
                core._send(0, report(0x001))
            clock.advance(SAMPLE)
            core.pump(clock.now)
        assert core.meter.reading(0).tx_rate == pytest.approx(10.0 / SAMPLE)


CONFIG = """
buses:
  - { channel: can0, bitrate: 125000 }
nodes: {}
bindings: []
"""


class TestHealth:
    def rig(self, tmp_path: Path, name: str = "can0") -> tuple[Clock, VirtualBus, RxThread, HubApi]:
        path = tmp_path / "hub.yaml"
        path.write_text(CONFIG, encoding="utf-8")
        clock = Clock()
        net = VirtualNetwork()
        hub = net.attach(name)
        node = net.attach("node")
        cold: queue.Queue[ColdItem] = queue.Queue()
        engine = BindingEngine([hub], Snapshot())
        rx = RxThread([hub], engine, cold)
        # A node the hub knows — an unknown address would be probed, and CORE's questions
        # would show up in `tx`.
        installation = Installation()
        installation.add(NodeModel(uid=UID_KEY, address=0x020, bus=0, state=NodeState.READY))
        core = CoreThread(installation, cold, lambda s: None, buses=[hub], clock=clock)
        core.meter.watch(received=[rx.counts], sent=[engine.counts])
        core.apply_config(load(path))

        def wait(future: Future[Any], timeout: float) -> Any:
            core.pump(clock.now)
            return future.result(timeout=0)

        api = HubApi(core, engine=engine, wait=wait)
        for _ in range(int(WINDOW / SAMPLE)):
            for _ in range(25):
                node.send(Frame(encode_id(MessageKind.REPORT, 0x020, 4, 0), bytes(8)))
            rx.pump()
            clock.advance(SAMPLE)
            core.pump(clock.now)
        return clock, hub, rx, api

    def test_health_carries_traffic_per_bus(self, tmp_path: Path) -> None:
        clock, _, _, api = self.rig(tmp_path)
        health = api.health()
        (bus,) = health.buses
        assert bus.rx == 75
        assert bus.rx_rate == pytest.approx(25.0)
        assert bus.tx == 0
        assert health.uptime == pytest.approx(clock.now)

    def test_load_comes_from_the_bitrate_in_the_file(self, tmp_path: Path) -> None:
        _, _, _, api = self.rig(tmp_path)
        (bus,) = api.health().buses
        assert bus.bitrate == 125_000
        assert bus.load == pytest.approx(25 * FRAME_BITS[8] / 125_000)

    def test_a_bus_not_in_the_file_has_no_load(self, tmp_path: Path) -> None:
        """`--udp` or `--bus` past the file: no bitrate, so no percentage — not a guess."""
        _, _, _, api = self.rig(tmp_path, name="udp")
        (bus,) = api.health().buses
        assert bus.bitrate is None
        assert bus.load is None
        assert bus.rx_rate == pytest.approx(25.0), "the rate does not need the bitrate"
