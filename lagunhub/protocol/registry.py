"""LagunBus identifiers — A GENERATED FILE, do not edit.

Source: lagunbus-spec/registry/*.yaml
Generator: tools/generate_registry.py
"""

from __future__ import annotations

from enum import IntEnum


class MessageKind(IntEnum):
    """The KIND field of an identifier. The number IS the priority (05-mapowanie-can.md §2)."""

    ALARM = 0x00  # ALARM
    EVENT = 0x01  # ZDARZENIE
    COMMAND = 0x03  # KOMENDA
    GROUP_COMMAND = 0x04  # KOMENDA_GRUPOWA
    WRITE = 0x05  # ZAPIS
    READ = 0x06  # ODCZYT
    RESPONSE = 0x07  # ODPOWIEDŹ
    REPORT = 0x08  # RAPORT
    LONG_RESPONSE = 0x09  # ODPOWIEDŹ_DŁUGA
    LONG_WRITE = 0x0a  # ZAPIS_DŁUGI
    QUERY = 0x0b  # ZAPYTANIE
    HUB_RESPONSE = 0x0c  # ODPOWIEDŹ_HUBA
    JOIN = 0x0f  # PRZYŁĄCZANIE


TRANSACTIONAL: frozenset[MessageKind] = frozenset({
    MessageKind.COMMAND,
    MessageKind.WRITE,
    MessageKind.READ,
    MessageKind.RESPONSE,
    MessageKind.QUERY,
    MessageKind.HUB_RESPONSE,
})
"""Kinds that carry a Tid in payload byte [0]."""


class ResultCode(IntEnum):
    """Result codes of a reply (04-interakcje.md §9)."""

    OK = 0x00  # done
    UNSUPPORTED_ENDPOINT = 0x01  # the node has no such endpoint
    UNSUPPORTED_ELEMENT = 0x02  # the type knows no such attribute or command
    UNSUPPORTED_FEATURE = 0x03  # the element exists, but its feature is not declared
    READ_ONLY = 0x04  # an attempt to write a read-only attribute
    WRITE_ONLY = 0x05  # an attempt to read a write-only attribute
    INVALID_VALUE = 0x06  # wrong length, or a value out of range
    INVALID_STATE = 0x07  # the operation is impossible in the current state
    BUSY = 0x08  # the node cannot serve the request just now
    FAILURE = 0x09  # a hardware or storage fault
    INSUFFICIENT_SPACE = 0x0a  # no room in a table of limited capacity
    SEGMENTED = 0x0b  # the value does not fit in a frame; it arrives in segments
    UNAVAILABLE = 0x0c  # the hub does not have this value - no source, or the source is silent


RETRYABLE: frozenset[ResultCode] = frozenset({
    ResultCode.BUSY,
})
"""The only codes after which a request is worth repeating."""


class NodeClass(IntEnum):
    """The NodeClass field of an ANNOUNCEMENT — nothing but the choice of address block (05-mapowanie-can.md §4.1)."""

    UNSPECIFIED = 0x00
    CONTROL = 0x01
    SENSOR = 0x02


NODE_CLASS_BLOCKS: dict[NodeClass, tuple[int, int]] = {
    NodeClass.UNSPECIFIED: (0x400, 0x7fd),
    NodeClass.CONTROL: (0x001, 0x0ff),
    NodeClass.SENSOR: (0x100, 0x3ff),
}
"""The address block for each class (03-adresacja.md §2.3)."""


class EndpointType(IntEnum):
    """The catalogue of endpoint types."""

    Device = 0x01  # the node identity
    Diagnostics = 0x02  # the health of the node and of the bus
    Scenes = 0x03  # stored output states of the node
    Groups = 0x04  # the table of endpoint group memberships
    KeyInput = 0x10  # a momentary key
    ValueInput = 0x11  # an input with a value of its own - a key pair, an encoder, a slider
    Contact = 0x12  # a two-state input that carries state
    ToggleInput = 0x13  # a two-state input whose state the node itself maintains; usually with a status LED
    Output = 0x20  # a driven output
    Cover = 0x21  # a blind, shutter or awning - position and slat angle
    Sense = 0x30  # an analogue measurement of a continuous quantity
    Meter = 0x31  # a cumulative meter - energy, water, gas; the value never decreases
    Thermostat = 0x40  # temperature control for one circuit; the first type with inputs


SYSTEM_ENDPOINTS: dict[int, EndpointType] = {
    0: EndpointType.Device,
    1: EndpointType.Diagnostics,
    2: EndpointType.Groups,
    3: EndpointType.Scenes,
}
"""Endpoints whose number the specification fixes (01-model-danych.md §1.2)."""

FIRST_FUNCTIONAL_ENDPOINT: int = 4
"""Functional endpoints are numbered from 4 with no gaps (06-cykl-zycia-urzadzenia.md §3.2)."""

TYPE_REVISION: dict[EndpointType, int] = {
    EndpointType.Device: 1,
    EndpointType.Diagnostics: 1,
    EndpointType.Scenes: 1,
    EndpointType.Groups: 1,
    EndpointType.KeyInput: 1,
    EndpointType.ValueInput: 1,
    EndpointType.Contact: 1,
    EndpointType.ToggleInput: 1,
    EndpointType.Output: 1,
    EndpointType.Sense: 1,
    EndpointType.Meter: 1,
    EndpointType.Thermostat: 1,
}
"""The type contract revision this version of the hub knows."""


class CommonAttribute(IntEnum):
    """Attributes present on every functional endpoint (01-model-danych.md §4)."""

    TypeId = 0xf0  # u16
    TypeRevision = 0xf1  # u8
    Features = 0xf2  # map16
    Enabled = 0xf3  # bool
    Channel = 0xf5  # u8
    QueryEnabled = 0xf6  # map16
    ReportInterval = 0xf8  # u16
    ReportMinGap = 0xf9  # u16
    ReportHysteresis = 0xfa  # u16


COMMON_ATTRIBUTE_TYPE: dict[CommonAttribute, str] = {
    CommonAttribute.TypeId: 'u16',
    CommonAttribute.TypeRevision: 'u8',
    CommonAttribute.Features: 'map16',
    CommonAttribute.Enabled: 'bool',
    CommonAttribute.Channel: 'u8',
    CommonAttribute.QueryEnabled: 'map16',
    CommonAttribute.ReportInterval: 'u16',
    CommonAttribute.ReportMinGap: 'u16',
    CommonAttribute.ReportHysteresis: 'u16',
}

COMMON_ATTRIBUTE_ACCESS: dict[CommonAttribute, str] = {
    CommonAttribute.TypeId: 'RO',
    CommonAttribute.TypeRevision: 'RO',
    CommonAttribute.Features: 'RO',
    CommonAttribute.Enabled: 'RW,P',
    CommonAttribute.Channel: 'RO,P',
    CommonAttribute.QueryEnabled: 'RW,P',
    CommonAttribute.ReportInterval: 'RW,P',
    CommonAttribute.ReportMinGap: 'RW,P',
    CommonAttribute.ReportHysteresis: 'RW,P',
}
