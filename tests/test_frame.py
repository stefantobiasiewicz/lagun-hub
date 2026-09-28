"""The identifier codec — lagunbus-spec/05-mapowanie-can.md §1, §4, §5.1."""

from __future__ import annotations

import random

import pytest

from lagunhub.protocol.frame import (
    ADDRESS_BROADCAST,
    ADDRESS_HUB,
    ADDRESS_MASK,
    CAN_ID_MASK,
    ELEMENT_MASK,
    ENDPOINT_MASK,
    KIND_MASK,
    Frame,
    decode_header,
    decode_id,
    decode_join_id,
    encode_header,
    encode_id,
    encode_join_id,
)


def test_encode_and_decode_round_trip() -> None:
    rng = random.Random(20260917)
    for _ in range(10_000):
        kind = rng.randint(0, KIND_MASK)
        address = rng.randint(0, ADDRESS_MASK)
        endpoint = rng.randint(0, ENDPOINT_MASK)
        element = rng.randint(0, ELEMENT_MASK)
        can_id = encode_id(kind, address, endpoint, element)
        assert can_id <= CAN_ID_MASK
        assert decode_id(can_id) == (kind, address, endpoint, element)


def test_an_identifier_takes_exactly_29_bits() -> None:
    """Twenty-nine bits in all, without a single spare one (05-mapowanie-can.md §1)."""
    assert encode_id(KIND_MASK, ADDRESS_MASK, ENDPOINT_MASK, ELEMENT_MASK) == CAN_ID_MASK
    assert encode_id(0, 0, 0, 0) == 0


def test_the_fields_do_not_overlap() -> None:
    assert decode_id(encode_id(0xF, 0, 0, 0)) == (0xF, 0, 0, 0)
    assert decode_id(encode_id(0, ADDRESS_MASK, 0, 0)) == (0, ADDRESS_MASK, 0, 0)
    assert decode_id(encode_id(0, 0, ENDPOINT_MASK, 0)) == (0, 0, ENDPOINT_MASK, 0)
    assert decode_id(encode_id(0, 0, 0, ELEMENT_MASK)) == (0, 0, 0, ELEMENT_MASK)


@pytest.mark.parametrize(
    ("kind", "address", "endpoint", "element"),
    [(16, 0, 0, 0), (0, 0x800, 0, 0), (0, 0, 64, 0), (0, 0, 0, 256), (-1, 0, 0, 0)],
)
def test_the_ranges_are_enforced(kind: int, address: int, endpoint: int, element: int) -> None:
    with pytest.raises(ValueError):
        encode_id(kind, address, endpoint, element)


def test_well_known_addresses() -> None:
    assert ADDRESS_HUB == 0x7FE
    assert ADDRESS_BROADCAST == 0x7FF


def test_a_key_goes_ahead_of_a_sensor() -> None:
    """The address block IS the priority mechanism (03-adresacja.md §2.3)."""
    key_sim = encode_id(1, 0x001, 4, 0x03)  # CONTROL
    sensor = encode_id(1, 0x100, 4, 0x03)  # SENSOR
    assert key_sim < sensor


def test_an_event_goes_ahead_of_a_report() -> None:
    """Events come before reports (04-interakcje.md §7.2), by the kind number alone."""
    event = encode_id(0x1, ADDRESS_MASK, 63, 0xFF)
    report = encode_id(0x8, 0x001, 0, 0x00)
    assert event < report


class TestJoinFrame:
    """05-mapowanie-can.md §4 — the only two frames without a node address."""

    def test_round_trip(self) -> None:
        for short_id in (0x0000_0000, 0xDEAD_BEEF, 0xFFFF_FFFF):
            for announce in (True, False):
                can_id = encode_join_id(announce=announce, short_id=short_id)
                got = decode_join_id(can_id)
                assert got.announce is announce
                assert got.short_id_low == short_id & 0xFFFFFF

    def test_an_assignment_goes_ahead_of_an_announcement(self) -> None:
        """K=0 has the lower identifier, so it pushes through the crowd
        (05-mapowanie-can.md §4.4)."""
        short_id = 0x00AB_CDEF
        assignment = encode_join_id(announce=False, short_id=short_id)
        announcement = encode_join_id(announce=True, short_id=short_id)
        assert assignment < announcement

    def test_different_nodes_send_different_identifiers(self) -> None:
        """The condition for collision-free arbitration at joining
        (05-mapowanie-can.md §4.2)."""
        ids = {encode_join_id(announce=True, short_id=i) for i in range(1000)}
        assert len(ids) == 1000

    def test_a_frame_identifies_itself(self) -> None:
        f = Frame(encode_join_id(announce=True, short_id=0x1234_5678))
        assert f.is_join
        assert not Frame(encode_id(1, 0x2A, 4, 3)).is_join


class TestHeaderByte:
    """05-mapowanie-can.md §5.1 — RESULT (5 b) and Tid (3 b) in one byte."""

    def test_round_trip(self) -> None:
        for result in range(32):
            for tid in range(8):
                assert decode_header(encode_header(tid, result)) == (result, tid)

    def test_a_request_has_a_zero_result(self) -> None:
        assert encode_header(5) == 5

    @pytest.mark.parametrize(("tid", "result"), [(8, 0), (-1, 0), (0, 32)])
    def test_ranges(self, tid: int, result: int) -> None:
        with pytest.raises(ValueError):
            encode_header(tid, result)


class TestFrame:
    def test_a_payload_up_to_eight_bytes(self) -> None:
        Frame(0, b"12345678")
        with pytest.raises(ValueError):
            Frame(0, b"123456789")

    def test_an_identifier_has_to_fit(self) -> None:
        Frame(CAN_ID_MASK)
        with pytest.raises(ValueError):
            Frame(CAN_ID_MASK + 1)

    def test_it_is_immutable(self) -> None:
        f = Frame(encode_id(1, 0x2A, 4, 3))
        with pytest.raises(AttributeError):
            f.can_id = 0  # type: ignore[misc]
