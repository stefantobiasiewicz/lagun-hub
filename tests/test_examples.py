"""Vectors from lagunbus-spec/PRZYKLADY.md — bit for bit.

The examples are worked out by hand from chapter 05 and are the only place in the
specification where whole frames are visible at once. If the codec drifts from them, it
drifts from the bus.

The document is non-normative — where something in it contradicts the specification, the
error is there. But as long as it agrees, it is the best test set we have.
"""

from __future__ import annotations

from lagunhub.core.segments import split
from lagunhub.protocol.endpoints import DISPLAY, GROUPS, KEY_INPUT, OUTPUT, SCENES, SENSE
from lagunhub.protocol.endpoints import output as out
from lagunhub.protocol.endpoints.definition import decode_fields, encode_fields
from lagunhub.protocol.frame import Frame, decode_header, encode_header, encode_id
from lagunhub.protocol.quantities import Quantity, raw_type, to_physical
from lagunhub.protocol.registry import MessageKind, ResultCode
from lagunhub.protocol.values import decode, encode, encode_blob, encode_string

# The installation from PRZYKLADY.md
PANEL = 0x012
RELAY_ADDR = 0x020
DIMMER = 0x021
KITCHEN_DISPLAY = 0x013


def frame(can_id: int, payload: bytes) -> Frame:
    """Build a frame from raw data and check that it is valid."""
    return Frame(can_id, payload)


class Test1AddingToAGroup:
    """The operator makes a "living room" group. The hub writes the membership with an
    addressed command."""

    def test_the_request(self) -> None:
        payload = bytes([encode_header(tid=1)]) + encode_fields(
            GROUPS.command("AddGroup").args, {"endpoint": 4, "group": 5}
        )
        assert payload == bytes.fromhex("01 04 05 00".replace(" ", ""))

        can_id = encode_id(MessageKind.COMMAND, RELAY_ADDR, 2, GROUPS.command("AddGroup").id)
        assert can_id == 0x06080200
        assert len(frame(can_id, payload).data) == 4

    def test_the_reply(self) -> None:
        payload = bytes([encode_header(tid=1, result=ResultCode.OK)])
        assert payload == b"\x01"
        assert encode_id(MessageKind.RESPONSE, RELAY_ADDR, 2, 0x00) == 0x0E080200
        assert decode_header(payload[0]) == (ResultCode.OK, 1)

    def test_the_target_endpoint_travels_in_the_payload(self) -> None:
        """The ENDPOINT field points at 2, that is Groups — the type that carries the
        command out."""
        args = decode_fields(GROUPS.command("AddGroup").args, bytes.fromhex("040500"))
        assert args == {"endpoint": 4, "group": 5}


class Test2StoringAScene:
    def test_the_request(self) -> None:
        payload = bytes([encode_header(tid=2)]) + encode_fields(
            SCENES.command("Store").args, {"group": 5, "scene": 1}
        )
        assert payload == bytes.fromhex("02050001")
        assert (
            encode_id(MessageKind.COMMAND, RELAY_ADDR, 3, SCENES.command("Store").id) == 0x06080300
        )

    def test_the_reply(self) -> None:
        assert encode_id(MessageKind.RESPONSE, RELAY_ADDR, 3, 0x00) == 0x0E080300


class Test3PressingAKey:
    def test_the_event(self) -> None:
        click = KEY_INPUT.event("Click")
        payload = bytes([44]) + encode_fields(click.params, {"count": 1})
        assert payload == bytes.fromhex("2C01")

        can_id = encode_id(MessageKind.EVENT, PANEL, 4, click.id)
        assert can_id == 0x02048403

    def test_the_address_is_the_sender(self) -> None:
        assert Frame(0x02048403).fields.address == PANEL


class Test4RecallingAScene:
    def test_a_group_command_does_not_repeat_the_group_number(self) -> None:
        """The group number travels in the ADDRESS field, so it is not in the payload."""
        recall = SCENES.command("Recall")
        payload = encode_fields(recall.args, {"scene": 1, "transition": 2000}, grouped=True)
        assert payload == bytes.fromhex("01D007")

        can_id = encode_id(MessageKind.GROUP_COMMAND, 5, 3, recall.id)
        assert can_id == 0x08014301

    def test_endpoint_three_not_zero(self) -> None:
        """Zero would mean "hand it out" and would land on outputs, where 0x01 is On."""
        assert Frame(0x08014301).fields.endpoint == 3
        assert OUTPUT.command("On").id == SCENES.command("Recall").id == 0x01

    def test_the_effect_reports(self) -> None:
        assert encode_id(MessageKind.REPORT, RELAY_ADDR, 4, out.ON_OFF) == 0x10080400
        assert encode("bool", True) == bytes.fromhex("01")

        assert encode_id(MessageKind.REPORT, DIMMER, 4, out.LEVEL) == 0x10084401
        assert encode("u16", 300) == bytes.fromhex("2C01")
        assert encode_id(MessageKind.REPORT, DIMMER, 5, out.LEVEL) == 0x10084501
        assert encode("u16", 100) == bytes.fromhex("6400")

    def test_per_mille_becomes_per_cent_by_division(self) -> None:
        assert decode("u16", bytes.fromhex("2C01")) == 300  # 30 %
        assert decode("u16", bytes.fromhex("6400")) == 100  # 10 %


