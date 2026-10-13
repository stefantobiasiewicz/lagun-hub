"""How an endpoint type is described: attributes, commands, events.

The [`types/`](../../../lagunbus-spec/types/) catalogue is written by hand in markdown
and is **not machine readable** — unlike `registry/`. These definitions are therefore
copied out by hand, but **in one place**: everyone who speaks LagunBus uses them.

Open point **H8** is about exactly this: until the specification gains a machine
readable description of types, this table is the only hand-copied place in the
project.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Final, Protocol

from lagunhub.protocol.quantities import Quantity, raw_type
from lagunhub.protocol.registry import (
    COMMON_ATTRIBUTE_ACCESS,
    COMMON_ATTRIBUTE_TYPE,
    CommonAttribute,
    EndpointType,
)
from lagunhub.protocol.values import Value, decode, encode, size_of

QUANTITY_TYPE: Final = "<Q>"
"""A type following from the endpoint’s ``Quantity`` attribute rather than from the
element definition."""


@dataclass(frozen=True, slots=True)
class Field:
    """One field of a command’s arguments or an event’s parameters."""

    name: str
    type: str
    in_group_address: bool = False
    names: tuple[str, ...] = ()
    """The names of its values 0, 1, 2…, where it has them — `Sense.Threshold`'s `zone` —
    so a person reads `ABOVE_HIGH`, not `2`. A tuple, so the definition stays hashable."""
    """A field that in a **group** command travels in the ADDRESS field, not the payload.

    Today this concerns scenes only: a scene is keyed by the pair ``(group, number)``,
    and in a group command the group is already in the frame identifier. Repeating it
    in the payload would cost two bytes and could drift away from it.
    """


@dataclass(frozen=True, slots=True)
class AttributeDef:
    id: int
    name: str
    type: str
    """``"<Q>"`` means "the type follows from ``Quantity``" (types/0x0030-sense.md §2)."""

    access: str = "RO"
    persist: bool = False
    reported: bool = False
    default: Value = 0
    lo: int | None = None
    hi: int | None = None
    feature: int | None = None
    """The feature bits without which this attribute does not exist on the endpoint.

    A mask, and **any one** of its bits is enough: conformance ``[MULTICLICK, RAW]``
    (01-model-danych.md §8) is the mask ``MULTICLICK | RAW``. A single feature is simply
    a mask of one bit.
    """

    value_features: tuple[tuple[int, int], ...] = ()
    """``(value, feature)`` pairs: an enumeration value that exists only with a feature.

    `KeyInput.EventMode` is the case: ``RAW`` is a value of an attribute every
    multi-mode key has, but only a key declaring ``RAW`` may take it — a write of it
    anywhere else is out of range (types/0x0010-key-input.md §2.3). A pair list rather
    than a dict, because a frozen definition should stay hashable.
    """

    physical: bool = False
    """In the units of the endpoint's ``Quantity`` though not typed ``<Q>`` — `Sense`'s
    ``Offset`` and ``ThresholdHysteresis`` (types/0x0030-sense.md §2.2, §5.1). A person
    reads and writes them as ``-0.5`` °C, the bus carries ``-50``. See `in_units`."""

    optional: bool = False
    """Conformance ``O`` from the type catalogue: a device **may** have it.

    It differs from ``feature`` in that the descriptor does not show it — the hub only
    finds out from an ``UNSUPPORTED_ELEMENT`` reply. That is why the node double has no
    optional ones by default: so that the hub has to cope.
    """

    def value_feature(self, value: Value) -> int | None:
        """The feature a value needs, or `None` when any endpoint may take it."""
        for known, feature in self.value_features:
            if known == value:
                return feature
        return None

    @property
    def in_units(self) -> bool:
        """A person sees and gives it in the quantity's units — ``26.5`` °C, not ``2650``:
        in the file, in the view and through the API (zadania/zrobione/DEVICE-01.md N5,
        N12). Only the node and the bus speak in steps."""
        return self.physical or self.type == QUANTITY_TYPE

    @property
    def readable(self) -> bool:
        return self.access in ("RO", "RW")

    @property
    def writable(self) -> bool:
        return self.access in ("WO", "RW")


COMMON_BASE: Final = 0xF0
"""The start of the range reserved for common attributes
(01-model-danych.md §3.1)."""

COMMON_DEFAULTS: Final[dict[CommonAttribute, int | bool]] = {
    CommonAttribute.Enabled: True,
}
"""Default values of the common attributes — the only thing the registry does not
carry."""


def _common() -> dict[int, AttributeDef]:
    out: dict[int, AttributeDef] = {}
    for attribute in CommonAttribute:
        access = COMMON_ATTRIBUTE_ACCESS[attribute]
        out[attribute.value] = AttributeDef(
            id=attribute.value,
            name=attribute.name,
            type=COMMON_ATTRIBUTE_TYPE[attribute],
            access=access.split(",")[0],
            persist="P" in access.split(","),
            default=COMMON_DEFAULTS.get(attribute, 0),
        )
    return out


COMMON: Final[dict[int, AttributeDef]] = _common()
"""The common attributes (01-model-danych.md §4). Numbers, types and access **come
from the registry**.

