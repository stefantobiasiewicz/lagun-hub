"""An endpoint of the node double — the machinery shared by every type.

The definitions of attributes, commands and events come from **the hub's type catalogue**
(`lagunhub.protocol.endpoints`); they are not transcribed here a second time. The double
adds one thing to them: **behaviour** — what happens when a command arrives.

Checking access, features, ranges and result codes sits in this file. If some type
repeats something from here, it means this file is written badly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar

from lagunhub.protocol.endpoints.common import COMMON
from lagunhub.protocol.endpoints.definition import (
    QUANTITY_TYPE,
    AttributeDef,
    CommandDef,
    TypeDef,
    decode_fields,
)
from lagunhub.protocol.quantities import Quantity, raw_type
from lagunhub.protocol.registry import CommonAttribute, ResultCode
from lagunhub.protocol.values import ABSENT, Value, decode, encode, in_range

if TYPE_CHECKING:
    from tests.node.node import NodeCore

__all__ = ["COMMON", "Endpoint", "FunctionalEndpoint", "SystemEndpoint"]


@dataclass
class Endpoint:
    """The base for every endpoint type."""

    TYPE: ClassVar[TypeDef]
    EXTRA_ATTRS: ClassVar[dict[int, AttributeDef]] = {}
    """The common attributes this endpoint gets. A functional one — all of them."""

    channel: int = 0
    features: int = 0
    overrides: dict[int, Value] = field(default_factory=dict)
    optional: tuple[int, ...] = ()
    """Conformance ``O`` attributes this device actually has."""

    number: int = field(default=-1, init=False)
    node: NodeCore | None = field(default=None, init=False, repr=False)
    values: dict[int, Value] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        chosen = {
            number: meta
            for number, meta in self.TYPE.attributes.items()
            if not meta.optional or number in self.optional
        }
        self.spec: dict[int, AttributeDef] = {**chosen, **self.EXTRA_ATTRS}
        for attribute, meta in self.spec.items():
            # A bool attribute is to hold a bool from the first moment, not only after
            # the first write — otherwise the initial state differs in type from the state
            # after a command, and that shows up only in a test of the whole.
            self.values[attribute] = bool(meta.default) if meta.type == "bool" else meta.default
        for attribute, meta in self.spec.items():
            if meta.type == QUANTITY_TYPE:
                self.values[attribute] = 0
        if CommonAttribute.TypeId in self.spec:
            self.values[CommonAttribute.TypeId] = int(self.TYPE.type_id)
            self.values[CommonAttribute.TypeRevision] = self.TYPE.revision
            self.values[CommonAttribute.Features] = self.features
            self.values[CommonAttribute.Channel] = self.channel
        self.values.update(self.overrides)

    # --- convenience --------------------------------------------------------

    def kind_of(self, meta: AttributeDef) -> str:
        """The attribute's carrying type. For `<Q>` the endpoint's `Quantity` settles it."""
        if meta.type != QUANTITY_TYPE:
            return meta.type
        return raw_type(Quantity(self.num(self.TYPE.attribute("Quantity").id)))

    def num(self, attribute: int) -> int:
        """The attribute's numeric value. "Absent" counts as zero."""
        value = self.values[attribute]
        return 0 if value is ABSENT else int(value)  # type: ignore[arg-type]

    def opt(self, attribute: int, default: int = 0) -> int:
        """The value of an **optional** attribute. A missing attribute is not an error.

        A separate method, because ``num()`` is to blow up on a typo in the number — while
        here absence is a normal state of the device.
        """
        return self.num(attribute) if attribute in self.spec else default

    def has(self, feature: int | None) -> bool:
        return feature is None or bool(self.features & feature)

    # --- reading and writing, 04-interakcje.md §3 and §4 --------------------

    def read(self, attribute: int) -> tuple[ResultCode, bytes]:
        meta = self.spec.get(attribute)
        if meta is None:
            return ResultCode.UNSUPPORTED_ELEMENT, b""
        if not self.has(meta.feature):
            # The element exists in the type — this device simply does not have it
            # (04-interakcje.md §9).
            return ResultCode.UNSUPPORTED_FEATURE, b""
        if not meta.readable:
            return ResultCode.WRITE_ONLY, b""
        if meta.type in ("string", "blob"):
            return ResultCode.SEGMENTED, b""  # the value does not fit in a frame
        return ResultCode.OK, encode(self.kind_of(meta), self.values[attribute])

    def write(self, attribute: int, data: bytes) -> ResultCode:
        """The order of checks is the one from 04-interakcje.md §4, and the first
        mismatch ends the write."""
        meta = self.spec.get(attribute)
        if meta is None:
            return ResultCode.UNSUPPORTED_ELEMENT
        if not self.has(meta.feature):
            return ResultCode.UNSUPPORTED_FEATURE
        if not meta.writable:
            return ResultCode.READ_ONLY
        carrying = self.kind_of(meta)
        try:
            value = decode(carrying, data)
        except ValueError:
            return ResultCode.INVALID_VALUE
        if not in_range(carrying, value) or not self._within_bounds(meta, value):
            return ResultCode.INVALID_VALUE
        self.values[attribute] = value
        self.on_write(attribute, value)
        if meta.reported:
            self.report(attribute)  # a write causes a report (04-interakcje.md §4.2)
        return ResultCode.OK

    @staticmethod
    def _within_bounds(meta: AttributeDef, value: Value) -> bool:
        """The range from the type's table. Numbers only — 04-interakcje.md §4."""
        if not isinstance(value, int) or isinstance(value, bool):
            return True
        below = meta.lo is not None and value < meta.lo
        above = meta.hi is not None and value > meta.hi
        return not (below or above)

    # --- commands -----------------------------------------------------------

    def command(self, command: int, args: bytes) -> tuple[ResultCode, bytes]:
        """Takes the arguments apart by the catalogue and hands them to the type's
        behaviour."""
        meta = self.TYPE.commands.get(command)
        if meta is None:
            return ResultCode.UNSUPPORTED_ELEMENT, b""
        if not self.has(meta.feature):
            return ResultCode.UNSUPPORTED_FEATURE, b""
        try:
            parsed = decode_fields(meta.args, args)
        except ValueError:
            return ResultCode.INVALID_VALUE, b""
        return self.on_command(meta, parsed)

    # --- for a type to override --------------------------------------------

    def on_command(self, command: CommandDef, args: dict[str, Value]) -> tuple[ResultCode, bytes]:
        return ResultCode.UNSUPPORTED_ELEMENT, b""

    def long_value(self, attribute: int) -> bytes | None:
        """The encoded value of an attribute that does not fit in a frame.

        Returns **the value with its length prefix**, ready to be sliced into segments
        (05-mapowanie-can.md §6). ``None`` means the type has no such value.
        """
        return None

    def on_write(self, attribute: int, value: Value) -> None: ...

    def tick(self, now: float) -> None: ...

    # --- the other way round, through the node ------------------------------

    def set(self, attribute: int, value: Value, *, report: bool = True) -> None:
        """A change of state from the hardware's side — not through the bus."""
        changed = self.values.get(attribute) != value
        self.values[attribute] = value
        if changed and report and self.spec[attribute].reported:
            self.report(attribute)

    def report(self, attribute: int) -> None:
        if self.node is not None:
            self.node.send_report(self.number, attribute)

    def emit(self, event: int, params: bytes = b"") -> None:
        if self.node is not None:
            self.node.send_event(self.number, event, params)


class FunctionalEndpoint(Endpoint):
    """An endpoint numbered from 4. It has the full set of common attributes."""

    EXTRA_ATTRS: ClassVar[dict[int, AttributeDef]] = COMMON


class SystemEndpoint(Endpoint):
    """An endpoint whose number the specification fixes.

    **It has the common attributes too** (01-model-danych.md §4.1) — otherwise the hub
    would have nothing to work out the silence threshold from
    (06-cykl-zycia-urzadzenia.md §5.1). Two of them are fixed: `Channel` is always 0,
    and `Enabled` always 1 — a write of 0 is refused.
    """

    EXTRA_ATTRS: ClassVar[dict[int, AttributeDef]] = COMMON

    def write(self, attribute: int, data: bytes) -> ResultCode:
        if attribute == CommonAttribute.Enabled and data[:1] == b"\x00":
            return ResultCode.INVALID_VALUE
        return super().write(attribute, data)