class Test5AllOff:
    def test_a_group_command_without_arguments(self) -> None:
        off = OUTPUT.command("Off")
        assert encode_fields(off.args, {}, grouped=True) == b""

        can_id = encode_id(MessageKind.GROUP_COMMAND, 1, 0, off.id)
        assert can_id == 0x08004000
        assert Frame(can_id, b"").data == b""

    def test_endpoint_zero_means_distribute(self) -> None:
        assert Frame(0x08004000).fields.endpoint == 0


class Test6SingleControl:
    def test_three_frames_two_meanings(self) -> None:
        toggle = OUTPUT.command("Toggle")

        command = bytes([encode_header(tid=3)])
        assert command == bytes.fromhex("03")
        assert encode_id(MessageKind.COMMAND, RELAY_ADDR, 4, toggle.id) == 0x06080402

        response = bytes([encode_header(tid=3, result=ResultCode.OK)])
        assert response == bytes.fromhex("03")
        assert encode_id(MessageKind.RESPONSE, RELAY_ADDR, 4, toggle.id) == 0x0E080402

        report = encode("bool", False)
        assert report == bytes.fromhex("00")
        assert encode_id(MessageKind.REPORT, RELAY_ADDR, 4, out.ON_OFF) == 0x10080400


class Test7CurrentReport:
    def test_value_and_scale(self) -> None:
        value = SENSE.attribute("Value")
        assert value.type == "<Q>", (
            "the type follows from Quantity, not from the attribute's definition"
        )

        raw_bytes = bytes.fromhex("A4010000")
        assert encode("i32", 420) == raw_bytes
        assert decode("i32", raw_bytes) == 420
        assert to_physical(Quantity.CURRENT, 420) == 0.42

        assert encode_id(MessageKind.REPORT, RELAY_ADDR, 8, value.id) == 0x10080800

    def test_current_is_signed(self) -> None:
        """An installation with photovoltaics gives current back to the grid."""
        from lagunhub.protocol.quantities import raw_type

        assert raw_type(Quantity.CURRENT) == "i32"
        assert decode("i32", encode("i32", -1500)) == -1500


class Test8ReadingUidInSegments:
    UID = bytes.fromhex("3300270012513433313237 30".replace(" ", ""))

    def test_the_read_and_the_announcement(self) -> None:
        assert encode_id(MessageKind.READ, RELAY_ADDR, 0, 0x00) == 0x0C080000
        assert bytes([encode_header(tid=4)]) == bytes.fromhex("04")

        herald = bytes([encode_header(tid=4, result=ResultCode.SEGMENTED)])
        assert herald == bytes.fromhex("5C")
        assert decode_header(0x5C) == (ResultCode.SEGMENTED, 4)
        assert encode_id(MessageKind.RESPONSE, RELAY_ADDR, 0, 0x00) == 0x0E080000

    def test_the_value_with_its_length_prefix(self) -> None:
        assert len(self.UID) == 12
        encoded = encode_blob(self.UID)
        assert encoded.hex() == "0c00330027001251343331323730"
        assert len(encoded) == 14, "12 bytes plus a two-byte prefix"

    def test_the_split_into_six_byte_segments(self) -> None:
        encoded = encode_blob(self.UID)
        segments_ = [encoded[i : i + 6] for i in range(0, len(encoded), 6)]

        assert [s.hex() for s in segments_] == ["0c0033002700", "125134333132", "3730"]
        assert encode_id(MessageKind.LONG_RESPONSE, RELAY_ADDR, 0, 0x00) == 0x12080000

    def test_the_segment_header(self) -> None:
        """[0] LAST in bit 7 plus the Tid; [1] the segment number."""
        assert bytes([0x04, 0x00]).hex() == "0400"  # segment 0, LAST = 0, Tid = 4
        assert 0x84 & 0x80, "the last segment has bit 7 set"
        assert 0x84 & 0x07 == 4, "and the Tid in the three lowest bits"