They live here rather than next door precisely so that `TypeDef` can add them itself
— see `TypeDef.__post_init__`.
"""


@dataclass(frozen=True, slots=True)
class CommandDef:
    id: int
    name: str
    args: tuple[Field, ...] = ()
    feature: int | None = None


@dataclass(frozen=True, slots=True)
class EventDef:
    id: int
    name: str
    params: tuple[Field, ...] = ()


@dataclass(frozen=True, slots=True)
class TypeDef:
    type_id: EndpointType
    revision: int
    attributes: dict[int, AttributeDef] = field(default_factory=dict)
    commands: dict[int, CommandDef] = field(default_factory=dict)
    events: dict[int, EventDef] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """The **definition** adds the common attributes, not everyone who uses it.

        The node side used to add them itself (`FunctionalEndpoint.EXTRA_ATTRS`) while
        the hub added them not at all — so `ReportInterval` could be set on a node but
        **could not be configured**: the file rejected the name and reconciling quietly
        skipped the attribute. All the worse because the common attributes are exactly
        the ones most often adjusted.

        We add them to every type, the system ones included — the specification gives
        every endpoint the full set (01-model-danych.md §4.1), not least because the
        silence threshold is worked out from the `ReportInterval` of `Diagnostics`.

        The 0xF0-0xFF range belongs to the specification, so a type that reaches into
        it **will not start at all**. A test used to guard this; now the import does,
        and that is one step closer to the mistake.
        """
        own = [number for number in self.attributes if number >= COMMON_BASE]
        if own:
            numbers = ", ".join(f"0x{number:02X}" for number in sorted(own))
            raise ValueError(
                f"{self.type_id.name}: {numbers} falls into the common range "
                f"0x{COMMON_BASE:02X}-0xFF, which the specification owns "
                f"(01-model-danych.md §3.1)"
            )
        object.__setattr__(self, "attributes", {**self.attributes, **COMMON})

    @property
    def name(self) -> str:
        return self.type_id.name

    def attribute(self, name: str) -> AttributeDef:
        return _by_name(self.attributes.values(), name, self.name, "attribute")

    def command(self, name: str) -> CommandDef:
        return _by_name(self.commands.values(), name, self.name, "command")

    def event(self, name: str) -> EventDef:
        return _by_name(self.events.values(), name, self.name, "event")


class _Named(Protocol):
    @property
    def name(self) -> str: ...


def _by_name[N: _Named](items: Iterable[N], name: str, type_name: str, what: str) -> N:
    candidates = list(items)
    for item in candidates:
        if item.name == name:
            return item
    familiar = ", ".join(sorted(i.name for i in candidates)) or "none"
    raise ValueError(f"{type_name} knows no {what} {name!r}; known: {familiar}")


def field_type(f: Field, quantity: Quantity | None) -> str:
    if f.type != QUANTITY_TYPE:
        return f.type
    if quantity is None:
        raise ValueError(f"field {f.name!r} needs a known quantity (the Quantity attribute)")
    return raw_type(quantity)


def payload_fields(fields: tuple[Field, ...], *, grouped: bool = False) -> tuple[Field, ...]:
    """The fields that actually travel in the payload."""
    if not grouped:
        return fields
    return tuple(f for f in fields if not f.in_group_address)


def fields_size(fields: tuple[Field, ...], quantity: Quantity | None = None) -> int:
    return sum(size_of(field_type(f, quantity)) for f in fields)


def encode_fields(
    fields: tuple[Field, ...],
    values: Mapping[str, Value],
    *,
    grouped: bool = False,
    quantity: Quantity | None = None,
) -> bytes:
    """A command’s arguments or an event’s parameters, in declaration order."""
    used = payload_fields(fields, grouped=grouped)
    missing_names = [f.name for f in used if f.name not in values]
    if missing_names:
        raise ValueError(f"missing arguments: {', '.join(missing_names)}")
    return b"".join(encode(field_type(f, quantity), values[f.name]) for f in used)


def decode_fields(
    fields: tuple[Field, ...],
    data: bytes,
    *,
    grouped: bool = False,
    quantity: Quantity | None = None,
) -> dict[str, Value]:
    used = payload_fields(fields, grouped=grouped)
    expected = fields_size(used, quantity)
    if len(data) != expected:
        raise ValueError(f"expected {expected} B of arguments, got {len(data)}")
    out: dict[str, Value] = {}
    offset = 0
    for f in used:
        width = size_of(field_type(f, quantity))
        out[f.name] = decode(field_type(f, quantity), data[offset : offset + width])
        offset += width
    return out
