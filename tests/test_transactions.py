"""Transactions: retries, matching replies, result codes."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from lagunhub.core.schedule import Schedule
from lagunhub.core.transactions import ATTEMPTS, TIMEOUT, Outcome, TransactionManager
from lagunhub.protocol.endpoints import output as out
from lagunhub.protocol.frame import Frame, encode_header, encode_id
from lagunhub.protocol.registry import MessageKind, ResultCode
from tests.conftest import Clock

RELAY = 0x040


class FakeBus:
    """Records what the manager sent. No socket needed."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, Frame]] = []

    def __call__(self, bus: int, frame: Frame) -> None:
        self.sent.append((bus, frame))

    @property
    def tids(self) -> list[int]:
        return [f.data[0] & 0x07 for _, f in self.sent]


def manager(clock: Clock) -> tuple[TransactionManager, FakeBus, Schedule]:
    bus = FakeBus()
    schedule = Schedule()
    return TransactionManager(bus, schedule, clock), bus, schedule


def response(endpoint: int, element: int, tid: int, result: int = 0, data: bytes = b"") -> Frame:
    return Frame(
        encode_id(MessageKind.RESPONSE, RELAY, endpoint, element),
        bytes([encode_header(tid, result)]) + data,
    )


def command(endpoint: int = 4, element: int = out.TOGGLE) -> int:
    return encode_id(MessageKind.COMMAND, RELAY, endpoint, element)


class TestRetries:
    def test_three_attempts_at_zero_hundred_two_hundred(self, clock: Clock) -> None:
        """The completion criterion: a lost reply gives exactly three attempts."""
        tx, bus, schedule = manager(clock)
        results: list[Outcome] = []
        tx.request(0, command(), on_done=results.append)

        times = [0.0]
        for _ in range(4):
            clock.now += TIMEOUT
            schedule.fire_due(clock.now)
            if len(bus.sent) > len(times):
                times.append(clock.now)

        assert len(bus.sent) == ATTEMPTS
        assert times == pytest.approx([0.0, 0.100, 0.200])
        assert len(results) == 1
        assert not results[0].answered, "silence after three attempts"
        assert results[0].attempts == 3

    def test_a_retry_goes_out_with_the_same_tid(self, clock: Clock) -> None:
        """04-interakcje.md §2.1 — that is how the node recognises them in its
        deduplication buffer."""
        tx, bus, schedule = manager(clock)
        tx.request(0, command())
        for _ in range(3):
            clock.now += TIMEOUT
            schedule.fire_due(clock.now)

        assert len(set(bus.tids)) == 1, "three attempts, one Tid"

    def test_a_reply_stops_the_retrying(self, clock: Clock) -> None:
        tx, bus, schedule = manager(clock)
        results: list[Outcome] = []
        tx.request(0, command(), on_done=results.append)

        clock.now += 0.050
        assert tx.on_response(response(4, out.TOGGLE, bus.tids[0]))

        clock.now += 1.0
        schedule.fire_due(clock.now)
        assert len(bus.sent) == 1, "we do not repeat what has already been answered"
        assert results[0].ok
        assert results[0].attempts == 1
        assert results[0].elapsed == pytest.approx(0.050)

    def test_a_reply_just_after_the_deadline_does_not_revive_the_transaction(
        self, clock: Clock
    ) -> None:
        tx, bus, schedule = manager(clock)
        tx.request(0, command())
        tid = bus.tids[0]
        for _ in range(3):
            clock.now += TIMEOUT
            schedule.fire_due(clock.now)

        assert tx.lost == 1
        assert not tx.on_response(response(4, out.TOGGLE, tid)), "a late one belongs to nobody"
        assert len(tx) == 0


class TestMatching2:
    def test_replies_out_of_order(self, clock: Clock) -> None:
        """That is what a Tid is for (04-interakcje.md §10)."""
        tx, bus, _ = manager(clock)
        results: dict[str, Outcome] = {}

        def save(name: str) -> Callable[[Outcome], None]:
            def done(outcome: Outcome) -> None:
                results[name] = outcome

            return done

        tx.request(0, command(4, out.ON), on_done=save("on"))
        tx.request(0, command(5, out.OFF), on_done=save("off"))

        tid_on, tid_off = bus.tids
        tx.on_response(response(5, out.OFF, tid_off, data=b"\x00"))
        tx.on_response(response(4, out.ON, tid_on, data=b"\x11"))

        assert results["on"].data == b"\x11"
        assert results["off"].data == b"\x00"

    def test_the_same_tid_on_different_endpoints_does_not_clash(self, clock: Clock) -> None:
        tx, bus, _ = manager(clock)
        results: list[int] = []
        tx.request(0, command(4, out.TOGGLE), on_done=lambda o: results.append(o.data[0]))
        tx.request(0, command(5, out.TOGGLE), on_done=lambda o: results.append(o.data[0]))

        assert bus.tids == [0, 0], "separate pools, because the key carries the endpoint"
        tx.on_response(response(5, out.TOGGLE, 0, data=b"\x05"))
        tx.on_response(response(4, out.TOGGLE, 0, data=b"\x04"))
        assert results == [5, 4]

    def test_a_reply_to_somebody_elses_request(self, clock: Clock) -> None:
        tx, _, _ = manager(clock)
        assert not tx.on_response(response(9, 0x00, 3))

    def test_a_reply_without_a_header(self, clock: Clock) -> None:
        tx, _, _ = manager(clock)
        assert not tx.on_response(Frame(encode_id(MessageKind.RESPONSE, RELAY, 4, 0x00)))


