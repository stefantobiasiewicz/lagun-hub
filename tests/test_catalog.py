"""The type catalogue is one source for the hub and for the node double — point H8.

At first the element numbers were transcribed in two places: once on the hub's side,
once on the node's. Now there is one, and these tests keep the second from growing back.
"""

from __future__ import annotations

import pytest

from lagunhub.protocol.endpoints import CATALOG, COMMON, by_name
from lagunhub.protocol.endpoints import output as out
from lagunhub.protocol.endpoints.definition import (
    AttributeDef,
    TypeDef,
    decode_fields,
    encode_fields,
)
from lagunhub.protocol.endpoints.diagnostics import ERROR_THRESHOLD
from lagunhub.protocol.registry import CommonAttribute, EndpointType, ResultCode
from tests.node.devices import key1, relay1
from tests.node.endpoints.diagnostics import Diagnostics
from tests.node.endpoints.output import Output

UID_A = bytes(range(8))
UID_B = bytes(range(8, 16))


class TestCatalogConsistency:
    def test_every_type_declares_its_own_number(self) -> None:
        for endpoint_type, definition in CATALOG.items():
            assert definition.type_id == endpoint_type

    def test_element_numbers_do_not_clash_within_a_kind(self) -> None:
        """The three spaces are disjoint, but each has to be unambiguous within itself."""
        for definition in CATALOG.values():
            for collected in (definition.attributes, definition.commands, definition.events):
                numbers = [element.id for element in collected.values()]
                assert len(numbers) == len(set(numbers)), definition.name
                names = [element.name for element in collected.values()]
                assert len(names) == len(set(names)), definition.name

    def test_a_type_may_not_enter_the_common_range(self) -> None:
        """0x00–0xEF belongs to the type, 0xF0–0xFF to the specification
        (01-model-danych.md §3.1).

        `TypeDef` itself watches this now, so a type that breaks the rule cannot even be
        imported — which means it cannot be overlooked.
        """
        with pytest.raises(ValueError, match="common range"):
            TypeDef(
                type_id=EndpointType.Output,
                revision=1,
                attributes={0xF5: AttributeDef(id=0xF5, name="Channel", type="u8")},
            )

    def test_every_type_has_the_common_attributes(self) -> None:
        """The definition adds them, not whoever uses it.

        Earlier the node side added them for itself and the hub did not add them at all —
        so `ReportInterval` could be set on a node but could not be configured.
        """
        for definition in CATALOG.values():
            assert COMMON.keys() <= definition.attributes.keys(), definition.name
            assert definition.attribute("ReportInterval").id == CommonAttribute.ReportInterval

    def test_the_common_attributes_come_from_the_registry(self) -> None:
        assert set(COMMON) == {attribute.value for attribute in CommonAttribute}

    def test_a_system_endpoint_refuses_to_be_disabled(self) -> None:
        """`Enabled` is always 1 on a system endpoint (01-model-danych.md §4.1); the
        rest of its common attributes are writable as anywhere else."""
        diagnostics = Diagnostics()
        assert diagnostics.write(CommonAttribute.Enabled, b"\x00") == ResultCode.INVALID_VALUE
        assert diagnostics.write(CommonAttribute.Enabled, b"\x01") == ResultCode.OK
        assert diagnostics.write(CommonAttribute.ReportInterval, b"\x3c\x00") == ResultCode.OK
        assert diagnostics.values[CommonAttribute.Channel] == 0

    def test_an_unknown_type_says_what_it_knows(self) -> None:
        with pytest.raises(ValueError, match="unknown endpoint type"):
            by_name("Dishwasher")
        with pytest.raises(ValueError, match="has no definition yet"):
            by_name("Cover")

    def test_a_typo_in_an_element_name_says_what_is_known(self) -> None:
        with pytest.raises(ValueError, match="Toggle"):
            by_name("Output").command("Toogle")


