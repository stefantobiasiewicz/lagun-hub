"""The binding engine and loading the POC configuration."""

from __future__ import annotations

from lagunhub.core.bindings import BindingEngine
from lagunhub.core.model import Snapshot, Target, Uid
from lagunhub.protocol.bus import VirtualNetwork
from lagunhub.protocol.frame import Frame, decode_header, encode_header, encode_id
from lagunhub.protocol.registry import MessageKind

UID_KEY = Uid(bytes(range(8)))
UID_RELAY = Uid(bytes(range(8, 16)))


def binding_engine(snapshot: Snapshot) -> tuple[BindingEngine, object]:
    net = VirtualNetwork()
    hub = net.attach("hub")
    preview = net.attach("preview_port")
    return BindingEngine([hub], snapshot), preview


def event(address: int, endpoint: int = 4, element: int = 0x03) -> Frame:
    return Frame(encode_id(MessageKind.EVENT, address, endpoint, element), b"\x00\x01")


def snapshot_with_binding() -> Snapshot:
    cel = Target(
        bus=0,
        can_id=encode_id(MessageKind.COMMAND, 0x040, 4, 0x02),
        payload=bytes([encode_header(0)]),
        transactional=True,
    )
    return Snapshot(
        addr={0x02A: UID_KEY, 0x040: UID_RELAY},
        bind={(UID_KEY, 4, 0x03): (cel,)},
    )


class TestMatching:
    def test_an_event_triggers_a_command(self) -> None:
        engine, preview = binding_engine(snapshot_with_binding())
        assert engine.handle(event(0x02A)) == 1

        sent = preview.recv()  # type: ignore[attr-defined]
        assert sent is not None
        assert sent.fields.kind == MessageKind.COMMAND
        assert (sent.fields.address, sent.fields.endpoint) == (0x040, 4)
        assert sent.fields.element == 0x02

    def test_no_binding_is_not_an_error(self) -> None:
        """An event nobody listens to is a normal state (07-hub.md §4)."""
        engine, preview = binding_engine(snapshot_with_binding())
        assert engine.handle(event(0x02A, element=0x07)) == 0
        assert engine.unmatched == 1
        assert preview.recv() is None  # type: ignore[attr-defined]

    def test_an_unknown_address_is_not_an_error(self) -> None:
        engine, _ = binding_engine(snapshot_with_binding())
        assert engine.handle(event(0x111)) == 0
        assert engine.unmatched == 1

    def test_a_report_is_not_a_source(self) -> None:
        """A binding on an attribute is tested in `test_reconcile.py`; what counts here is that
        a REPORT is not fast."""
        engine, _ = binding_engine(snapshot_with_binding())
        report = Frame(encode_id(MessageKind.REPORT, 0x02A, 4, 0x03), b"\x01")
        assert engine.handle(report) == 0
        assert engine.unmatched == 0, "a report does not even enter the fast path"

    def test_a_command_is_not_a_source(self) -> None:
        """Bindings do not join into chains (07-hub.md §4.1) — and have no way to."""
        engine, _ = binding_engine(snapshot_with_binding())
        command = Frame(encode_id(MessageKind.COMMAND, 0x040, 4, 0x02), b"\x00")
        assert engine.handle(command) == 0
        assert engine.matched == 0

    def test_an_alarm_is_a_fast_path(self) -> None:
        cel = Target(
            bus=0,
            can_id=encode_id(MessageKind.COMMAND, 0x040, 4, 0x00),
            payload=bytes([encode_header(0)]),
            transactional=True,
        )
        snapshot = Snapshot(addr={0x02A: UID_KEY}, bind={(UID_KEY, 4, 0x01): (cel,)})
        engine, _ = binding_engine(snapshot)
        alarm = Frame(encode_id(MessageKind.ALARM, 0x02A, 4, 0x01), b"\x01")
        assert engine.handle(alarm) == 1

    def test_one_source_many_targets(self) -> None:
        targets = tuple(
            Target(
                bus=0,
                can_id=encode_id(MessageKind.COMMAND, address, 4, 0x02),
                payload=bytes([encode_header(0)]),
                transactional=True,
            )
            for address in (0x040, 0x041, 0x042)
        )
        snapshot = Snapshot(addr={0x02A: UID_KEY}, bind={(UID_KEY, 4, 0x03): targets})
        engine, preview = binding_engine(snapshot)

        assert engine.handle(event(0x02A)) == 3
        addresses = []
        while (f := preview.recv()) is not None:  # type: ignore[attr-defined]
            addresses.append(f.fields.address)
        assert addresses == [0x040, 0x041, 0x042], "the configuration's order"


class TestTid:
    def test_it_cycles_through_the_full_pool(self) -> None:
        """All eight values, because the specification counts on all of them
        (04-interakcje.md §2.2)."""
        engine, preview = binding_engine(snapshot_with_binding())
        for _ in range(10):
            engine.handle(event(0x02A))

        seen = []
        while (f := preview.recv()) is not None:  # type: ignore[attr-defined]
            _, tid = decode_header(f.data[0])
            seen.append(tid)

        assert seen == [0, 1, 2, 3, 4, 5, 6, 7, 0, 1]

    def test_six_commands_in_the_dedup_window_get_different_tids(self) -> None:
        """H10 — this is the case the specification counts on: a Step every 50 ms.

        With a pool of four values the sixth command would land on a number the node still
        remembers and **would be silently swallowed** — dimming by holding a key would
        stop halfway.
        """
        engine, preview = binding_engine(snapshot_with_binding())
        for _ in range(6):  # 300 ms / 50 ms
            engine.handle(event(0x02A))

        seen = []
        while (f := preview.recv()) is not None:  # type: ignore[attr-defined]
            _, tid = decode_header(f.data[0])
            seen.append(tid)

        assert len(set(seen)) == 6, "six commands in the window are six different Tids"

    def test_different_endpoints_do_not_share_the_budget(self) -> None:
        """The key carries the endpoint and the command number, so the pools are
        disjoint."""
        from lagunhub.core.transactions import TidAllocator

        tids = TidAllocator()
        assert [tids.next(0x100) for _ in range(3)] == [0, 1, 2]
        assert [tids.next(0x200) for _ in range(3)] == [0, 1, 2]

    def test_allocation_is_shared_across_threads(self) -> None:
        """RX and CORE draw from the same allocator, so they cannot land on the same
        one."""
        import threading

        from lagunhub.core.transactions import TidAllocator

        tids = TidAllocator()
        results: list[int] = []
        lock = threading.Lock()

        def fetch() -> None:
            local = [tids.next(0x100) for _ in range(200)]
            with lock:
                results.extend(local)

        threads = [threading.Thread(target=fetch) for _ in range(4)]
        for w in threads:
            w.start()
        for w in threads:
            w.join()

        assert len(results) == 800
        from collections import Counter

        assert set(Counter(results).values()) == {100}, "every value exactly 100 times"


class TestLatencyMeasurement:
    def test_the_latency_is_recorded(self) -> None:
        engine, _ = binding_engine(snapshot_with_binding())
        for _ in range(100):
            engine.handle(event(0x02A))

        stats = engine.latency.percentiles()
        assert stats["n"] == 100
        assert stats["p99"] > 0
        assert stats["p999"] < 50_000, "p99.9 below 50 ms — the threshold from ARCHITEKTURA.md §11"

    def test_unmatched_ones_do_not_pollute_the_measurement(self) -> None:
        engine, _ = binding_engine(snapshot_with_binding())
        for _ in range(50):
            engine.handle(event(0x111))
        assert engine.latency.percentiles() == {}