class TestResultCodes:
    def test_busy_is_retried(self, clock: Clock) -> None:
        """The only code after which repeating is worth it (04-interakcje.md §9)."""
        tx, bus, _ = manager(clock)
        results: list[Outcome] = []
        tx.request(0, command(), on_done=results.append)

        tx.on_response(response(4, out.TOGGLE, bus.tids[0], ResultCode.BUSY))
        assert len(bus.sent) == 2, "BUSY caused a retry at once"
        assert results == [], "the transaction is still running"

        tx.on_response(response(4, out.TOGGLE, bus.tids[1], ResultCode.OK))
        assert results[0].ok

    def test_busy_does_not_retry_forever(self, clock: Clock) -> None:
        tx, bus, _ = manager(clock)
        results: list[Outcome] = []
        tx.request(0, command(), on_done=results.append)
        for _ in range(ATTEMPTS):
            tx.on_response(response(4, out.TOGGLE, bus.tids[-1], ResultCode.BUSY))

        assert len(bus.sent) == ATTEMPTS
        assert results[0].result == ResultCode.BUSY

    @pytest.mark.parametrize(
        "code",
        [
            ResultCode.UNSUPPORTED_ELEMENT,
            ResultCode.READ_ONLY,
            ResultCode.INVALID_VALUE,
            ResultCode.FAILURE,
        ],
    )
    def test_the_remaining_errors_are_not_retried(self, clock: Clock, code: ResultCode) -> None:
        """Repeated they will give the same result (04-interakcje.md §9)."""
        tx, bus, _ = manager(clock)
        results: list[Outcome] = []
        tx.request(0, command(), on_done=results.append)
        tx.on_response(response(4, out.TOGGLE, bus.tids[0], code))

        assert len(bus.sent) == 1
        assert results[0].result == code
        assert not results[0].ok


class TestWatchingSomebodyElsesRequest:
    def test_track_does_not_send_on_its_own(self, clock: Clock) -> None:
        """The fast path has already sent the command; CORE takes over the retries."""
        tx, bus, schedule = manager(clock)
        payload = bytes([encode_header(5)])
        tx.track(0, command(), payload, tid=5)

        assert bus.sent == [], "nothing was sent a second time"
        clock.now += TIMEOUT
        schedule.fire_due(clock.now)
        assert len(bus.sent) == 1, "only the retry goes out from CORE"
        assert bus.tids[0] == 5, "and with the same Tid"


class TestLongWrite:
    """`LONG_WRITE` — a text for a display field (05-mapowanie-can.md §6; FEAT-12)."""

    TEXT = 0x04
    VALUE = bytes([13]) + b"Pranie gotowe"

    def long_write(self) -> int:
        return encode_id(MessageKind.LONG_WRITE, RELAY, 5, self.TEXT)

    def test_every_segment_goes_out_at_once_with_one_tid(self, clock: Clock) -> None:
        tx, bus, _ = manager(clock)
        tx.request_long(0, self.long_write(), self.VALUE)
        frames = [f for _, f in bus.sent]
        assert [f.can_id for f in frames] == [self.long_write()] * 3
        assert [f.data.hex(" ") for f in frames] == [
            "00 00 0d 50 72 61 6e 69",
            "00 01 65 20 67 6f 74 6f",
            "80 02 77 65",
        ], "PRZYKLADY 10, with the first Tid"

    def test_the_one_reply_closes_it(self, clock: Clock) -> None:
        tx, _, _ = manager(clock)
        results: list[Outcome] = []
        tx.request_long(0, self.long_write(), self.VALUE, on_done=results.append)
        assert tx.on_response(response(5, self.TEXT, 0))
        assert [r.result for r in results] == [ResultCode.OK]
        assert len(tx) == 0

    def test_silence_repeats_the_whole_value(self, clock: Clock) -> None:
        """A node that lost a segment drops the value and says nothing; the hub sends
        all of it again, never the missing piece (§6.6)."""
        tx, bus, schedule = manager(clock)
        tx.request_long(0, self.long_write(), self.VALUE)
        clock.now += TIMEOUT
        schedule.fire_due(clock.now)
        assert len(bus.sent) == 6
        assert [f.data for _, f in bus.sent[3:]] == [f.data for _, f in bus.sent[:3]]

    def test_busy_repeats_the_whole_value(self, clock: Clock) -> None:
        """BUSY: the node is assembling another value (§6.7)."""
        tx, bus, _ = manager(clock)
        results: list[Outcome] = []
        tx.request_long(0, self.long_write(), self.VALUE, on_done=results.append)
        tx.on_response(response(5, self.TEXT, 0, ResultCode.BUSY))
        assert len(bus.sent) == 6 and not results
        tx.on_response(response(5, self.TEXT, 0))
        assert [(r.result, r.attempts) for r in results] == [(ResultCode.OK, 2)]

    def test_an_empty_text_is_one_segment(self, clock: Clock) -> None:
        tx, bus, _ = manager(clock)
        tx.request_long(0, self.long_write(), b"\x00")
        assert [f.data.hex(" ") for _, f in bus.sent] == ["80 00 00"]