class TestTheNodeDoubleFollowsTheCatalog:
    """Every element from the catalogue has to have some answer in the double.

    Since the node was moved out into `tests/node/` this class does more than before: the
    sense of every test that talks to the double as to a real node rests on the double's
    fidelity. If it stopped answering some element of the catalogue, the hub would be
    tested against something it will never meet on the bus.
    """

    def test_the_mandatory_attributes_can_be_read(self) -> None:
        for node in (key1(uid=UID_A, address=0x02A), relay1(uid=UID_B, address=0x040)):
            for number, endpoint in node.endpoints.items():
                definition = endpoint.TYPE
                for attribute, meta in definition.attributes.items():
                    if meta.optional or not endpoint.has(meta.feature) or not meta.readable:
                        continue
                    result, _ = endpoint.read(attribute)
                    assert result in (ResultCode.OK, ResultCode.SEGMENTED), (
                        f"{definition.name} ep{number} 0x{attribute:02X} → {result!r}"
                    )

    def test_every_command_gets_a_sensible_reply(self) -> None:
        """Not "carry everything out", but: none of them may blow up or go silent."""
        allowed = {
            ResultCode.OK,
            ResultCode.UNSUPPORTED_ELEMENT,
            ResultCode.UNSUPPORTED_FEATURE,
            ResultCode.INSUFFICIENT_SPACE,
            ResultCode.INVALID_VALUE,
        }
        node = relay1(uid=UID_B, address=0x040)
        for endpoint in node.endpoints.values():
            for command, meta in endpoint.TYPE.commands.items():
                args = encode_fields(meta.args, dict.fromkeys((f.name for f in meta.args), 0))
                result, _ = endpoint.command(command, args)
                assert result in allowed, f"{endpoint.TYPE.name}.{meta.name} → {result!r}"

    def test_the_shared_codec_takes_the_arguments_apart(self) -> None:
        """The node no longer assembles payloads by hand — the criterion for the catalogue."""
        output = Output(channel=1, features=out.Feature.LEVEL)
        setlevel = output.TYPE.command("SetLevel")
        args = encode_fields(setlevel.args, {"level": 300, "transition": 2000})
        assert args.hex() == "2c01d007"

        result, _ = output.command(setlevel.id, args)
        assert result == ResultCode.OK
        assert output.values[out.LEVEL] == 300
        assert output.values[out.ON_OFF] is True

    def test_bad_arguments_are_invalid_value(self) -> None:
        output = Output(channel=1, features=out.Feature.LEVEL)
        result, _ = output.command(out.SET_LEVEL, b"\x2c")
        assert result == ResultCode.INVALID_VALUE

    def test_a_command_from_outside_a_feature(self) -> None:
        output = Output(channel=1, features=0)
        result, _ = output.command(out.SET_LEVEL, b"\x2c\x01\xd0\x07")
        assert result == ResultCode.UNSUPPORTED_FEATURE

    def test_an_optional_attribute_does_not_exist_by_default(self) -> None:
        """The hub is to learn to cope with UNSUPPORTED_ELEMENT, not to assume a full
        set."""
        output = Output(channel=1)
        assert output.read(out.INVERT)[0] == ResultCode.UNSUPPORTED_ELEMENT
        declared = Output(channel=1, optional=(out.INVERT,))
        assert declared.read(out.INVERT)[0] == ResultCode.OK

    @pytest.mark.parametrize(
        ("type_name", "attribute"),
        [
            ("Diagnostics", "ErrorThreshold"),
            ("Sense", "ThresholdHysteresis"),
            ("Output", "Invert"),
        ],
    )
    def test_tuning_the_author_or_installer_knows_is_optional(
        self, type_name: str, attribute: str
    ) -> None:
        """CR-04: a setting follows whoever knows its value (01-model-danych.md §8.1).
        Build and installation are optional, so the hub must not treat their absence on
        a node as a fault — and must not count on writing them."""
        definition = by_name(type_name)
        meta = next(a for a in definition.attributes.values() if a.name == attribute)
        assert meta.optional, f"{type_name}.{attribute}"

    def test_a_diagnostics_without_error_threshold(self) -> None:
        """The node fixes the threshold itself; reading it is UNSUPPORTED_ELEMENT."""
        assert Diagnostics().read(ERROR_THRESHOLD)[0] == ResultCode.UNSUPPORTED_ELEMENT


class TestArgumentsBothWays:
    def test_round_trip_for_every_command_with_arguments(self) -> None:
        for definition in CATALOG.values():
            for meta in definition.commands.values():
                if not meta.args or any(f.type == "<Q>" for f in meta.args):
                    continue
                values = {f.name: 1 for f in meta.args}
                raw_bytes = encode_fields(meta.args, values)
                where = f"{definition.name}.{meta.name}"
                assert decode_fields(meta.args, raw_bytes) == values, where

    def test_events_the_same_way(self) -> None:
        for definition in CATALOG.values():
            for meta in definition.events.values():
                if not meta.params or any(f.type == "<Q>" for f in meta.params):
                    continue
                values = {f.name: 1 for f in meta.params}
                raw_bytes = encode_fields(meta.params, values)
                assert decode_fields(meta.params, raw_bytes) == values

    def test_the_quantity_settles_the_length(self) -> None:
        """The same Value attribute has 2 or 4 bytes — it follows from Quantity."""
        from lagunhub.protocol.quantities import Quantity, raw_type
        from lagunhub.protocol.values import size_of

        assert size_of(raw_type(Quantity.TEMPERATURE)) == 2
        assert size_of(raw_type(Quantity.CURRENT)) == 4


def test_the_panel_descriptor_from_the_spec() -> None:
    """01-model-danych.md §7 — a three-key panel with backlighting and a thermometer."""
    assert EndpointType.KeyInput in CATALOG
    assert EndpointType.Output in CATALOG
    assert EndpointType.Sense in CATALOG
    assert CATALOG[EndpointType.Output].attribute("Purpose").id == out.PURPOSE
    assert out.Purpose.INDICATOR != out.Purpose.LIGHT
