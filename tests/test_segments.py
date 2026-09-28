"""Values longer than a frame.

Segmenting **is not a separate interaction**: a read stays a read, and for the hub a `Uid`
arriving in three frames is still one read.
"""

from __future__ import annotations

import pytest

from lagunhub.core.schedule import Schedule
from lagunhub.core.segments import (
    CAPACITY,
    DATA_PER_SEGMENT,
    TIMEOUT,
    SegmentReassembler,
    decode_segment,
    encode_segment,
    split,
)
from lagunhub.core.transactions import ATTEMPTS, Outcome, TransactionManager
from lagunhub.protocol.endpoints import device as dev
from lagunhub.protocol.frame import Frame, encode_header, encode_id
from lagunhub.protocol.registry import MessageKind, ResultCode
from lagunhub.protocol.values import decode_blob, encode_blob
from tests.conftest import Clock

NODE = 0x020
UID = bytes.fromhex("330027001251343331323730")


def segment(payload: bytes, endpoint: int = 0, element: int = dev.UID) -> Frame:
    return Frame(encode_id(MessageKind.LONG_RESPONSE, NODE, endpoint, element), payload)


class TestCodec:
    def test_the_vector_from_the_spec(self) -> None:
        """PRZYKLADY.md §8 — three segments of a twelve-byte Uid, byte for byte."""
        assert [s.hex(" ") for s in split(encode_blob(UID), tid=4)] == [
            "04 00 0c 00 33 00 27 00",
            "04 01 12 51 34 33 31 32",
            "84 02 37 30",
        ]

    def test_round_trip(self) -> None:
        for number in (0, 1, 255):
            for last in (True, False):
                raw_bytes = encode_segment(4, number, b"abcdef", last=last)
                read_back = decode_segment(raw_bytes)
                assert (read_back.tid, read_back.number, read_back.last) == (4, number, last)
                assert read_back.data == b"abcdef"

    def test_the_last_one_has_bit_seven_set(self) -> None:
        assert split(b"123456", tid=0)[-1][0] & 0x80

    def test_an_empty_value_is_one_segment(self) -> None:
        assert len(split(b"", tid=1)) == 1

    def test_value_capacity(self) -> None:
        assert CAPACITY == 256 * DATA_PER_SEGMENT == 1536
        with pytest.raises(ValueError, match="1536"):
            split(b"\x00" * (CAPACITY + 1), tid=0)


class TestReassembler:
    def build(self, clock: Clock) -> tuple[SegmentReassembler, list[bytes], list[str]]:
        values: list[bytes] = []
        lost: list[str] = []
        reassembler = SegmentReassembler(
            Schedule(),
            clock,
            lambda _can_id, _tid, value: values.append(value),
            lambda _can_id, _tid, reason: lost.append(reason),
        )
        return reassembler, values, lost

    def test_a_uid_is_assembled_from_three_segments(self, clock: Clock) -> None:
        """The completion criterion for segments."""
        reassembler, values, lost = self.build(clock)
        for payload in split(encode_blob(UID), tid=4):
            reassembler.feed(segment(payload))

        assert lost == []
        assert decode_blob(values[0]) == UID
        assert reassembler.completed == 1
        assert len(reassembler) == 0, "after assembly nothing is left under construction"

    def test_a_hole_discards_the_value_entirely(self, clock: Clock) -> None:
        reassembler, values, lost = self.build(clock)
        segments = split(encode_blob(UID), tid=4)
        reassembler.feed(segment(segments[0]))
        reassembler.feed(segment(segments[2]))  # the second one was lost

        assert values == []
        assert "hole" in lost[0]
        assert reassembler.dropped == 1

    def test_segment_zero_invalidates_the_earlier_ones(self, clock: Clock) -> None:
        """05-mapowanie-can.md §6.5 — there is no way for two attempts to be glued into
        one."""
        reassembler, values, _ = self.build(clock)
        dropped = split(encode_blob(b"\xaa" * 12), tid=4)
        reassembler.feed(segment(dropped[0]))
        reassembler.feed(segment(dropped[1]))

        for payload in split(encode_blob(UID), tid=5):
            reassembler.feed(segment(payload))

        assert decode_blob(values[0]) == UID, "the new attempt is not glued to the old one"

    def test_a_late_segment_dies_with_the_abandoned_attempt(self, clock: Clock) -> None:
        """The order is guaranteed, so late ones arrive BEFORE segment 0."""
        reassembler, values, _ = self.build(clock)
        reassembler.feed(segment(encode_segment(4, 7, b"late!", last=False)))
        assert len(reassembler) == 0, "a segment with no beginning has nothing to begin"

        for payload in split(encode_blob(UID), tid=4):
            reassembler.feed(segment(payload))
        assert decode_blob(values[0]) == UID

    def test_no_next_segment_for_a_hundred_ms(self, clock: Clock) -> None:
        schedule = Schedule()
        lost: list[str] = []
        reassembler = SegmentReassembler(
            schedule, clock, lambda *_: None, lambda _c, _t, reason: lost.append(reason)
        )
        reassembler.feed(segment(split(encode_blob(UID), tid=4)[0]))

        clock.advance(TIMEOUT / 2)
        schedule.fire_due(clock.now)
        assert lost == []

        clock.advance(TIMEOUT)
        schedule.fire_due(clock.now)
        assert "no next segment" in lost[0]

    def test_a_value_from_another_read_discards_the_current_one(self, clock: Clock) -> None:
        reassembler, values, lost = self.build(clock)
        reassembler.feed(segment(split(encode_blob(UID), tid=4)[0]))
        reassembler.feed(
            segment(encode_segment(4, 1, b"xxxxxx", last=False), element=dev.MODEL_NAME)
        )

        assert "another value" in lost[0]
        assert values == []