class Test9Arbitration:
    """The numbering of kinds IS the priority hierarchy (05-mapowanie-can.md §2)."""

    def test_the_order_from_the_table(self) -> None:
        order = [
            0x02048403,  # a key event, 0x012
            0x06080402,  # a command to 0x020
            0x08004000,  # a group command, group 1
            0x10080800,  # a current report, 0x020 ep8
            0x10540400,  # a temperature report, 0x150
        ]
        assert order == sorted(order)

    def test_among_reports_the_lower_address_wins(self) -> None:
        assert Frame(0x10080800).fields.address == 0x020
        assert Frame(0x10540400).fields.address == 0x150
        assert 0x10080800 < 0x10540400


class Test10Display:
    """A number by an ordinary write, a text by a long one (types/0x0022-display.md)."""

    def test_the_value_write(self) -> None:
        value = DISPLAY.attribute("Value")
        can_id = encode_id(MessageKind.WRITE, KITCHEN_DISPLAY, 4, value.id)
        assert can_id == 0x0A04C402
        payload = bytes([encode_header(tid=2)]) + encode(raw_type(Quantity.TEMPERATURE), 2137)
        assert payload == bytes.fromhex("025908"), "21.37 °C, five of eight at the widest"
        assert encode_id(MessageKind.RESPONSE, KITCHEN_DISPLAY, 4, value.id) == 0x0E04C402

    def test_content_is_reported(self) -> None:
        content = DISPLAY.attribute("Content")
        assert encode_id(MessageKind.REPORT, KITCHEN_DISPLAY, 4, content.id) == 0x1004C400
        assert encode("enum8", 1) == b"\x01"

    def test_the_text_in_three_segments(self) -> None:
        text = DISPLAY.attribute("Text")
        assert encode_id(MessageKind.LONG_WRITE, KITCHEN_DISPLAY, 5, text.id) == 0x1404C504
        value = encode_string("Pranie gotowe")
        assert value.hex(" ") == "0d 50 72 61 6e 69 65 20 67 6f 74 6f 77 65"
        assert [s.hex(" ") for s in split(value, tid=3)] == [
            "03 00 0d 50 72 61 6e 69",
            "03 01 65 20 67 6f 74 6f",
            "83 02 77 65",
        ]
        assert encode_id(MessageKind.RESPONSE, KITCHEN_DISPLAY, 5, text.id) == 0x0E04C504

    def test_text_length_counts_bytes(self) -> None:
        assert len("Pranie skończone") == 16
        assert len(encode_string("Pranie skończone")) - 1 == 17


class TestTightestCase:
    """05-mapowanie-can.md §5.3 — it settles how many groups one reply carries."""

    def test_get_membership_takes_eight_of_eight(self) -> None:
        from lagunhub.protocol.endpoints.groups import MEMBERSHIP_PER_RESPONSE

        header = bytes([encode_header(tid=1, result=ResultCode.OK)])
        counter = encode("u8", MEMBERSHIP_PER_RESPONSE)
        groups_ = b"".join(encode("u16", number) for number in (1, 5, 9))
        response = header + counter + groups_

        assert len(response) == 8, "eight of eight bytes, without a single spare one"
        assert MEMBERSHIP_PER_RESPONSE == 3, "a fourth group would not fit"

    def test_a_fourth_group_would_not_fit(self) -> None:
        from lagunhub.protocol.frame import MAX_PAYLOAD

        assert MAX_PAYLOAD < 1 + 1 + 4 * 2


class TestAbsentValue:
    """02-typy-danych.md §2.1 — "absent" is a valid result of a read, not an error."""

    def test_it_survives_the_codec_without_turning_into_zero(self) -> None:
        from lagunhub.protocol.values import ABSENT

        for type_name in ("u8", "u16", "u32", "i8", "i16", "i32", "bool", "enum8", "enum16"):
            raw_bytes = encode(type_name, ABSENT)
            assert decode(type_name, raw_bytes) is ABSENT, (
                f"{type_name} lost its \u201cabsent\u201d"
            )
            assert decode(type_name, raw_bytes) != 0

    def test_the_ranges_are_one_shorter(self) -> None:
        from lagunhub.protocol.values import SCALARS

        assert SCALARS["u16"].hi == 65534, "one value given up for \u201cabsent\u201d"
        assert SCALARS["i16"].lo == -32767

    def test_bit_fields_have_no_absent(self) -> None:
        import pytest

        from lagunhub.protocol.values import ABSENT, SCALARS

        assert SCALARS["map16"].absent is None
        with pytest.raises(ValueError, match="absent"):
            encode("map16", ABSENT)

    def test_a_sensor_that_measured_nothing(self) -> None:
        """Without this a thermostat would heat a room to −273 °C."""
        from lagunhub.protocol.values import ABSENT

        assert decode("i16", encode("i16", ABSENT)) is ABSENT
        assert decode("i16", encode("i16", 0)) == 0