class TestTransactionWithSegments:
    """For the hub this is still ONE read (04-interakcje.md §3)."""

    def manager(self, clock: Clock) -> tuple[TransactionManager, list[Frame], Schedule]:
        sent: list[Frame] = []
        schedule = Schedule()
        tx = TransactionManager(lambda _bus, frame: sent.append(frame), schedule, clock)
        return tx, sent, schedule

    def read_op(self) -> int:
        return encode_id(MessageKind.READ, NODE, 0, dev.UID)

    def test_segmented_does_not_end_the_transaction(self, clock: Clock) -> None:
        tx, sent, schedule = self.manager(clock)
        results: list[Outcome] = []
        tx.request(0, self.read_op(), on_done=results.append)
        tid = sent[0].data[0] & 0x07

        herald = Frame(
            encode_id(MessageKind.RESPONSE, NODE, 0, dev.UID),
            bytes([encode_header(tid, ResultCode.SEGMENTED)]),
        )
        assert tx.on_response(herald)
        assert results == [], "SEGMENTED is an announcement, not a result"
        assert tx.segmented == 1

        clock.advance(1.0)
        schedule.fire_due(clock.now)
        assert len(sent) == 1, "we do not repeat the request while waiting for segments"

        assert tx.on_segments(self.read_op(), tid, encode_blob(UID))
        assert results[0].ok
        assert decode_blob(results[0].data) == UID

    def test_lost_segments_repeat_the_whole_request(self, clock: Clock) -> None:
        """05-mapowanie-can.md §6.6 — there is no partial retransmission."""
        tx, sent, _ = self.manager(clock)
        tx.request(0, self.read_op())
        tid = sent[0].data[0] & 0x07
        tx.on_response(
            Frame(
                encode_id(MessageKind.RESPONSE, NODE, 0, dev.UID),
                bytes([encode_header(tid, ResultCode.SEGMENTED)]),
            )
        )

        assert tx.on_segments_lost(self.read_op(), tid)
        assert len(sent) == 2, "the whole request was repeated"
        assert sent[1].data[0] & 0x07 == tid, "with the same Tid"

    def test_loss_does_not_repeat_forever(self, clock: Clock) -> None:
        tx, sent, _ = self.manager(clock)
        results: list[Outcome] = []
        tx.request(0, self.read_op(), on_done=results.append)
        tid = sent[0].data[0] & 0x07
        for _ in range(ATTEMPTS):
            tx.on_response(
                Frame(
                    encode_id(MessageKind.RESPONSE, NODE, 0, dev.UID),
                    bytes([encode_header(tid, ResultCode.SEGMENTED)]),
                )
            )
            tx.on_segments_lost(self.read_op(), tid)

        assert len(sent) == ATTEMPTS
        assert not results[0].answered
